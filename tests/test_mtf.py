"""Схема 1ч → 15м: наблюдение, вход, фильтр «поздно». OKX и Telegram подменены."""
import os
import unittest
from unittest import mock

import config as C
import scanner as SC
from structure import bearish_state

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


class CrashWarningTests(SmallPivots):
    def test_normal_breakout_has_no_warning(self):
        cs = reversal(M15)
        self.assertIsNone(SC.crash_warning(cs, 12))

    def test_crash_candle_warned(self):
        cs = reversal(M15)
        cs[12] = dict(cs[12], l=4.0)
        self.assertIn("обвал", SC.crash_warning(cs, 12))


def forming(cs, bar_ms):
    return cs + [dict(cs[-1], ts=cs[-1]["ts"] + bar_ms, closed=False)]


class FlowTests(SmallPivots):
    def setUp(self):
        super().setUp()
        os.environ["DRY_RUN"] = "1"
        self.sent = []
        self.state = {"processed": {}, "sent": {}, "pending": {}, "watch": {}}

    def tearDown(self):
        super().tearDown()
        os.environ.pop("DRY_RUN", None)
        os.environ.pop("SCAN_ALL", None)

    def run_scan(self, c1h, c15, now, setups=True):
        def fetch(inst, bar):
            return {"1H": c1h, "15m": c15}.get(bar)
        if setups:
            os.environ["SCAN_ALL"] = "1"
        else:
            os.environ.pop("SCAN_ALL", None)
        with mock.patch.object(SC, "fetch_instruments", return_value=["PLTR-USDT-SWAP"]), \
             mock.patch.object(SC, "fetch_candles", side_effect=fetch), \
             mock.patch.object(SC, "telegram_send", side_effect=lambda t: self.sent.append(t) or True), \
             mock.patch.object(SC.time, "time", return_value=now / 1000):
            return SC.scan(self.state)

    def breakout(self):
        """1ч: пробой вниз (свеча 12), ждём ретест к уровню 9; нижний максимум 12."""
        c1h = forming(reversal(H)[:13], H)
        now = c1h[-2]["ts"] + H + 60_000
        self.run_scan(c1h, None, now)
        return c1h, now

    def test_breakout_gives_watch_message(self):
        self.breakout()
        self.assertIn("PLTR-USDT-SWAP|1h", self.state["watch"])
        msg = [t for t in self.sent if t.startswith("👀")]
        self.assertEqual(len(msg), 1)
        self.assertIn("PLTR (1ч): уровень 9.0000", msg[0])

    def test_alarm_when_price_returns_to_level_before_hour_closes(self):
        c1h, now = self.breakout()
        later = now + 16 * 60_000
        c15 = [dict(candle(9.0, 8.6, 8.8), ts=later - 2 * M15), dict(candle(8.95, 8.7, 8.9), ts=later - M15, closed=False)]
        c15[0]["closed"] = True
        self.run_scan(c1h, c15, later, setups=False)
        alarms = [t for t in self.sent if t.startswith("⚡")]
        self.assertEqual(len(alarms), 1, self.sent)
        self.assertIn("ретест идёт сейчас", alarms[0])
        self.assertIn("Свеча 1ч закроется", alarms[0])
        self.assertIn("Вход: 9.0000", alarms[0])
        self.run_scan(c1h, c15, later + 60_000, setups=False)  # повторно не шлёт
        self.assertEqual(len([t for t in self.sent if t.startswith("⚡")]), 1)

    def test_no_alarm_while_price_is_below_level(self):
        c1h, now = self.breakout()
        later = now + 16 * 60_000
        c15 = [dict(candle(8.6, 8.3, 8.5), ts=later - 2 * M15), dict(candle(8.55, 8.3, 8.4), ts=later - M15, closed=False)]
        self.run_scan(c1h, c15, later, setups=False)
        self.assertFalse([t for t in self.sent if t.startswith("⚡")])

    def test_confirmation_after_hour_closes(self):
        self.breakout()
        c1h = forming(reversal(H), H)
        self.run_scan(c1h, None, c1h[-2]["ts"] + H + 60_000)
        ok = [t for t in self.sent if t.startswith("✅")]
        self.assertEqual(len(ok), 1, self.sent)
        self.assertIn("ретест подтверждён", ok[0])
        self.assertNotIn("PLTR-USDT-SWAP|1h", self.state["watch"])
        self.assertFalse([t for t in self.sent if "Отбой" in t])

    def test_cancel_when_price_closes_above_lower_high(self):
        self.breakout()
        cs = reversal(H)[:13] + [dict(candle(13, 9, 12.8), ts=13 * H)]
        c1h = forming(cs, H)
        self.run_scan(c1h, None, c1h[-2]["ts"] + H + 60_000)
        self.assertNotIn("PLTR-USDT-SWAP|1h", self.state["watch"])
        self.assertTrue(any("Отбой" in t and "выше нижнего максимума" in t for t in self.sent), self.sent)


if __name__ == "__main__":
    unittest.main()
