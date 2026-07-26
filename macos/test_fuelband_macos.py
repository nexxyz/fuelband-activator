import io
import unittest
from unittest import mock

import fuelband_macos as cli


def response(tag, payload, report_id=cli.RESPONSE_REPORT_ID):
    body = bytes((tag,)) + bytes(payload)
    return bytes((report_id, len(body))) + body


def setting_response(tag, setting_id, value):
    return response(tag, (0, 1, setting_id, len(value)) + tuple(value))


def ack_response(tag):
    return bytes((1, 2, tag, 0))


class MockCommandDevice:
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
            return cli.Transaction(
                tag, b"", setting_response(tag, cli.DEVICE_STATE_SETTING, value)
            )
        if command[:2] == bytes((cli.SETTING_SET, cli.DEVICE_STATE_SETTING)):
            self.writes.append(command)
            return cli.Transaction(tag, b"", ack_response(tag))
        raise AssertionError("unexpected command: %r" % (command,))


class FramingTests(unittest.TestCase):
    def test_output_report_buckets(self):
        self.assertEqual(cli.select_output_report(bytes(5)), 0x09)
        self.assertEqual(cli.select_output_report(bytes(13)), 0x0A)
        self.assertEqual(cli.select_output_report(bytes(29)), 0x0B)
        self.assertEqual(cli.select_output_report(bytes(61)), 0x0C)
        with self.assertRaises(cli.FuelBandError):
            cli.select_output_report(bytes(62))


class ParserTests(unittest.TestCase):
    def test_strict_ack_and_report(self):
        self.assertEqual(cli.parse_ack(bytes((1, 2, 0x40, 0)), 0x40), 0)
        with self.assertRaises(cli.FuelBandError):
            cli.parse_ack(bytes((2, 2, 0x40, 0)), 0x40)
        with self.assertRaises(cli.FuelBandError):
            cli.parse_ack(bytes((1, 2, 0x40, 0, 1)), 0x40)

    def test_state_and_fixed_name_storage(self):
        tag = 0x41
        state = response(tag, (0, 1, 78, 4, 2, 0xA5, 0x5A, 0xFF))
        self.assertEqual(cli.parse_setting_get(state, tag, 78, expected_width=4), b"\x02\xA5\x5A\xFF")
        padded_name = b"Emilia" + b"\0" * 19
        name = setting_response(tag, 97, padded_name)
        self.assertEqual(
            cli.parse_setting_get(name, tag, 97, expected_width=cli.NAME_STORAGE_WIDTH),
            padded_name,
        )
        self.assertEqual(cli.decode_name(padded_name), "Emilia")
        with self.assertRaises(cli.FuelBandError):
            cli.parse_setting_get(setting_response(tag, 97, b"A" * 24), tag, 97, expected_width=25)
        with self.assertRaises(cli.FuelBandError):
            cli.decode_name(b"Emilia")

    def test_rtc_exact_lengths_and_ranges(self):
        tag = 0x42
        self.assertEqual(cli.parse_rtc(response(tag, (0, 12, 34, 56)), tag), (12, 34, 56))
        with self.assertRaises(cli.FuelBandError):
            cli.parse_rtc(response(tag, (0, 12, 34, 56, 0)), tag)
        with self.assertRaises(cli.FuelBandError):
            cli.parse_rtc(response(tag, (0, 2, 30, 30, 5)), tag, date_response=True)


