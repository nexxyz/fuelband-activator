#!/usr/bin/env python3
"""Small, dependency-free FuelBand CLI for Linux hidraw under WSL.

Only the supported 0x11AC:0x317D FuelBand interface is selected.  ``status``
is read-only; the other commands perform one narrow, verified update.
"""

import argparse
import datetime as datetime_module
import glob
import os
import struct
import sys
import time

try:
    import fcntl
except ImportError:  # Allows the parser tests to run on Windows; device use is Linux-only.
    fcntl = None


HIDRAW_GLOB = "/dev/hidraw*"
FEATURE_SIZE = 64
HIDIOCGRAWINFO = 0x80084803  # _IOR('H', 0x03, struct hidraw_devinfo)
HIDIOCSFEATURE_64 = 0xC0404806
HIDIOCGFEATURE_64 = 0xC0404807

BUS_USB = 0x03
EXPECTED_VENDOR_ID = 0x11AC
EXPECTED_PRODUCT_ID = 0x317D
INPUT_REPORT_ID = 0x04
RESPONSE_REPORT_ID = 0x01

SETTING_GET = 0x0A
SETTING_SET = 0x0B
RTC = 0x09
FIRST_NAME_SETTING = 97
DEVICE_STATE_SETTING = 78
ACTIVATED_BIT = 1 << 1
NAME_WRITE_MAX_BYTES = 23
NAME_STORAGE_WIDTH = 25

# The length is the encoded command payload after the output report ID:
# protocol length byte + transaction tag + command bytes.
OUTPUT_REPORT_BUCKETS = (
    (7, 0x09),
    (15, 0x0A),
    (31, 0x0B),
    (63, 0x0C),
)

# This is the complete status read allowlist.  It intentionally contains no
# token, credential, or other private-setting IDs.
STATUS_SETTING_ALLOWLIST = frozenset((DEVICE_STATE_SETTING, FIRST_NAME_SETTING))

COMMAND_DELAY_SECONDS = 0.10
READBACK_DELAY_SECONDS = 1.0
FIRST_TAG = 0x40


class FuelBandError(RuntimeError):
    """An expected device, protocol, or command validation failure."""


class Transaction:
    def __init__(self, tag, frame, response):
        self.tag = tag
        self.frame = frame
        self.response = response


def raw_hid_info(fd):
    """Read Linux hidraw's bus type, vendor, and product from one fd."""

    if fcntl is None:
        raise FuelBandError("raw hidraw access requires Linux/WSL")
    info = bytearray(struct.calcsize("=IHH"))
    try:
        fcntl.ioctl(fd, HIDIOCGRAWINFO, info, True)
    except OSError as error:
        raise FuelBandError("HIDIOCGRAWINFO failed: %s" % error) from error
    return struct.unpack("=IHH", info)


def validate_supported_hid_info(info, context="hidraw device"):
    """Require a USB hidraw node for the supported FuelBand VID/PID."""

    bus_type, vendor_id, product_id = info
    if bus_type != BUS_USB:
        raise FuelBandError(
            "%s is not USB (bus type 0x%02x; expected 0x%02x)"
            % (context, bus_type, BUS_USB)
        )
    if vendor_id != EXPECTED_VENDOR_ID or product_id != EXPECTED_PRODUCT_ID:
        raise FuelBandError(
            "%s is VID:PID %04X:%04X, not %04X:%04X"
            % (
                context,
                vendor_id,
                product_id,
                EXPECTED_VENDOR_ID,
                EXPECTED_PRODUCT_ID,
            )
        )


def find_unique_fuelband(pattern=HIDRAW_GLOB):
    """Return the only hidraw path matching the supported VID/PID."""

    paths = sorted(glob.glob(pattern))
    if not paths:
        raise FuelBandError("no hidraw devices found under %s" % pattern)

    matches = []
    inspection_errors = []
    for path in paths:
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY)
            info = raw_hid_info(fd)
            if info[0] == BUS_USB and info[1] == EXPECTED_VENDOR_ID and info[2] == EXPECTED_PRODUCT_ID:
                matches.append(path)
        except (OSError, FuelBandError) as error:
            inspection_errors.append("%s: %s" % (path, error))
        finally:
            if fd is not None:
                os.close(fd)

    if inspection_errors:
        raise FuelBandError(
            "could not inspect every hidraw device; refusing selection: %s"
            % "; ".join(inspection_errors)
        )
    if not matches:
        raise FuelBandError(
            "no hidraw device matched VID:PID %04X:%04X"
            % (EXPECTED_VENDOR_ID, EXPECTED_PRODUCT_ID)
        )
    if len(matches) != 1:
        raise FuelBandError(
            "multiple hidraw devices matched VID:PID %04X:%04X: %s"
            % (EXPECTED_VENDOR_ID, EXPECTED_PRODUCT_ID, ", ".join(matches))
        )
    return matches[0]


