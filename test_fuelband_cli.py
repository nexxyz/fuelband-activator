import unittest
from pathlib import Path
from unittest import mock

import fuelband_cli as cli


def response(tag, payload, report_id=cli.RESPONSE_REPORT_ID):
    body = bytes((tag,)) + bytes(payload)
    return bytes((report_id, len(body))) + body


def setting_response(tag, setting_id, value):
    return response(tag, (0, 1, setting_id, len(value)) + tuple(value))


def ack_response(tag):
    return bytes((cli.RESPONSE_REPORT_ID, 2, tag, 0))


class MockMarkDevice:
    def __init__(self, state_reads):
        self.state_reads = list(state_reads)
        self.read_index = 0
        self.exchange_calls = []
        self.writes = []

    def exchange(self, command):
        command = bytes(command)
        self.exchange_calls.append(command)
        tag = 0x60 + len(self.exchange_calls)
        if command == bytes((cli.SETTING_GET, 1, cli.DEVICE_STATE_SETTING)):
            value = self.state_reads[self.read_index]
            self.read_index += 1
            return cli.Transaction(tag, b"", setting_response(tag, cli.DEVICE_STATE_SETTING, value))
        if command[:2] == bytes((cli.SETTING_SET, cli.DEVICE_STATE_SETTING)):
            self.writes.append(command)
            return cli.Transaction(tag, b"", ack_response(tag))
        raise AssertionError("unexpected command: %r" % (command,))


class MockTargetDevice:
    def __init__(self, readbacks):
        self.readbacks = dict(readbacks)
        self.exchange_calls = []
        self.writes = []

    def exchange(self, command):
        command = bytes(command)
        self.exchange_calls.append(command)
        tag = 0x70 + len(self.exchange_calls)
        if command[0] == cli.SETTING_SET:
            self.writes.append(command)
            return cli.Transaction(tag, b"", ack_response(tag))
        if command[0] == cli.SETTING_GET and command[2] in cli.DAILY_TARGET_SETTING_IDS:
            setting_id = command[2]
            return cli.Transaction(
                tag,
                b"",
                setting_response(tag, setting_id, self.readbacks[setting_id]),
            )
        raise AssertionError("unexpected command: %r" % (command,))


class FramingTests(unittest.TestCase):
    def test_output_report_buckets_use_encoded_payload_length(self):
        self.assertEqual(cli.select_output_report(bytes(5)), 0x09)  # 2 + 5 = 7
        self.assertEqual(cli.select_output_report(bytes(6)), 0x0A)
        self.assertEqual(cli.select_output_report(bytes(13)), 0x0A)  # 2 + 13 = 15
        self.assertEqual(cli.select_output_report(bytes(14)), 0x0B)
        self.assertEqual(cli.select_output_report(bytes(29)), 0x0B)  # 2 + 29 = 31
        self.assertEqual(cli.select_output_report(bytes(30)), 0x0C)
        self.assertEqual(cli.select_output_report(bytes(61)), 0x0C)  # 2 + 61 = 63

    def test_commands_over_final_capacity_are_rejected(self):
        with self.assertRaises(cli.FuelBandError):
            cli.select_output_report(bytes(62))


