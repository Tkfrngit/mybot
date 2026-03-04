import os
import time
import json
import uuid
import hashlib
import sqlite3
import threading
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, List, Tuple

import requests
import numpy as np
import jwt

# =========================
# 운영 설정
# =========================
TOP_N = int(os.getenv("TOP_N", "5"))
LOOP_SEC = int(os.getenv("LOOP_SEC", "15"))

MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "2"))
KRW_PER_TRADE = int(os.getenv("KRW_PER_TRADE", "5000"))
DAILY_LOSS_LIMIT_KRW = int(os.getenv("DAILY_LOSS_LIMIT_KRW", "20000"))
MIN_ORDER_KRW = int(os.getenv("MIN_ORDER_KRW", "5000"))

RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_BUY = float(os.getenv("RSI_BUY", "30"))
RSI_SELL = float(os.getenv("RSI_SELL", "65"))

STOP_LOSS = float(os.getenv("STOP_LOSS", "0.02"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.03"))
TRAIL_GAP = float(os.getenv("TRAIL_GAP", "0.015"))

STATE_FILE = "state.json"
POSITIONS_FILE = "positions.json"

ARM_FILE = "armed.flag"
PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"

DB_FILE = "trades.db"

ACCESS = os.getenv("UPBIT_ACCESS", "").strip()
SECRET = os.getenv("UPBIT_SECRET", "").strip()

# ✅ 실거래 스위치(환경변수)
LIVE_TRADING = os.getenv("LIVE_TRADING", "0").strip() in ("1", "true", "True", "YES", "yes")

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "mybot/2.0"})

DB_LOCK = threading.Lock()


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def telegram(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        SESSION.post(url, data={"chat_id": TG_CHAT, "text": msg}, timeout=10)
    except Exception:
        pass


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def write_state(state: Dict[str, Any]) -> None:
    save_json(STATE_FILE, state)


def is_armed() -> bool:
    return os.path.exists(ARM_FILE)


def is_paused() -> bool:
    return os.path.exists(PAUSE_FILE)


def clear_force_flag():
    if os.path.exists(FORCE_FILE):
        os.remove(FORCE_FILE)


def can_trade_live() -> bool:
    # ✅ 실거래 주문이 나가려면 2중 체크
    return LIVE_TRADING and is_armed() and bool(ACCESS) and bool(SECRET)


# =========================
# DB (SQLite)
# =========================
def db_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with DB_LOCK:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            data TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            mode TEXT NOT NULL,           -- PAPER / LIVE
            market TEXT NOT NULL,
            side TEXT NOT NULL,           -- BUY / SELL
            qty REAL NOT NULL,
            price REAL NOT NULL,          -- 평균 체결가(추정/실체결)
            krw REAL NOT NULL,            -- 체결금액(수수료 제외/포함은 아래 fee로)
            fee REAL NOT NULL,
            reason TEXT,
            order_uuid TEXT,
            realized_pnl_krw REAL
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS cost_basis (
            market TEXT PRIMARY KEY,
            qty REAL NOT NULL,
            cost_krw REAL NOT NULL
        )
        """)
        conn.commit()
        conn.close()


def db_event(level: str, message: str, data: Optional[dict] = None):
    with DB_LOCK:
        conn = db_conn()
        conn.execute(
            "INSERT INTO events(ts, level, message, data) VALUES(?,?,?,?)",
            (now_str(), level, message, json.dumps(data, ensure_ascii=False) if data else None),
        )
        conn.commit()
        conn.close()


def db_get_cost(market: str) -> Tuple[float, float]:
    with DB_LOCK:
        conn = db_conn()
        row = conn.execute("SELECT qty, cost_krw FROM cost_basis WHERE market=?", (market,)).fetchone()
        conn.close()
    if not row:
        return 0.0, 0.0
    return float(row["qty"]), float(row["cost_krw"])


def db_set_cost(market: str, qty: float, cost_krw: float):
    with DB_LOCK:
        conn = db_conn()
        conn.execute(
            "INSERT INTO cost_basis(market, qty, cost_krw) VALUES(?,?,?) "
            "ON CONFLICT(market) DO UPDATE SET qty=excluded.qty, cost_krw=excluded.cost_krw",
            (market, qty, cost_krw),
        )
        conn.commit()
        conn.close()


def db_trade(mode: str, market: str, side: str, qty: float, price: float, krw: float, fee: float,
             reason: str = "", order_uuid: Optional[str] = None, realized_pnl: Optional[float] = None):
    with DB_LOCK:
        conn = db_conn()
        conn.execute("""
        INSERT INTO trades(ts, mode, market, side, qty, price, krw, fee, reason, order_uuid, realized_pnl_krw)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (now_str(), mode, market, side, qty, price, krw, fee, reason, order_uuid, realized_pnl))
        conn.commit()
        conn.close()


