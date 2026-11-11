"""Deterministic market-structure rules for closed OHLC candles."""
from __future__ import annotations

from typing import Iterable


def find_swings(candles: list[dict], left: int = 3, right: int = 3) -> list[dict]:
    """Return confirmed pivots; each pivot has its original index and confirmation index.

    Equal highs/lows are rejected to avoid marking flat plateaus multiple times.
    """
    if left < 1 or right < 1:
        raise ValueError("left and right must be positive")
    swings = []
    for i in range(left, len(candles) - right):
        high = candles[i]["h"]
        low = candles[i]["l"]
        around = candles[i-left:i] + candles[i+1:i+right+1]
        if all(high > c["h"] for c in around):
            swings.append({"kind": "high", "index": i, "confirmed_at": i + right, "price": high})
        if all(low < c["l"] for c in around):
            swings.append({"kind": "low", "index": i, "confirmed_at": i + right, "price": low})
    swings.sort(key=lambda p: (p["confirmed_at"], p["index"], p["kind"]))
    return swings


def classify_swings(swings: Iterable[dict]) -> list[dict]:
    """Add HH/LH or HL/LL labels by comparing consecutive pivots of the same kind."""
    previous = {}
    labeled = []
    for swing in swings:
        item = dict(swing)
        old = previous.get(item["kind"])
        if old is not None:
            if item["kind"] == "high":
                item["label"] = "HH" if item["price"] > old["price"] else "LH"
            else:
                item["label"] = "HL" if item["price"] > old["price"] else "LL"
        else:
            item["label"] = None
        previous[item["kind"]] = item
        labeled.append(item)
    return labeled


def _atr(candles: list[dict], end: int, period: int) -> float:
    start = max(1, end - period + 1)
    ranges = []
    for i in range(start, end + 1):
        prev_close = candles[i - 1]["c"]
        ranges.append(max(candles[i]["h"] - candles[i]["l"],
                          abs(candles[i]["h"] - prev_close),
                          abs(candles[i]["l"] - prev_close)))
    return sum(ranges) / len(ranges) if ranges else 0.0


def find_bearish_confirmations(
    candles: list[dict],
    left: int = 3,
    right: int = 3,
    atr_period: int = 14,
    atr_tolerance: float = 0.20,
    min_tolerance_pct: float = 0.001,
) -> list[dict]:
    """Find completed bearish CHoCH -> LH -> BOS -> retest sequences.

    Every input candle must be closed and ordered oldest to newest. No current
    forming candle may be passed in by the caller.
    """
    swings = find_swings(candles, left, right)
    by_confirmation = {}
    for swing in swings:
        by_confirmation.setdefault(swing["confirmed_at"], []).append(swing)

    highs, lows = [], []
    stage = "trend"
    high_reference = choch_index = None
    interim_low = lh_index = bos_index = None
    bos_level = lh_price = None
    output = []

    for i, candle in enumerate(candles):
        for pivot in by_confirmation.get(i, []):
            (highs if pivot["kind"] == "high" else lows).append(pivot)

        if stage == "trend":
            if len(highs) < 2 or len(lows) < 2:
                continue
            last_high, last_low = highs[-1], lows[-1]
            if not (highs[-1]["price"] > highs[-2]["price"]
                    and lows[-1]["price"] > lows[-2]["price"]
                    and last_low["index"] > last_high["index"]):
                continue
            crossed = i > last_low["index"] and candles[i-1]["c"] >= last_low["price"] and candle["c"] < last_low["price"] if i else False
            if crossed:
                stage = "after_choch"
                high_reference = last_high["price"]
                choch_index = i

        elif stage == "after_choch":
            fresh_lows = [p for p in by_confirmation.get(i, []) if p["kind"] == "low" and p["index"] > choch_index]
            if fresh_lows:
                interim_low = fresh_lows[-1]
            fresh_highs = [p for p in by_confirmation.get(i, []) if p["kind"] == "high" and p["index"] > choch_index]
            if interim_low and fresh_highs:
                lh = next((p for p in reversed(fresh_highs) if p["index"] > interim_low["index"]
                           and p["price"] < high_reference), None)
                if lh:
                    lh_index = lh["index"]
                    lh_price = lh["price"]
                    bos_level = interim_low["price"]
                    stage = "after_lh"

        elif stage == "after_lh":
            if i > lh_index and i > 0 and candles[i-1]["c"] >= bos_level and candle["c"] < bos_level:
                bos_index = i
                stage = "retest"

        elif stage == "retest":
            if i <= bos_index:
                continue
            tolerance = max(bos_level * min_tolerance_pct, _atr(candles, i, atr_period) * atr_tolerance)
            touched = candle["h"] >= bos_level - tolerance and candle["l"] <= bos_level + tolerance
            if touched and candle["c"] < bos_level:
                output.append({
                    "choch_index": choch_index,
                    "lh_index": lh_index,
                    "bos_index": bos_index,
                    "retest_index": i,
                    "bos_level": bos_level,
                    "lh_price": lh_price,
                    "tolerance": tolerance,
                    "swing_labels": ["HH", "HL", "LH", "LL"],
                })
                stage = "trend"
                high_reference = choch_index = interim_low = lh_index = bos_index = bos_level = lh_price = None

    return output
