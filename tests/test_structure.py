import unittest

from structure import classify_swings, find_bearish_confirmations, find_swings


def candle(high, low, close):
    return {"ts": 0, "o": close, "h": high, "l": low, "c": close, "v": 1, "closed": True}


class StructureTests(unittest.TestCase):
    def test_swings_require_right_side_confirmation(self):
        candles = [candle(10, 8, 9), candle(12, 9, 11), candle(10, 7, 8)]
        swings = find_swings(candles, left=1, right=1)
        self.assertEqual([(s["kind"], s["index"], s["confirmed_at"]) for s in swings],
                         [("high", 1, 2)])

    def test_swing_labels_classify_higher_and_lower_points(self):
        swings = [
            {"kind": "high", "index": 1, "price": 10},
            {"kind": "low", "index": 2, "price": 5},
            {"kind": "high", "index": 3, "price": 12},
            {"kind": "low", "index": 4, "price": 6},
            {"kind": "high", "index": 5, "price": 11},
            {"kind": "low", "index": 6, "price": 4},
        ]
        labels = [p["label"] for p in classify_swings(swings)]
        self.assertEqual(labels, [None, None, "HH", "HL", "LH", "LL"])

    def make_reversal(self):
        return [
            candle(10, 9, 9.5),
            candle(12, 10, 11),       # high
            candle(11, 8, 9),         # low
            candle(13, 10.2, 12),     # higher high
            candle(12, 10, 11),       # higher low
            candle(14, 11.8, 13),     # higher high (low above 11.5 so the next candle is a real HL)
            candle(13, 11.5, 12),     # higher low
            candle(12, 12, 12.5),     # confirms higher low
            candle(12, 10.5, 10.8),   # CHoCH below 11.5
            candle(11.5, 9, 10),      # interim low
            candle(12, 10, 11),       # lower high
            candle(10.5, 9.5, 10),    # confirms pivots; no BOS yet
            candle(9.2, 8.2, 8.5),    # BOS below 9
            candle(9.4, 8.3, 8.7),    # retest and rejection
        ]

    def test_full_bearish_confirmation_sequence(self):
        events = find_bearish_confirmations(self.make_reversal(), left=1, right=1, atr_period=3)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["choch_index"], 8)
        self.assertEqual(event["bos_index"], 12)
        self.assertEqual(event["retest_index"], 13)
        self.assertEqual(event["bos_level"], 9)

    def test_no_signal_before_retest(self):
        candles = self.make_reversal()[:-1]
        self.assertEqual(find_bearish_confirmations(candles, left=1, right=1), [])


if __name__ == "__main__":
    unittest.main()