def apply_cost_basis_and_log(mode: str, market: str, side: str, qty: float, avg_price: float, fee: float,
                             reason: str, order_uuid: Optional[str]):
    """
    - BUY: cost += (qty*price + fee)
    - SELL: realized = (qty*price - fee) - avg_cost*qty
    """
    qty = float(qty)
    avg_price = float(avg_price)
    fee = float(fee)
    gross = qty * avg_price

    cb_qty, cb_cost = db_get_cost(market)

    if side == "BUY":
        new_qty = cb_qty + qty
        new_cost = cb_cost + gross + fee
        db_set_cost(market, new_qty, new_cost)
        db_trade(mode, market, "BUY", qty, avg_price, gross, fee, reason, order_uuid, None)
        return

    # SELL
    realized = None
    if cb_qty > 0:
        avg_cost = cb_cost / cb_qty
        cost_out = avg_cost * qty
        proceeds = gross - fee
        realized = proceeds - cost_out
        new_qty = max(0.0, cb_qty - qty)
        new_cost = max(0.0, cb_cost - cost_out)
        db_set_cost(market, new_qty, new_cost)

    db_trade(mode, market, "SELL", qty, avg_price, gross, fee, reason, order_uuid, realized)


# =========================
# 지표
# =========================
def rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    seed = deltas[:period]
    up = seed[seed > 0].sum() / period
    down = (-seed[seed < 0]).sum() / period
    if down == 0:
        return 100.0
    rs = up / down
    return float(100 - (100 / (1 + rs)))


def bollinger(prices: List[float], period: int = 20) -> Tuple[float, float, float]:
    p = prices[-period:] if len(prices) >= period else prices
    ma = float(np.mean(p))
    std = float(np.std(p))
    return ma + 2 * std, ma, ma - 2 * std


def sma(prices: List[float], period: int = 50) -> float:
    p = prices[-period:] if len(prices) >= period else prices
    return float(np.mean(p))


# =========================
# 업비트 Public
# =========================
def upbit_public_get(url: str, params: Optional[dict] = None):
    return SESSION.get(url, params=params, timeout=10).json()


def get_top_krw_markets(top_n: int) -> List[str]:
    markets = upbit_public_get("https://api.upbit.com/v1/market/all", params={"isDetails": "false"})
    krw = [m["market"] for m in markets if m["market"].startswith("KRW-")]
    tickers = upbit_public_get("https://api.upbit.com/v1/ticker", params={"markets": ",".join(krw)})
    tickers.sort(key=lambda x: x.get("acc_trade_price_24h", 0), reverse=True)
    return [t["market"] for t in tickers[:top_n]]


def get_candles_1m(market: str, count: int = 200) -> List[float]:
    data = upbit_public_get(
        "https://api.upbit.com/v1/candles/minutes/1",
        params={"market": market, "count": str(count)},
    )
    closes = [c["trade_price"] for c in data]
    closes.reverse()
    return closes


def get_tickers(markets: List[str]) -> Dict[str, float]:
    if not markets:
        return {}
    data = upbit_public_get("https://api.upbit.com/v1/ticker", params={"markets": ",".join(markets)})
    return {t["market"]: float(t["trade_price"]) for t in data}


# =========================
# 업비트 Private (에러를 문자열로 받는 경우 포함해서 처리)
# =========================
def make_auth_headers(query: Optional[dict] = None) -> dict:
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


