#!/usr/bin/env python3
"""Native macOS FuelBand maintenance CLI using the external hidapi package.

The 0x11AC:0x317D current protocol is supported; detected 0x11AC:0x6565
legacy/original devices are refused before any transfer.
"""

import argparse
import datetime as datetime_module
import sys
import time

try:
    import hid
except ImportError:  # Tests can inject a fake module; real use needs hidapi.
    hid = None


FEATURE_SIZE = 64
HID_VENDOR_ID = 0x11AC
SUPPORTED_PRODUCT_ID = 0x317D
LEGACY_PRODUCT_ID = 0x6565
FUELBAND_PID_MAP = {
    SUPPORTED_PRODUCT_ID: {"name": "supported SE/current protocol", "supported": True},
    LEGACY_PRODUCT_ID: {"name": "legacy/original protocol", "supported": False},
}
# Backward-compatible name for the supported current protocol.
HID_PRODUCT_ID = SUPPORTED_PRODUCT_ID
HID_USAGE_PAGE = 0xFF00
HID_USAGE = 0x01
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
DAILY_TARGET_SETTING_IDS = tuple(range(40, 47))
DAILY_TARGET_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAILY_TARGET_MIN = 1
DAILY_TARGET_MAX = 0xFFFFFFFF
STATUS_SETTING_ALLOWLIST = frozenset((DEVICE_STATE_SETTING, FIRST_NAME_SETTING))

# Capacities are the encoded command payload after the output report ID:
# protocol length byte + tag + command bytes.
OUTPUT_REPORT_BUCKETS = (
    (7, 0x09),
    (15, 0x0A),
    (31, 0x0B),
    (63, 0x0C),
)

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


def select_output_report(command):
    """Select output report 09/0A/0B/0C from encoded command length."""

    encoded_length = len(bytes(command)) + 2
    for capacity, report_id in OUTPUT_REPORT_BUCKETS:
        if encoded_length <= capacity:
            return report_id
    raise FuelBandError(
        "encoded command payload is %d bytes; maximum is 63" % encoded_length
    )


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
    packet = response_header(raw_response, expected_tag)
    expected = bytes((RESPONSE_REPORT_ID, 2, expected_tag, 0))
    if packet != expected:
        raise FuelBandError("ACK packet is not exactly [1,2,tag,0]")
    return 0


def parse_setting_get(
    raw_response, expected_tag, requested_setting, expected_width=None, max_width=None
):
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


def enumerate_fuelband():
    if hid is None:
        raise FuelBandError("hidapi is unavailable; install requirements.txt first")
    try:
        entries = list(hid.enumerate(HID_VENDOR_ID, 0) or [])
    except Exception as error:
        raise FuelBandError("hidapi enumeration failed: %s" % error) from error
    matches = [
        entry
        for entry in entries
        if entry.get("vendor_id") == HID_VENDOR_ID
        and entry.get("product_id") in FUELBAND_PID_MAP
    ]
    if not matches:
        raise FuelBandError(
            "no known FuelBand-family HID collection matched VID:PID %04X:%04X or %04X:%04X"
            % (HID_VENDOR_ID, SUPPORTED_PRODUCT_ID, HID_VENDOR_ID, LEGACY_PRODUCT_ID)
        )
    if len(matches) != 1:
        raise FuelBandError(
            "expected exactly one matching HID collection, found %d" % len(matches)
        )
    selected = matches[0]
    selected_pid = selected["product_id"]
    if not FUELBAND_PID_MAP[selected_pid]["supported"]:
        raise FuelBandError(
            "legacy/original FuelBand PID 0x%04X detected; this CLI does not support "
            "its protocol framing and sent no command" % selected_pid
        )
    if selected.get("usage_page") != HID_USAGE_PAGE or selected.get("usage") != HID_USAGE:
        raise FuelBandError(
            "supported FuelBand PID 0x%04X has wrong or missing HID usage metadata; "
            "expected 0x%04X:0x%02X"
            % (selected_pid, HID_USAGE_PAGE, HID_USAGE)
        )
    if not selected.get("path"):
        raise FuelBandError("matching HID collection has no hidapi path")
    return selected


