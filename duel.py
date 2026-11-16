"""Бот-конкурент: виртуально торгует сигналы ⚡ по своей логике. Реальных ордеров нет.

Правила бота:
  • входит по рынку в момент ⚡ (цена вернулась к уровню ретеста на 1ч/4ч);
  • пропускает: пробой свечой-обвалом, широкий стоп, сетап 1ч при росте на 4ч (цена выше SMA50 4ч);
  • стоп над нижним максимумом, на цели 1 закрывает половину и переносит стоп в безубыток, остаток — на цели 2;
  • если за DUEL_MAX_HOURS сделка не закрылась — закрывает по рынку.
Итоги пишутся в state/duel.json; workflow публикует его в ветку duel-data, оттуда табло берёт бот-помощник.
"""
from __future__ import annotations

import json
from pathlib import Path

import config as C

DUEL_FILE = "state/duel.json"          # полное состояние (кэш Actions)
PUBLIC_FILE = "state/duel_public.json"  # то, что публикуется для табло: без служебных полей
M15 = 15 * 60 * 1000
H = 60 * 60 * 1000
KEEP_DAYS = 14


def load() -> dict:
    try:
        d = json.loads(Path(DUEL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.setdefault("trades", [])
    d.setdefault("signals", [])
    d.setdefault("skipped", [])
    return d


def save(d: dict, now_ms: int) -> bool:
    """Сохраняет состояние; публичный файл переписывает, только если сделки/сигналы изменились."""
    cutoff = now_ms - KEEP_DAYS * 24 * H
    d["trades"] = [t for t in d["trades"] if t.get("closed") is None or t["closed"] >= cutoff]
    d["signals"] = [s for s in d["signals"] if s["ts"] >= cutoff]
    d["skipped"] = [s for s in d["skipped"] if s["ts"] >= cutoff]
    path = Path(DUEL_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    public = dict(d, trades=[{k: v for k, v in t.items() if k != "checked"} for t in d["trades"]])
    text = json.dumps(public, ensure_ascii=False, sort_keys=True)
    pub = Path(PUBLIC_FILE)
    try:
        if pub.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    pub.write_text(text, encoding="utf-8")
    return True


def add_signal(d: dict, inst_id: str, tf: str, kind: str, ts: int) -> None:
    d["signals"].append({"inst": inst_id, "tf": tf, "kind": kind, "ts": ts})


def _sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def decide(inst_id: str, tf: str, w: dict, current: float, fetch_candles) -> str | None:
    """None — бот входит; иначе причина пропуска."""
    if w.get("warn"):
        return "пробой свечой-обвалом"
    sl = w["lh"] * (1 + C.SL_BUFFER_PCT / 100)
    if current >= sl:
        return "цена выше стопа"
    if (sl - current) / current * 100 > C.MAX_RISK_PCT:
        return "широкий стоп"
    if tf != "4h":
        cs = fetch_candles(inst_id, C.TIMEFRAMES["4h"]["okx"]) or []
        closes = [c["c"] for c in cs if c["closed"]]
        sma = _sma(closes, 50)
        if sma is not None and closes[-1] > sma:
            return "на 4ч рост (цена выше средней)"
    return None


def on_alarm(d: dict, inst_id: str, tf: str, w: dict, current: float, now_ms: int, fetch_candles) -> None:
    add_signal(d, inst_id, tf, "alarm", now_ms)
    if any(t["inst"] == inst_id and t.get("closed") is None for t in d["trades"]):
        d["skipped"].append({"inst": inst_id, "tf": tf, "ts": now_ms, "why": "уже в сделке по этой монете"})
        return
    why = decide(inst_id, tf, w, current, fetch_candles)
    if why:
        d["skipped"].append({"inst": inst_id, "tf": tf, "ts": now_ms, "why": why})
        return
    sl = w["lh"] * (1 + C.SL_BUFFER_PCT / 100)
    risk = sl - current
    d["trades"].append({"inst": inst_id, "tf": tf, "opened": now_ms, "checked": now_ms, "entry": current,
                        "sl0": sl, "stop": sl, "risk": risk, "tp1": current - risk * C.TP1_R,
                        "tp2": current - risk * C.TP2_R, "t1": False, "closed": None, "r": None, "why": None})


def _close(t: dict, price: float, now_ms: int, why: str) -> None:
    rest = (t["entry"] - price) / t["risk"]
    t["r"] = round(0.5 * C.TP1_R + 0.5 * rest if t["t1"] else rest, 3)
    t["exit"], t["closed"], t["why"] = price, now_ms, why


def track(d: dict, now_ms: int, fetch_candles) -> None:
    """Ведёт открытые виртуальные сделки по свечам 15м с прошлой проверки (стоп проверяется первым — осторожно)."""
    for t in d["trades"]:
        if t.get("closed") is not None:
            continue
        cs = fetch_candles(t["inst"], C.TIMEFRAMES["15m"]["okx"])
        if not cs:
            continue
        for c in cs:
            if c["ts"] + M15 <= t["checked"]:
                continue
            if c["h"] >= t["stop"]:
                _close(t, t["stop"], now_ms, "безубыток" if t["t1"] else "стоп")
                break
            if not t["t1"] and c["l"] <= t["tp1"]:
                t["t1"], t["stop"] = True, t["entry"]
            if t["t1"] and c["l"] <= t["tp2"]:
                _close(t, t["tp2"], now_ms, "цель 2")
                break
        if t.get("closed") is None:
            t["checked"] = now_ms
            if now_ms - t["opened"] > C.DUEL_MAX_HOURS * H:
                _close(t, cs[-1]["c"], now_ms, f"{C.DUEL_MAX_HOURS} ч — закрыто по рынку")