def upbit_private_get(path: str, query: Optional[dict] = None):
    headers = make_auth_headers(query)
    r = SESSION.get(f"https://api.upbit.com{path}", params=query, headers=headers, timeout=10)
    try:
        return r.json()
    except Exception:
        return r.text


def upbit_private_post(path: str, query: dict):
    headers = make_auth_headers(query)
    r = SESSION.post(f"https://api.upbit.com{path}", params=query, headers=headers, timeout=10)
    try:
        return r.json()
    except Exception:
        return r.text


def parse_upbit_error(data):
    # 업비트가 실패하면 dict {"error":{...}} 또는 문자열이 올 수 있음
    if isinstance(data, dict) and "error" in data:
        return data["error"]
    if isinstance(data, str):
        return data
    return None


def get_accounts() -> List[dict]:
    data = upbit_private_get("/v1/accounts")
    err = parse_upbit_error(data)
    if err:
        raise Exception(err)
    if not isinstance(data, list):
        raise Exception(f"Unexpected accounts type: {type(data)}")
    return data


def get_order(uuid_str: str) -> dict:
    data = upbit_private_get("/v1/order", {"uuid": uuid_str})
    err = parse_upbit_error(data)
    if err:
        raise Exception(err)
    if not isinstance(data, dict):
        raise Exception(f"Unexpected order type: {type(data)}")
    return data


def order_buy_krw(market: str, krw_amount: int) -> dict:
    query = {"market": market, "side": "bid", "price": str(krw_amount), "ord_type": "price"}
    data = upbit_private_post("/v1/orders", query)
    err = parse_upbit_error(data)
    if err:
        raise Exception(err)
    return data


def order_sell_market(market: str, volume: float) -> dict:
    query = {"market": market, "side": "ask", "volume": str(volume), "ord_type": "market"}
    data = upbit_private_post("/v1/orders", query)
    err = parse_upbit_error(data)
    if err:
        raise Exception(err)
    return data


def extract_fills_from_order(order_detail: dict) -> Tuple[float, float, float]:
    """
    returns: (filled_qty, avg_price, fee)
    """
    trades = order_detail.get("trades") or []
    if not trades:
        # 체결 리스트 없을 수 있음(바로 못 받는 경우)
        vol = float(order_detail.get("executed_volume") or 0)
        price = float(order_detail.get("price") or 0)
        fee = float(order_detail.get("paid_fee") or 0)
        return vol, price, fee

    qty_sum = 0.0
    krw_sum = 0.0
    fee_sum = 0.0
    for t in trades:
        v = float(t.get("volume") or 0)
        p = float(t.get("price") or 0)
        qty_sum += v
        krw_sum += v * p
        fee_sum += float(t.get("fee") or 0)
    avg = (krw_sum / qty_sum) if qty_sum > 0 else 0.0
    return qty_sum, avg, fee_sum


# =========================
# 포지션(봇 추적용)
# =========================
@dataclass
class Position:
    market: str
    entry_price: float
    volume: float
    peak_price: float
    entry_time: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load_positions() -> Dict[str, Position]:
    raw = load_json(POSITIONS_FILE, {})
    out: Dict[str, Position] = {}
    for m, d in raw.items():
        try:
            out[m] = Position(
                market=m,
                entry_price=float(d["entry_price"]),
                volume=float(d["volume"]),
                peak_price=float(d.get("peak_price", d["entry_price"])),
                entry_time=str(d.get("entry_time", now_str())),
            )
        except Exception:
            continue
    return out


def save_positions(pos: Dict[str, Position]) -> None:
    save_json(POSITIONS_FILE, {m: p.as_dict() for m, p in pos.items()})


