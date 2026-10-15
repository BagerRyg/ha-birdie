"""Malformed and unknown measurement handling using the real parsers/models."""

import importlib
import unittest

from test_polling import BirdieData

ble = importlib.import_module("birdie_under_test.ble")


class MeasurementValidationTests(unittest.TestCase):
    def test_unknown_birdie_state_does_not_report_alarm_ok(self):
        for value in (None, 3, 255):
            with self.subTest(state=value):
                data = BirdieData(address="02:00:00:00:00:01", birdie_state=value)
                self.assertEqual(data.birdie_state_value, "unknown")
                self.assertIsNone(data.co2_alarm)

    def test_known_birdie_states_keep_alarm_semantics(self):
        for state, expected in ((0, False), (1, True), (2, False)):
            self.assertEqual(BirdieData(address="02:00:00:00:00:01", birdie_state=state).co2_alarm,
                             expected)

    def test_humidity_rejects_impossible_percentage(self):
        for value in (100001, 4294967295):
            with self.assertRaisesRegex(ValueError, "relative humidity"):
                ble.parse_humidity_percent(value.to_bytes(4, "little"))

    def test_humidity_boundaries_and_payload_widths(self):
        for value, width, expected in ((0, 2, 0), (50000, 2, 50), (100000, 4, 100)):
            self.assertEqual(ble.parse_humidity_percent(value.to_bytes(width, "little")), expected)
        for width in (0, 1, 3, 5):
            with self.assertRaises(ValueError):
                ble.parse_humidity_percent(bytes(width))


if __name__ == "__main__":
    unittest.main()
