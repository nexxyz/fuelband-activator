import unittest
from unittest import mock

import wsl_hidraw_probe as probe


class HidrawProbeTests(unittest.TestCase):
    def test_exact_usb_vid_pid_match(self):
        expected = probe.parse_vid_pid("11ac:317d")
        self.assertTrue(
            probe.uevent_matches("HID_ID=0003:000011AC:0000317D\n", expected)
        )
        self.assertFalse(
            probe.uevent_matches("HID_ID=0003:000011AC:00006565\n", expected)
        )
        self.assertFalse(
            probe.uevent_matches("HID_ID=0001:000011AC:0000317D\n", expected)
        )

    def test_unrelated_hidraw_is_rejected_and_exact_match_is_accepted(self):
        expected = probe.parse_vid_pid("11ac:317d")
        unrelated = mock.mock_open(read_data="HID_ID=0003:00001234:00005678\n")
        with mock.patch("builtins.open", unrelated):
            self.assertFalse(probe.hidraw_matches(expected, ["unrelated-uevent"]))
        matching = mock.mock_open(read_data="HID_ID=0003:000011AC:0000317D\n")
        with mock.patch("builtins.open", matching):
            self.assertTrue(probe.hidraw_matches(expected, ["matching-uevent"]))

    def test_invalid_expected_pid_is_rejected(self):
        with self.assertRaises(ValueError):
            probe.parse_vid_pid("11ac:317d:extra")
        self.assertEqual(probe.main(["not-a-pid"]), 2)


if __name__ == "__main__":
    unittest.main()