# =========================
# 포트폴리오 요약(계좌 기준)
# =========================
def build_portfolio_view(accounts: List[dict]) -> Dict[str, Any]:
    portfolio = []
    markets_for_ticker = []
    krw_balance = 0.0

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
            market = f"KRW-{cur}"
            markets_for_ticker.append(market)
            portfolio.append({
                "market": market,
                "currency": cur,
                "balance": bal,
                "locked": locked,
                "avg_buy_price": avg,
            })

    prices = get_tickers(markets_for_ticker)

    total_eval = krw_balance
    total_cost = krw_balance
    rows = []

    for row in portfolio:
        m = row["market"]
        qty = row["balance"] + row["locked"]
        avg = row["avg_buy_price"]
        price = prices.get(m)

        if price is None:
            rows.append({"market": m, "qty": round(qty, 8), "avg_buy_price": round(avg, 2),
                         "price": None, "eval_krw": None, "pnl_krw": None, "pnl_rate": None})
            continue

        eval_krw = qty * price
        cost = qty * avg if avg > 0 else 0
        pnl_krw = eval_krw - cost
        pnl_rate = (pnl_krw / cost * 100) if cost > 0 else None

        total_eval += eval_krw
        total_cost += cost

        rows.append({
            "market": m,
            "qty": round(qty, 8),
            "avg_buy_price": round(avg, 2),
            "price": round(price, 2),
            "eval_krw": round(eval_krw, 0),
            "pnl_krw": round(pnl_krw, 0),
            "pnl_rate": None if pnl_rate is None else round(pnl_rate, 2),
        })

    total_pnl = total_eval - total_cost

    return {
        "krw_balance": round(krw_balance, 0),
        "portfolio": rows,
        "total_eval_krw": round(total_eval, 0),
        "total_cost_krw": round(total_cost, 0),
        "total_pnl_krw": round(total_pnl, 0),
    }


