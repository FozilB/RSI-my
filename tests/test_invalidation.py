import random
import unittest

from structure import find_bearish_confirmations


def walk(seed, n=300):
    random.seed(seed)
    p, out = 100.0, []
    for i in range(n):
        o = p
        p *= 1 + random.gauss(0, 0.01)
        h = max(o, p) * (1 + abs(random.gauss(0, 0.004)))
        l = min(o, p) * (1 - abs(random.gauss(0, 0.004)))
        out.append({"ts": i, "o": o, "h": h, "l": l, "c": p, "v": 1, "closed": True})
    return out


class InvalidationTests(unittest.TestCase):
    def test_no_signal_after_close_above_lower_high(self):
        for seed in range(300):
            cs = walk(seed)
            for e in find_bearish_confirmations(cs):
                closes = [c["c"] for c in cs[e["lh_index"] + 1:e["retest_index"] + 1]]
                self.assertLessEqual(max(closes), e["lh_price"], f"seed {seed}")

    def test_retest_not_too_late(self):
        for seed in range(300):
            for e in find_bearish_confirmations(walk(seed), retest_max_bars=12, setup_max_bars=60):
                self.assertLessEqual(e["retest_index"] - e["bos_index"], 12)
                self.assertLessEqual(e["retest_index"] - e["choch_index"], 60)


if __name__ == "__main__":
    unittest.main()
