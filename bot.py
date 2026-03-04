import os
import time
import json
import uuid
import hashlib
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, date
from typing import Dict, Any, Optional, Tuple, List

import requests
import jwt


# =========================
# 파일/플래그
# =========================
STATE_FILE = "state.json"
POSITIONS_FILE = "positions.json"
TRADES_LOG = "trades.jsonl"
RISK_FILE = "risk.json"

ARM_FILE = "armed.flag"
PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"


# =========================
# 환경변수 설정
# =========================
# 업비트 키
UPBIT_ACCESS = os.getenv("UPBIT_ACCESS", "").strip()
UPBIT_SECRET = os.getenv("UPBIT_SECRET", "").strip()

# 실거래 스위치 (1/true/yes)
LIVE_TRADING = os.getenv("LIVE_TRADING", "0").strip().lower() in ("1", "true", "yes", "y")

# 자동선정: 거래대금 상위 N개
TOP_N = int(os.getenv("TOP_N", "5"))
EXCLUDE_MARKETS = set(m.strip() for m in os.getenv("EXCLUDE_MARKETS", "").split(",") if m.strip())

# 루프/봉
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "10"))
CANDLE_MIN = int(os.getenv("CANDLE_MIN", "5"))
CANDLE_COUNT = int(os.getenv("CANDLE_COUNT", "200"))

# 매수금액/포지션
TRADE_KRW = int(os.getenv("TRADE_KRW", "10000"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))

# 분할
SPLIT_BUY = int(os.getenv("SPLIT_BUY", "2"))     # 2면 1만원 → 5천+5천
SPLIT_SELL = int(os.getenv("SPLIT_SELL", "2"))   # 2면 수량을 절반씩 매도

# 전략(기본: RSI + 볼린저)
RSI_BUY = float(os.getenv("RSI_BUY", "30"))
RSI_SELL = float(os.getenv("RSI_SELL", "60"))
BB_K = float(os.getenv("BB_K", "2.0"))

# 손절/익절 (%)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))   # +3%
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.0"))       # -2%

# 쿨다운(재진입 제한)
REENTRY_COOLDOWN_SEC = int(os.getenv("REENTRY_COOLDOWN_SEC", str(30 * 60)))  # 기본 30분

# 일일 손실 제한(원)
DAILY_LOSS_LIMIT_KRW = int(os.getenv("DAILY_LOSS_LIMIT_KRW", "30000"))       # -3만원이면 자동 PAUSE

# 폭락 보호 (BTC 기준)
CRASH_LOOKBACK_MIN = int(os.getenv("CRASH_LOOKBACK_MIN", "60"))  # 최근 60분
CRASH_THRESHOLD_PCT = float(os.getenv("CRASH_THRESHOLD_PCT", "3.0"))  # -3%면 전체 거래 중지 + PAUSE
CRASH_MARKET = os.getenv("CRASH_MARKET", "KRW-BTC").strip()


# =========================
# HTTP 세션
# =========================
S = requests.Session()
S.headers.update({"User-Agent": "mybot/3.0"})