class FuelBand:
    """Serialized native hidapi feature transport."""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.device = None
        self.info = None
        self.next_tag = FIRST_TAG
        self.used_transport = False

    def __enter__(self):
        self.info = enumerate_fuelband()
        try:
            self.device = hid.device()
            self.device.open_path(self.info["path"])
        except Exception as error:
            self.device = None
            raise FuelBandError("could not open the matching HID collection: %s" % error) from error
        if self.verbose:
            print("selected HID VID:PID %04X:%04X" % (HID_VENDOR_ID, HID_PRODUCT_ID))
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.device is not None:
            try:
                self.device.close()
            finally:
                self.device = None

    def _take_tag(self):
        tag = self.next_tag
        self.next_tag = (self.next_tag + 1) & 0xFF
        return tag

    def exchange(self, command):
        if self.device is None:
            raise FuelBandError("device is not open")
        command = bytes(command)
        output_report_id = select_output_report(command)
        tag = self._take_tag()
        frame = bytes((output_report_id, len(command) + 1, tag)) + command
        if len(frame) > FEATURE_SIZE:
            raise FuelBandError("encoded command does not fit the 64-byte feature buffer")
        if self.used_transport:
            time.sleep(COMMAND_DELAY_SECONDS)
        self.used_transport = True

        request = frame.ljust(FEATURE_SIZE, b"\0")
        try:
            sent = self.device.send_feature_report(request)
        except Exception as error:
            raise FuelBandError("hidapi feature send failed: %s" % error) from error
        if sent != FEATURE_SIZE:
            raise FuelBandError(
                "hidapi feature send returned %r bytes; expected exactly %d"
                % (sent, FEATURE_SIZE)
            )

        try:
            returned = self.device.get_feature_report(INPUT_REPORT_ID, FEATURE_SIZE)
        except Exception as error:
            raise FuelBandError("hidapi feature read failed: %s" % error) from error
        if returned is None:
            raise FuelBandError("hidapi feature read returned no data")
        try:
            response = bytes(returned)
        except (TypeError, ValueError) as error:
            raise FuelBandError("hidapi feature read returned non-byte data") from error
        if not response:
            raise FuelBandError("hidapi feature read returned an empty report")
        if len(response) > FEATURE_SIZE:
            raise FuelBandError("hidapi feature read returned more than 64 bytes")
        return Transaction(tag, frame, response)


def show_transaction(label, transaction, verbose):
    print("%s tag: 0x%02x" % (label, transaction.tag))
    if verbose:
        print("%s request: %s" % (label, transaction.frame.hex(" ")))
        print("%s response: %s" % (label, transaction.response.hex(" ")))


def read_setting(device, setting_id, label, verbose=False):
    if setting_id not in STATUS_SETTING_ALLOWLIST:
        raise FuelBandError("setting %d is not in the safe read allowlist" % setting_id)
    transaction = device.exchange((SETTING_GET, 1, setting_id))
    show_transaction(label, transaction, verbose)
    if setting_id == DEVICE_STATE_SETTING:
        return parse_setting_get(
            transaction.response, transaction.tag, setting_id, expected_width=4
        )
    return parse_setting_get(
        transaction.response, transaction.tag, setting_id, expected_width=NAME_STORAGE_WIDTH
    )


def read_daily_target_setting(device, setting_id, label, verbose=False):
    if setting_id not in DAILY_TARGET_SETTING_IDS:
        raise FuelBandError("setting %d is not a daily-target setting" % setting_id)
    transaction = device.exchange((SETTING_GET, 1, setting_id))
    show_transaction(label, transaction, verbose)
    return parse_setting_get(
        transaction.response, transaction.tag, setting_id, expected_width=4
    )


def read_rtc_time(device, label="RTC time", verbose=False):
    transaction = device.exchange((RTC, 2))
    show_transaction(label, transaction, verbose)
    return parse_rtc(transaction.response, transaction.tag)


def read_rtc_date(device, label="RTC date", verbose=False):
    transaction = device.exchange((RTC, 4))
    show_transaction(label, transaction, verbose)
    return parse_rtc(transaction.response, transaction.tag, date_response=True)


def decode_name(value):
    if len(value) != NAME_STORAGE_WIDTH:
        raise FuelBandError(
            "name storage value must be exactly %d bytes" % NAME_STORAGE_WIDTH
        )
    first_nul = value.find(b"\0")
    visible = value if first_nul < 0 else value[:first_nul]
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
        raise FuelBandError("NAME must contain 1-%d ASCII bytes" % NAME_WRITE_MAX_BYTES)
    if any(byte < 0x20 or byte > 0x7E for byte in encoded):
        raise FuelBandError("NAME must contain printable ASCII characters only")
    return encoded


def parse_target(value):
    try:
        target = value if isinstance(value, int) else int(str(value), 0)
    except (TypeError, ValueError) as error:
        raise FuelBandError("FUEL must be an integer") from error
    if not DAILY_TARGET_MIN <= target <= DAILY_TARGET_MAX:
        raise FuelBandError(
            "FUEL must be in the range %d..0x%08x"
            % (DAILY_TARGET_MIN, DAILY_TARGET_MAX)
        )
    return target


