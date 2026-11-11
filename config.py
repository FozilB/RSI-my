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
REQUEST_PAUSE_SECONDS = 0.12
REQUEST_TIMEOUT_SECONDS = 20
MAX_RETRIES = 3
STATE_FILE = "state/state.json"
STATE_RETENTION_DAYS = 30