def select_output_report(command):
    """Select an output report from the encoded command payload length."""

    command_length = len(bytes(command))
    encoded_length = command_length + 2  # length byte and tag
    for capacity, report_id in OUTPUT_REPORT_BUCKETS:
        if encoded_length <= capacity:
            return report_id
    raise FuelBandError(
        "encoded command payload is %d bytes; maximum is 63" % encoded_length
    )


class FuelBand:
    """Raw 64-byte feature-report transport with serialized transactions."""

    def __init__(self, verbose=False, hidraw_pattern=HIDRAW_GLOB):
        self.verbose = verbose
        self.hidraw_pattern = hidraw_pattern
        self.path = None
        self.fd = None
        self.next_tag = FIRST_TAG
        self.used_transport = False

    def __enter__(self):
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise FuelBandError("run this command as root inside WSL")
        self.path = find_unique_fuelband(self.hidraw_pattern)
        try:
            self.fd = os.open(self.path, os.O_RDWR)
        except OSError as error:
            raise FuelBandError("cannot open selected %s: %s" % (self.path, error)) from error
        try:
            # Re-read identity on the descriptor that will carry transfers;
            # the path may have been replaced after enumeration.
            validate_supported_hid_info(raw_hid_info(self.fd), "final opened hidraw device")
        except FuelBandError:
            os.close(self.fd)
            self.fd = None
            raise
        if self.verbose:
            print(
                "selected hidraw VID:PID %04X:%04X"
                % (EXPECTED_VENDOR_ID, EXPECTED_PRODUCT_ID)
            )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _take_tag(self):
        tag = self.next_tag
        self.next_tag = (self.next_tag + 1) & 0xFF
        return tag

    def exchange(self, command):
        """Send one framed command and return its raw response."""

        if self.fd is None:
            raise FuelBandError("device is not open")
        if fcntl is None:
            raise FuelBandError("raw hidraw access requires Linux/WSL")
        command = bytes(command)
        output_report_id = select_output_report(command)
        tag = self._take_tag()
        frame = bytes((output_report_id, len(command) + 1, tag)) + command
        if len(frame) > FEATURE_SIZE:
            raise FuelBandError("encoded command does not fit the 64-byte feature buffer")

        if self.used_transport:
            time.sleep(COMMAND_DELAY_SECONDS)
        self.used_transport = True

        request = bytearray(FEATURE_SIZE)
        request[: len(frame)] = frame
        response = bytearray(FEATURE_SIZE)
        response[0] = INPUT_REPORT_ID
        try:
            fcntl.ioctl(self.fd, HIDIOCSFEATURE_64, request, True)
            received = fcntl.ioctl(self.fd, HIDIOCGFEATURE_64, response, True)
        except OSError as error:
            raise FuelBandError("HID feature transaction failed: %s" % error) from error

        if isinstance(received, int) and 0 < received <= FEATURE_SIZE:
            raw_response = bytes(response[:received])
        else:
            raw_response = bytes(response)
        return Transaction(tag, frame, raw_response)


def bounded_packet(raw_response):
    """Return exactly the declared packet and reject nonzero trailing data."""

    if len(raw_response) < 2:
        raise FuelBandError("response is shorter than its report header")
    declared_length = raw_response[1]
    if declared_length < 2:
        raise FuelBandError("response declares fewer than tag and status bytes")
    packet_length = 2 + declared_length
    if packet_length > len(raw_response):
        raise FuelBandError(
            "response is truncated: declares %d bytes after length, got %d"
            % (declared_length, len(raw_response) - 2)
        )
    if any(raw_response[packet_length:]):
        raise FuelBandError("response contains nonzero data beyond declared length")
    return raw_response[:packet_length]


