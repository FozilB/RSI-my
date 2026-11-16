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
    state.setdefault("last_entry", {})
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


def late_reason(candles: list[dict], event: dict) -> str | None:
    """Фильтр «поздно»: цена уже далеко от нижнего максимума или пробой был огромной свечой-обвалом."""
    # «Обычная свеча» — по свечам ДО пробоя, иначе свеча-обвал сама раздувает ATR и прячет опоздание.
    atr = _atr(candles, max(1, event["bos_index"] - 1), C.ATR_PERIOD)
    if atr <= 0:
        return None
    entry = candles[event["retest_index"]]["c"]
    passed = (event["lh_price"] - entry) / atr
    if passed > C.LATE_MAX_ATR:
        return f"от нижнего максимума уже пройдено {passed:.1f} ATR"
    b = candles[event["bos_index"]]
    if (b["h"] - b["l"]) > C.BIG_BOS_CANDLE_ATR * atr:
        return f"пробой свечой-обвалом ({(b['h'] - b['l']) / atr:.1f} ATR)"
    return None


def signal_age_min(timeframe: str, candle_ts: int, now_ms: int) -> float:
    close_ts = candle_ts + C.TIMEFRAMES[timeframe]["milliseconds"]
    return (now_ms - close_ts) / 60000


def timeframe_due(timeframe: str, now_ms: int) -> bool:
    """15м проверяется всегда; 1ч - только в первые DUE_WINDOW_MIN минут после закрытия часовой свечи."""
    if timeframe == C.ENTRY_TF or os.environ.get("SCAN_ALL") == "1":
        return True
    period = C.TIMEFRAMES[timeframe]["milliseconds"]
    return now_ms % period < C.DUE_WINDOW_MIN * 60000


def chart_link(inst_id: str, timeframe: str) -> str:
    base = inst_id.removesuffix("-USDT-SWAP")
    tf = {"15m": "15", "30m": "30", "1h": "60", "4h": "240"}[timeframe]
    return f"https://www.tradingview.com/chart/?symbol=OKX:{base}USDT.P&interval={tf}"


STAGE_TEXT = {
    "after_choch": "рост сломался",
    "after_lh": "рост сломался, нижний максимум {lh}",
    "retest": "нижний максимум {lh}, пробой вниз {bos}",
    "done": "разворот подтверждён, нижний максимум {lh}",
}


def stage_text(watch: dict) -> str:
    return STAGE_TEXT[watch["stage"]].format(lh=plan_price(watch["lh"]) if watch.get("lh") else "—",
                                             bos=plan_price(watch["bos"]) if watch.get("bos") else "—")


def message_for(inst_id: str, timeframe: str, candles: list[dict], event: dict,
                current: float | None = None, now_ms: int | None = None, watch: dict | None = None) -> str:
    index = event["retest_index"]
    candle = candles[index]
    ts = candle["ts"]
    base = inst_id.removesuffix("-USDT-SWAP")
    label = C.TIMEFRAMES[timeframe]["label"]
    entry = candle["c"]
    lines = [f"🔻 Шорт {base} — точка входа ({label})"]
    if watch:
        lines.append(f"1ч: {stage_text(watch)}")
    lines.append(f"{label}: цена вернулась к уровню {plan_price(event['bos_level'])} и пошла вниз")
    if C.TRADE_PLAN and event.get("lh_price"):
        lines += [""] + plan_lines(entry, event["lh_price"])
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
    lines.append(f"График: {chart_link(inst_id, timeframe)}")
    return "\n".join(lines)


def watch_summary(added: list[tuple[str, dict]], removed: list[tuple[str, str]]) -> str | None:
    if not added and not removed:
        return None
    lines = []
    if added:
        lines.append("👀 Наблюдение (1ч) — здесь может появиться шорт:")
        for inst, w in added:
            lines.append(f"• {inst.removesuffix('-USDT-SWAP')} — {stage_text(w)}")
        lines.append("Это ещё не вход. Точку входа пришлю отдельно, когда она сложится на 15м.")
    if removed:
        if lines:
            lines.append("")
        lines.append("❌ Отбой: " + ", ".join(f"{i.removesuffix('-USDT-SWAP')} ({why})" for i, why in removed))
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


H_MS = 60 * 60 * 1000
MAX_WATCH_LIST = 15


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


