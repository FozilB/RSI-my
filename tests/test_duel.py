"""Бот-конкурент: решение о входе и виртуальное ведение сделки."""
import os
import tempfile
import unittest
from unittest import mock

import config as C
import duel as D

M15 = 15 * 60 * 1000
H = 60 * 60 * 1000


def c(ts, h, l, cl, closed=True):
    return {"ts": ts, "o": cl, "h": h, "l": l, "c": cl, "v": 1, "closed": closed}


def flat_4h(price, n=60):
    return [c(i * 4 * H, price * 1.01, price * 0.99, price) for i in range(n)]


W = {"level": 100.0, "lh": 102.0, "tol": 0.2, "warn": None}  # стоп ≈ 102.15


class DecideTests(unittest.TestCase):
    def test_takes_normal_signal(self):
        fetch = lambda inst, bar: flat_4h(110) + [c(999 * H, 101, 99, 100)]  # noqa: E731 — 4ч: цена ниже средней
        self.assertIsNone(D.decide("X", "1h", W, 100.0, fetch))

    def test_skips_crash(self):
        self.assertEqual(D.decide("X", "1h", dict(W, warn="⚠️"), 100.0, lambda *a: None), "пробой свечой-обвалом")

    def test_skips_wide_stop(self):
        self.assertEqual(D.decide("X", "4h", dict(W, lh=120.0), 100.0, lambda *a: None), "широкий стоп")

    def test_skips_against_4h_uptrend(self):
        fetch = lambda inst, bar: flat_4h(90) + [c(999 * H, 101, 99, 100)]  # noqa: E731 — 4ч: цена выше средней
        self.assertIn("4ч", D.decide("X", "1h", W, 100.0, fetch))


class TrackTests(unittest.TestCase):
    def open_trade(self):
        d = {"trades": [], "signals": [], "skipped": [], "deposit": 100.0, "balance": 100.0}
        D.on_alarm(d, "X-USDT-SWAP", "4h", W, 100.0, 0, lambda *a: None)
        return d, d["trades"][0]

    def test_target1_then_target2(self):
        d, t = self.open_trade()
        risk = t["risk"]
        cs = [c(0, 100.5, 99.0, 99.5), c(M15, 99.5, t["tp1"] - 0.01, t["tp1"]), c(2 * M15, t["tp1"], t["tp2"] - 0.01, t["tp2"])]
        D.track(d, 3 * M15, lambda *a: cs)
        self.assertEqual(t["why"], "цель 2")
        self.assertAlmostEqual(t["r"], 0.5 * C.TP1_R + 0.5 * C.TP2_R)
        self.assertGreater(risk, 0)

    def test_stop(self):
        d, t = self.open_trade()
        D.track(d, M15, lambda *a: [c(0, 102.5, 99.5, 102.3)])
        self.assertEqual(t["why"], "стоп")
        self.assertAlmostEqual(t["r"], -1.0)

    def test_breakeven_after_target1(self):
        d, t = self.open_trade()
        cs = [c(0, 100.2, t["tp1"] - 0.01, t["tp1"]), c(M15, 100.5, t["tp1"], 100.3)]
        D.track(d, 2 * M15, lambda *a: cs)
        self.assertEqual(t["why"], "безубыток")
        self.assertAlmostEqual(t["r"], 0.5 * C.TP1_R)

    def test_money_with_deposit_and_fees(self):
        d, t = self.open_trade()
        self.assertAlmostEqual(t["risk_usd"], 2.0)                    # 2% от $100
        self.assertAlmostEqual(t["qty"] * t["risk"], 2.0)
        D.track(d, M15, lambda *a: [c(0, 102.5, 99.5, 102.3)])        # стоп
        fees = (t["qty"] * t["entry"] + t["qty"] * t["sl0"]) * C.DUEL_FEE_PCT / 100
        self.assertAlmostEqual(t["usd"], round(-2.0 - fees, 2), places=2)
        self.assertAlmostEqual(d["balance"], round(100 - 2.0 - fees, 2), places=2)
        self.assertEqual(t["balance"], d["balance"])

    def test_next_trade_sized_from_new_balance(self):
        d, t = self.open_trade()
        D.track(d, M15, lambda *a: [c(0, 102.5, 99.5, 102.3)])
        D.on_alarm(d, "Y-USDT-SWAP", "4h", W, 100.0, 2 * M15, lambda *a: None)
        self.assertAlmostEqual(d["trades"][-1]["risk_usd"], round(d["balance"] * 0.02, 2))

    def test_one_trade_per_coin(self):
        d, _ = self.open_trade()
        D.on_alarm(d, "X-USDT-SWAP", "4h", W, 100.0, 1, lambda *a: None)
        self.assertEqual(len(d["trades"]), 1)
        self.assertEqual(d["skipped"][-1]["why"], "уже в сделке по этой монете")

    def test_public_file_only_changes_with_trades(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(D, "DUEL_FILE", os.path.join(tmp, "d.json")), \
             mock.patch.object(D, "PUBLIC_FILE", os.path.join(tmp, "p.json")):
            d, t = self.open_trade()
            self.assertTrue(D.save(d, 0))
            t["checked"] = 12345  # служебное поле — публикации нет
            self.assertFalse(D.save(d, 0))
            t["t1"] = True
            self.assertTrue(D.save(d, 0))


if __name__ == "__main__":
    unittest.main()
