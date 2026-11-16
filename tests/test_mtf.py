"""Схема 1ч → 15м: наблюдение, вход, фильтр «поздно». OKX и Telegram подменены."""
import os
import unittest
from unittest import mock

import config as C
import scanner as SC
from structure import bearish_state, find_bearish_confirmations

M15 = 15 * 60 * 1000
H = 60 * 60 * 1000


def candle(high, low, close):
    return {"ts": 0, "o": close, "h": high, "l": low, "c": close, "v": 1, "closed": True}


def reversal(bar_ms, start=0, scale=1.0):
    """Рост → слом (8) → нижний максимум (10) → пробой (12) → ретест (13), как в test_structure."""
    raw = [(10, 9, 9.5), (12, 10, 11), (11, 8, 9), (13, 10.2, 12), (12, 10, 11), (14, 11.8, 13), (13, 11.5, 12),
           (12, 12, 12.5), (12, 10.5, 10.8), (11.5, 9, 10), (12, 10, 11), (10.5, 9.5, 10), (9.2, 8.2, 8.5),
           (9.4, 8.3, 8.7)]
    return [dict(candle(h * scale, l * scale, c * scale), ts=start + i * bar_ms) for i, (h, l, c) in enumerate(raw)]


KW = dict(left=1, right=1, atr_period=3)


class SmallPivots(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.multiple(C, PIVOT_LEFT=1, PIVOT_RIGHT=1, ATR_PERIOD=3)
        self.p.start()

    def tearDown(self):
        self.p.stop()


class StructureStateTests(unittest.TestCase):
    def test_state_after_choch(self):
        st = bearish_state(reversal(H)[:9], **KW)
        self.assertEqual(st["stage"], "after_choch")
        self.assertEqual(st["high_reference"], 14)

    def test_state_waiting_for_retest(self):
        st = bearish_state(reversal(H)[:13], **KW)
        self.assertEqual(st["stage"], "retest")
        self.assertEqual(st["lh_price"], 12)
        self.assertEqual(st["bos_level"], 9)

    def test_state_after_event(self):
        st = bearish_state(reversal(H), **KW)
        self.assertEqual(st["stage"], "trend")
        self.assertEqual(st["last_event_index"], 13)
        self.assertEqual(st["last_event_lh"], 12)


class LateFilterTests(SmallPivots):
    def test_normal_retest_is_not_late(self):
        cs = reversal(M15)
        e = find_bearish_confirmations(cs, **KW)[-1]
        self.assertIsNone(SC.late_reason(cs, e))

    def test_far_from_lower_high_is_late(self):
        cs = reversal(M15)
        e = dict(find_bearish_confirmations(cs, **KW)[-1], lh_price=20)
        self.assertIn("ATR", SC.late_reason(cs, e))

    def test_crash_candle_is_late(self):
        cs = reversal(M15)
        e = find_bearish_confirmations(cs, **KW)[-1]
        cs[12] = dict(cs[12], l=4.0)
        self.assertIn("обвал", SC.late_reason(cs, e))


class FlowTests(SmallPivots):
    def setUp(self):
        super().setUp()
        os.environ["DRY_RUN"] = "1"
        self.sent = []
        self.state = {"processed": {}, "sent": {}, "pending": {}, "watch": {}, "last_entry": {}}

    def tearDown(self):
        super().tearDown()
        os.environ.pop("DRY_RUN", None)
        os.environ.pop("SCAN_ALL", None)

    def run_scan(self, c1h, c15, now):
        def fetch(inst, bar):
            return {"1H": c1h, "15m": c15}.get(bar)
        with mock.patch.object(SC, "fetch_instruments", return_value=["PLTR-USDT-SWAP"]), \
             mock.patch.object(SC, "fetch_candles", side_effect=fetch), \
             mock.patch.object(SC, "telegram_send", side_effect=lambda t: self.sent.append(t) or True), \
             mock.patch.object(SC.time, "time", return_value=now / 1000):
            stats = SC.scan(self.state)
            SC.flush_pending(self.state)
        return stats

    def test_watch_then_entry(self):
        c1h = reversal(H)[:9]                            # на 1ч рост только сломался
        now = c1h[-1]["ts"] + H + 60_000                 # через минуту после закрытия часовой свечи
        os.environ["SCAN_ALL"] = "1"
        self.run_scan(c1h, None, now)
        self.assertIn("PLTR-USDT-SWAP", self.state["watch"])
        self.assertTrue(any("👀" in t and "PLTR" in t for t in self.sent))
        os.environ.pop("SCAN_ALL")
        # на 15м сложилась точка входа: последняя закрытая свеча — ретест (цены ниже часового максимума 14)
        c15 = reversal(M15, start=now, scale=0.9)
        c15.append(dict(c15[-1], ts=c15[-1]["ts"] + M15, closed=False))
        now15 = c15[-2]["ts"] + M15 + 60_000
        self.run_scan(c1h, c15, now15)
        entries = [t for t in self.sent if t.startswith("🔻")]
        self.assertEqual(len(entries), 1, self.sent)
        self.assertIn("1ч: рост сломался", entries[0])
        self.assertIn("Вход:", entries[0])
        # повтор того же сигнала не приходит
        self.run_scan(c1h, c15, now15 + 60_000)
        self.assertEqual(len([t for t in self.sent if t.startswith("🔻")]), 1)

    def test_no_entry_for_coin_not_watched(self):
        c15 = reversal(M15)
        c15.append(dict(c15[-1], ts=c15[-1]["ts"] + M15, closed=False))
        self.run_scan(None, c15, c15[-2]["ts"] + M15 + 60_000)
        self.assertFalse([t for t in self.sent if t.startswith("🔻")])

    def test_watch_removed_when_trend_resumes(self):
        self.state["watch"]["PLTR-USDT-SWAP"] = {"stage": "after_choch", "lh": None, "bos": None,
                                                 "ceiling": 105, "since": 0, "seen": 0}
        c1h = [dict(candle(10 + i, 9 + i, 9.5 + i), ts=i * H) for i in range(14)]  # ровный рост
        os.environ["SCAN_ALL"] = "1"
        self.run_scan(c1h, None, c1h[-1]["ts"] + H + 60_000)
        self.assertNotIn("PLTR-USDT-SWAP", self.state["watch"])
        self.assertTrue(any("Отбой" in t for t in self.sent))


if __name__ == "__main__":
    unittest.main()
