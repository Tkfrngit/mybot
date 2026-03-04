import os
import time
import json
import uuid
import hashlib
import requests
import jwt

STATE_FILE = "state.json"
ARM_FILE = "armed.flag"
PAUSE_FILE = "pause.flag"

ACCESS = os.getenv("UPBIT_ACCESS", "").strip()
SECRET = os.getenv("UPBIT_SECRET", "").strip()
LIVE_TRADING = os.getenv("LIVE_TRADING", "0").strip() in ("1", "true", "True", "YES", "yes")

S = requests.Session()
S.headers.update({"User-Agent": "mybot/1.0"})


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def save_state(d):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def is_armed():
    return os.path.exists(ARM_FILE)


def is_paused():
    return os.path.exists(PAUSE_FILE)


def can_trade_live():
    return LIVE_TRADING and is_armed() and bool(ACCESS) and bool(SECRET)


def make_auth_headers(query=None):
    if not ACCESS or not SECRET:
        raise RuntimeError("UPBIT_ACCESS/UPBIT_SECRET 환경변수가 없어요.")

    payload = {"access_key": ACCESS, "nonce": str(uuid.uuid4())}

    if query:
        query_string = "&".join([f"{k}={query[k]}" for k in sorted(query.keys())])
        m = hashlib.sha512()
        m.update(query_string.encode("utf-8"))
        payload["query_hash"] = m.hexdigest()
        payload["query_hash_alg"] = "SHA512"

    token = jwt.encode(payload, SECRET)
    return {"Authorization": f"Bearer {token}"}


def upbit_private_get(path, query=None):
    headers = make_auth_headers(query)
    r = S.get(f"https://api.upbit.com{path}", params=query, headers=headers, timeout=10)
    try:
        return r.json()
    except Exception:
        return r.text


def parse_upbit_error(data):
    if isinstance(data, dict) and "error" in data:
        return data["error"]
    if isinstance(data, str):
        return data
    return None


def get_accounts():
    data = upbit_private_get("/v1/accounts")
    err = parse_upbit_error(data)
    if err:
        raise Exception(err)
    if not isinstance(data, list):
        raise Exception(f"Unexpected accounts type: {type(data)}")
    return data


def upbit_public_get(url, params=None):
    return S.get(url, params=params, timeout=10).json()


def get_tickers(markets):
    if not markets:
        return {}
    data = upbit_public_get("https://api.upbit.com/v1/ticker", {"markets": ",".join(markets)})
    return {t["market"]: float(t["trade_price"]) for t in data}


def build_portfolio_view(accounts):
    rows = []
    krw_balance = 0.0
    markets = []

    for a in accounts:
        cur = a.get("currency")
        bal = float(a.get("balance", 0))
        locked = float(a.get("locked", 0))
        avg = float(a.get("avg_buy_price", 0) or 0)
        unit = a.get("unit_currency", "KRW")

        if cur == "KRW":
            krw_balance = bal
            continue

        if bal + locked <= 0:
            continue

        if unit == "KRW":
            m = f"KRW-{cur}"
            markets.append(m)
            rows.append({
                "market": m,
                "qty": bal + locked,
                "avg_buy_price": avg,
            })

    prices = get_tickers(markets)

    out = []
    total_eval = krw_balance
    total_cost = krw_balance

    for r in rows:
        m = r["market"]
        qty = r["qty"]
        avg = r["avg_buy_price"]
        price = prices.get(m)

        if price is None:
            out.append({**r, "price": None, "eval_krw": None, "pnl_krw": None, "pnl_rate": None})
            continue

        eval_krw = qty * price
        cost = qty * avg if avg > 0 else 0.0
        pnl = eval_krw - cost
        pnl_rate = (pnl / cost * 100) if cost > 0 else None

        total_eval += eval_krw
        total_cost += cost

        out.append({
            "market": m,
            "qty": round(qty, 8),
            "avg_buy_price": round(avg, 2),
            "price": round(price, 2),
            "eval_krw": round(eval_krw, 0),
            "pnl_krw": round(pnl, 0),
            "pnl_rate": None if pnl_rate is None else round(pnl_rate, 2),
        })

    return {
        "krw_balance": round(krw_balance, 0),
        "portfolio": out,
        "total_eval_krw": round(total_eval, 0),
        "total_cost_krw": round(total_cost, 0),
        "total_pnl_krw": round(total_eval - total_cost, 0),
    }


def main():
    print("BOT START", now_str())

    while True:
        try:
            if is_paused():
                save_state({
                    "time": now_str(),
                    "message": "⏸ 일시정지 중",
                    "armed": is_armed(),
                    "live_trading": LIVE_TRADING,
                    "can_trade_live": can_trade_live(),
                    "balance_error": None,
                    "portfolio": None,
                    "markets": {},
                    "positions": {},
                })
                time.sleep(3)
                continue

            balance_error = None
            portfolio = None

            if ACCESS and SECRET:
                try:
                    accounts = get_accounts()
                    portfolio = build_portfolio_view(accounts)
                except Exception as e:
                    balance_error = str(e)
            else:
                balance_error = "UPBIT_ACCESS/UPBIT_SECRET 환경변수가 없어요"

            save_state({
                "time": now_str(),
                "message": "✅ 실행중",
                "armed": is_armed(),
                "live_trading": LIVE_TRADING,
                "can_trade_live": can_trade_live(),
                "balance_error": balance_error,
                "portfolio": portfolio,
                "markets": {},
                "positions": {},
            })

            time.sleep(5)

        except Exception as e:
            save_state({
                "time": now_str(),
                "message": f"❌ BOT ERROR: {e}",
                "armed": is_armed(),
                "live_trading": LIVE_TRADING,
                "can_trade_live": can_trade_live(),
                "balance_error": str(e),
                "portfolio": None,
                "markets": {},
                "positions": {},
            })
            time.sleep(5)


if __name__ == "__main__":
    main()