class ResponseParserTests(unittest.TestCase):
    def test_strict_ack(self):
        tag = 0x40
        self.assertEqual(cli.parse_ack(bytes((1, 2, tag, 0)), tag), 0)
        with self.assertRaises(cli.FuelBandError):
            cli.parse_ack(bytes((1, 3, tag, 0, 0)), tag)
        with self.assertRaises(cli.FuelBandError):
            cli.parse_ack(bytes((2, 2, tag, 0)), tag)
        with self.assertRaises(cli.FuelBandError):
            cli.parse_ack(bytes((1, 2, tag, 0, 1)), tag)

    def test_state_requires_four_bytes_and_expected_header(self):
        tag = 0x41
        raw = response(tag, (0, 1, 78, 4, 0x02, 0x00, 0xAA, 0x55))
        self.assertEqual(cli.parse_setting_get(raw, tag, 78, expected_width=4), b"\x02\x00\xaaU")
        bad_width = response(tag, (0, 1, 78, 3, 1, 2, 3))
        with self.assertRaises(cli.FuelBandError):
            cli.parse_setting_get(bad_width, tag, 78, expected_width=4)
        wrong_report = response(tag, (0, 1, 78, 4, 1, 2, 3, 4), report_id=4)
        with self.assertRaises(cli.FuelBandError):
            cli.parse_setting_get(wrong_report, tag, 78, expected_width=4)

    def test_name_is_bounded(self):
        tag = 0x42
        padded = b"Alice" + b"\0" * 20
        raw = setting_response(tag, 97, padded)
        self.assertEqual(
            cli.parse_setting_get(raw, tag, 97, expected_width=cli.NAME_STORAGE_WIDTH), padded
        )
        self.assertEqual(cli.decode_name(padded), "Alice")
        too_long = setting_response(tag, 97, b"A" * 26)
        with self.assertRaises(cli.FuelBandError):
            cli.parse_setting_get(too_long, tag, 97, expected_width=cli.NAME_STORAGE_WIDTH)
        with self.assertRaises(cli.FuelBandError):
            cli.decode_name(b"Alice")

    def test_rtc_requires_exact_lengths_and_ranges(self):
        tag = 0x43
        self.assertEqual(cli.parse_rtc(response(tag, (0, 12, 34, 56)), tag), (12, 34, 56))
        with self.assertRaises(cli.FuelBandError):
            cli.parse_rtc(response(tag, (0, 12, 34, 56, 0)), tag)
        bad_date = response(tag, (0, 24, 2, 30, 5))
        with self.assertRaises(cli.FuelBandError):
            cli.parse_rtc(bad_date, tag, date_response=True)

    def test_declared_length_and_status_are_strict(self):
        tag = 0x44
        with self.assertRaises(cli.FuelBandError):
            cli.parse_ack(bytes((1, 2, tag, 0, 1)), tag)
        with self.assertRaises(cli.FuelBandError):
            cli.parse_ack(bytes((1, 2, tag, 1)), tag)