# =========================
# 메인 루프
# =========================
def main():
    db_init()
    db_event("INFO", "BOT_START", {"live_trading": LIVE_TRADING})

    print("🚀 BOT STARTED", now_str(), "LIVE_TRADING=", LIVE_TRADING)
    telegram(f"🚀 봇 시작 | LIVE_TRADING={LIVE_TRADING}")

    positions = load_positions()

    daily_pnl_est = 0.0
    day_key = time.strftime("%Y-%m-%d")

    while True:
        try:
            if time.strftime("%Y-%m-%d") != day_key:
                day_key = time.strftime("%Y-%m-%d")
                daily_pnl_est = 0.0
                db_event("INFO", "DAY_RESET", {"day": day_key})

            # ---- 계좌 조회(항상) ----
            accounts = None
            balance_error = None
            portfolio_view = None

            if ACCESS and SECRET:
                try:
                    accounts = get_accounts()
                    portfolio_view = build_portfolio_view(accounts)
                except Exception as e:
                    balance_error = str(e)
                    db_event("ERROR", "ACCOUNTS_FAILED", {"error": balance_error})
            else:
                balance_error = "UPBIT_ACCESS/UPBIT_SECRET 환경변수가 없어요"
                db_event("WARN", "NO_KEYS", {})

            if is_paused():
                write_state({
                    "time": now_str(),
                    "message": "⏸ 일시정지 중",
                    "armed": is_armed(),
                    "live_trading": LIVE_TRADING,
                    "can_trade_live": can_trade_live(),
                    "balance_error": balance_error,
                    "portfolio": portfolio_view,
                    "daily_pnl_est_krw": round(daily_pnl_est, 0),
                    "markets": {},
                    "positions": {m: p.as_dict() for m, p in positions.items()},
                })
                time.sleep(3)
                continue

            if daily_pnl_est <= -abs(DAILY_LOSS_LIMIT_KRW):
                if not os.path.exists(PAUSE_FILE):
                    open(PAUSE_FILE, "w").close()
                telegram(f"⛔ 일일 손실 한도 초과로 자동 일시정지: {daily_pnl_est:.0f} KRW")
                db_event("WARN", "DAILY_LOSS_LIMIT_HIT", {"daily_pnl_est": daily_pnl_est})
                continue

            # ---- 강제청산(봇 추적 포지션만) ----
            if os.path.exists(FORCE_FILE):
                db_event("WARN", "FORCE_SELL_ALL", {})
                telegram("🔴 강제청산 요청")
                if can_trade_live() and accounts:
                    for m in list(positions.keys()):
                        coin = m.split("-")[1]
                        vol = 0.0
                        for a in accounts:
                            if a.get("currency") == coin:
                                vol = float(a.get("balance", 0))
                                break
                        if vol > 0:
                            od = order_sell_market(m, vol)
                            ou = od.get("uuid")
                            # 체결 확인(짧게)
                            filled_qty = vol
                            avg_price = 0.0
                            fee = 0.0
                            if ou:
                                try:
                                    time.sleep(0.7)
                                    detail = get_order(ou)
                                    filled_qty, avg_price, fee = extract_fills_from_order(detail)
                                except Exception as e:
                                    db_event("ERROR", "ORDER_DETAIL_FAIL", {"uuid": ou, "error": str(e)})
                            apply_cost_basis_and_log("LIVE", m, "SELL", filled_qty, avg_price, fee, "FORCE_SELL", ou)

                        positions.pop(m, None)

                    save_positions(positions)
                else:
                    positions.clear()
                    save_positions(positions)

                clear_force_flag()

            watch_markets = get_top_krw_markets(TOP_N)
            market_view: Dict[str, Any] = {}

            # ---- 1) 보유 포지션 관리 ----
            for m in list(positions.keys()):
                closes = get_candles_1m(m, 200)
                price = float(closes[-1])
                r = rsi(closes, RSI_PERIOD)
                upper, mid, lower = bollinger(closes, 20)
                trend = sma(closes, 50)

                p = positions[m]
                p.peak_price = max(p.peak_price, price)

                pnl_rate = (price - p.entry_price) / p.entry_price if p.entry_price > 0 else 0.0
                sell_reason = None

                if pnl_rate <= -STOP_LOSS:
                    sell_reason = f"STOP_LOSS {pnl_rate*100:.2f}%"
                elif pnl_rate >= TAKE_PROFIT:
                    sell_reason = f"TAKE_PROFIT {pnl_rate*100:.2f}%"
                elif price <= p.peak_price * (1 - TRAIL_GAP):
                    sell_reason = f"TRAIL {pnl_rate*100:.2f}%"
                elif r >= RSI_SELL:
                    sell_reason = f"RSI_SELL {r:.1f}"

                market_view[m] = {
                    "price": round(price, 2),
                    "rsi": round(r, 2),
                    "bb_upper": round(upper, 2),
                    "bb_mid": round(mid, 2),
                    "bb_lower": round(lower, 2),
                    "sma50": round(trend, 2),
                    "position": True,
                    "entry_price": round(p.entry_price, 2),
                    "peak_price": round(p.peak_price, 2),
                    "pnl_rate": round(pnl_rate * 100, 2),
                    "note": sell_reason or "holding",
                }

                if sell_reason:
                    db_event("INFO", "SELL_SIGNAL", {"market": m, "reason": sell_reason, "price": price})
                    telegram(f"🔻 매도 {m} | {sell_reason}")

                    if not can_trade_live():
                        # PAPER
                        apply_cost_basis_and_log("PAPER", m, "SELL", p.volume, price, 0.0, sell_reason, None)
                        daily_pnl_est += pnl_rate * KRW_PER_TRADE
                        positions.pop(m, None)
                        save_positions(positions)
                    else:
                        # LIVE
                        if accounts is None:
                            accounts = get_accounts()
                        coin = m.split("-")[1]
                        vol = 0.0
                        for a in accounts:
                            if a.get("currency") == coin:
                                vol = float(a.get("balance", 0))
                                break

                        if vol > 0:
                            od = order_sell_market(m, vol)
                            ou = od.get("uuid")
                            filled_qty = vol
                            avg_price = price
                            fee = float(od.get("paid_fee") or 0)

                            if ou:
                                try:
                                    # 체결 상세 짧게 확인
                                    for _ in range(5):
                                        time.sleep(0.7)
                                        detail = get_order(ou)
                                        filled_qty, avg_price, fee = extract_fills_from_order(detail)
                                        state = detail.get("state")
                                        if state == "done":
                                            break
                                except Exception as e:
                                    db_event("ERROR", "ORDER_DETAIL_FAIL", {"uuid": ou, "error": str(e)})

                            apply_cost_basis_and_log("LIVE", m, "SELL", filled_qty, avg_price, fee, sell_reason, ou)

                        daily_pnl_est += pnl_rate * KRW_PER_TRADE
                        positions.pop(m, None)
                        save_positions(positions)

            # ---- 2) 신규 진입 ----
            if len(positions) < MAX_POSITIONS:
                for m in watch_markets:
                    if m in positions:
                        continue
                    if len(positions) >= MAX_POSITIONS:
                        break

                    closes = get_candles_1m(m, 200)
                    price = float(closes[-1])
                    r = rsi(closes, RSI_PERIOD)
                    upper, mid, lower = bollinger(closes, 20)
                    trend = sma(closes, 50)

                    near_lower = price <= lower * 1.01
                    trend_ok = price >= trend * 0.98

                    market_view.setdefault(m, {})
                    market_view[m].update({
                        "price": round(price, 2),
                        "rsi": round(r, 2),
                        "bb_upper": round(upper, 2),
                        "bb_mid": round(mid, 2),
                        "bb_lower": round(lower, 2),
                        "sma50": round(trend, 2),
                        "position": False,
                        "note": "watch",
                    })

                    buy_signal = (r < RSI_BUY and near_lower and trend_ok)
                    if not buy_signal:
                        continue

                    reason = f"RSI_BUY {r:.1f} + NearLower + TrendOK"
                    db_event("INFO", "BUY_SIGNAL", {"market": m, "reason": reason, "price": price})
                    telegram(f"🟢 매수 시도 {m} | {reason}")

                    if not can_trade_live():
                        # PAPER
                        fake_vol = KRW_PER_TRADE / price
                        apply_cost_basis_and_log("PAPER", m, "BUY", fake_vol, price, 0.0, reason, None)
                        positions[m] = Position(m, price, fake_vol, price, now_str())
                        save_positions(positions)
                    else:
                        # LIVE
                        if accounts is None:
                            accounts = get_accounts()
                            portfolio_view = build_portfolio_view(accounts)
                        krw_bal = (portfolio_view or {}).get("krw_balance", 0) if portfolio_view else 0

                        if krw_bal < max(MIN_ORDER_KRW, KRW_PER_TRADE):
                            db_event("WARN", "KRW_NOT_ENOUGH", {"krw": krw_bal})
                            market_view[m]["note"] = "KRW 부족"
                            telegram(f"⚠️ KRW 잔고 부족: {krw_bal:.0f} KRW")
                            continue

                        od = order_buy_krw(m, KRW_PER_TRADE)
                        ou = od.get("uuid")

                        filled_qty = KRW_PER_TRADE / price
                        avg_price = price
                        fee = float(od.get("paid_fee") or 0)

                        if ou:
                            try:
                                for _ in range(6):
                                    time.sleep(0.7)
                                    detail = get_order(ou)
                                    filled_qty, avg_price, fee = extract_fills_from_order(detail)
                                    if detail.get("state") == "done":
                                        break
                            except Exception as e:
                                db_event("ERROR", "ORDER_DETAIL_FAIL", {"uuid": ou, "error": str(e)})

                        apply_cost_basis_and_log("LIVE", m, "BUY", filled_qty, avg_price, fee, reason, ou)

                        positions[m] = Position(m, avg_price, filled_qty, avg_price, now_str())
                        save_positions(positions)

            # ---- state 저장(대시보드) ----
            write_state({
                "time": now_str(),
                "message": "✅ 실행중",
                "armed": is_armed(),
                "live_trading": LIVE_TRADING,
                "can_trade_live": can_trade_live(),
                "balance_error": balance_error,
                "portfolio": portfolio_view,
                "daily_pnl_est_krw": round(daily_pnl_est, 0),
                "markets": market_view,
                "positions": {m: p.as_dict() for m, p in positions.items()},
            })

            time.sleep(LOOP_SEC)

        except Exception as e:
            err = f"❌ BOT ERROR: {e}"
            print(err)
            db_event("ERROR", "BOT_CRASH_LOOP", {"error": str(e)})
            telegram(err)
            write_state({
                "time": now_str(),
                "message": err,
                "armed": is_armed(),
                "live_trading": LIVE_TRADING,
                "can_trade_live": can_trade_live(),
                "balance_error": str(e),
                "portfolio": None,
                "daily_pnl_est_krw": None,
                "markets": {},
                "positions": {m: p.as_dict() for m, p in positions.items()},
            })
            time.sleep(5)


if __name__ == "__main__":
    main()
