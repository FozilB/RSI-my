"""Hourly OKX market-structure scanner. Public-data only; no trading endpoints."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path

import requests

import config as C
from structure import _atr, bearish_state, find_bearish_confirmations

OKX = "https://www.okx.com"
TASHKENT = dt.timezone(dt.timedelta(hours=5))
SESSION = requests.Session()


def log(message: str) -> None:
    print(message, flush=True)


def api_get(path: str, params: dict) -> list | None:
    for attempt in range(C.MAX_RETRIES):
        try:
            response = SESSION.get(OKX + path, params=params, timeout=C.REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") == "0":
                return payload.get("data", [])
            log(f"OKX API error code={payload.get('code')}: {payload.get('msg')}")
        except (requests.RequestException, ValueError) as exc:
            log(f"OKX request failed ({attempt + 1}/{C.MAX_RETRIES}): {exc}")
        if attempt + 1 < C.MAX_RETRIES:
            time.sleep(min(2 ** attempt, 8))
    return None


def fetch_instruments() -> list[str]:
    rows = api_get("/api/v5/market/tickers", {"instType": "SWAP"})
    if rows is None:
        raise RuntimeError("Could not load OKX swap instruments")
    return sorted(row["instId"] for row in rows
                  if row.get("instId", "").endswith("-USDT-SWAP"))


def fetch_candles(inst_id: str, bar: str) -> list[dict] | None:
    rows = api_get("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(C.CANDLE_LIMIT)})
    time.sleep(C.REQUEST_PAUSE_SECONDS)
    if rows is None:
        return None
    candles = []
    for row in reversed(rows):
        try:
            candles.append({
                "ts": int(row[0]), "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]), "v": float(row[5]),
                "closed": row[8] == "1",
            })
        except (IndexError, TypeError, ValueError):
            continue
    return candles


def load_state() -> dict:
    try:
        state = json.loads(Path(C.STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("processed", {})
    state.setdefault("sent", {})
    state.setdefault("pending", {})
    state.setdefault("watch", {})
    state.pop("last_entry", None)
    return state


def save_state(state: dict) -> None:
    path = Path(C.STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def signal_key(inst_id: str, timeframe: str, candle_ts: int) -> str:
    return f"{inst_id}|{timeframe}|{candle_ts}"


def format_time(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, TASHKENT).strftime("%Y-%m-%d %H:%M")


def format_price(value: float) -> str:
    return f"{value:.10f}".rstrip("0").rstrip(".")


def plan_price(value: float) -> str:
    """Цена уровня плана: 5 значащих цифр (191.59, 0.0012346), без хвоста из лишних знаков."""
    if value <= 0:
        return format_price(value)
    import math
    digits = max(0, 4 - int(math.floor(math.log10(value))))
    return f"{value:.{digits}f}"


def trade_levels(entry: float, stop_above: float) -> dict | None:
    """Уровни шорта: стоп чуть выше LH, тейки кратны риску. None - вход не ниже стопа."""
    sl = stop_above * (1 + C.SL_BUFFER_PCT / 100)
    risk = sl - entry
    if risk <= 0:
        return None
    return {"entry": entry, "sl": sl, "risk": risk, "risk_pct": risk / entry * 100,
            "tp1": entry - risk * C.TP1_R, "tp2": entry - risk * C.TP2_R}


def pct_from(x: float, base: float) -> str:
    return f"{(x / base - 1) * 100:+.1f}%".replace("-", "−")


def plan_lines(entry: float, stop_above: float) -> list[str]:
    """План шорта простыми словами. Только подсказка: бот сам ордера не ставит."""
    t = trade_levels(entry, stop_above)
    if t is None:
        return []
    size = C.RISK_USDT / (t["risk_pct"] / 100)
    lines = [
        f"Вход: {plan_price(t['entry'])}",
        f"Стоп: {plan_price(t['sl'])} ({pct_from(t['sl'], entry)}) — над нижним максимумом",
        f"Цель 1: {plan_price(t['tp1'])} ({pct_from(t['tp1'], entry)}) — закрыть половину, стоп в безубыток",
        f"Цель 2: {plan_price(t['tp2'])} ({pct_from(t['tp2'], entry)}) — закрыть остаток",
        f"Объём: ~{size:,.0f} USDT (если сработает стоп — минус {C.RISK_USDT:g} USDT)",
    ]
    if t["risk_pct"] > C.MAX_RISK_PCT:
        lines.append(f"⚠️ Стоп широкий ({t['risk_pct']:.1f}%) — лучше пропустить.")
    return lines


def trade_plan_text(entry: float, stop_above: float) -> str:
    lines = plan_lines(entry, stop_above)
    return ("\n" + "\n".join(lines) + "\n") if lines else ""


def entry_status(entry: float, stop_above: float, current: float) -> str:
    """'ok' - цена в зоне входа; 'chased' - уже ушла вниз дальше MAX_CHASE_R; 'stopped' - выше стопа."""
    t = trade_levels(entry, stop_above)
    if t is None:
        return "ok"
    if current >= t["sl"]:
        return "stopped"
    if current < t["entry"] - t["risk"] * C.MAX_CHASE_R:
        return "chased"
    return "ok"


def crash_warning(candles: list[dict], bos_index: int) -> str | None:
    """Пробой огромной свечой-обвалом: «ретест» после неё — часто уже отскок, а не начало падения."""
    atr = _atr(candles, max(1, bos_index - 1), C.ATR_PERIOD)  # «обычная свеча» — по свечам до пробоя
    b = candles[bos_index]
    if atr > 0 and (b["h"] - b["l"]) > C.BIG_BOS_CANDLE_ATR * atr:
        return f"⚠️ Пробой был свечой-обвалом (в {(b['h'] - b['l']) / atr:.1f} раза больше обычной) — возможно, движение уже прошло."
    return None


def signal_age_min(timeframe: str, candle_ts: int, now_ms: int) -> float:
    close_ts = candle_ts + C.TIMEFRAMES[timeframe]["milliseconds"]
    return (now_ms - close_ts) / 60000


def timeframe_due(timeframe: str, now_ms: int) -> bool:
    """15м проверяется всегда; 1ч/4ч - только в первые DUE_WINDOW_MIN минут после закрытия их свечи."""
    if timeframe == C.ALARM_TF or os.environ.get("SCAN_ALL") == "1":
        return True
    period = C.TIMEFRAMES[timeframe]["milliseconds"]
    return now_ms % period < C.DUE_WINDOW_MIN * 60000


def chart_link(inst_id: str, timeframe: str) -> str:
    base = inst_id.removesuffix("-USDT-SWAP")
    tf = {"15m": "15", "30m": "30", "1h": "60", "4h": "240"}[timeframe]
    return f"https://www.tradingview.com/chart/?symbol=OKX:{base}USDT.P&interval={tf}"


def coin(inst_id: str) -> str:
    return inst_id.removesuffix("-USDT-SWAP")


def message_for(inst_id: str, timeframe: str, candles: list[dict], event: dict,
                current: float | None = None, now_ms: int | None = None) -> str:
    """✅ Свеча ретеста на 1ч/4ч закрылась ниже уровня — подтверждение с планом."""
    index = event["retest_index"]
    candle = candles[index]
    ts = candle["ts"]
    label = C.TIMEFRAMES[timeframe]["label"]
    entry = candle["c"]
    lines = [f"✅ Шорт {coin(inst_id)} ({label}) — ретест подтверждён",
             f"Свеча {label} закрылась ниже уровня {plan_price(event['bos_level'])}: отскок вниз от уровня состоялся."]
    if C.TRADE_PLAN and event.get("lh_price"):
        lines += [""] + plan_lines(entry, event["lh_price"])
    warn = crash_warning(candles, event["bos_index"]) if event.get("bos_index") is not None else None
    if warn:
        lines.append(warn)
    lines.append("")
    time_line = f"⏱ Свеча закрылась {format_time(ts + C.TIMEFRAMES[timeframe]['milliseconds'])}"
    if current is not None:
        if now_ms is not None:
            time_line += f" · сигналу {signal_age_min(timeframe, ts, now_ms):.0f} мин"
        time_line += f" · цена сейчас {format_price(current)} ({pct_from(current, entry)})"
    lines.append(time_line)
    t = trade_levels(entry, event["lh_price"]) if event.get("lh_price") else None
    if t:
        lines.append(f"⛔ Не входить, если цена уже ниже {plan_price(t['entry'] - t['risk'] * C.MAX_CHASE_R)} "
                     f"или выше {plan_price(t['sl'])}")
    lines.append(f"График {label}: {chart_link(inst_id, timeframe)}")
    return "\n".join(lines)


def watch_line(key: str, w: dict) -> str:
    inst_id, tf = key.split("|")
    sl = w["lh"] * (1 + C.SL_BUFFER_PCT / 100)
    line = (f"• {coin(inst_id)} ({C.TIMEFRAMES[tf]['label']}): уровень {plan_price(w['level'])}, "
            f"стоп над {plan_price(w['lh'])} ({pct_from(sl, w['level'])})")
    if w.get("warn"):
        line += " ⚠️ обвал"
    return line


def watch_summary(added: list[tuple[str, dict]], removed: list[tuple[str, str]]) -> str | None:
    if not added and not removed:
        return None
    lines = []
    if added:
        lines.append("👀 Пробой вниз — жду возврат к уровню (ретест):")
        lines += [watch_line(k, w) for k, w in added[:C.MAX_WATCH_LIST]]
        if len(added) > C.MAX_WATCH_LIST:
            lines.append(f"…и ещё {len(added) - C.MAX_WATCH_LIST}")
        lines.append("Это ещё не вход. Когда цена вернётся к уровню, пришлю ⚡ — не дожидаясь закрытия свечи.")
        if any(w.get("warn") for _, w in added):
            lines.append("⚠️ обвал — пробой был огромной свечой, движение могло уже пройти.")
    if removed:
        if lines:
            lines.append("")
        lines.append("❌ Отбой: " + ", ".join(f"{coin(k.split('|')[0])} ({C.TIMEFRAMES[k.split('|')[1]]['label']}, {why})"
                                             for k, why in removed))
    return "\n".join(lines)


def alarm_message(key: str, w: dict, current: float, now_ms: int) -> str:
    """⚡ Цена вернулась к уровню, пока свеча 1ч/4ч ещё не закрылась."""
    inst_id, tf = key.split("|")
    label = C.TIMEFRAMES[tf]["label"]
    period = C.TIMEFRAMES[tf]["milliseconds"]
    closes_at = (now_ms // period + 1) * period
    lines = [f"⚡ {coin(inst_id)} ({label}) — цена у уровня {plan_price(w['level'])}, ретест идёт сейчас",
             f"Цена сейчас {format_price(current)}. Свеча {label} закроется в {format_time(closes_at)} (Ташкент).",
             "", f"Если решите входить (смотрите {label}):"]
    lines += plan_lines(w["level"], w["lh"])
    if w.get("warn"):
        lines.append(w["warn"])
    sl = w["lh"] * (1 + C.SL_BUFFER_PCT / 100)
    lines += ["", f"⛔ Не входить, если цена закрылась выше {plan_price(sl)} — разворот отменён.",
              f"Решение за вами. Если свеча {label} закроется ниже уровня — пришлю ✅.",
              f"График {label}: {chart_link(inst_id, tf)}"]
    return "\n".join(lines)


def telegram_send(text: str) -> bool:
    if os.environ.get("DRY_RUN") == "1":
        log("[DRY RUN] " + text.replace("\n", " | "))
        return True
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("Telegram secrets are not configured; notification stays queued")
        return False
    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        if response.status_code == 200 and response.json().get("ok"):
            return True
        log(f"Telegram returned HTTP {response.status_code}: {response.text[:250]}")
    except (requests.RequestException, ValueError) as exc:
        log(f"Telegram request failed: {exc}")
    return False


def structure_kwargs() -> dict:
    return dict(left=C.PIVOT_LEFT, right=C.PIVOT_RIGHT, atr_period=C.ATR_PERIOD,
                atr_tolerance=C.RETEST_ATR_TOLERANCE, min_tolerance_pct=C.MIN_RETEST_TOLERANCE_PCT,
                setup_max_bars=C.SETUP_MAX_BARS, retest_max_bars=C.RETEST_MAX_BARS)


def closed_candles(inst_id: str, timeframe: str) -> tuple[list[dict], list[dict]] | None:
    candles_all = fetch_candles(inst_id, C.TIMEFRAMES[timeframe]["okx"])
    if not candles_all:
        return None
    candles = [c for c in candles_all if c["closed"]]
    return (candles_all, candles) if candles else None


def queue(state: dict, key: str, text: str, close_ts: int) -> None:
    now_ms = int(time.time() * 1000)
    state["pending"].setdefault(key, {"message": text, "created_at": now_ms, "close_ts": close_ts})


def check_alarms(state: dict, stats: dict) -> None:
    """⚡ Каждые 15 минут: у сетапов, ждущих ретест, смотрим 15м — дошла ли цена до уровня."""
    for key, w in list(state["watch"].items()):
        if w.get("alarmed"):
            continue
        inst_id, _ = key.split("|")
        got = closed_candles(inst_id, C.ALARM_TF)
        if got is None:
            stats["errors"] += 1
            continue
        candles_all, _ = got
        stats["alarm_checks"] += 1
        fresh = [c for c in candles_all if c["ts"] + C.TIMEFRAMES[C.ALARM_TF]["milliseconds"] > w["since"]][-2:]
        touched = any(c["h"] >= w["level"] - w["tol"] for c in fresh)
        current = candles_all[-1]["c"]
        if not touched:
            continue
        if C.SKIP_IF_CHASE and entry_status(w["level"], w["lh"], current) != "ok":
            log(f"Skip alarm {key}: price {current} outside entry zone")
            continue
        now_ms = int(time.time() * 1000)
        w["alarmed"] = True
        queue(state, f"alarm|{key}|{w['since']}", alarm_message(key, w, current, now_ms), now_ms)
        stats["alarms"] += 1
    stats["sent"] += flush_pending(state)


def scan_setups(state: dict, stats: dict, timeframe: str, instruments: list[str]) -> None:
    """1ч/4ч: 👀 пробой, ждём ретест · ✅ ретест подтверждён · ❌ отбой."""
    now_ms = int(time.time() * 1000)
    added, removed = [], []
    for inst_id in instruments:
        got = closed_candles(inst_id, timeframe)
        if got is None:
            stats["errors"] += 1
            continue
        candles_all, candles = got
        stats["instruments"] += 1
        wkey = f"{inst_id}|{timeframe}"
        state_key = wkey
        previous_ts = state["processed"].get(state_key)
        events = find_bearish_confirmations(candles, **structure_kwargs())
        confirmed = False
        for event in events:
            signal_ts = candles[event["retest_index"]]["ts"]
            is_new = signal_ts > previous_ts if previous_ts is not None else event["retest_index"] == len(candles) - 1
            key = signal_key(inst_id, timeframe, signal_ts)
            if not is_new or key in state["sent"]:
                continue
            confirmed = True
            age = signal_age_min(timeframe, signal_ts, now_ms)
            if age > C.MAX_SIGNAL_AGE_MIN:
                log(f"Skip stale {key}: {age:.0f} min after candle close")
                stats["stale"] += 1
                continue
            current = candles_all[-1]["c"]
            entry = candles[event["retest_index"]]["c"]
            if C.SKIP_IF_CHASE and entry_status(entry, event["lh_price"], current) != "ok":
                log(f"Skip {key}: price {current} already outside entry zone")
                stats["chased"] += 1
                continue
            queue(state, key, message_for(inst_id, timeframe, candles, event, current, now_ms),
                  signal_ts + C.TIMEFRAMES[timeframe]["milliseconds"])
            stats["signals"] += 1
        state["processed"][state_key] = candles[-1]["ts"]

        st = bearish_state(candles, **structure_kwargs())
        old = state["watch"].get(wkey)
        if st["stage"] == "retest":
            if old is None or old.get("bos_ts") != candles[st["bos_index"]]["ts"]:
                i = len(candles) - 1
                tol = max(st["bos_level"] * C.MIN_RETEST_TOLERANCE_PCT, _atr(candles, i, C.ATR_PERIOD) * C.RETEST_ATR_TOLERANCE)
                w = {"level": st["bos_level"], "lh": st["lh_price"], "tol": tol,
                     "bos_ts": candles[st["bos_index"]]["ts"], "since": now_ms, "alarmed": False,
                     "warn": crash_warning(candles, st["bos_index"])}
                state["watch"][wkey] = w
                added.append((wkey, w))
        elif old is not None:
            del state["watch"][wkey]
            if not confirmed:
                above = candles[-1]["c"] > old["lh"]
                removed.append((wkey, "цена выше нижнего максимума" if above else "ретеста не дождались"))
    stats["watch_added"] += len(added)
    stats["watch_removed"] += len(removed)
    text = watch_summary(added, removed)
    if text:
        queue(state, f"watch|{timeframe}|{now_ms}", text, now_ms)
    stats["sent"] += flush_pending(state)
    log(f"{timeframe}: scanned {len(instruments)} instruments, waiting for retest: "
        f"{sum(1 for k in state['watch'] if k.endswith('|' + timeframe))}")


def scan(state: dict) -> dict:
    stats = {"instruments": 0, "alarm_checks": 0, "alarms": 0, "signals": 0, "errors": 0, "stale": 0,
             "chased": 0, "watch_added": 0, "watch_removed": 0, "sent": 0}
    # Сначала будильник ⚡ (быстро и срочно), потом полный проход 1ч/4ч, если их свеча только что закрылась.
    check_alarms(state, stats)
    due = [tf for tf in C.SETUP_TIMEFRAMES if timeframe_due(tf, int(time.time() * 1000))]
    for tf in C.SETUP_TIMEFRAMES:
        if tf not in due:
            log(f"{tf}: skipped (its candle did not just close)")
    if due:
        instruments = fetch_instruments()
        log(f"Found {len(instruments)} USDT perpetual instruments on OKX")
        for tf in due:
            scan_setups(state, stats, tf, instruments)
    return stats


def flush_pending(state: dict) -> int:
    sent_count = 0
    now_ms = int(time.time() * 1000)
    for key, item in list(state["pending"].items()):
        # Не досылать из очереди то, что уже устарело (например, после сбоя Telegram)
        if now_ms - item.get("close_ts", item.get("created_at", 0)) > C.MAX_SIGNAL_AGE_MIN * 60000:
            log(f"Drop stale queued {key}")
            del state["pending"][key]
            continue
        if telegram_send(item["message"]):
            state["sent"][key] = int(time.time() * 1000)
            del state["pending"][key]
            sent_count += 1
    return sent_count


def prune_state(state: dict) -> None:
    cutoff = int(time.time() * 1000) - C.STATE_RETENTION_DAYS * 24 * 60 * 60 * 1000
    state["sent"] = {key: ts for key, ts in state["sent"].items() if ts >= cutoff}
    state["pending"] = {
        key: item for key, item in state["pending"].items()
        if item.get("created_at", 0) >= cutoff
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan OKX market structure")
    parser.add_argument("--dry-run", action="store_true", help="Print notifications without sending them")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"
        os.environ["SCAN_ALL"] = "1"
    if os.environ.get("TEST_TELEGRAM") == "1":
        ok = telegram_send("✅ Сканер на связи. 👀 пробой на 1ч/4ч → ⚡ цена у уровня (15м) → ✅ ретест подтверждён.")
        return 0 if ok else 1
    state = load_state()
    started = time.monotonic()
    try:
        stats = scan(state)
        stats["sent"] += flush_pending(state)
        prune_state(state)
    finally:
        save_state(state)
    elapsed = (time.monotonic() - started) / 60
    log(f"Summary: setup checks={stats['instruments']}, 15m alarm checks={stats['alarm_checks']}, "
        f"waiting for retest={len(state['watch'])} (+{stats['watch_added']} / -{stats['watch_removed']}), "
        f"alarms={stats['alarms']}, confirmations={stats['signals']}, errors={stats['errors']}, "
        f"skipped stale={stats['stale']}, skipped chased={stats['chased']}, "
        f"notifications sent={stats['sent']}, pending={len(state['pending'])}, elapsed={elapsed:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
