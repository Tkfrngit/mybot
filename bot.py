import os
import time
import requests
import numpy as np

# Railway Variables에 넣을 것:
# DASHBOARD_INGEST_URL = https://mybot-production-7716.up.railway.app/ingest
INGEST_URL = os.environ.get("DASHBOARD_INGEST_URL", "").strip()

MARKET = "KRW-BTC"
SLEEP_SEC = 30

def get_candles():
    url = f"https://api.upbit.com/v1/candles/minutes/5?market={MARKET}&count=100"
    return requests.get(url, timeout=10).json()

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0:
        return 100.0
    rs = up / down
    return 100 - (100 / (1 + rs))

def post_state(state: dict):
    if not INGEST_URL:
        print("❌ DASHBOARD_INGEST_URL 환경변수가 비어있어요.")
        return
    try:
        requests.post(INGEST_URL, json=state, timeout=10)
        print("✅ sent:", state["time"], state["markets"].get(MARKET, {}).get("price"))
    except Exception as e:
        print("❌ ingest 실패:", e)

print("🚀 BOT START")
while True:
    try:
        candles = get_candles()
        closes = [c["trade_price"] for c in candles][::-1]
        price = float(closes[-1])
        r = rsi(closes)

        state = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "balance": None,
            "total_pnl": 0,
            "message": "봇이 정상 동작 중",
            "markets": {
                MARKET: {
                    "price": price,
                    "rsi": None if r is None else round(float(r), 2),
                    "position": False,
                    "pnl": 0
                }
            }
        }

        post_state(state)
        time.sleep(SLEEP_SEC)

    except Exception as e:
        post_state({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "balance": None,
            "total_pnl": None,
            "message": f"봇 에러: {e}",
            "markets": {}
        })
        time.sleep(5)
