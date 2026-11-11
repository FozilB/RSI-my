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
from structure import find_bearish_confirmations

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


def message_for(inst_id: str, timeframe: str, candles: list[dict], event: dict) -> str:
    index = event["retest_index"]
    candle = candles[index]
    ts = candle["ts"]
    base = inst_id.removesuffix("-USDT-SWAP")
    chart_tf = {"30m": "30", "1h": "60", "4h": "240"}[timeframe]
    chart = f"https://www.tradingview.com/chart/?symbol=OKX:{base}USDT.P&interval={chart_tf}"
    return (
        f"🔻 Market Structure Scanner · {base}/USDT\n\n"
        f"Таймфрейм: {timeframe}\n"
        f"Подтверждение: CHoCH → LH → BOS → ретест\n"
        f"Уровень BOS: {format_price(event['bos_level'])}\n"
        f"Ретест закрытой свечи: {format_time(ts + C.TIMEFRAMES[timeframe]["milliseconds"])} (Ташкент)\n"
        f"Закрытие: {format_price(candle['c'])}\n\n"
        f"График: {chart}"
    )


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


def scan(state: dict) -> dict:
    stats = {"instruments": 0, "candles": 0, "signals": 0, "errors": 0}
    instruments = fetch_instruments()
    log(f"Found {len(instruments)} USDT perpetual instruments on OKX")
    for timeframe, settings in C.TIMEFRAMES.items():
        for inst_id in instruments:
            state_key = f"{inst_id}|{timeframe}"
            candles_all = fetch_candles(inst_id, settings["okx"])
            if candles_all is None:
                stats["errors"] += 1
                continue
            candles = [c for c in candles_all if c["closed"]]
            if not candles:
                stats["errors"] += 1
                continue
            stats["instruments"] += 1
            previous_ts = state["processed"].get(state_key)
            events = find_bearish_confirmations(
                candles,
                left=C.PIVOT_LEFT,
                right=C.PIVOT_RIGHT,
                atr_period=C.ATR_PERIOD,
                atr_tolerance=C.RETEST_ATR_TOLERANCE,
                min_tolerance_pct=C.MIN_RETEST_TOLERANCE_PCT,
            )
            for event in events:
                signal_ts = candles[event["retest_index"]]["ts"]
                is_new = signal_ts > previous_ts if previous_ts is not None else event["retest_index"] == len(candles) - 1
                key = signal_key(inst_id, timeframe, signal_ts)
                if is_new and key not in state["sent"]:
                    state["pending"].setdefault(key, {
                        "message": message_for(inst_id, timeframe, candles, event),
                        "created_at": int(time.time() * 1000),
                    })
                    stats["signals"] += 1
            state["processed"][state_key] = candles[-1]["ts"]
        log(f"{timeframe}: scanned {len(instruments)} instruments")
    return stats


def flush_pending(state: dict) -> int:
    sent_count = 0
    for key, item in list(state["pending"].items()):
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
    if os.environ.get("TEST_TELEGRAM") == "1":
        ok = telegram_send("✅ Market Structure Scanner: Telegram connection works.")
        return 0 if ok else 1
    state = load_state()
    started = time.monotonic()
    try:
        stats = scan(state)
        stats["sent"] = flush_pending(state)
        prune_state(state)
    finally:
        save_state(state)
    elapsed = (time.monotonic() - started) / 60
    log(f"Summary: instrument/timeframe checks={stats['instruments']}, "
        f"errors={stats['errors']}, new confirmations={stats['signals']}, "
        f"notifications sent={stats.get('sent', 0)}, pending={len(state['pending'])}, "
        f"elapsed={elapsed:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
