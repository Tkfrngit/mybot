import os
import time
import json
import uuid
import hashlib
import requests
import jwt
from datetime import datetime

# =========================
# 설정
# =========================
STATE_FILE = "state.json"
POSITIONS_FILE = "positions.json"

ARM_FILE = "armed.flag"
PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"

# 매매 설정(잔고 몰라도 안전하게)
MARKETS = os.getenv("MARKETS", "KRW-BTC,KRW-ETH,KRW-XRP").split(",")
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "10"))         # 루프 주기
CANDLE_MIN = int(os.getenv("CANDLE_MIN", "5"))              # 5분봉
TRADE_KRW = int(os.getenv("TRADE_KRW", "10000"))            # 매수 금액(고정)
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))        # 동시에 잡을 최대 포지션 수

# 매매 조건(예시: RSI + 볼린저)
RSI_BUY = float(os.getenv("RSI_BUY", "30"))
RSI_SELL = float(os.getenv("RSI_SELL", "60"))
BB_K = float(os.getenv("BB_K", "2.0"))                      # 볼린저 표준편차 배수

# 손절/익절(포지션 기록 기준)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))  # +3%
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.0"))      # -2%

# 실거래 스위치
LIVE_TRADING = os.getenv("LIVE_TRADING", "0").strip().lower() in ("1", "true", "yes", "y")

# 업비트 키
ACCESS = os.getenv("UPBIT_ACCESS", "").strip()
SECRET = os.getenv("UPBIT_SECRET", "").strip()

S = requests.Session()
S.headers.update({"User-Agent": "mybot/1.0"})


# =========================
# 유틸
# =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_armed():
    return os.path.exists(ARM_FILE)


def is_paused():
    return os.path.exists(PAUSE_FILE)


def force_sell_requested():
    return os.path.exists(FORCE_FILE)


def clear_force_sell_flag():
    if os.path.exists(FORCE_FILE):
        os.remove(FORCE_FILE)


def can_trade_live():
    # 실거래는 LIVE_TRADING=1 + Arm + 키 존재가 기본 조건
    return LIVE_TRADING and is_armed() and bool(ACCESS) and bool(SECRET)


# =========================
# 지표 계산
# =========================
def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def std(values, n):
    if len(values) < n:
        return None
    m = sma(values, n)
    var = sum((x - m) ** 2 for x in values[-n:]) / n
    return var ** 0.5


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-n, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger(closes, n=20, k=2.0):
    if len(closes) < n:
        return None, None, None
    mid = sma(closes, n)
    sd = std(closes, n)
    if mid is None or sd is None:
        return None, None, None
    upper = mid + k * sd
    lower = mid - k * sd
    return lower, mid, upper


# =========================
# 업비트 API (Public)
# =========================
def upbit_public(url, params=None):
    r = S.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def get_candles(market, minutes=5, count=200):
    return upbit_public(
        "https://api.upbit.com/v1/candles/minutes/%d" % minutes,
        {"market": market, "count": count},
    )


def get_price(market):
    data = upbit_public("https://api.upbit.com/v1/ticker", {"markets": market})
    return float(data[0]["trade_price"])


# =========================
# 업비트 API (Private: 주문)
# =========================
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


def upbit_private_post(path, query):
    headers = make_auth_headers(query)
    r = S.post(f"https://api.upbit.com{path}", data=query, headers=headers, timeout=15)
    # 업비트는 에러도 json으로 옴
    try:
        return r.json()
    except Exception:
        return {"error": {"name": "non_json", "message": r.text}}


def parse_error(resp):
    if isinstance(resp, dict) and "error" in resp:
        return resp["error"]
    return None


def place_market_buy(market, krw_amount):
    # 시장가 매수 (금액지정)
    query = {
        "market": market,
        "side": "bid",
        "price": str(krw_amount),
        "ord_type": "price",
    }
    return upbit_private_post("/v1/orders", query)


def place_market_sell(market, volume):
    # 시장가 매도 (수량지정)
    query = {
        "market": market,
        "side": "ask",
        "volume": str(volume),
        "ord_type": "market",
    }
    return upbit_private_post("/v1/orders", query)


# =========================
# 포지션(봇 자체 기록)
# =========================
def load_positions():
    # 예: {"KRW-BTC": {"qty":0.001,"entry":90000000,"time":"..."}, ...}
    return read_json(POSITIONS_FILE, {})


def save_positions(p):
    write_json(POSITIONS_FILE, p)


def count_positions(p):
    return sum(1 for v in p.values() if v and float(v.get("qty", 0)) > 0)


# =========================
# 상태 저장 (대시보드가 읽음)
# =========================
def save_state(message, markets_state, positions_state, balance_error=None):
    state = {
        "time": now_str(),
        "message": message,
        "armed": is_armed(),
        "paused": is_paused(),
        "live_trading": LIVE_TRADING,
        "can_trade_live": can_trade_live(),
        "balance_error": balance_error,   # 잔고조회 안하지만, 주문/인증 에러를 여기 표시
        "portfolio": None,               # 잔고조회 없음
        "markets": markets_state,
        "positions": positions_state,
    }
    write_json(STATE_FILE, state)


