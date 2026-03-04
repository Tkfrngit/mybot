import os
import time
import json
import uuid
import hashlib
import requests
import jwt
from datetime import datetime, date

STATE_FILE = "state.json"
POSITIONS_FILE = "positions.json"          # 봇 포지션 기록(잔고조회 없어도 운영)
TRADES_LOG = "trades.jsonl"                # 거래 로그(줄단위 JSON)
RISK_FILE = "risk.json"                    # 일일 손익/리스크 상태

ARM_FILE = "armed.flag"
PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"

# ====== 환경설정 ======
MARKETS = [m.strip() for m in os.getenv("MARKETS", "KRW-BTC,KRW-ETH,KRW-XRP").split(",") if m.strip()]
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "10"))
CANDLE_MIN = int(os.getenv("CANDLE_MIN", "5"))

TRADE_KRW = int(os.getenv("TRADE_KRW", "10000"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))

RSI_BUY = float(os.getenv("RSI_BUY", "30"))
RSI_SELL = float(os.getenv("RSI_SELL", "60"))
BB_K = float(os.getenv("BB_K", "2.0"))

TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.0"))

# 재진입 쿨다운(초)
REENTRY_COOLDOWN_SEC = int(os.getenv("REENTRY_COOLDOWN_SEC", str(30 * 60)))  # 기본 30분

# ✅ 실전 필수: 하루 손실 제한(원 단위)
DAILY_LOSS_LIMIT_KRW = int(os.getenv("DAILY_LOSS_LIMIT_KRW", "30000"))       # 기본 -3만원이면 정지

LIVE_TRADING = os.getenv("LIVE_TRADING", "0").strip().lower() in ("1", "true", "yes", "y")

ACCESS = os.getenv("UPBIT_ACCESS", "").strip()
SECRET = os.getenv("UPBIT_SECRET", "").strip()

S = requests.Session()
S.headers.update({"User-Agent": "mybot/2.0"})


# ====== 유틸 ======
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


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def is_armed():
    return os.path.exists(ARM_FILE)


def is_paused():
    return os.path.exists(PAUSE_FILE)


def set_paused(flag: bool):
    if flag:
        open(PAUSE_FILE, "w").close()
    else:
        if os.path.exists(PAUSE_FILE):
            os.remove(PAUSE_FILE)


def force_sell_requested():
    return os.path.exists(FORCE_FILE)


def clear_force_sell_flag():
    if os.path.exists(FORCE_FILE):
        os.remove(FORCE_FILE)


def can_trade_live():
    return LIVE_TRADING and is_armed() and bool(ACCESS) and bool(SECRET) and (not is_paused())


# ====== 지표 ======
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


# ====== Upbit Public ======
def upbit_public(url, params=None):
    r = S.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def get_candles(market, minutes=5, count=200):
    return upbit_public(
        f"https://api.upbit.com/v1/candles/minutes/{minutes}",
        {"market": market, "count": count},
    )


def get_price(market):
    data = upbit_public("https://api.upbit.com/v1/ticker", {"markets": market})
    return float(data[0]["trade_price"])


# ====== Upbit Private (주문) ======
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
    try:
        return r.json()
    except Exception:
        return {"error": {"name": "non_json", "message": r.text}}


def parse_error(resp):
    if isinstance(resp, dict) and "error" in resp:
        return resp["error"]
    return None


def place_market_buy(market, krw_amount):
    query = {"market": market, "side": "bid", "price": str(krw_amount), "ord_type": "price"}
    return upbit_private_post("/v1/orders", query)


def place_market_sell(market, volume):
    query = {"market": market, "side": "ask", "volume": str(volume), "ord_type": "market"}
    return upbit_private_post("/v1/orders", query)


# ====== 포지션 / 리스크 ======
def load_positions():
    # { "KRW-BTC": {"qty":0.001,"entry":90000000,"time":"...","last_action_ts":...}, ... }
    return read_json(POSITIONS_FILE, {})


def save_positions(p):
    write_json(POSITIONS_FILE, p)


def count_positions(p):
    return sum(1 for v in p.values() if v and float(v.get("qty", 0)) > 0)


def load_risk():
    # {"date":"YYYY-MM-DD","realized_pnl_krw":0,"blocked":false,"reason":null}
    r = read_json(RISK_FILE, None)
    today = str(date.today())
    if not r or r.get("date") != today:
        r = {"date": today, "realized_pnl_krw": 0.0, "blocked": False, "reason": None}
        write_json(RISK_FILE, r)
    return r