def scan_entries(state: dict, stats: dict) -> None:
    """15м: ищем точку входа только у монет из наблюдения."""
    tf = C.ENTRY_TF
    for inst_id, watch in list(state["watch"].items()):
        got = closed_candles(inst_id, tf)
        if got is None:
            stats["errors"] += 1
            continue
        candles_all, candles = got
        stats["entry_checks"] += 1
        state_key = f"{inst_id}|{tf}"
        previous_ts = state["processed"].get(state_key)
        for event in find_bearish_confirmations(candles, **structure_kwargs()):
            signal_ts = candles[event["retest_index"]]["ts"]
            is_new = signal_ts > previous_ts if previous_ts is not None else event["retest_index"] == len(candles) - 1
            key = signal_key(inst_id, tf, signal_ts)
            if not is_new or key in state["sent"]:
                continue
            now_ms = int(time.time() * 1000)
            age = signal_age_min(tf, signal_ts, now_ms)
            if age > C.MAX_SIGNAL_AGE_MIN:
                log(f"Skip stale {key}: {age:.0f} min after candle close")
                stats["stale"] += 1
                continue
            ceiling = watch.get("ceiling")
            if ceiling and event["lh_price"] > ceiling * (1 + C.SL_BUFFER_PCT / 100):
                log(f"Skip {key}: 15m lower high is above the 1h level")
                continue
            last = state["last_entry"].get(inst_id)
            if last and now_ms - last < C.ENTRY_COOLDOWN_MIN * 60000:
                log(f"Skip {key}: entry signal for this coin was sent recently")
                continue
            late = late_reason(candles, event)
            if late:
                log(f"Skip late {key}: {late}")
                stats["late"] += 1
                continue
            current = candles_all[-1]["c"]
            entry = candles[event["retest_index"]]["c"]
            status = entry_status(entry, event["lh_price"], current)
            if C.SKIP_IF_CHASE and status != "ok":
                log(f"Skip {key}: price {current} already {status}")
                stats["chased"] += 1
                continue
            state["pending"].setdefault(key, {
                "message": message_for(inst_id, tf, candles, event, current, now_ms, watch),
                "created_at": now_ms,
                "close_ts": signal_ts + C.TIMEFRAMES[tf]["milliseconds"],
            })
            state["last_entry"][inst_id] = now_ms
            stats["signals"] += 1
        state["processed"][state_key] = candles[-1]["ts"]
    log(f"{tf}: checked {len(state['watch'])} watched instruments")


def scan_watch(state: dict, stats: dict) -> None:
    """1ч: раз в час по всем монетам — где начался разворот вниз. Новые попадают в наблюдение."""
    tf = C.WATCH_TF
    instruments = fetch_instruments()
    log(f"Found {len(instruments)} USDT perpetual instruments on OKX")
    now_ms = int(time.time() * 1000)
    added, removed = [], []
    for inst_id in instruments:
        got = closed_candles(inst_id, tf)
        if got is None:
            stats["errors"] += 1
            continue
        _, candles = got
        stats["instruments"] += 1
        st = bearish_state(candles, **structure_kwargs())
        old = state["watch"].get(inst_id)
        stage, lh = st["stage"], st["lh_price"]
        ceiling = st["lh_price"] or st["high_reference"]
        active = stage in C.WATCH_STAGES
        last_event = st["last_event_index"]
        if not active and last_event is not None and len(candles) - 1 - last_event < C.WATCH_KEEP_AFTER_SIGNAL_H:
            stage, lh, ceiling, active = "done", st["last_event_lh"], st["last_event_lh"], True
        if active:
            state["watch"][inst_id] = {"stage": stage, "lh": lh, "bos": st["bos_level"], "ceiling": ceiling,
                                       "since": old["since"] if old else now_ms, "seen": now_ms}
            if old is None:
                added.append((inst_id, state["watch"][inst_id]))
        elif old is not None:
            del state["watch"][inst_id]
            up = old.get("ceiling") and candles[-1]["c"] > old["ceiling"]
            removed.append((inst_id, "цена выше максимума" if up else "разворот не подтвердился"))
    for inst_id, w in list(state["watch"].items()):
        if now_ms - w["seen"] > 2 * H_MS or now_ms - w["since"] > C.WATCH_MAX_HOURS * H_MS:
            del state["watch"][inst_id]
            removed.append((inst_id, "устарело"))
    stats["watch_added"], stats["watch_removed"] = len(added), len(removed)
    extra = len(added) - MAX_WATCH_LIST
    text = watch_summary(added[:MAX_WATCH_LIST], removed)
    if text and extra > 0:
        text = text.replace("\nЭто ещё не вход", f"\n…и ещё {extra}\nЭто ещё не вход", 1)
    if text:
        state["pending"][f"watch|{now_ms}"] = {"message": text, "created_at": now_ms, "close_ts": now_ms}
    log(f"{tf}: scanned {len(instruments)} instruments, watching {len(state['watch'])}")


def scan(state: dict) -> dict:
    stats = {"instruments": 0, "entry_checks": 0, "signals": 0, "errors": 0, "stale": 0, "chased": 0,
             "late": 0, "watch_added": 0, "watch_removed": 0, "sent": 0}
    # Сначала вход (быстро и срочно) — и сразу отправить, не дожидаясь проверки всех монет на 1ч.
    scan_entries(state, stats)
    stats["sent"] += flush_pending(state)
    if timeframe_due(C.WATCH_TF, int(time.time() * 1000)):
        scan_watch(state, stats)
    else:
        log(f"{C.WATCH_TF}: skipped (its candle did not just close)")
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
        ok = telegram_send("✅ Сканер на связи. Схема: 👀 предупреждение на 1ч → 🔻 точка входа на 15м.")
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
    log(f"Summary: 1h checks={stats['instruments']}, 15m checks={stats['entry_checks']}, "
        f"watching={len(state['watch'])} (+{stats['watch_added']} / -{stats['watch_removed']}), "
        f"errors={stats['errors']}, entry signals={stats['signals']}, skipped stale={stats['stale']}, "
        f"skipped late={stats['late']}, skipped chased={stats['chased']}, "
        f"notifications sent={stats['sent']}, pending={len(state['pending'])}, elapsed={elapsed:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
