"""Configuration for Market Structure Scanner."""
TIMEFRAMES = {
    "30m": {"okx": "30m", "milliseconds": 30 * 60 * 1000, "label": "30m"},
    "1h": {"okx": "1H", "milliseconds": 60 * 60 * 1000, "label": "1h"},
    "4h": {"okx": "4H", "milliseconds": 4 * 60 * 60 * 1000, "label": "4h"},
}
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
CANDLE_LIMIT = 300
ATR_PERIOD = 14
RETEST_ATR_TOLERANCE = 0.20
MIN_RETEST_TOLERANCE_PCT = 0.001
SETUP_MAX_BARS = 60      # схема (от CHoCH до ретеста) не дольше стольких свечей, иначе сброс
RETEST_MAX_BARS = 12     # ретест не позже стольких свечей после BOS, иначе сброс
REQUEST_PAUSE_SECONDS = 0.12
REQUEST_TIMEOUT_SECONDS = 20
MAX_RETRIES = 3
STATE_FILE = "state/state.json"
STATE_RETENTION_DAYS = 30

# --- План сделки в сообщении (шорт) ---
# Бот сам не торгует: только подсказывает вход, стоп и тейки. Решение и сделку делаете вы.
TRADE_PLAN = True        # False = присылать сообщение без плана сделки
SL_BUFFER_PCT = 0.15     # стоп выше LH (нижнего максимума) на столько процентов
TP1_R = 1.0              # тейк 1 = риск × это число (1.0 = прибыль равна риску)
TP2_R = 2.0              # тейк 2 = риск × это число
MAX_RISK_PCT = 5.0       # стоп дальше стольких % от входа = предупреждение «широкий стоп»
MAX_CHASE_R = 0.5        # если цена уже ушла вниз от входа больше чем на N рисков, вход не догонять
RISK_USDT = 1.0          # сколько USDT вы готовы потерять в одной сделке; по нему считается объём позиции
