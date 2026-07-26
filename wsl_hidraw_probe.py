#!/usr/bin/env python3
"""Return success only when a matching USB HID hidraw uevent is present."""

import glob
import re
import sys


BUS_USB = 0x0003
UEVENT_GLOB = "/sys/class/hidraw/hidraw*/device/uevent"
VID_PID_PATTERN = re.compile(r"\A([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\Z")


def parse_vid_pid(value):
    match = VID_PID_PATTERN.fullmatch(str(value).strip())
    if match is None:
        raise ValueError("expected VID:PID in four-hex-digit form")
    return int(match.group(1), 16), int(match.group(2), 16)


def uevent_matches(text, expected):
    expected_vendor, expected_product = expected
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if key != "HID_ID" or not separator:
            continue
        fields = value.strip().split(":")
        if len(fields) != 3:
            continue
        try:
            bus, vendor, product = (int(field, 16) for field in fields)
        except ValueError:
            continue
        if bus == BUS_USB and vendor == expected_vendor and product == expected_product:
            return True
    return False


def hidraw_matches(expected, paths=None):
    """Scan hidraw uevents, ignoring nodes that disappear during the scan."""

    if paths is None:
        paths = glob.glob(UEVENT_GLOB)
    for path in paths:
        try:
            with open(path, encoding="ascii", errors="ignore") as handle:
                text = handle.read()
        except OSError:
            continue
        if uevent_matches(text, expected):
            return True
    return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1:
        return 2
    try:
        expected = parse_vid_pid(argv[0])
    except ValueError:
        return 2
    return 0 if hidraw_matches(expected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