def response_header(raw_response, expected_tag):
    packet = bounded_packet(raw_response)
    if len(packet) < 4:
        raise FuelBandError("response has no tag and status fields")
    if packet[0] != RESPONSE_REPORT_ID:
        raise FuelBandError(
            "unexpected response report ID 0x%02x; expected 0x%02x"
            % (packet[0], RESPONSE_REPORT_ID)
        )
    if packet[2] != expected_tag:
        raise FuelBandError(
            "response tag mismatch: expected 0x%02x, got 0x%02x"
            % (expected_tag, packet[2])
        )
    if packet[3] != 0:
        raise FuelBandError("response status is 0x%02x, not zero" % packet[3])
    return packet


def parse_ack(raw_response, expected_tag):
    """Require the exact four-byte successful ACK packet."""

    packet = response_header(raw_response, expected_tag)
    expected = bytes((RESPONSE_REPORT_ID, 0x02, expected_tag, 0x00))
    if packet != expected:
        raise FuelBandError("ACK packet is not exactly [1,2,tag,0]")
    return 0


def parse_setting_get(raw_response, expected_tag, requested_setting, expected_width=None, max_width=None):
    """Parse a strictly framed SETTING_GET response."""

    packet = response_header(raw_response, expected_tag)
    if len(packet) < 7:
        raise FuelBandError("setting response is missing its value header")
    if packet[4] != 1:
        raise FuelBandError("unexpected setting-id length %d" % packet[4])
    setting_id = packet[5]
    value_length = packet[6]
    if setting_id != requested_setting:
        raise FuelBandError(
            "setting readback mismatch: requested %d, got %d"
            % (requested_setting, setting_id)
        )
    if expected_width is not None and value_length != expected_width:
        raise FuelBandError(
            "setting %d width is %d; expected %d"
            % (setting_id, value_length, expected_width)
        )
    if max_width is not None and value_length > max_width:
        raise FuelBandError(
            "setting %d width is %d; maximum is %d" % (setting_id, value_length, max_width)
        )
    end = 7 + value_length
    if end != len(packet):
        raise FuelBandError(
            "setting %d response length does not exactly match value width" % setting_id
        )
    return bytes(packet[7:end])


def parse_rtc(raw_response, expected_tag, date_response=False):
    """Parse an exact-length, range-checked RTC GET response."""

    packet = response_header(raw_response, expected_tag)
    expected_length = 8 if date_response else 7
    if len(packet) != expected_length:
        raise FuelBandError(
            "RTC response length is %d; expected exactly %d" % (len(packet), expected_length)
        )
    if date_response:
        year_offset, month, day, weekday = packet[4:8]
        year = 2000 + year_offset
        if not 1 <= month <= 12 or not 1 <= day <= 31 or not 1 <= weekday <= 7:
            raise FuelBandError("RTC date fields are outside normal ranges")
        try:
            datetime_module.date(year, month, day)
        except ValueError as error:
            raise FuelBandError("RTC date is not a valid calendar date") from error
        return year, month, day, weekday
    hour, minute, second = packet[4:7]
    if hour > 23 or minute > 59 or second > 59:
        raise FuelBandError("RTC time fields are outside normal ranges")
    return hour, minute, second


def show_transaction(label, transaction, verbose):
    print("%s tag: 0x%02x" % (label, transaction.tag))
    if verbose:
        print("%s request: %s" % (label, transaction.frame.hex(" ")))
        print("%s response: %s" % (label, transaction.response.hex(" ")))


def read_setting(device, setting_id, label, verbose=False):
    if setting_id not in STATUS_SETTING_ALLOWLIST:
        raise FuelBandError("setting %d is not in the safe read allowlist" % setting_id)
    transaction = device.exchange((SETTING_GET, 0x01, setting_id))
    show_transaction(label, transaction, verbose)
    if setting_id == DEVICE_STATE_SETTING:
        return parse_setting_get(
            transaction.response, transaction.tag, setting_id, expected_width=4
        )
    # The device stores setting 97 in a fixed 25-byte slot.  Writes are still
    # capped at 23 bytes by validate_name(), but reads require the full slot.
    return parse_setting_get(
        transaction.response, transaction.tag, setting_id, expected_width=NAME_STORAGE_WIDTH
    )


def read_rtc_time(device, label="RTC time", verbose=False):
    transaction = device.exchange((RTC, 0x02))
    show_transaction(label, transaction, verbose)
    return parse_rtc(transaction.response, transaction.tag)