# =========================
# 매매 로직
# =========================
def decision_for_market(market, positions):
    candles = get_candles(market, minutes=CANDLE_MIN, count=200)
    closes = [float(c["trade_price"]) for c in reversed(candles)]  # 오래된→최신

    price = closes[-1]
    r = rsi(closes, 14)
    lower, mid, upper = bollinger(closes, 20, BB_K)

    pos = positions.get(market)

    # 포지션 없으면 매수 판단
    if not pos:
        note = "watch"
        can_buy = (
            r is not None and lower is not None and
            r <= RSI_BUY and
            price <= lower * 1.01  # 하단 근처
        )
        if can_buy:
            note = "buy_signal"
        return {
            "price": price,
            "rsi": r,
            "bb_lower": lower, "bb_mid": mid, "bb_upper": upper,
            "position": False,
            "note": note
        }

    # 포지션 있으면 매도 판단(익절/손절/RSI)
    entry = float(pos["entry"])
    qty = float(pos["qty"])
    pnl_pct = (price - entry) / entry * 100.0

    note = "hold"
    sell_reason = None

    if pnl_pct >= TAKE_PROFIT_PCT:
        sell_reason = f"take_profit({pnl_pct:.2f}%)"
    elif pnl_pct <= -STOP_LOSS_PCT:
        sell_reason = f"stop_loss({pnl_pct:.2f}%)"
    elif r is not None and r >= RSI_SELL:
        sell_reason = f"rsi_sell({r:.2f})"

    if sell_reason:
        note = f"sell_signal:{sell_reason}"

    return {
        "price": price,
        "rsi": r,
        "bb_lower": lower, "bb_mid": mid, "bb_upper": upper,
        "position": True,
        "entry": entry,
        "qty": qty,
        "pnl_pct": round(pnl_pct, 2),
        "note": note
    }


def try_buy(market, price, positions):
    if count_positions(positions) >= MAX_POSITIONS:
        return "max_positions_reached"

    if can_trade_live():
        resp = place_market_buy(market, TRADE_KRW)
        err = parse_error(resp)
        if err:
            return f"BUY_FAIL: {err}"
        # 잔고 조회 안 하므로, 체결 수량을 정확히 모름 → 보수적으로 “추정 qty” 기록
        est_qty = TRADE_KRW / price
        positions[market] = {"qty": est_qty, "entry": price, "time": now_str()}
        save_positions(positions)
        return "BUY_OK(live_estimated_qty)"
    else:
        # 모의매매
        est_qty = TRADE_KRW / price
        positions[market] = {"qty": est_qty, "entry": price, "time": now_str()}
        save_positions(positions)
        return "BUY_OK(paper)"


def try_sell(market, price, positions):
    pos = positions.get(market)
    if not pos:
        return "no_position"

    qty = float(pos["qty"])

    if can_trade_live():
        resp = place_market_sell(market, qty)
        err = parse_error(resp)
        if err:
            return f"SELL_FAIL: {err}"
        positions.pop(market, None)
        save_positions(positions)
        return "SELL_OK(live)"
    else:
        positions.pop(market, None)
        save_positions(positions)
        return "SELL_OK(paper)"


def force_sell_all(positions, markets_state):
    results = {}
    for market in list(positions.keys()):
        try:
            price = float(markets_state.get(market, {}).get("price") or get_price(market))
            results[market] = try_sell(market, price, positions)
        except Exception as e:
            results[market] = f"FORCE_SELL_ERROR: {e}"
    clear_force_sell_flag()
    return results


# =========================
# 메인 루프
# =========================
def main():
    print("BOT START", now_str())
    positions = load_positions()

    while True:
        try:
            if is_paused():
                save_state("⏸ 일시정지 중", {}, positions, balance_error=None)
                time.sleep(3)
                continue

            markets_state = {}
            balance_error = None

            # 강제청산
            if force_sell_requested():
                res = force_sell_all(positions, markets_state)
                save_state(f"🔴 Force Sell 실행: {res}", markets_state, positions)
                time.sleep(2)
                continue

            # 마켓별 상태 업데이트 + 신호 체크
            actions = []
            for market in MARKETS:
                market = market.strip()
                if not market:
                    continue

                info = decision_for_market(market, positions)
                markets_state[market] = info

                # 신호 실행은 Arm 상태에서만(안전장치)
                if not is_armed():
                    continue

                # 매수 신호
                if info.get("note") == "buy_signal" and not positions.get(market):
                    actions.append((market, "buy", info["price"]))

                # 매도 신호
                if isinstance(info.get("note"), str) and info["note"].startswith("sell_signal") and positions.get(market):
                    actions.append((market, "sell", info["price"]))

            # 실제 액션 수행
            for market, side, price in actions:
                if side == "buy":
                    result = try_buy(market, price, positions)
                    # 주문이 IP 인증 문제면 여기서 드러남
                    if "no_authorization_ip" in result:
                        balance_error = result
                else:
                    result = try_sell(market, price, positions)
                    if "no_authorization_ip" in result:
                        balance_error = result

            # 상태 저장
            msg = "✅ 실행중"
            if not (ACCESS and SECRET):
                balance_error = "UPBIT_ACCESS/UPBIT_SECRET 환경변수가 없어요"
            elif LIVE_TRADING and not is_armed():
                msg = "✅ 실행중 (LIVE_TRADING=1 이지만 Arm 안됨 → 주문 안함)"
            elif can_trade_live():
                msg = "✅ 실행중 (실거래 가능)"
            else:
                msg = "✅ 실행중 (모의/제한 상태: Arm 또는 IP 인증 필요)"

            save_state(msg, markets_state, positions, balance_error=balance_error)

            time.sleep(INTERVAL_SEC)

        except Exception as e:
            save_state(f"❌ BOT ERROR: {e}", {}, positions, balance_error=str(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