def mark_imprinted_value(value):
    if len(value) != 4:
        raise FuelBandError("device state must contain exactly four bytes")
    marked = bytearray(value)
    marked[0] |= ACTIVATED_BIT
    return bytes(marked)


def parse_requested_time(timestamp):
    if timestamp is None:
        return datetime_module.datetime.now().replace(microsecond=0), "macOS local time"
    text = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        requested = datetime_module.datetime.fromisoformat(text)
    except ValueError as error:
        raise FuelBandError("timestamp must be valid ISO-8601") from error
    if requested.tzinfo is not None:
        requested = requested.astimezone().replace(tzinfo=None)
        label = "ISO-8601 timestamp converted to macOS local time"
    else:
        label = "ISO-8601 timestamp (no offset)"
    return requested.replace(microsecond=0), label


def command_status(device, verbose=False):
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
            5,
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
    transaction = device.exchange(bytes((SETTING_SET, FIRST_NAME_SETTING, len(name_bytes))) + name_bytes)
    show_transaction("set-name", transaction, verbose)
    parse_ack(transaction.response, transaction.tag)
    time.sleep(READBACK_DELAY_SECONDS)
    actual_name = decode_name(read_setting(device, FIRST_NAME_SETTING, "set-name readback", verbose))
    if actual_name != name:
        raise FuelBandError("name readback mismatch: expected %r, got %r" % (name, actual_name))
    print("verified name: %s" % actual_name)
    return 0


def command_set_target(device, fuel, verbose=False):
    target = parse_target(fuel)
    target_bytes = target.to_bytes(4, "little", signed=False)
    for day_name, setting_id in zip(DAILY_TARGET_DAY_NAMES, DAILY_TARGET_SETTING_IDS):
        command = bytes((SETTING_SET, setting_id, 4)) + target_bytes
        transaction = device.exchange(command)
        show_transaction("set-target %s ACK (setting %d)" % (day_name, setting_id), transaction, verbose)
        parse_ack(transaction.response, transaction.tag)
        readback = read_daily_target_setting(
            device, setting_id, "set-target %s readback (setting %d)" % (day_name, setting_id), verbose
        )
        if readback != target_bytes:
            raise FuelBandError(
                "set-target %s readback mismatch: expected %s, got %s"
                % (day_name, target_bytes.hex(" "), readback.hex(" "))
            )
        print("%s: NikeFuel daily target %d verified" % (day_name, target))
    return 0


def command_mark_imprinted(device, verbose=False):
    print("WARNING: experimental mark-imprinted is NOT proven to finish onboarding.")
    old_value = read_setting(device, DEVICE_STATE_SETTING, "mark-imprinted readback before", verbose)
    if old_value[0] & ACTIVATED_BIT:
        print("device-state bit 1 is already set; no write performed")
        return 0
    new_value = mark_imprinted_value(old_value)
    transaction = device.exchange(bytes((SETTING_SET, DEVICE_STATE_SETTING, 4)) + new_value)
    show_transaction("mark-imprinted", transaction, verbose)
    parse_ack(transaction.response, transaction.tag)
    time.sleep(READBACK_DELAY_SECONDS)
    readback = read_setting(device, DEVICE_STATE_SETTING, "mark-imprinted readback after", verbose)
    if readback != new_value:
        raise FuelBandError(
            "mark-imprinted readback mismatch: expected %s, got %s"
            % (new_value.hex(" "), readback.hex(" "))
        )
    print("verified state bit 1 write; no onboarding result is claimed")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Native macOS hidapi FuelBand CLI for VID:PID 11AC:317D."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show nonsecret framed request/response hex in addition to safe results",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="read state 78, RTC, and name 97")
    set_time = subparsers.add_parser("set-time", help="set and verify macOS-local RTC time")
    set_time.add_argument("timestamp", nargs="?", help="optional ISO-8601 timestamp")
    set_name = subparsers.add_parser("set-name", help="set and verify a printable ASCII name")
    set_name.add_argument("name", metavar="NAME", help="1-23 printable ASCII bytes")
    set_target = subparsers.add_parser(
        "set-target", help="set and verify one NikeFuel daily target for Monday-Sunday"
    )
    set_target.add_argument("fuel", metavar="FUEL", help="1..0xffffffff NikeFuel value")
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
            if args.command == "set-target":
                return command_set_target(device, args.fuel, args.verbose)
            if args.command == "mark-imprinted":
                return command_mark_imprinted(device, args.verbose)
            raise FuelBandError("unknown command %s" % args.command)
    except FuelBandError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
