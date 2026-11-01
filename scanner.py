"""Сканер OKX: «RSI кандидат» -> сообщение в Telegram.

Условие (то же, что в индикаторе TradingView «RSI Candidate»):
  1) RSI(14) на закрытии свечи вышел выше RSI_UPPER (75) - монета «взведена»;
  2) потом на закрытии свечи RSI опустился до RSI_BACK (71.5) или ниже - это кандидат.

Бот каждый раз заново прогоняет это условие по последним 300 закрытым свечам, поэтому
пропущенный запуск ничего не ломает. Одна и та же свеча не отправляется дважды.
За один запуск проверяется только свеча, которая закрылась и ещё не проверялась.
"""
import datetime as dt
import json
import os
import sys
import time

import requests

import config as C

OKX = "https://www.okx.com"
TASHKENT = dt.timezone(dt.timedelta(hours=5))
TF_MS = {"15m": 900_000, "30m": 1_800_000, "1H": 3_600_000, "2H": 7_200_000, "4H": 14_400_000}
TF_RU = {"15m": "15м", "30m": "30м", "1H": "1ч", "2H": "2ч", "4H": "4ч"}
TV_INTERVAL = {"15m": "15", "30m": "30", "1H": "60", "2H": "120", "4H": "240"}
REFERENCE = "BTC-USDT-SWAP"
STATE_FILE = os.environ.get("STATE_FILE", "state/state.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
PAUSE = float(os.environ.get("REQUEST_PAUSE", C.REQUEST_PAUSE))
BUDGET_S = float(os.environ.get("RUN_BUDGET_MIN", "20")) * 60  # дольше этого новые монеты не начинаем
MAX_CONSEC_FAIL = 5     # столько ошибок подряд - биржа недоступна, поиск прерываем
CANDLES = 300           # свечей на запрос (максимум OKX); нужно для точного RSI
WARMUP = 50             # первые свечи RSI не учитываем: он ещё «разогревается»
DAY_MS = 86_400_000
T0 = time.time()


def log(msg):
    print(msg, flush=True)


def now_ms():
    return int(time.time() * 1000)


def fmt_time(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, TASHKENT).strftime("%d.%m %H:%M")


def fmt_hm(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, TASHKENT).strftime("%H:%M")


def price(x):
    s = f"{x:.2f}" if x >= 1000 else f"{x:.4f}" if x >= 1 else f"{x:.8f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def num(x):
    """75.0 -> '75', 71.5 -> '71.5'"""
    return f"{x:g}"


def mean(xs):
    return sum(xs) / len(xs)


# ---------------------------------------------------------------- RSI

def _rsi_value(gain, loss):
    if loss == 0:
        return 50.0 if gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def rsi_series(closes, n):
    """RSI по Уайлдеру (как ta.rsi в TradingView) для каждой свечи. None, пока свечей мало."""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain, loss = gain / n, loss / n
    out[n] = _rsi_value(gain, loss)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = (gain * (n - 1) + max(d, 0.0)) / n
        loss = (loss * (n - 1) + max(-d, 0.0)) / n
        out[i] = _rsi_value(gain, loss)
    return out


def find_candidate(closed):
    """Последняя закрытая свеча - кандидат? closed - только закрытые свечи, от старых к новым.

    Тот же автомат, что в Pine: RSI > верхнего уровня -> «взведён»; потом RSI <= уровня возврата -> сигнал.
    """
    if len(closed) < C.RSI_LEN + WARMUP + 5:
        return None
    vals = rsi_series([c["c"] for c in closed], C.RSI_LEN)
    last = len(vals) - 1
    armed = False
    peak = peak_i = None
    for i in range(WARMUP, last + 1):
        v = vals[i]
        if v is None:
            continue
        if v > C.RSI_UPPER:
            if not armed:
                armed, peak, peak_i = True, v, i
            elif v > peak:
                peak, peak_i = v, i
        elif armed and v <= C.RSI_BACK:
            if i == last:
                return {"rsi_now": v, "rsi_peak": peak, "peak_i": peak_i, "last_i": last}
            armed = False
    return None


def vol_mult(closed):
    """Объём последней закрытой свечи относительно среднего за 20 предыдущих."""
    if len(closed) < 21:
        return None
    avg = mean([x["v"] for x in closed[-21:-1]])
    return closed[-1]["v"] / avg if avg > 0 else None


# ---------------------------------------------------------------- OKX

def okx_get(path, params, tries=3):
    """GET к публичному API OKX с повтором и нарастающей паузой (не дольше 30 секунд)."""
    for attempt in range(tries):
        try:
            r = requests.get(OKX + path, params=params, timeout=15)
            if r.status_code == 200:
                j = r.json()
                if j.get("code") == "0":
                    return j.get("data", [])
                log(f"  OKX отказал: code={j.get('code')} {j.get('msg')} {params}")
                if j.get("code") == "51001":  # такого инструмента нет, повторять нет смысла
                    return None
            else:
                log(f"  HTTP {r.status_code}: {path} {params}")
        except (requests.RequestException, ValueError) as e:
            log(f"  Ошибка запроса: {e}")
        if attempt < tries - 1:
            time.sleep(min(max(PAUSE, 0.5) * (2 ** attempt), 30))
    return None


def fetch_candles(inst, bar, limit):
    """Свечи от старых к новым. closed=False у ещё не закрытой свечи."""
    data = okx_get("/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": str(limit)})
    time.sleep(PAUSE)
    if not data:
        return None
    out = []
    for r in reversed(data):
        out.append({"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                    "c": float(r[4]), "v": float(r[5]), "closed": r[8] == "1"})
    return out


def top_instruments():
    """Самые оборотистые USDT-перпетуалы OKX."""
    data = okx_get("/api/v5/market/tickers", {"instType": "SWAP"})
    time.sleep(PAUSE)
    if not data:
        return None
    rows = []
    for d in data:
        inst = d.get("instId", "")
        if not inst.endswith("-USDT-SWAP") or inst in C.EXCLUDE:
            continue
        try:
            turnover = float(d["volCcy24h"]) * float(d["last"])
        except (KeyError, ValueError, TypeError):
            continue
        rows.append((turnover, inst))
    rows.sort(reverse=True)
    return [inst for _, inst in rows[:C.TOP_COINS]]


# ---------------------------------------------------------------- Telegram

def send(text):
    if DRY_RUN:
        log("----- [DRY_RUN] сообщение не отправлено, вот его текст -----\n" + text + "\n-----")
        return True
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("  Нет секретов TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (Settings -> Secrets and variables -> Actions)")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=15)
    except requests.RequestException as e:
        log(f"  Telegram: ошибка запроса: {e}")
        return False
    if r.status_code != 200:
        log(f"  Telegram: HTTP {r.status_code}: {r.text[:200]}")
        return False
    return True


# ---------------------------------------------------------------- Сообщение

def build_message(inst, tf, info, closed, cur, now):
    base = inst.split("-")[0]
    tf_ms = TF_MS[tf]
    close_ts = closed[-1]["ts"] + tf_ms
    ago = info["last_i"] - info["peak_i"]
    peak_close = closed[info["peak_i"]]["ts"] + tf_ms
    ago_text = "на прошлой свече" if ago == 1 else f"{ago} св. назад"
    url = f"https://www.tradingview.com/chart/?symbol=OKX:{base}USDT.P&interval={TV_INTERVAL[tf]}"

    lines = [
        f"{C.EMOJI} <b>{C.TITLE}</b> · {base}/USDT",
        "",
        f"Таймфрейм: {TF_RU[tf]}, свеча закрылась {fmt_time(close_ts)} (Ташкент)",
        f"Цена сейчас: {price(cur)} ({fmt_hm(now)})",
        f"RSI({C.RSI_LEN}): {info['rsi_now']:.1f} — вернулся к {num(C.RSI_BACK)} "
        f"после выхода выше {num(C.RSI_UPPER)}",
        f"Максимум RSI: {info['rsi_peak']:.1f} ({ago_text}, {fmt_time(peak_close)})",
    ]
    vm = vol_mult(closed)
    if vm:
        lines.append(f"Объём свечи: ×{vm:.1f} от среднего")
    lines += ["", f'<a href="{url}">График TradingView</a>']
    return "\n".join(lines)


# ---------------------------------------------------------------- Шаги работы

def scan_timeframes(state, now, stats):
    insts = None
    for tf in C.SCAN_TIMEFRAMES:
        if time.time() - T0 > BUDGET_S:
            log(f"[{tf}] лимит времени запуска исчерпан, проверка продолжится в следующем запуске")
            return
        tf_ms = TF_MS[tf]
        if state["scanned"].get(tf) == (now // tf_ms) * tf_ms - tf_ms:
            continue  # свеча этого ТФ уже проверена, лишний запрос не нужен
        ref = fetch_candles(REFERENCE, tf, 3)
        ref_closed = [c for c in ref or [] if c["closed"]]
        if not ref_closed:
            log(f"[{tf}] не удалось получить опорные свечи, пропускаю")
            stats["errors"] += 1
            continue
        ref_ts = ref_closed[-1]["ts"]
        if state["scanned"].get(tf) == ref_ts:
            continue  # новая свеча этого ТФ ещё не закрылась
        if insts is None:
            insts = top_instruments()
            if not insts:
                log("Не удалось получить список монет")
                stats["errors"] += 1
                return
        log(f"[{tf}] закрылась свеча {fmt_time(ref_ts)}: монет в проверке: {len(insts)}")
        done = failed = found = consec = 0
        complete = True
        for inst in insts:
            if time.time() - T0 > BUDGET_S:
                log(f"[{tf}] лимит времени запуска исчерпан после {done} монет")
                complete = False
                break
            done += 1
            cs = fetch_candles(inst, tf, CANDLES)
            if not cs:
                failed += 1
                consec += 1
                if consec >= MAX_CONSEC_FAIL:
                    log(f"[{tf}] {consec} ошибок подряд, биржа недоступна, прерываю проверку")
                    complete = False
                    break
                continue
            consec = 0
            closed = [c for c in cs if c["closed"]]
            if not closed or closed[-1]["ts"] != ref_ts:
                continue
            info = find_candidate(closed)
            if not info:
                continue
            key = f"{inst}|{tf}|{ref_ts}"
            if key in state["seen"]:
                continue
            found += 1
            log(f"  кандидат: {inst} {tf}, RSI {info['rsi_now']:.1f} (максимум {info['rsi_peak']:.1f})")
            cur = cs[-1]["c"]  # цена сейчас - закрытие последней (формирующейся) свечи
            if send(build_message(inst, tf, info, closed, cur, now_ms())):
                state["seen"][key] = ref_ts
                stats["sent"] += 1
                log(f"  ОТПРАВЛЕНО: {inst} {tf}")
            else:
                stats["send_failed"] += 1
        stats["scanned"][tf] = done - failed
        stats["failed"] += failed
        stats["found"] += found
        if complete and failed <= max(3, len(insts) // 10):
            state["scanned"][tf] = ref_ts
        else:
            log(f"[{tf}] проверка неполная (ошибок {failed}), повторю в следующем запуске")
        return  # за один запуск - один таймфрейм, следующий - в следующем запуске


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    for k in ("scanned", "seen"):
        s.setdefault(k, {})
    return s


def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)


def prune(state, now):
    state["seen"] = {k: v for k, v in state["seen"].items() if now - v < 7 * DAY_MS}


def main():
    if "--test" in sys.argv or os.environ.get("TEST_TELEGRAM") == "1":
        ok = send(f"✅ {C.TITLE}: бот на связи. Сообщения о кандидатах будут приходить сюда.")
        log("Тестовое сообщение отправлено" if ok else "Тестовое сообщение НЕ отправлено")
        sys.exit(0 if ok else 1)

    state = load_state()
    now = now_ms()
    stats = {"scanned": {}, "failed": 0, "found": 0, "errors": 0, "sent": 0, "send_failed": 0}
    try:
        scan_timeframes(state, now, stats)
        prune(state, now)
    finally:
        save_state(state)
    log(f"Итог: проверено монет {stats['scanned'] or 'новых свечей нет'}, ошибок запросов {stats['failed']}, "
        f"кандидатов {stats['found']}, отправлено {stats['sent']}, "
        f"время работы {(time.time() - T0) / 60:.1f} мин")
    if stats["send_failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
