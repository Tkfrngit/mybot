import requests
import time
import json
import numpy as np
import os
import jwt
import uuid
import hashlib

# ===== 설정 =====
DRY_RUN = True   # ⚠️ 실매매 전환 전 True 유지
TOP_N = 2
STOP_LOSS = 0.02
TRAIL_GAP = 0.015
ASSUMED_VOLUME = 0.001

ACCESS_KEY = "rwknDCClWcPrdtgW4p87Jfubt9CcYmReAfCpSSm5"
SECRET_KEY = "8WZmycY0ZIvBHuyn8eqonWYacx9eBHUzCH1mWzzh"

STATE_FILE = "state.json"
HISTORY_FILE = "pnl_history.json"
PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"
ARM_FILE = "armed.flag"   # 실매매 승인 파일

SLEEP_SEC = 30


# ===== 인증 =====
def get_headers(query=None):
    payload = {"access_key": ACCESS_KEY, "nonce": str(uuid.uuid4())}

    if query:
        m = hashlib.sha512()
        m.update(query.encode())
        payload["query_hash"] = m.hexdigest()
        payload["query_hash_alg"] = "SHA512"

    token = jwt.encode(payload, SECRET_KEY)
    return {"Authorization": f"Bearer {token}"}


# ===== 잔고 조회 =====
def get_balance():
    try:
        headers = get_headers()
        res = requests.get("https://api.upbit.com/v1/accounts", headers=headers).json()
        krw = next((x for x in res if x["currency"] == "KRW"), None)
        return float(krw["balance"]) if krw else 0
    except:
        return 0


# ===== 주문 =====
def market_order(market, side, volume=None, price=None):
    if DRY_RUN or not os.path.exists(ARM_FILE):
        print("🧪 안전모드 — 주문 안 나감")
        return

    body = {"market": market, "side": side, "ord_type": "market"}

    if side == "ask":
        body["volume"] = str(volume)
    else:
        body["price"] = str(price)

    query = "&".join([f"{k}={v}" for k, v in body.items()])
    headers = get_headers(query)
    requests.post("https://api.upbit.com/v1/orders", json=body, headers=headers)


# ===== 보조 =====
def save_json(file, data):
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_json(file):
    if not os.path.exists(file):
        return []
    with open(file, "r", encoding="utf-8") as f:
        return json.load(f)


def get_top_markets():
    markets = requests.get("https://api.upbit.com/v1/market/all").json()
    krw = [m["market"] for m in markets if m["market"].startswith("KRW-")]
    tickers = requests.get("https://api.upbit.com/v1/ticker?markets=" + ",".join(krw)).json()
    sorted_markets = sorted(tickers, key=lambda x: x["acc_trade_price_24h"], reverse=True)
    return [m["market"] for m in sorted_markets[:TOP_N]]


def rsi(closes, period=14):
    deltas = np.diff(closes)
    seed = deltas[:period]
    up = seed[deltas[:period] >= 0].sum() / period
    down = -seed[deltas[:period] < 0].sum() / period
    if down == 0:
        return 100
    return 100 - (100 / (1 + up / down))


def bollinger(closes):
    ma = np.mean(closes[-20:])
    std = np.std(closes[-20:])
    return ma + 2 * std, ma - 2 * std


def get_candles(market):
    url = f"https://api.upbit.com/v1/candles/minutes/5?market={market}&count=100"
    return requests.get(url).json()


# ===== 메인 =====
print("🚀 고급 대시보드 모드 시작")

positions = {}
history = load_json(HISTORY_FILE)

while True:
    try:
        if os.path.exists(PAUSE_FILE):
            time.sleep(5)
            continue

        markets = get_top_markets()
        state = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "balance": get_balance(),
                 "markets": {}}

        total_pnl = 0

        for market in markets:
            candles = get_candles(market)
            closes = [c["trade_price"] for c in candles][::-1]
            price = closes[-1]

            upper, lower = bollinger(closes)
            r = rsi(closes)

            if market not in positions:
                positions[market] = {"position": False, "entry": 0, "peak": 0, "pnl": 0}

            pos = positions[market]

            if not pos["position"] and price <= lower and r < 35:
                pos["position"] = True
                pos["entry"] = price
                pos["peak"] = price
                market_order(market, "bid", price=10000)

            if pos["position"]:
                pos["peak"] = max(pos["peak"], price)
                if price <= pos["peak"] * (1 - TRAIL_GAP):
                    profit = (price - pos["entry"]) * ASSUMED_VOLUME
                    pos["pnl"] += profit
                    pos["position"] = False
                    market_order(market, "ask", volume=ASSUMED_VOLUME)

            total_pnl += pos["pnl"]

            state["markets"][market] = {
                "price": price,
                "rsi": round(r, 2),
                "position": pos["position"],
                "pnl": round(pos["pnl"], 0)
            }

        history.append({"time": state["time"], "pnl": round(total_pnl, 0)})
        save_json(HISTORY_FILE, history[-50:])

        state["total_pnl"] = round(total_pnl, 0)
        save_json(STATE_FILE, state)

        time.sleep(SLEEP_SEC)

    except Exception as e:
        print("에러:", e)
        time.sleep(5)