def read_rtc_date(device, label="RTC date", verbose=False):
    transaction = device.exchange((RTC, 0x04))
    show_transaction(label, transaction, verbose)
    return parse_rtc(transaction.response, transaction.tag, date_response=True)


def decode_name(value):
    if len(value) != NAME_STORAGE_WIDTH:
        raise FuelBandError(
            "name storage value must be exactly %d bytes" % NAME_STORAGE_WIDTH
        )
    if b"\0" in value:
        first_nul = value.index(0)
        if any(value[first_nul:]):
            raise FuelBandError("name contains nonzero data after its NUL terminator")
        visible = value[:first_nul]
    else:
        visible = value
    try:
        name = visible.decode("ascii")
    except UnicodeDecodeError as error:
        raise FuelBandError("name readback is not ASCII") from error
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in name):
        raise FuelBandError("name readback is not printable ASCII")
    return name


def validate_name(name):
    try:
        encoded = name.encode("ascii")
    except UnicodeEncodeError as error:
        raise FuelBandError("NAME must contain ASCII characters only") from error
    if not 1 <= len(encoded) <= NAME_WRITE_MAX_BYTES:
        raise FuelBandError(
            "NAME must contain 1-%d ASCII bytes" % NAME_WRITE_MAX_BYTES
        )
    if any(byte < 0x20 or byte > 0x7E for byte in encoded):
        raise FuelBandError("NAME must contain printable ASCII characters only")
    return encoded


def mark_imprinted_value(value):
    """Return exactly four state bytes with only bit 1 ORed into byte zero."""

    if len(value) != 4:
        raise FuelBandError("device state must contain exactly four bytes")
    marked = bytearray(value)
    marked[0] |= ACTIVATED_BIT
    return bytes(marked)


def parse_requested_time(timestamp):
    """Return WSL-local wall-clock fields and a clear display label."""

    if timestamp is None:
        return datetime_module.datetime.now().replace(microsecond=0), "WSL local time"
    text = timestamp
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        requested = datetime_module.datetime.fromisoformat(text)
    except ValueError as error:
        raise FuelBandError("timestamp must be valid ISO-8601") from error
    if requested.tzinfo is not None:
        # The FuelBand RTC has no offset field.  Store the instant's WSL-local
        # wall-clock fields rather than silently storing the supplied offset's
        # literal wall-clock value.
        requested = requested.astimezone().replace(tzinfo=None)
        label = "ISO-8601 timestamp converted to WSL local time"
    else:
        label = "ISO-8601 timestamp (no offset)"
    return requested.replace(microsecond=0), label


def command_status(device, verbose=False):
    """Read only state 78, RTC time/date, and name 97."""

    failures = []
    try:
        state = read_setting(device, DEVICE_STATE_SETTING, "device state (setting 78)", verbose)
        print("device state: %s (bit 1: %s)" % (state.hex(" "), "set" if state[0] & ACTIVATED_BIT else "clear"))
    except FuelBandError as error:
        print("device state ERROR: %s" % error)
        failures.append(error)

    try:
        hour, minute, second = read_rtc_time(device, verbose=verbose)
        print("RTC time: %02d:%02d:%02d" % (hour, minute, second))
    except FuelBandError as error:
        print("RTC time ERROR: %s" % error)
        failures.append(error)

    try:
        year, month, day, weekday = read_rtc_date(device, verbose=verbose)
        print("RTC date: %04d-%02d-%02d (weekday %d)" % (year, month, day, weekday))
    except FuelBandError as error:
        print("RTC date ERROR: %s" % error)
        failures.append(error)

    try:
        name = decode_name(read_setting(device, FIRST_NAME_SETTING, "name (setting 97)", verbose))
        print("name: %s" % name)
    except FuelBandError as error:
        print("name ERROR: %s" % error)
        failures.append(error)
    return 1 if failures else 0


