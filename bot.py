import os
import time
import json
import uuid
import hashlib
import requests
import jwt
from datetime import datetime

# =====================
# 설정
# =====================

TRADE_KRW = 10000
MAX_POSITIONS = 3
SPLIT_BUY = 2
SPLIT_SELL = 2

RSI_BUY = 30
RSI_SELL = 60

TAKE_PROFIT = 0.03
STOP_LOSS = 0.02

DAILY_LOSS_LIMIT = -30000

REENTRY_COOLDOWN = 1800

CANDLE_MIN = 5
INTERVAL = 10

STATE_FILE = "state.json"
POSITIONS_FILE = "positions.json"

ACCESS = os.getenv("UPBIT_ACCESS")
SECRET = os.getenv("UPBIT_SECRET")

LIVE_TRADING = os.getenv("LIVE_TRADING", "0") == "1"

session = requests.Session()

# =====================
# 유틸
# =====================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# =====================
# 지표
# =====================

def sma(data, n):
    return sum(data[-n:]) / n


def rsi(data, n=14):
    gains = []
    losses = []

    for i in range(1, len(data)):
        diff = data[i] - data[i-1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))

    avg_gain = sum(gains[-n:]) / n if gains else 0
    avg_loss = sum(losses[-n:]) / n if losses else 1

    rs = avg_gain / avg_loss

    return 100 - (100/(1+rs))


def bollinger(data, n=20, k=2):
    mid = sma(data, n)
    std = (sum([(x-mid)**2 for x in data[-n:]])/n)**0.5

    return mid-k*std, mid, mid+k*std


# =====================
# Upbit API
# =====================

def public(url, params=None):
    return session.get(url, params=params).json()


def candles(market):
    url=f"https://api.upbit.com/v1/candles/minutes/{CANDLE_MIN}"
    return public(url,{"market":market,"count":200})


def price(market):
    r=public("https://api.upbit.com/v1/ticker",{"markets":market})
    return r[0]["trade_price"]


# =====================
# 주문
# =====================

def headers(query=None):

    payload={
        "access_key":ACCESS,
        "nonce":str(uuid.uuid4())
    }

    if query:

        qs="&".join([f"{k}={query[k]}" for k in sorted(query)])

        m=hashlib.sha512()
        m.update(qs.encode())

        payload["query_hash"]=m.hexdigest()
        payload["query_hash_alg"]="SHA512"

    token=jwt.encode(payload,SECRET)

    return {"Authorization":f"Bearer {token}"}


def buy(market, krw):

    q={
        "market":market,
        "side":"bid",
        "price":str(krw),
        "ord_type":"price"
    }

    return session.post(
        "https://api.upbit.com/v1/orders",
        data=q,
        headers=headers(q)
    ).json()


def sell(market, volume):

    q={
        "market":market,
        "side":"ask",
        "volume":str(volume),
        "ord_type":"market"
    }

    return session.post(
        "https://api.upbit.com/v1/orders",
        data=q,
        headers=headers(q)
    ).json()


# =====================
# 코인 자동선정
# =====================

def top_markets(n=5):

    markets=public("https://api.upbit.com/v1/market/all")

    krw=[m["market"] for m in markets if m["market"].startswith("KRW-")]

    tickers=public("https://api.upbit.com/v1/ticker",{"markets":",".join(krw)})

    sorted_markets=sorted(
        tickers,
        key=lambda x:x["acc_trade_price_24h"],
        reverse=True
    )

    return [m["market"] for m in sorted_markets[:n]]


# =====================
# 매매
# =====================

def decision(market, positions):

    cs=candles(market)

    closes=[c["trade_price"] for c in reversed(cs)]

    p=closes[-1]

    r=rsi(closes)

    low,mid,up=bollinger(closes)

    pos=positions.get(market)

    if not pos:

        if r<=RSI_BUY and p<=low*1.01:

            return "buy",p,r,low,mid,up

        return "watch",p,r,low,mid,up

    entry=pos["entry"]

    pnl=(p-entry)/entry

    if pnl>=TAKE_PROFIT:

        return "sell",p,r,low,mid,up

    if pnl<=-STOP_LOSS:

        return "sell",p,r,low,mid,up

    if r>=RSI_SELL:

        return "sell",p,r,low,mid,up

    return "hold",p,r,low,mid,up


# =====================
# 메인
# =====================

def main():

    positions=load_json(POSITIONS_FILE,{})

    while True:

        markets=top_markets(5)

        state_markets={}

        for m in markets:

            action,p,r,low,mid,up=decision(m,positions)

            state_markets[m]={
                "price":p,
                "rsi":r,
                "bb_lower":low,
                "bb_mid":mid,
                "bb_upper":up,
                "position":m in positions,
                "note":action
            }

            if action=="buy" and len(positions)<MAX_POSITIONS:

                amount=TRADE_KRW/SPLIT_BUY

                for _ in range(SPLIT_BUY):

                    if LIVE_TRADING:
                        buy(m,amount)

                    time.sleep(1)

                positions[m]={
                    "entry":p,
                    "qty":TRADE_KRW/p
                }

                save_json(POSITIONS_FILE,positions)

            if action=="sell" and m in positions:

                qty=positions[m]["qty"]

                part=qty/SPLIT_SELL

                for _ in range(SPLIT_SELL):

                    if LIVE_TRADING:
                        sell(m,part)

                    time.sleep(1)

                del positions[m]

                save_json(POSITIONS_FILE,positions)

        save_json(STATE_FILE,{
            "time":now(),
            "message":"running",
            "markets":state_markets,
            "positions":positions
        })

        time.sleep(INTERVAL)


if __name__=="__main__":
    main()
