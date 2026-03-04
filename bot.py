import os
import time
import json
import uuid
import hashlib
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, List, Tuple

import requests
import numpy as np
import jwt

# =========================
# 운영 설정
# =========================
TOP_N = 5
LOOP_SEC = 15

MAX_POSITIONS = 2
KRW_PER_TRADE = 5000
DAILY_LOSS_LIMIT_KRW = 20000
MIN_ORDER_KRW = 5000

RSI_PERIOD = 14
RSI_BUY = 30
RSI_SELL = 65

STOP_LOSS = 0.02
TAKE_PROFIT = 0.03
TRAIL_GAP = 0.015

STATE_FILE = "state.json"
POSITIONS_FILE = "positions.json"

ARM_FILE = "armed.flag"
PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"

ACCESS = os.getenv("UPBIT_ACCESS", "").strip()
SECRET = os.getenv("UPBIT_SECRET", "").strip()

# ✅ 실거래 스위치 (Railway Variables에서 LIVE_TRADING=1 일 때만 True)
LIVE_TRADING = os.getenv("LIVE_TRADING", "0").strip() in ("1", "true", "True", "YES", "yes")

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "mybot/1.0"})


def telegram(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        SESSION.post(url, data={"chat_id": TG_CHAT, "text": msg}, timeout=10)
    except Exception:
        pass


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


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


def is_armed() -> bool:
    return os.path.exists(ARM_FILE)


def is_paused() -> bool:
    return os.path.exists(PAUSE_FILE)


def clear_force_flag():
    if os.path.exists(FORCE_FILE):
        os.remove(FORCE_FILE)


def can_trade_live() -> bool:
    """
    ✅ 실거래 주문이 나가려면:
    1) LIVE_TRADING=1
    2) 대시보드에서 Arm(armed.flag 생성)
    3) UPBIT_ACCESS/UPBIT_SECRET 존재
    """
    return LIVE_TRADING and is_armed() and bool(ACCESS) and bool(SECRET)


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
# 업비트 Private
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
    return SESSION.get(f"https://api.upbit.com{path}", params=query, headers=headers, timeout=10).json()


def upbit_private_post(path: str, query: dict):
    headers = make_auth_headers(query)
    return SESSION.post(f"https://api.upbit.com{path}", params=query, headers=headers, timeout=10).json()


def get_accounts():
    data = upbit_private_get("/v1/accounts")

    if isinstance(data, dict) and "error" in data:
        raise Exception(data["error"])

    if isinstance(data, str):
        raise Exception(data)

    return data


def order_buy_krw(market: str, krw_amount: int) -> dict:
    query = {"market": market, "side": "bid", "price": str(krw_amount), "ord_type": "price"}
    return upbit_private_post("/v1/orders", query)


def order_sell_market(market: str, volume: float) -> dict:
    query = {"market": market, "side": "ask", "volume": str(volume), "ord_type": "market"}
    return upbit_private_post("/v1/orders", query)


# =========================
# 포지션
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


def write_state(state: Dict[str, Any]) -> None:
    save_json(STATE_FILE, state)


# =========================
# 포트폴리오 요약
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
# 메인
# =========================
def main():
    print("🚀 BOT STARTED", now_str())
    telegram(f"🚀 봇 시작 | LIVE_TRADING={LIVE_TRADING}")

    positions = load_positions()

    daily_pnl_est = 0.0
    day_key = time.strftime("%Y-%m-%d")

    while True:
        try:
            if time.strftime("%Y-%m-%d") != day_key:
                day_key = time.strftime("%Y-%m-%d")
                daily_pnl_est = 0.0

            accounts = None
            balance_error = None
            portfolio_view = None

            if ACCESS and SECRET:
                try:
                    accounts = get_accounts()
                    portfolio_view = build_portfolio_view(accounts)
                except Exception as e:
                    balance_error = str(e)
            else:
                balance_error = "UPBIT_ACCESS/UPBIT_SECRET 환경변수가 없어요"

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
                continue

            # 강제청산
            if os.path.exists(FORCE_FILE):
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
                            order_sell_market(m, vol)
                        positions.pop(m, None)
                    save_positions(positions)
                else:
                    positions.clear()
                    save_positions(positions)
                clear_force_flag()

            watch_markets = get_top_krw_markets(TOP_N)
            market_view: Dict[str, Any] = {}

            # 1) 보유 포지션 관리
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
                    sell_reason = f"손절 {pnl_rate*100:.2f}%"
                elif pnl_rate >= TAKE_PROFIT:
                    sell_reason = f"익절 {pnl_rate*100:.2f}%"
                elif price <= p.peak_price * (1 - TRAIL_GAP):
                    sell_reason = f"트레일 {pnl_rate*100:.2f}%"
                elif r >= RSI_SELL:
                    sell_reason = f"RSI 매도 {r:.1f}"

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
                    telegram(f"🔻 매도 {m} | {sell_reason}")
                    if not can_trade_live():
                        daily_pnl_est += pnl_rate * KRW_PER_TRADE
                        positions.pop(m, None)
                        save_positions(positions)
                    else:
                        if accounts is None:
                            accounts = get_accounts()
                        coin = m.split("-")[1]
                        vol = 0.0
                        for a in accounts:
                            if a.get("currency") == coin:
                                vol = float(a.get("balance", 0))
                                break
                        if vol > 0:
                            order_sell_market(m, vol)
                        daily_pnl_est += pnl_rate * KRW_PER_TRADE
                        positions.pop(m, None)
                        save_positions(positions)

            # 2) 신규 진입
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

                    if r < RSI_BUY and near_lower and trend_ok:
                        telegram(f"🟢 매수 시도 {m} | rsi={r:.1f}")
                        if not can_trade_live():
                            fake_vol = KRW_PER_TRADE / price
                            positions[m] = Position(m, price, fake_vol, price, now_str())
                            save_positions(positions)
                        else:
                            # 실거래: KRW 잔고 체크
                            if accounts is None:
                                accounts = get_accounts()
                                portfolio_view = build_portfolio_view(accounts)
                            krw_bal = (portfolio_view or {}).get("krw_balance", 0) if portfolio_view else 0

                            if krw_bal >= max(MIN_ORDER_KRW, KRW_PER_TRADE):
                                order_buy_krw(m, KRW_PER_TRADE)
                                time.sleep(1.0)
                                # 체결 수량 정확화는 고급 기능(주문 조회/체결 조회)에서 처리 가능
                                positions[m] = Position(
                                    market=m,
                                    entry_price=price,
                                    volume=KRW_PER_TRADE / price,
                                    peak_price=price,
                                    entry_time=now_str(),
                                )
                                save_positions(positions)
                            else:
                                telegram(f"⚠️ KRW 잔고 부족: {krw_bal:.0f} KRW")
                                market_view[m]["note"] = "KRW 부족"

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
            err = f"❌ 봇 에러: {e}"
            print(err)
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

