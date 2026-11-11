import unittest

import config as C
from scanner import message_for, trade_levels


class TradePlanTests(unittest.TestCase):
    def test_levels_are_symmetric_in_risk(self):
        t = trade_levels(100.0, 104.0)
        sl = 104.0 * (1 + C.SL_BUFFER_PCT / 100)
        self.assertAlmostEqual(t["sl"], sl)
        self.assertAlmostEqual(t["entry"] - t["tp1"], (sl - 100.0) * C.TP1_R)
        self.assertAlmostEqual(t["entry"] - t["tp2"], (sl - 100.0) * C.TP2_R)
        self.assertLess(t["tp2"], t["tp1"])
        self.assertLess(t["tp1"], t["entry"])

    def test_no_levels_when_entry_above_stop(self):
        self.assertIsNone(trade_levels(105.0, 104.0))

    def test_message_has_plan(self):
        candles = [{"ts": 1_762_876_800_000, "o": 1.02, "h": 1.03, "l": 0.99, "c": 1.00, "v": 1, "closed": True}]
        event = {"retest_index": 0, "bos_level": 1.01, "lh_price": 1.04}
        text = message_for("TEST-USDT-SWAP", "1h", candles, event)
        self.assertIn("План сделки", text)
        self.assertIn("Стоп-лосс", text)
        self.assertIn("Тейк 2", text)
        print("\n" + text)


if __name__ == "__main__":
    unittest.main()
