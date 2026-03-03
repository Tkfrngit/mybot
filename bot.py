import time
import json
import requests
import numpy as np

STATE_FILE = "state.json"

print("🚀 BOT STARTED")

# ==========================
# 상태 저장
# ==========================

def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"markets": {}, "time": None}

# ==========================
# RSI 계산
# ==========================

def get_rsi(prices, period=14):
    deltas = np.diff(prices)
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum()/period
    down = -seed[seed < 0].sum()/period
    rs = up/down if down != 0 else 0
    rsi = 100 - 100/(1+rs)
    return rsi

# ==========================
# 업비트 데이터
# ==========================

def fetch_candles(market):
    url = "https://api.upbit.com/v1/candles/minutes/5"
    params = {"market": market, "count": 50}
    res = requests.get(url, params=params)
    return res.json()

# ==========================
# 메인 루프
# ==========================

while True:

    print("⏱ LOOP START", time.strftime("%H:%M:%S"))

    try:
        market = "KRW-BTC"

        candles = fetch_candles(market)

        closes = [c["trade_price"] for c in candles][::-1]

        price = closes[-1]

        rsi = get_rsi(closes)

        print("💰 PRICE:", price)
        print("📊 RSI:", rsi)

        state = load_state()

        state["markets"] = {
            market: {
                "price": price,
                "rsi": round(rsi,2)
            }
        }

        state["time"] = time.strftime("%Y-%m-%d %H:%M:%S")

        save_state(state)

        print("✅ STATE UPDATED")

    except Exception as e:
        print("❌ ERROR:", e)

    time.sleep(30)
