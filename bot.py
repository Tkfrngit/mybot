import time
import json
import os
import requests
import numpy as np
import jwt
import uuid
from urllib.parse import urlencode
from datetime import datetime

ACCESS = os.environ.get("UPBIT_ACCESS")
SECRET = os.environ.get("UPBIT_SECRET")

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"

TRADE_AMOUNT = 5000

RSI_BUY = 30
RSI_SELL = 65

STOP_LOSS = -0.02
TAKE_PROFIT = 0.03

position = {}


def telegram(msg):
    if not TG_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    requests.post(url, data={
        "chat_id": TG_CHAT,
        "text": msg
    })


def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


def get_top_markets():

    url = "https://api.upbit.com/v1/ticker"

    markets = requests.get(
        "https://api.upbit.com/v1/market/all"
    ).json()

    krw = [m["market"] for m in markets if "KRW-" in m["market"]]

    data = requests.get(url, params={"markets": ",".join(krw)}).json()

    data.sort(key=lambda x: x["acc_trade_price_24h"], reverse=True)

    return [d["market"] for d in data[:5]]


def get_candles(market):

    url = "https://api.upbit.com/v1/candles/minutes/1"

    r = requests.get(url, params={
        "market": market,
        "count": 200
    }).json()

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


def get_price(market):

    url = "https://api.upbit.com/v1/ticker"

    r = requests.get(url, params={"markets": market}).json()

    return r[0]["trade_price"]


def make_headers(query=None):

    payload = {
        "access_key": ACCESS,
        "nonce": str(uuid.uuid4())
    }

    if query:

        m = urlencode(query).encode()

        payload["query_hash"] = jwt.utils.base64url_encode(m).decode()

        payload["query_hash_alg"] = "SHA512"

    jwt_token = jwt.encode(payload, SECRET)

    return {"Authorization": f"Bearer {jwt_token}"}


def buy(market):

    query = {
        "market": market,
        "side": "bid",
        "price": TRADE_AMOUNT,
        "ord_type": "price"
    }

    headers = make_headers(query)

    requests.post(
        "https://api.upbit.com/v1/orders",
        params=query,
        headers=headers
    )


def sell(market, volume):

    query = {
        "market": market,
        "side": "ask",
        "volume": volume,
        "ord_type": "market"
    }

    headers = make_headers(query)

    requests.post(
        "https://api.upbit.com/v1/orders",
        params=query,
        headers=headers
    )


print("TRADING BOT STARTED")

while True:

    try:

        markets = get_top_markets()

        data_markets = {}

        for m in markets:

            price = get_price(m)

            candles = get_candles(m)

            rsi = calculate_rsi(candles)

            data_markets[m] = {
                "price": price,
                "rsi": rsi
            }

            if m not in position:

                if rsi < RSI_BUY:

                    buy(m)

                    position[m] = price

                    telegram(f"매수 {m} {price}")

            else:

                entry = position[m]

                pnl = (price - entry) / entry

                if pnl <= STOP_LOSS:

                    sell(m, 0.001)

                    telegram(f"손절 {m} {pnl*100:.2f}%")

                    del position[m]

                elif pnl >= TAKE_PROFIT:

                    sell(m, 0.001)

                    telegram(f"익절 {m} {pnl*100:.2f}%")

                    del position[m]

                elif rsi > RSI_SELL:

                    sell(m, 0.001)

                    telegram(f"RSI 매도 {m}")

                    del position[m]

        state = {
            "balance": None,
            "markets": data_markets,
            "message": "자동매매 실행중",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_pnl": None
        }

        save_state(state)

        time.sleep(15)

    except Exception as e:

        print("ERROR", e)

        time.sleep(5)