# =========================
# 유틸
# =========================
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_jsonl(path: str, obj: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def flag_exists(path: str) -> bool:
    return os.path.exists(path)


def set_flag(path: str, on: bool):
    if on:
        open(path, "w").close()
    else:
        if os.path.exists(path):
            os.remove(path)


def is_armed() -> bool:
    return flag_exists(ARM_FILE)


def is_paused() -> bool:
    return flag_exists(PAUSE_FILE)


def can_trade_live() -> bool:
    # ✅ 실거래는 3개가 모두 True일 때만
    # 1) LIVE_TRADING=1
    # 2) ARMED on
    # 3) 키 존재
    # 4) PAUSE 아님
    return LIVE_TRADING and is_armed() and bool(UPBIT_ACCESS) and bool(UPBIT_SECRET) and (not is_paused())


# =========================
# 지표
# =========================
def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def std(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    m = sma(values, n)
    if m is None:
        return None
    var = sum((x - m) ** 2 for x in values[-n:]) / n
    return var ** 0.5


def rsi(closes: List[float], n: int = 14) -> Optional[float]:
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


def bollinger(closes: List[float], n: int = 20, k: float = 2.0) -> Tuple[Optional[float], Optional[float], Optional[float]]:
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
# Upbit Public
# =========================
def upbit_public(url: str, params: dict = None) -> Any:
    r = S.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def get_all_krw_markets() -> List[str]:
    data = upbit_public("https://api.upbit.com/v1/market/all")
    mk = [m["market"] for m in data if m["market"].startswith("KRW-")]
    mk = [m for m in mk if m not in EXCLUDE_MARKETS]
    return mk


def get_tickers(markets: List[str]) -> List[dict]:
    # 업비트는 markets를 콤마로
    if not markets:
        return []
    return upbit_public("https://api.upbit.com/v1/ticker", {"markets": ",".join(markets)})


def get_top_markets(top_n: int) -> List[str]:
    # 거래대금(24h) 기준 TOP N
    mk = get_all_krw_markets()
    # 너무 길면 요청이 커질 수 있어 → 여기서는 그냥 한번에 (대부분 ok)
    tickers = get_tickers(mk)
    tickers_sorted = sorted(tickers, key=lambda x: x.get("acc_trade_price_24h", 0), reverse=True)
    return [t["market"] for t in tickers_sorted[:top_n]]


def get_candles(market: str, minutes: int, count: int) -> List[dict]:
    return upbit_public(f"https://api.upbit.com/v1/candles/minutes/{minutes}", {"market": market, "count": count})


def get_price(market: str) -> float:
    data = get_tickers([market])
    return float(data[0]["trade_price"])


# =========================
# Upbit Private (주문)
# =========================
def make_auth_headers(query: Optional[dict] = None) -> dict:
    if not UPBIT_ACCESS or not UPBIT_SECRET:
        raise RuntimeError("UPBIT_ACCESS/UPBIT_SECRET 없음")

    payload = {
        "access_key": UPBIT_ACCESS,
        "nonce": str(uuid.uuid4()),
    }

    if query:
        query_string = "&".join([f"{k}={query[k]}" for k in sorted(query.keys())])
        m = hashlib.sha512()
        m.update(query_string.encode("utf-8"))
        payload["query_hash"] = m.hexdigest()
        payload["query_hash_alg"] = "SHA512"

    token = jwt.encode(payload, UPBIT_SECRET)
    return {"Authorization": f"Bearer {token}"}


def upbit_private_post(path: str, query: dict) -> dict:
    headers = make_auth_headers(query)
    r = S.post(f"https://api.upbit.com{path}", data=query, headers=headers, timeout=15)
    try:
        return r.json()
    except Exception:
        return {"error": {"name": "non_json", "message": r.text}}


def parse_error(resp: Any) -> Optional[dict]:
    if isinstance(resp, dict) and "error" in resp:
        return resp["error"]
    return None


def place_market_buy(market: str, krw_amount: int) -> dict:
    query = {"market": market, "side": "bid", "price": str(krw_amount), "ord_type": "price"}
    return upbit_private_post("/v1/orders", query)


def place_market_sell(market: str, volume: float) -> dict:
    query = {"market": market, "side": "ask", "volume": str(volume), "ord_type": "market"}
    return upbit_private_post("/v1/orders", query)


# =========================
# 데이터 구조
# =========================
@dataclass
class Position:
    entry: float
    qty: float
    time: str
    last_action_ts: float  # 쿨다운용


def load_positions() -> Dict[str, dict]:
    return read_json(POSITIONS_FILE, {})


def save_positions(p: Dict[str, dict]):
    write_json(POSITIONS_FILE, p)


def count_positions(p: Dict[str, dict]) -> int:
    return sum(1 for v in p.values() if v and float(v.get("qty", 0)) > 0)


def load_risk() -> dict:
    today = str(date.today())
    r = read_json(RISK_FILE, None)
    if not r or r.get("date") != today:
        r = {"date": today, "realized_pnl_krw_est": 0.0, "blocked": False, "reason": None}
        write_json(RISK_FILE, r)
    return r


def save_risk(r: dict):
    write_json(RISK_FILE, r)


def risk_block_if_needed(risk: dict):
    if risk.get("blocked"):
        return
    if float(risk.get("realized_pnl_krw_est", 0.0)) <= -abs(DAILY_LOSS_LIMIT_KRW):
        risk["blocked"] = True
        risk["reason"] = f"Daily loss limit reached: {risk['realized_pnl_krw_est']:.0f} KRW"
        save_risk(risk)
        set_flag(PAUSE_FILE, True)  # 자동 정지


# =========================
# 전략/보호 장치
# =========================
def crash_protection_triggered() -> Tuple[bool, str]:
    """
    BTC(또는 CRASH_MARKET)가 최근 LOOKBACK 동안 THRESHOLD% 이상 하락하면 True
    """
    try:
        # LOOKBACK_MIN 분 + 여유로 count
        count = max(10, int(CRASH_LOOKBACK_MIN / CANDLE_MIN) + 5)
        cs = get_candles(CRASH_MARKET, CANDLE_MIN, min(count, 200))
        closes = [float(c["trade_price"]) for c in reversed(cs)]
        if len(closes) < 2:
            return False, "not_enough_data"
        start = closes[0]
        end = closes[-1]
        pct = (end - start) / start * 100.0
        if pct <= -abs(CRASH_THRESHOLD_PCT):
            return True, f"{CRASH_MARKET} crash {pct:.2f}% over ~{CRASH_LOOKBACK_MIN}m"
        return False, f"{CRASH_MARKET} change {pct:.2f}%"
    except Exception as e:
        return False, f"crash_check_error:{e}"


def cooldown_ok(market: str, positions: Dict[str, dict]) -> bool:
    last_ts = float(positions.get(market, {}).get("last_action_ts", 0) or 0)
    return (time.time() - last_ts) >= REENTRY_COOLDOWN_SEC


# =========================
# 상태 저장
# =========================
def save_state(
    message: str,
    markets_state: Dict[str, Any],
    positions: Dict[str, Any],
    risk: dict,
    last_trade: Optional[dict] = None,
    last_error: Optional[str] = None,
):
    state = {
        "time": now_str(),
        "message": message,
        "paused": is_paused(),
        "armed": is_armed(),
        "live_trading": LIVE_TRADING,
        "can_trade_live": can_trade_live() and (not risk.get("blocked", False)),
        "risk": risk,
        "last_trade": last_trade,
        "last_error": last_error,
        "portfolio": None,  # 잔고조회는 안 씀(원하면 나중에 추가 가능)
        "markets": markets_state,
        "positions": positions,
    }
    write_json(STATE_FILE, state)


# =========================
# 매매 로직
# =========================
def analyze_market(market: str, positions: Dict[str, dict]) -> Dict[str, Any]:
    cs = get_candles(market, CANDLE_MIN, CANDLE_COUNT)
    closes = [float(c["trade_price"]) for c in reversed(cs)]

    price = closes[-1]
    rr = rsi(closes, 14)
    lower, mid, upper = bollinger(closes, 20, BB_K)

    pos = positions.get(market)
    if not pos:
        note = "watch"
        can_buy = (
            rr is not None and lower is not None and
            rr <= RSI_BUY and
            price <= lower * 1.01
        )
        if can_buy:
            note = "buy_signal"
        return {
            "price": price,
            "rsi": rr,
            "bb_lower": lower, "bb_mid": mid, "bb_upper": upper,
            "position": False,
            "note": note,
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
    elif rr is not None and rr >= RSI_SELL:
        reason = f"rsi_sell({rr:.2f})"

    if reason:
        note = f"sell_signal:{reason}"

    return {
        "price": price,
        "rsi": rr,
        "bb_lower": lower, "bb_mid": mid, "bb_upper": upper,
        "position": True,
        "entry": entry,
        "qty": qty,
        "pnl_pct": round(pnl_pct, 2),
        "note": note,
    }


def do_split_buy(market: str, price: float, positions: Dict[str, dict]) -> Tuple[Optional[dict], Optional[str]]:
    if count_positions(positions) >= MAX_POSITIONS:
        return None, "max_positions_reached"
    if not cooldown_ok(market, positions):
        return None, "cooldown_active"

    per = int(TRADE_KRW / max(1, SPLIT_BUY))
    est_qty_total = 0.0
    resp_list = []

    if can_trade_live():
        for i in range(SPLIT_BUY):
            resp = place_market_buy(market, per)
            err = parse_error(resp)
            if err:
                return None, f"BUY_FAIL:{err}"
            resp_list.append(resp)
            est_qty_total += per / price
            time.sleep(1)
        mode = "LIVE"
    else:
        # 모의
        for i in range(SPLIT_BUY):
            est_qty_total += per / price
            time.sleep(0.2)
        mode = "PAPER"

    positions[market] = {
        "entry": price,
        "qty": est_qty_total,
        "time": now_str(),
        "last_action_ts": time.time(),
    }
    save_positions(positions)

    trade = {
        "time": now_str(),
        "market": market,
        "side": "BUY",
        "mode": mode,
        "krw": TRADE_KRW,
        "price": price,
        "est_qty": est_qty_total,
        "responses": resp_list if resp_list else None,
    }
    append_jsonl(TRADES_LOG, trade)
    return trade, None


def do_split_sell(market: str, price: float, positions: Dict[str, dict], risk: dict, reason: str) -> Tuple[Optional[dict], Optional[str]]:
    pos = positions.get(market)
    if not pos:
        return None, "no_position"

    entry = float(pos["entry"])
    qty = float(pos["qty"])
    pnl_krw_est = (price - entry) * qty  # 추정(잔고조회 없이)

    resp_list = []

    if can_trade_live():
        part = qty / max(1, SPLIT_SELL)
        remaining = qty
        for i in range(SPLIT_SELL):
            vol = part if i < SPLIT_SELL - 1 else remaining
            resp = place_market_sell(market, vol)
            err = parse_error(resp)
            if err:
                return None, f"SELL_FAIL:{err}"
            resp_list.append(resp)
            remaining -= vol
            time.sleep(1)
        mode = "LIVE"
    else:
        time.sleep(0.2)
        mode = "PAPER"

    # 포지션 제거
    positions.pop(market, None)
    save_positions(positions)

    # 리스크 업데이트(추정 실현손익)
    risk["realized_pnl_krw_est"] = float(risk.get("realized_pnl_krw_est", 0.0)) + pnl_krw_est
    save_risk(risk)
    risk_block_if_needed(risk)

    trade = {
        "time": now_str(),
        "market": market,
        "side": "SELL",
        "mode": mode,
        "price": price,
        "qty": qty,
        "entry": entry,
        "pnl_krw_est": pnl_krw_est,
        "reason": reason,
        "responses": resp_list if resp_list else None,
    }
    append_jsonl(TRADES_LOG, trade)
    return trade, None


def force_sell_all(positions: Dict[str, dict], risk: dict) -> dict:
    results = {}
    for market in list(positions.keys()):
        try:
            p = get_price(market)
            trade, err = do_split_sell(market, p, positions, risk, reason="FORCE_SELL")
            results[market] = {"ok": trade is not None, "trade": trade, "err": err}
        except Exception as e:
            results[market] = {"ok": False, "err": str(e)}

    set_flag(FORCE_FILE, False)
    return results


# =========================
# Bot Runner (스레드)
# =========================
class TradingBot:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self.last_trade: Optional[dict] = None
        self.last_error: Optional[str] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def run(self):
        positions = load_positions()
        risk = load_risk()

        save_state("✅ 봇 시작", {}, positions, risk)

        while not self._stop.is_set():
            try:
                positions = load_positions()
                risk = load_risk()
                risk_block_if_needed(risk)

                # 1) 일시정지면 관찰만
                if is_paused():
                    # 시장 상태는 가볍게만
                    save_state("⏸ 일시정지 중", {}, positions, risk, self.last_trade, self.last_error)
                    time.sleep(3)
                    continue

                # 2) 폭락 보호
                crashed, crash_msg = crash_protection_triggered()
                if crashed:
                    # 자동 정지 + 안내
                    set_flag(PAUSE_FILE, True)
                    self.last_error = f"CRASH_PROTECT: {crash_msg}"
                    save_state(f"⛔ 폭락 보호 발동 → 자동 PAUSE", {}, positions, risk, self.last_trade, self.last_error)
                    time.sleep(5)
                    continue

                # 3) 강제청산
                if flag_exists(FORCE_FILE):
                    res = force_sell_all(positions, risk)
                    self.last_trade = {"time": now_str(), "type": "FORCE_SELL_ALL", "result": res}
                    save_state("🔴 Force Sell All 실행", {}, load_positions(), load_risk(), self.last_trade, self.last_error)
                    time.sleep(2)
                    continue

                # 4) Arm 아니면 관찰만
                if not is_armed():
                    save_state("✅ 실행중 (Arm 꺼짐 → 거래 안함)", {}, positions, risk, self.last_trade, self.last_error)
                    time.sleep(INTERVAL_SEC)
                    continue

                # 5) 일일 손실 제한 걸렸으면 거래 금지
                if risk.get("blocked"):
                    save_state(f"⛔ 리스크 차단: {risk.get('reason')}", {}, positions, risk, self.last_trade, self.last_error)
                    time.sleep(INTERVAL_SEC)
                    continue

                # 6) 자동 마켓 선정
                markets = get_top_markets(TOP_N)

                markets_state: Dict[str, Any] = {}
                actions = []  # (market, side, price, reason)

                for m in markets:
                    info = analyze_market(m, positions)
                    markets_state[m] = info

                    # 매수 신호
                    if info.get("note") == "buy_signal" and m not in positions:
                        actions.append((m, "buy", float(info["price"]), "buy_signal"))

                    # 매도 신호
                    if isinstance(info.get("note"), str) and info["note"].startswith("sell_signal") and m in positions:
                        reason = info["note"].split(":", 1)[1] if ":" in info["note"] else info["note"]
                        actions.append((m, "sell", float(info["price"]), reason))

                # 7) 실행
                for m, side, p, reason in actions:
                    if side == "buy":
                        trade, err = do_split_buy(m, p, positions)
                        if err:
                            self.last_error = err
                        if trade:
                            self.last_trade = trade
                    else:
                        trade, err = do_split_sell(m, p, positions, risk, reason=reason)
                        if err:
                            self.last_error = err
                        if trade:
                            self.last_trade = trade

                # 상태 메시지
                msg = "✅ 실행중"
                if can_trade_live():
                    msg += " (실거래 가능)"
                else:
                    msg += " (모의/제한 상태)"

                save_state(msg, markets_state, load_positions(), load_risk(), self.last_trade, self.last_error)
                time.sleep(INTERVAL_SEC)

            except Exception as e:
                self.last_error = str(e)
                save_state(f"❌ BOT ERROR: {e}", {}, load_positions(), load_risk(), self.last_trade, self.last_error)
                time.sleep(5)