def save_risk(r):
    write_json(RISK_FILE, r)


def risk_block_if_needed(risk):
    if risk["blocked"]:
        return True
    if risk["realized_pnl_krw"] <= -abs(DAILY_LOSS_LIMIT_KRW):
        risk["blocked"] = True
        risk["reason"] = f"Daily loss limit reached: {risk['realized_pnl_krw']:.0f} KRW"
        save_risk(risk)
        set_paused(True)  # 자동 정지
        return True
    return False


# ====== 상태 저장 ======
def save_state(message, markets_state, positions_state, risk_state, last_trade=None, last_error=None):
    state = {
        "time": now_str(),
        "message": message,
        "paused": is_paused(),
        "armed": is_armed(),
        "live_trading": LIVE_TRADING,
        "can_trade_live": can_trade_live() and (not risk_state.get("blocked", False)),
        "risk": risk_state,
        "last_trade": last_trade,
        "last_error": last_error,
        "portfolio": None,          # 잔고조회 없음
        "markets": markets_state,
        "positions": positions_state,
    }
    write_json(STATE_FILE, state)


# ====== 전략 판단 ======
def decision_for_market(market, positions):
    candles = get_candles(market, minutes=CANDLE_MIN, count=200)
    closes = [float(c["trade_price"]) for c in reversed(candles)]  # 오래된→최신

    price = closes[-1]
    r = rsi(closes, 14)
    lower, mid, upper = bollinger(closes, 20, BB_K)

    pos = positions.get(market)

    if not pos:
        note = "watch"
        can_buy = (
            r is not None and lower is not None and
            r <= RSI_BUY and
            price <= lower * 1.01
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

    entry = float(pos["entry"])
    qty = float(pos["qty"])
    pnl_pct = (price - entry) / entry * 100.0

    note = "hold"
    reason = None

    if pnl_pct >= TAKE_PROFIT_PCT:
        reason = f"take_profit({pnl_pct:.2f}%)"
    elif pnl_pct <= -STOP_LOSS_PCT:
        reason = f"stop_loss({pnl_pct:.2f}%)"
    elif r is not None and r >= RSI_SELL:
        reason = f"rsi_sell({r:.2f})"

    if reason:
        note = f"sell_signal:{reason}"

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


def cooldown_ok(market, positions):
    last_ts = float(positions.get(market, {}).get("last_action_ts", 0) or 0)
    return (time.time() - last_ts) >= REENTRY_COOLDOWN_SEC


def mark_action_ts(market, positions):
    if market not in positions:
        positions[market] = {}
    positions[market]["last_action_ts"] = time.time()


# ====== 매매 실행 ======
def do_buy(market, price, positions):
    if count_positions(positions) >= MAX_POSITIONS:
        return None, None, "max_positions_reached"

    if not cooldown_ok(market, positions):
        return None, None, "cooldown_active"

    if can_trade_live():
        resp = place_market_buy(market, TRADE_KRW)
        err = parse_error(resp)
        if err:
            return None, None, f"BUY_FAIL:{err}"
        est_qty = TRADE_KRW / price
        positions[market] = {"qty": est_qty, "entry": price, "time": now_str(), "last_action_ts": time.time()}
        save_positions(positions)

        trade = {"time": now_str(), "market": market, "side": "BUY", "mode": "LIVE", "krw": TRADE_KRW,
                 "price": price, "est_qty": est_qty, "resp": resp}
        append_jsonl(TRADES_LOG, trade)
        return trade, None, None
    else:
        est_qty = TRADE_KRW / price
        positions[market] = {"qty": est_qty, "entry": price, "time": now_str(), "last_action_ts": time.time()}
        save_positions(positions)

        trade = {"time": now_str(), "market": market, "side": "BUY", "mode": "PAPER", "krw": TRADE_KRW,
                 "price": price, "est_qty": est_qty}
        append_jsonl(TRADES_LOG, trade)
        return trade, None, None


def do_sell(market, price, positions, risk, reason=""):
    pos = positions.get(market)
    if not pos:
        return None, None, "no_position"

    entry = float(pos["entry"])
    qty = float(pos["qty"])
    pnl_krw_est = (price - entry) * qty  # 추정 손익(잔고조회 없이)

    if can_trade_live():
        resp = place_market_sell(market, qty)
        err = parse_error(resp)
        if err:
            return None, None, f"SELL_FAIL:{err}"

        # 포지션 제거
        positions.pop(market, None)
        save_positions(positions)

        # 리스크(일일 실현손익) 업데이트(추정)
        risk["realized_pnl_krw"] = float(risk.get("realized_pnl_krw", 0.0)) + pnl_krw_est
        save_risk(risk)

        trade = {"time": now_str(), "market": market, "side": "SELL", "mode": "LIVE",
                 "price": price, "qty": qty, "entry": entry, "pnl_krw_est": pnl_krw_est, "reason": reason,
                 "resp": resp}
        append_jsonl(TRADES_LOG, trade)
        return trade, pnl_krw_est, None
    else:
        positions.pop(market, None)
        save_positions(positions)

        risk["realized_pnl_krw"] = float(risk.get("realized_pnl_krw", 0.0)) + pnl_krw_est
        save_risk(risk)

        trade = {"time": now_str(), "market": market, "side": "SELL", "mode": "PAPER",
                 "price": price, "qty": qty, "entry": entry, "pnl_krw_est": pnl_krw_est, "reason": reason}
        append_jsonl(TRADES_LOG, trade)
        return trade, pnl_krw_est, None


def force_sell_all(positions, risk, markets_state):
    results = {}
    for market in list(positions.keys()):
        try:
            price = float(markets_state.get(market, {}).get("price") or get_price(market))
            trade, pnl, err = do_sell(market, price, positions, risk, reason="FORCE_SELL")
            results[market] = {"trade": trade, "pnl_krw_est": pnl, "err": err}
        except Exception as e:
            results[market] = {"err": str(e)}
    clear_force_sell_flag()
    return results


def main():
    print("BOT START", now_str())
    positions = load_positions()
    risk = load_risk()

    last_trade = None
    last_error = None

    while True:
        try:
            risk = load_risk()
            risk_block_if_needed(risk)

            if is_paused():
                save_state("⏸ 일시정지 중", {}, positions, risk, last_trade=last_trade, last_error=last_error)
                time.sleep(3)
                continue

            markets_state = {}

            # 강제 청산
            if force_sell_requested():
                res = force_sell_all(positions, risk, markets_state)
                last_trade = {"time": now_str(), "type": "FORCE_SELL_ALL", "result": res}
                save_state("🔴 Force Sell 실행", markets_state, positions, risk, last_trade=last_trade, last_error=last_error)
                time.sleep(2)
                continue

            # Arm 아니면 관찰만
            if not is_armed():
                for market in MARKETS:
                    markets_state[market] = decision_for_market(market, positions)
                save_state("✅ 실행중 (Arm 꺼짐 → 거래 안함)", markets_state, positions, risk, last_trade=last_trade, last_error=last_error)
                time.sleep(INTERVAL_SEC)
                continue

            # 위험 차단이면 거래 안함
            if risk.get("blocked"):
                for market in MARKETS:
                    markets_state[market] = decision_for_market(market, positions)
                save_state(f"⛔ 리스크 차단: {risk.get('reason')}", markets_state, positions, risk, last_trade=last_trade, last_error=last_error)
                time.sleep(INTERVAL_SEC)
                continue

            # 마켓 상태 + 신호 실행
            actions = []
            for market in MARKETS:
                info = decision_for_market(market, positions)
                markets_state[market] = info

                # 매수 신호
                if info.get("note") == "buy_signal" and not positions.get(market):
                    actions.append((market, "buy", info["price"]))

                # 매도 신호
                if isinstance(info.get("note"), str) and info["note"].startswith("sell_signal") and positions.get(market):
                    actions.append((market, "sell", info["price"], info["note"]))

            for act in actions:
                if act[1] == "buy":
                    market, _, price = act
                    trade, _, err = do_buy(market, price, positions)
                    if err:
                        last_error = err
                    if trade:
                        last_trade = trade
                else:
                    market, _, price, note = act
                    reason = note.split(":", 1)[1] if ":" in note else note
                    trade, pnl, err = do_sell(market, price, positions, risk, reason=reason)
                    if err:
                        last_error = err
                    if trade:
                        last_trade = trade

            # 상태 메시지
            if can_trade_live():
                msg = "✅ 실행중 (실거래 가능)"
            else:
                msg = "✅ 실행중 (모의/제한 상태)"

            save_state(msg, markets_state, positions, risk, last_trade=last_trade, last_error=last_error)
            time.sleep(INTERVAL_SEC)

        except Exception as e:
            last_error = str(e)
            save_state(f"❌ BOT ERROR: {e}", {}, positions, load_risk(), last_trade=last_trade, last_error=last_error)
            time.sleep(5)


if __name__ == "__main__":
    main()
