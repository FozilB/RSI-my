import os
import unittest

import config as C
from scanner import entry_status, message_for, signal_age_min, timeframe_due

H = 60 * 60 * 1000
M = 60 * 1000


class FreshnessTests(unittest.TestCase):
    def test_age_counts_from_candle_close(self):
        # Свеча 1h открылась в 07:00, закрылась в 08:00; сейчас 14:49 -> 409 минут
        open_ts = 7 * H
        self.assertAlmostEqual(signal_age_min("1h", open_ts, 14 * H + 49 * M), 409)
        self.assertGreater(signal_age_min("1h", open_ts, 14 * H + 49 * M), C.MAX_SIGNAL_AGE_MIN)
        self.assertLess(signal_age_min("1h", open_ts, 8 * H + 9 * M), C.MAX_SIGNAL_AGE_MIN)

    def test_higher_timeframes_only_right_after_close(self):
        os.environ.pop("SCAN_ALL", None)
        self.assertTrue(timeframe_due("30m", 10 * H + 31 * M))
        self.assertTrue(timeframe_due("1h", 10 * H + 1 * M))
        self.assertFalse(timeframe_due("1h", 10 * H + 31 * M))
        self.assertTrue(timeframe_due("4h", 8 * H + 1 * M))
        self.assertFalse(timeframe_due("4h", 9 * H + 1 * M))
        os.environ["SCAN_ALL"] = "1"
        try:
            self.assertTrue(timeframe_due("4h", 9 * H + 1 * M))
        finally:
            os.environ.pop("SCAN_ALL")

    def test_entry_status(self):
        # вход 100, LH 104 -> стоп ~104.16, риск ~4.16; «не догонять» ниже ~97.92
        self.assertEqual(entry_status(100.0, 104.0, 99.0), "ok")
        self.assertEqual(entry_status(100.0, 104.0, 97.0), "chased")
        self.assertEqual(entry_status(100.0, 104.0, 105.0), "stopped")

    def test_message_shows_current_price_and_age(self):
        candles = [{"ts": 0, "o": 1.02, "h": 1.03, "l": 0.99, "c": 1.00, "v": 1, "closed": True}]
        event = {"retest_index": 0, "bos_level": 1.01, "lh_price": 1.04}
        text = message_for("TEST-USDT-SWAP", "1h", candles, event, current=0.99, now_ms=H + 7 * M)
        self.assertIn("Сейчас: 0.99 (-1.00% от входа) · сигналу 7 мин", text)


if __name__ == "__main__":
    unittest.main()
