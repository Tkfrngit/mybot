import time
import json
import requests
import numpy as np
from datetime import datetime

STATE_FILE = "state.json"

MARKET = "KRW-BTC"
RSI_PERIOD = 14


def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


def get_price():
    url = "https://api.upbit.com/v1/ticker"
    params = {"markets": MARKET}
    r = requests.get(url, params=params).json()
    return r[0]["trade_price"]


def get_candles():
    url = "https://api.upbit.com/v1/candles/minutes/1"
    params = {"market": MARKET, "count": 200}
    r = requests.get(url, params=params).json()
    closes = [c["trade_price"] for c in r]
    closes.reverse()
    return closes


def calculate_rsi(prices, period=14):
    deltas = np.diff(prices)
    seed = deltas[:period]

    up = seed[seed > 0].sum() / period
    down = -seed[seed < 0].sum() / period

    rs = up / down if down != 0 else 0
    rsi = 100 - (100 / (1 + rs))

    return round(rsi, 2)


print("BOT LOOP START")

while True:

    try:

        price = get_price()
        prices = get_candles()
        rsi = calculate_rsi(prices, RSI_PERIOD)

        message = "대기중"

        if rsi < 30:
            message = "매수 신호 (RSI < 30)"

        elif rsi > 70:
            message = "매도 신호 (RSI > 70)"

        state = {
            "balance": None,
            "markets": {
                MARKET: {
                    "price": price,
                    "rsi": rsi
                }
            },
            "message": message,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_pnl": None
        }

        save_state(state)

        print("PRICE:", price, "RSI:", rsi)

        time.sleep(10)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(5)