class GuardTests(unittest.TestCase):
    def test_name_write_guard_is_23_ascii_bytes(self):
        self.assertEqual(cli.validate_name("A" * 23), b"A" * 23)
        for value in ("", "A" * 24, "caf\u00e9", "A\n", "A\0"):
            with self.subTest(value=value):
                with self.assertRaises(cli.FuelBandError):
                    cli.validate_name(value)

    def test_mark_already_set_performs_zero_writes(self):
        device = MockCommandDevice([b"\x02\xA5\x5A\xFF"])
        self.assertEqual(cli.command_mark_imprinted(device), 0)
        self.assertEqual(device.writes, [])
        self.assertEqual(len(device.exchange_calls), 1)

    def test_mark_clear_performs_one_preserving_write_and_readback(self):
        original = b"\x00\xA5\x5A\xFF"
        expected = b"\x02\xA5\x5A\xFF"
        device = MockCommandDevice([original, expected])
        with mock.patch.object(cli.time, "sleep"):
            self.assertEqual(cli.command_mark_imprinted(device), 0)
        self.assertEqual(device.writes, [bytes((cli.SETTING_SET, 78, 4)) + expected])
        self.assertEqual(len(device.exchange_calls), 3)

    def test_mark_mismatch_is_error_without_retry(self):
        device = MockCommandDevice([b"\0\xA5\x5A\xFF", b"\x02\xA5\x5A\0"])
        with mock.patch.object(cli.time, "sleep"):
            with self.assertRaises(cli.FuelBandError):
                cli.command_mark_imprinted(device)
        self.assertEqual(len(device.writes), 1)
        self.assertEqual(len(device.exchange_calls), 3)

    def test_cli_requires_both_mark_flags_before_hid_open(self):
        for argv in (("mark-imprinted",), ("mark-imprinted", "--experimental")):
            with self.subTest(argv=argv), mock.patch.object(cli, "FuelBand") as fuelband:
                with mock.patch("sys.stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        cli.main(list(argv))
                fuelband.assert_not_called()


class FakeHidDevice:
    def __init__(self, send_result=None):
        self.send_result = send_result
        self.path = None
        self.sent_reports = []
        self.read_requests = []
        self.closed = False

    def open_path(self, path):
        self.path = path

    def close(self):
        self.closed = True

    def send_feature_report(self, report):
        self.sent_reports.append(bytes(report))
        if self.send_result is not None:
            return self.send_result
        return len(report)

    def get_feature_report(self, report_id, size):
        self.read_requests.append((report_id, size))
        tag = self.sent_reports[-1][2]
        return [1, 2, tag, 0]


class FakeHidModule:
    def __init__(self, entries, device):
        self.entries = entries
        self.fake_device = device
        self.enumerate_calls = []

    def enumerate(self, vendor_id, product_id):
        self.enumerate_calls.append((vendor_id, product_id))
        return self.entries

    def device(self):
        return self.fake_device


class HidTransportTests(unittest.TestCase):
    def test_identity_open_and_feature_exchange_are_mocked(self):
        fake_device = FakeHidDevice()
        fake_hid = FakeHidModule(
            [
                {
                    "vendor_id": 0x11AC,
                    "product_id": 0x317D,
                    "usage_page": 0xFF00,
                    "usage": 0x01,
                    "path": b"mock-path",
                }
            ],
            fake_device,
        )
        with mock.patch.object(cli, "hid", fake_hid):
            with cli.FuelBand() as device:
                transaction = device.exchange(bytes((cli.SETTING_GET, 1, 78)))
        self.assertEqual(fake_hid.enumerate_calls, [(0x11AC, 0x317D)])
        self.assertEqual(fake_device.path, b"mock-path")
        self.assertEqual(len(fake_device.sent_reports), 1)
        self.assertEqual(len(fake_device.sent_reports[0]), 64)
        self.assertEqual(fake_device.sent_reports[0][0], 0x09)
        self.assertEqual(fake_device.read_requests, [(4, 64)])
        self.assertEqual(transaction.response, b"\x01\x02\x40\0")
        self.assertTrue(fake_device.closed)

    def test_nonpositive_send_is_failure(self):
        fake_device = FakeHidDevice(send_result=0)
        fake_hid = FakeHidModule(
            [
                {
                    "vendor_id": 0x11AC,
                    "product_id": 0x317D,
                    "usage_page": 0xFF00,
                    "usage": 0x01,
                    "path": "mock-path",
                }
            ],
            fake_device,
        )
        with mock.patch.object(cli, "hid", fake_hid):
            with cli.FuelBand() as device:
                with self.assertRaises(cli.FuelBandError):
                    device.exchange(bytes((cli.SETTING_GET, 1, 78)))

    def test_positive_short_send_is_failure(self):
        fake_device = FakeHidDevice(send_result=63)
        fake_hid = FakeHidModule(
            [
                {
                    "vendor_id": 0x11AC,
                    "product_id": 0x317D,
                    "usage_page": 0xFF00,
                    "usage": 0x01,
                    "path": "mock-path",
                }
            ],
            fake_device,
        )
        with mock.patch.object(cli, "hid", fake_hid):
            with cli.FuelBand() as device:
                with self.assertRaises(cli.FuelBandError):
                    device.exchange(bytes((cli.SETTING_GET, 1, 78)))

    def test_multiple_matching_collections_are_rejected(self):
        fake_hid = FakeHidModule(
            [
                {
                    "vendor_id": 0x11AC,
                    "product_id": 0x317D,
                    "usage_page": 0xFF00,
                    "usage": 0x01,
                    "path": "a",
                },
                {
                    "vendor_id": 0x11AC,
                    "product_id": 0x317D,
                    "usage_page": 0xFF00,
                    "usage": 0x01,
                    "path": "b",
                },
            ],
            FakeHidDevice(),
        )
        with mock.patch.object(cli, "hid", fake_hid):
            with self.assertRaises(cli.FuelBandError):
                cli.enumerate_fuelband()

    def test_wrong_or_missing_usage_metadata_is_rejected(self):
        for metadata in (
            {"usage_page": 0xFF01, "usage": 0x01},
            {"usage_page": 0xFF00},
        ):
            entry = {
                "vendor_id": 0x11AC,
                "product_id": 0x317D,
                "path": "mock-path",
            }
            entry.update(metadata)
            fake_hid = FakeHidModule([entry], FakeHidDevice())
            with self.subTest(metadata=metadata), mock.patch.object(cli, "hid", fake_hid):
                with self.assertRaises(cli.FuelBandError):
                    cli.enumerate_fuelband()


if __name__ == "__main__":
    unittest.main()