class SafetyTests(unittest.TestCase):
    def test_hid_identity_requires_usb_bus_and_exact_vid_pid(self):
        self.assertTrue(cli.FUELBAND_PID_MAP[cli.SUPPORTED_PRODUCT_ID]["supported"])
        self.assertFalse(cli.FUELBAND_PID_MAP[cli.LEGACY_PRODUCT_ID]["supported"])
        cli.validate_supported_hid_info(
            (cli.BUS_USB, cli.EXPECTED_VENDOR_ID, cli.EXPECTED_PRODUCT_ID)
        )
        with self.assertRaises(cli.FuelBandError):
            cli.validate_supported_hid_info(
                (0x01, cli.EXPECTED_VENDOR_ID, cli.EXPECTED_PRODUCT_ID)
            )
        with self.assertRaises(cli.FuelBandError):
            cli.validate_supported_hid_info((cli.BUS_USB, 0x1234, cli.EXPECTED_PRODUCT_ID))
        with self.assertRaisesRegex(cli.FuelBandError, "legacy/original"):
            cli.validate_supported_hid_info(
                (cli.BUS_USB, cli.EXPECTED_VENDOR_ID, cli.LEGACY_PRODUCT_ID)
            )

    def test_hidraw_matching_recognizes_legacy_family(self):
        with mock.patch.object(cli.glob, "glob", return_value=["/dev/hidraw-legacy"]), mock.patch.object(
            cli.os, "open", return_value=17
        ), mock.patch.object(cli.os, "close"), mock.patch.object(
            cli, "raw_hid_info",
            return_value=(cli.BUS_USB, cli.EXPECTED_VENDOR_ID, cli.LEGACY_PRODUCT_ID),
        ):
            self.assertEqual(cli.find_unique_fuelband(), "/dev/hidraw-legacy")

    def test_hidraw_family_ambiguity_fails_safe(self):
        with mock.patch.object(cli.glob, "glob", return_value=["/dev/current", "/dev/legacy"]), mock.patch.object(
            cli.os, "open", side_effect=(17, 18)
        ), mock.patch.object(cli.os, "close"), mock.patch.object(
            cli, "raw_hid_info",
            side_effect=(
                (cli.BUS_USB, cli.EXPECTED_VENDOR_ID, cli.SUPPORTED_PRODUCT_ID),
                (cli.BUS_USB, cli.EXPECTED_VENDOR_ID, cli.LEGACY_PRODUCT_ID),
            ),
        ):
            with self.assertRaisesRegex(cli.FuelBandError, "multiple known FuelBand-family"):
                cli.find_unique_fuelband()

    def test_final_open_fd_is_revalidated_before_use(self):
        with mock.patch.object(cli.os, "geteuid", create=True, return_value=0), mock.patch.object(
            cli, "find_unique_fuelband", return_value="/dev/final-hidraw"
        ), mock.patch.object(cli.os, "open", return_value=41), mock.patch.object(
            cli.os, "close"
        ) as close, mock.patch.object(
            cli, "raw_hid_info",
            return_value=(cli.BUS_USB, cli.EXPECTED_VENDOR_ID, cli.EXPECTED_PRODUCT_ID),
        ) as raw_info:
            with cli.FuelBand() as device:
                self.assertEqual(device.fd, 41)
            raw_info.assert_called_once_with(41)
            close.assert_called_once_with(41)

    def test_legacy_is_blocked_after_open_before_any_transfer(self):
        with mock.patch.object(cli.os, "geteuid", create=True, return_value=0), mock.patch.object(
            cli, "find_unique_fuelband", return_value="/dev/legacy-hidraw"
        ), mock.patch.object(cli.os, "open", return_value=42), mock.patch.object(
            cli.os, "close"
        ) as close, mock.patch.object(
            cli, "raw_hid_info",
            return_value=(cli.BUS_USB, cli.EXPECTED_VENDOR_ID, cli.LEGACY_PRODUCT_ID),
        ) as raw_info:
            with self.assertRaisesRegex(cli.FuelBandError, "legacy/original"):
                cli.FuelBand().__enter__()
            raw_info.assert_called_once_with(42)
            close.assert_called_once_with(42)

    def test_state_bit_preserves_all_other_bytes(self):
        original = bytes((0x00, 0xA5, 0x5A, 0xFF))
        marked = cli.mark_imprinted_value(original)
        self.assertEqual(marked, bytes((0x02, 0xA5, 0x5A, 0xFF)))
        self.assertEqual(cli.mark_imprinted_value(marked), marked)

    def test_status_setting_allowlist_contains_no_private_settings(self):
        self.assertEqual(cli.STATUS_SETTING_ALLOWLIST, frozenset((78, 97)))
        self.assertNotIn(66, cli.STATUS_SETTING_ALLOWLIST)
        self.assertNotIn(67, cli.STATUS_SETTING_ALLOWLIST)
        self.assertNotIn(76, cli.STATUS_SETTING_ALLOWLIST)

    def test_name_validation_is_strict(self):
        self.assertEqual(cli.validate_name("Alice"), b"Alice")
        self.assertEqual(
            cli.validate_name("A" * cli.NAME_WRITE_MAX_BYTES), b"A" * cli.NAME_WRITE_MAX_BYTES
        )
        for invalid in ("", "A" * 24, "caf\u00e9", "A\n", "A\x00"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(cli.FuelBandError):
                    cli.validate_name(invalid)


class MarkImprintedFlowTests(unittest.TestCase):
    def test_already_set_performs_zero_writes(self):
        device = MockMarkDevice([b"\x02\xA5\x5A\xFF"])
        result = cli.command_mark_imprinted(device)
        self.assertEqual(result, 0)
        self.assertEqual(device.writes, [])
        self.assertEqual(device.exchange_calls, [bytes((cli.SETTING_GET, 1, 78))])

    def test_clear_performs_one_bit_preserving_write_and_readback(self):
        original = b"\x00\xA5\x5A\xFF"
        expected = b"\x02\xA5\x5A\xFF"
        device = MockMarkDevice([original, expected])
        with mock.patch.object(cli.time, "sleep"):
            result = cli.command_mark_imprinted(device)
        self.assertEqual(result, 0)
        self.assertEqual(device.writes, [bytes((cli.SETTING_SET, 78, 4)) + expected])
        self.assertEqual(len(device.exchange_calls), 3)

    def test_readback_mismatch_is_an_error_after_one_write(self):
        original = b"\x00\xA5\x5A\xFF"
        wrong_readback = b"\x02\xA5\x5A\x00"
        device = MockMarkDevice([original, wrong_readback])
        with mock.patch.object(cli.time, "sleep"):
            with self.assertRaises(cli.FuelBandError):
                cli.command_mark_imprinted(device)
        self.assertEqual(len(device.writes), 1)
        self.assertEqual(len(device.exchange_calls), 3)


class SetTargetFlowTests(unittest.TestCase):
    def test_target_payload_is_little_endian_for_all_days_in_order(self):
        target = 0x12345678
        target_bytes = b"\x78\x56\x34\x12"
        device = MockTargetDevice(
            {setting_id: target_bytes for setting_id in cli.DAILY_TARGET_SETTING_IDS}
        )
        with mock.patch.object(cli.time, "sleep"):
            self.assertEqual(cli.command_set_target(device, str(target)), 0)
        expected_writes = [
            bytes((cli.SETTING_SET, setting_id, 4)) + target_bytes
            for setting_id in cli.DAILY_TARGET_SETTING_IDS
        ]
        self.assertEqual(device.writes, expected_writes)
        expected_calls = []
        for setting_id in cli.DAILY_TARGET_SETTING_IDS:
            expected_calls.extend(
                (
                    bytes((cli.SETTING_SET, setting_id, 4)) + target_bytes,
                    bytes((cli.SETTING_GET, 1, setting_id)),
                )
            )
        self.assertEqual(device.exchange_calls, expected_calls)

    def test_target_readback_failure_stops_without_retry(self):
        target_bytes = b"\x78\x56\x34\x12"
        wrong = b"\x79\x56\x34\x12"
        readbacks = {setting_id: target_bytes for setting_id in cli.DAILY_TARGET_SETTING_IDS}
        readbacks[40] = wrong
        device = MockTargetDevice(readbacks)
        with self.assertRaises(cli.FuelBandError):
            cli.command_set_target(device, 0x12345678)
        self.assertEqual(len(device.writes), 1)
        self.assertEqual(len(device.exchange_calls), 2)

    def test_target_bounds(self):
        self.assertEqual(cli.parse_target("1"), 1)
        self.assertEqual(cli.parse_target("0xffffffff"), 0xFFFFFFFF)
        for value in ("0", "0x100000000", "-1", "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaises(cli.FuelBandError):
                    cli.parse_target(value)


class ReleaseStaticTests(unittest.TestCase):
    def test_wrapper_has_explicit_mark_switches_and_exact_usb_row_matching(self):
        wrapper = (Path(__file__).with_name("fuelband.ps1")).read_text(encoding="utf-8")
        self.assertIn("[switch]$Experimental", wrapper)
        self.assertIn("[switch]$Yes", wrapper)
        self.assertIn("Confirm-MarkImprintedSwitches", wrapper)
        self.assertIn("$fields[0] -eq $RequestedBusId", wrapper)
        self.assertIn("$FuelBandPidMap.ContainsKey($devicePid)", wrapper)
        self.assertIn('"--experimental", "--yes"', wrapper)
        self.assertIn('"set-target"', wrapper)
        self.assertIn("[string]$Fuel", wrapper)
        self.assertIn("Get-FuelBandSelections", wrapper)
        self.assertIn("Resolve-FuelBandSelection", wrapper)
        self.assertIn('"11ac:6565"', wrapper)
        self.assertIn("Multiple FuelBand-family devices", wrapper)
        self.assertIn("Selected FuelBand BUSID", wrapper)
        self.assertIn("Confirm-SupportedProtocol", wrapper)
        self.assertIn("function Test-WslHidraw", wrapper)
        self.assertIn("function Wait-WslHidraw", wrapper)
        self.assertIn("Wait-WslHidraw $wsl $selection.Pid 10", wrapper)
        self.assertIn("$ExpectedPid", wrapper)
        self.assertIn("wsl_hidraw_probe.py", wrapper)
        self.assertIn("Convert-ReleasePathToWsl", wrapper)
        self.assertIn("$linuxProbe", wrapper)
        self.assertNotIn('"--user", "root", "--", "python3", "-c"', wrapper)
        self.assertIn("return $true", wrapper)
        self.assertIn("return $false", wrapper)
        self.assertIn("$deadline", wrapper)
        self.assertIn("Start-Sleep -Milliseconds 250", wrapper)
        self.assertIn("detach -BusId $($selection.BusId)", wrapper)

    def test_cli_requires_mark_flags_before_opening_fuelband(self):
        for argv in (("mark-imprinted",), ("mark-imprinted", "--experimental")):
            with self.subTest(argv=argv), mock.patch.object(cli, "FuelBand") as fuelband:
                with self.assertRaises(SystemExit):
                    cli.main(list(argv))
                fuelband.assert_not_called()

    def test_attach_readiness_static_scenarios_are_exact_and_bounded(self):
        wrapper = (Path(__file__).with_name("fuelband.ps1")).read_text(encoding="utf-8")
        probe = (Path(__file__).with_name("wsl_hidraw_probe.py")).read_text(encoding="utf-8")
        # Unrelated hidraw nodes cannot satisfy the HID_ID vendor/product test.
        self.assertIn("vendor == expected_vendor and product == expected_product", probe)
        self.assertIn("return False", probe)
        self.assertIn("/sys/class/hidraw/hidraw*/device/uevent", probe)
        # A delayed matching node is retried with the selected PID as an arg.
        self.assertIn("Test-WslHidraw $WslCommand $ExpectedPid", wrapper)
        self.assertIn("Start-Sleep -Milliseconds 250", wrapper)
        # The bounded wait returns false and attach reports cleanup on timeout.
        self.assertIn("$deadline = (Get-Date).AddSeconds($TimeoutSeconds)", wrapper)
        self.assertIn("return $false", wrapper)
        self.assertIn("usbipd attach succeeded", wrapper)


if __name__ == "__main__":
    unittest.main()