def command_set_time(device, timestamp=None, verbose=False):
    requested, label = parse_requested_time(timestamp)
    if not 2000 <= requested.year <= 2255:
        raise FuelBandError("requested year is outside the FuelBand RTC range")
    weekday = requested.weekday() + 1
    command = bytes(
        (
            RTC,
            0x05,
            requested.hour,
            requested.minute,
            requested.second,
            requested.year - 2000,
            requested.month,
            requested.day,
            weekday,
            0x50,
            0x46,
            0x00,
            0x00,
            0x3C,
            0x00,
            0x00,
        )
    )
    transaction = device.exchange(command)
    show_transaction("set-time", transaction, verbose)
    parse_ack(transaction.response, transaction.tag)
    print("requested %s: %s" % (label, requested.isoformat(sep=" ")))

    time.sleep(READBACK_DELAY_SECONDS)
    read_hour, read_minute, read_second = read_rtc_time(
        device, "set-time readback time", verbose
    )
    read_year, read_month, read_day, read_weekday = read_rtc_date(
        device, "set-time readback date", verbose
    )
    actual = datetime_module.datetime(
        read_year, read_month, read_day, read_hour, read_minute, read_second
    )
    delta = (actual - requested).total_seconds()
    if read_weekday != actual.weekday() + 1 or not 0 <= delta <= 2:
        raise FuelBandError(
            "RTC readback outside expected +0-2 seconds: requested %s, got %s (delta %.0fs)"
            % (requested, actual, delta)
        )
    print("verified RTC within +0-2 seconds: %s (delta %.0fs)" % (actual, delta))
    return 0


def command_set_name(device, name, verbose=False):
    name_bytes = validate_name(name)
    command = bytes((SETTING_SET, FIRST_NAME_SETTING, len(name_bytes))) + name_bytes
    transaction = device.exchange(command)
    show_transaction("set-name", transaction, verbose)
    parse_ack(transaction.response, transaction.tag)

    time.sleep(READBACK_DELAY_SECONDS)
    actual_name = decode_name(
        read_setting(device, FIRST_NAME_SETTING, "set-name readback", verbose)
    )
    if actual_name != name:
        raise FuelBandError("name readback mismatch: expected %r, got %r" % (name, actual_name))
    print("verified name: %s" % actual_name)
    return 0


def command_mark_imprinted(device, verbose=False):
    print("WARNING: experimental mark-imprinted is NOT proven to finish onboarding.")
    old_value = read_setting(
        device, DEVICE_STATE_SETTING, "mark-imprinted readback before", verbose
    )
    if old_value[0] & ACTIVATED_BIT:
        print("device-state bit 1 is already set; no write performed")
        return 0

    new_value = mark_imprinted_value(old_value)
    command = bytes((SETTING_SET, DEVICE_STATE_SETTING, len(new_value))) + new_value
    transaction = device.exchange(command)
    show_transaction("mark-imprinted", transaction, verbose)
    parse_ack(transaction.response, transaction.tag)

    time.sleep(READBACK_DELAY_SECONDS)
    readback = read_setting(
        device, DEVICE_STATE_SETTING, "mark-imprinted readback after", verbose
    )
    if readback != new_value:
        raise FuelBandError(
            "mark-imprinted readback mismatch: expected %s, got %s"
            % (new_value.hex(" "), readback.hex(" "))
        )
    print("verified state bit 1 write; no onboarding result is claimed")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Minimal raw-hidraw FuelBand CLI for VID:PID 11AC:317D."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show nonsecret framed request/response hex in addition to safe results",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="read state 78, RTC, and name 97")
    set_time = subparsers.add_parser("set-time", help="set and verify WSL-local RTC time")
    set_time.add_argument("timestamp", nargs="?", help="optional ISO-8601 timestamp")
    set_name = subparsers.add_parser("set-name", help="set and verify a printable ASCII name")
    set_name.add_argument("name", metavar="NAME", help="1-23 printable ASCII bytes")
    mark = subparsers.add_parser(
        "mark-imprinted",
        help="EXPERIMENTAL: set state bit 1; not proven to finish onboarding",
    )
    mark.add_argument("--experimental", action="store_true", required=True)
    mark.add_argument("--yes", action="store_true", required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2

    try:
        with FuelBand(verbose=args.verbose) as device:
            if args.command == "status":
                return command_status(device, args.verbose)
            if args.command == "set-time":
                return command_set_time(device, args.timestamp, args.verbose)
            if args.command == "set-name":
                return command_set_name(device, args.name, args.verbose)
            if args.command == "mark-imprinted":
                return command_mark_imprinted(device, args.verbose)
            raise FuelBandError("unknown command %s" % args.command)
    except FuelBandError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
