import os
import time
import json
import uuid
import hashlib
import threading
from datetime import datetime, date
from typing import Dict, Any, List, Optional, Tuple

import requests
import jwt

# =========================
# 파일
# =========================
STATE_FILE = "state.json"
POSITIONS_FILE = "positions.json"
TRADES_LOG = "trades.jsonl"
RISK_FILE = "risk.json"

ARM_FILE = "armed.flag"
PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"


# =========================
# 환경변수
# =========================
UPBIT_ACCESS = os.getenv("UPBIT_ACCESS", "").strip()
UPBIT_SECRET = os.getenv("UPBIT_SECRET", "").strip()

LIVE_TRADING = os.getenv("LIVE_TRADING", "0").strip().lower() in ("1", "true", "yes", "y")

PORT = int(os.getenv("PORT", "8080"))

# 루프
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "10"))

# 봉 설정
CANDLE_MIN = int(os.getenv("CANDLE_MIN", "5"))
CANDLE_COUNT = int(os.getenv("CANDLE_COUNT", "200"))

# 자동선정 (스코어 상위)
TOP_N = int(os.getenv("TOP_N", "8"))            # 후보 풀에서 상위 N개
TRADE_UNIVERSE = int(os.getenv("TRADE_UNIVERSE", "25"))  # 1차 후보: 거래대금 상위 K개

# 포지션/금액
TRADE_KRW = int(os.getenv("TRADE_KRW", "10000"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))

# 분할
SPLIT_BUY = int(os.getenv("SPLIT_BUY", "2"))
SPLIT_SELL = int(os.getenv("SPLIT_SELL", "2"))

# 기본 RSI + 볼린저
RSI_BUY = float(os.getenv("RSI_BUY", "30"))
RSI_SELL = float(os.getenv("RSI_SELL", "60"))
BB_K = float(os.getenv("BB_K", "2.0"))

# 고급 전략 필터
TREND_SMA_FAST = int(os.getenv("TREND_SMA_FAST", "20"))   # 추세필터 fast
TREND_SMA_SLOW = int(os.getenv("TREND_SMA_SLOW", "60"))   # 추세필터 slow
VOLUME_RATIO_MIN = float(os.getenv("VOLUME_RATIO_MIN", "1.2"))  # 최근 거래량이 평균 대비 최소 배수
MACD_CONFIRM = os.getenv("MACD_CONFIRM", "1").strip().lower() in ("1", "true", "yes", "y")

# 손절/익절
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))  # +3%
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.0"))      # -2%

# 재진입 쿨다운
REENTRY_COOLDOWN_SEC = int(os.getenv("REENTRY_COOLDOWN_SEC", str(30 * 60)))

# 일일 손실 제한(추정)
DAILY_LOSS_LIMIT_KRW = int(os.getenv("DAILY_LOSS_LIMIT_KRW", "30000"))

# 폭락 보호 (BTC 기준)
CRASH_MARKET = os.getenv("CRASH_MARKET", "KRW-BTC").strip()
CRASH_LOOKBACK_MIN = int(os.getenv("CRASH_LOOKBACK_MIN", "60"))
CRASH_THRESHOLD_PCT = float(os.getenv("CRASH_THRESHOLD_PCT", "3.0"))

# 제외 마켓
EXCLUDE_MARKETS = set(m.strip() for m in os.getenv("EXCLUDE_MARKETS", "").split(",") if m.strip())


# =========================
# HTTP
# =========================
S = requests.Session()
S.headers.update({"User-Agent": "mybot/4.0"})


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
    return LIVE_TRADING and is_armed() and (not is_paused()) and bool(UPBIT_ACCESS) and bool(UPBIT_SECRET)


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
    return mid - k * sd, mid, mid + k * sd


def ema(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = values[-n]
    for v in values[-n + 1:]:
        e = v * k + e * (1 - k)
    return e


def macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(closes) < slow + signal + 5:
        return None, None, None

    macd_line_series = []
    for i in range(slow, len(closes) + 1):
        window = closes[:i]
        ef = ema(window, fast)
        es = ema(window, slow)
        if ef is None or es is None:
            continue
        macd_line_series.append(ef - es)

    if len(macd_line_series) < signal:
        return None, None, None

    signal_line = ema(macd_line_series, signal)
    macd_line = macd_line_series[-1]
    if signal_line is None:
        return None, None, None

    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def volume_ratio(volumes: List[float], n: int = 20) -> Optional[float]:
    if len(volumes) < n + 1:
        return None
    avg = sum(volumes[-n-1:-1]) / n
    if avg <= 0:
        return None
    return volumes[-1] / avg


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
    if not markets:
        return []
    return upbit_public("https://api.upbit.com/v1/ticker", {"markets": ",".join(markets)})


def get_candles(market: str, minutes: int, count: int) -> List[dict]:
    return upbit_public(f"https://api.upbit.com/v1/candles/minutes/{minutes}", {"market": market, "count": count})


def get_price(market: str) -> float:
    data = get_tickers([market])
    return float(data[0]["trade_price"])


def top_by_trade_value(k: int) -> List[str]:
    mk = get_all_krw_markets()
    tickers = get_tickers(mk)
    tickers_sorted = sorted(tickers, key=lambda x: x.get("acc_trade_price_24h", 0), reverse=True)
    return [t["market"] for t in tickers_sorted[:k]]


# =========================
# Upbit Private (주문)
# =========================
def make_auth_headers(query: Optional[dict] = None) -> dict:
    payload = {"access_key": UPBIT_ACCESS, "nonce": str(uuid.uuid4())}
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
# 상태/리스크/포지션
# =========================
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
        set_flag(PAUSE_FILE, True)


def cooldown_ok(market: str, positions: Dict[str, dict]) -> bool:
    last_ts = float(positions.get(market, {}).get("last_action_ts", 0) or 0)
    return (time.time() - last_ts) >= REENTRY_COOLDOWN_SEC


# =========================
# 폭락 보호
# =========================
def crash_protection_triggered() -> Tuple[bool, str]:
    try:
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


# =========================
# 1) “AI식” 코인 필터(스코어링)
#    - 거래대금 상위 K에서
#    - 추세/모멘텀/거래량/변동성 기반 점수 계산
# =========================
def score_market(market: str) -> Tuple[float, dict]:
    cs = get_candles(market, CANDLE_MIN, CANDLE_COUNT)
    closes = [float(c["trade_price"]) for c in reversed(cs)]
    vols = [float(c["candle_acc_trade_volume"]) for c in reversed(cs)]

    p = closes[-1]
    r = rsi(closes, 14)
    low, mid, up = bollinger(closes, 20, BB_K)

    sma_fast = sma(closes, TREND_SMA_FAST)
    sma_slow = sma(closes, TREND_SMA_SLOW)

    vr = volume_ratio(vols, 20)
    macd_line, signal_line, hist = macd(closes)

    # 스코어(0~100 느낌으로)
    score = 0.0

    # 1) 추세: fast > slow면 가산, 아니면 감산
    if sma_fast is not None and sma_slow is not None:
        if sma_fast > sma_slow:
            score += 20
        else:
            score -= 10

    # 2) RSI: 너무 과열이면 감점, 적당한 모멘텀은 가산
    if r is not None:
        if 40 <= r <= 65:
            score += 20
        elif r > 75:
            score -= 15
        elif r < 25:
            score += 5  # 과매도 반등 후보(조금)

    # 3) 볼린저 위치: mid 위면 추세, 하단 근처면 반등 후보
    if low is not None and mid is not None and up is not None:
        if p >= mid:
            score += 10
        if p <= low * 1.01:
            score += 8

    # 4) 거래량 급증
    if vr is not None:
        if vr >= 1.8:
            score += 18
        elif vr >= 1.2:
            score += 10
        else:
            score -= 3

    # 5) MACD 히스토그램 상승(확인 옵션)
    if hist is not None:
        if hist > 0:
            score += 10
        else:
            score -= 5

    # 안정성(데이터 부족이면 큰 감점)
    if r is None or sma_fast is None or sma_slow is None or vr is None:
        score -= 30

    features = {
        "price": p,
        "rsi": r,
        "bb_lower": low, "bb_mid": mid, "bb_upper": up,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "volume_ratio": vr,
        "macd_hist": hist
    }
    return score, features


def pick_markets() -> Tuple[List[str], Dict[str, Any]]:
    # 1) 거래대금 상위 K를 후보로
    universe = top_by_trade_value(TRADE_UNIVERSE)

    scored = []
    features_map = {}
    for m in universe:
        try:
            sc, feats = score_market(m)
            scored.append((m, sc))
            features_map[m] = {"score": round(sc, 2), **feats}
        except Exception as e:
            features_map[m] = {"score": -999, "error": str(e)}

    scored.sort(key=lambda x: x[1], reverse=True)
    selected = [m for m, _ in scored[:TOP_N]]
    return selected, features_map


# =========================
# 3) 고급 전략(진입/청산)
#    - 기본 RSI+BB + 추세필터 + 거래량필터 + (옵션) MACD 확인
# =========================
def entry_signal(feats: dict) -> bool:
    p = feats["price"]
    r = feats.get("rsi")
    low = feats.get("bb_lower")
    sma_fast = feats.get("sma_fast")
    sma_slow = feats.get("sma_slow")
    vr = feats.get("volume_ratio")
    hist = feats.get("macd_hist")

    # 필수 데이터 없으면 False
    if r is None or low is None or sma_fast is None or sma_slow is None or vr is None:
        return False

    # 거래량 필터
    if vr < VOLUME_RATIO_MIN:
        return False

    # 추세 필터 (추세가 너무 약하면 진입 제한)
    trend_ok = sma_fast >= sma_slow * 0.995  # 완전 하락장만 제한 (살짝 완화)
    if not trend_ok:
        return False

    # 기본 진입: 과매도 + 하단 근처
    base = (r <= RSI_BUY) and (p <= low * 1.01)

    # MACD 확인(옵션)
    if MACD_CONFIRM:
        if hist is None:
            return False
        # 히스토그램이 너무 음수면 반등 확인 전이라 제한
        macd_ok = hist >= -0.0001
        return base and macd_ok

    return base


def exit_signal(feats: dict, pos: dict) -> Tuple[bool, str]:
    p = feats["price"]
    r = feats.get("rsi")

    entry = float(pos["entry"])
    pnl_pct = (p - entry) / entry * 100.0

    if pnl_pct >= TAKE_PROFIT_PCT:
        return True, f"take_profit({pnl_pct:.2f}%)"
    if pnl_pct <= -STOP_LOSS_PCT:
        return True, f"stop_loss({pnl_pct:.2f}%)"
    if r is not None and r >= RSI_SELL:
        return True, f"rsi_sell({r:.2f})"

    return False, "hold"


# =========================
# 2) 실시간 PnL 계산(잔고조회 없이 봇 추정)
# =========================
def calc_pnl(positions: Dict[str, dict]) -> dict:
    unrealized_krw = 0.0
    unrealized_pct = 0.0
    detail = {}

    total_cost = 0.0
    for m, pos in positions.items():
        try:
            p = get_price(m)
            entry = float(pos["entry"])
            qty = float(pos["qty"])
            cost = entry * qty
            value = p * qty
            pnl = value - cost
            pnlp = (p - entry) / entry * 100.0 if entry > 0 else 0.0

            detail[m] = {
                "price": p,
                "entry": entry,
                "qty": qty,
                "cost_est": cost,
                "value_est": value,
                "pnl_krw_est": pnl,
                "pnl_pct": round(pnlp, 2),
            }

            unrealized_krw += pnl
            total_cost += cost
        except Exception as e:
            detail[m] = {"error": str(e)}

    if total_cost > 0:
        unrealized_pct = unrealized_krw / total_cost * 100.0

    return {
        "unrealized_pnl_krw_est": unrealized_krw,
        "unrealized_pnl_pct_est": round(unrealized_pct, 2),
        "positions_detail": detail
    }


# =========================
# 주문 실행
# =========================
def do_split_buy(market: str, price: float, positions: Dict[str, dict]) -> Tuple[Optional[dict], Optional[str]]:
    if count_positions(positions) >= MAX_POSITIONS:
        return None, "max_positions_reached"
    if not cooldown_ok(market, positions):
        return None, "cooldown_active"

    per = int(TRADE_KRW / max(1, SPLIT_BUY))
    est_qty_total = 0.0
    resp_list = []

    if can_trade_live():
        for _ in range(SPLIT_BUY):
            resp = place_market_buy(market, per)
            err = parse_error(resp)
            if err:
                return None, f"BUY_FAIL:{err}"
            resp_list.append(resp)
            est_qty_total += per / price
            time.sleep(1)
        mode = "LIVE"
    else:
        for _ in range(SPLIT_BUY):
            est_qty_total += per / price
            time.sleep(0.1)
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
    pnl_krw_est = (price - entry) * qty

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
        time.sleep(0.1)
        mode = "PAPER"

    positions.pop(market, None)
    save_positions(positions)

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
# 상태 저장
# =========================
def save_state(
    message: str,
    selected_markets: List[str],
    features_map: Dict[str, Any],
    markets_state: Dict[str, Any],
    positions: Dict[str, Any],
    risk: dict,
    pnl: dict,
    last_trade: Optional[dict],
    last_error: Optional[str],
    crash_info: Optional[str] = None,
):
    state = {
        "time": now_str(),
        "message": message,
        "paused": is_paused(),
        "armed": is_armed(),
        "live_trading": LIVE_TRADING,
        "can_trade_live": can_trade_live() and (not risk.get("blocked", False)),
        "crash_info": crash_info,
        "selected_markets": selected_markets,        # 1) 선정 결과
        "features": features_map,                    # 1) 스코어/피처
        "markets": markets_state,                    # 3) 신호/상태
        "positions": positions,                      # 봇 추적 포지션
        "pnl": pnl,                                  # 2) 실시간 PnL
        "risk": risk,
        "last_trade": last_trade,
        "last_error": last_error,
    }
    write_json(STATE_FILE, state)


# =========================
# 봇
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
        save_state("✅ 봇 시작", [], {}, {}, load_positions(), load_risk(), calc_pnl(load_positions()), None, None)

        while not self._stop.is_set():
            try:
                positions = load_positions()
                risk = load_risk()
                risk_block_if_needed(risk)

                # PAUSE
                if is_paused():
                    pnl = calc_pnl(positions)
                    save_state("⏸ 일시정지", [], {}, {}, positions, risk, pnl, self.last_trade, self.last_error)
                    time.sleep(3)
                    continue

                # 폭락 보호
                crashed, crash_msg = crash_protection_triggered()
                if crashed:
                    set_flag(PAUSE_FILE, True)
                    self.last_error = f"CRASH_PROTECT: {crash_msg}"
                    pnl = calc_pnl(positions)
                    save_state("⛔ 폭락 보호 발동 → 자동 PAUSE", [], {}, {}, positions, risk, pnl, self.last_trade, self.last_error, crash_info=crash_msg)
                    time.sleep(5)
                    continue

                # Force Sell
                if flag_exists(FORCE_FILE):
                    res = force_sell_all(positions, risk)
                    self.last_trade = {"time": now_str(), "type": "FORCE_SELL_ALL", "result": res}
                    positions = load_positions()
                    risk = load_risk()
                    pnl = calc_pnl(positions)
                    save_state("🔴 Force Sell All 실행", [], {}, {}, positions, risk, pnl, self.last_trade, self.last_error, crash_info=crash_msg)
                    time.sleep(2)
                    continue

                # Arm off: 관찰
                if not is_armed():
                    pnl = calc_pnl(positions)
                    save_state("✅ 실행중(Arm 꺼짐 → 관찰만)", [], {}, {}, positions, risk, pnl, self.last_trade, self.last_error, crash_info=crash_msg)
                    time.sleep(INTERVAL_SEC)
                    continue

                # 리스크 차단
                if risk.get("blocked"):
                    pnl = calc_pnl(positions)
                    save_state(f"⛔ 리스크 차단: {risk.get('reason')}", [], {}, {}, positions, risk, pnl, self.last_trade, self.last_error, crash_info=crash_msg)
                    time.sleep(INTERVAL_SEC)
                    continue

                # 1) 코인 선정(스코어링)
                selected, features_map = pick_markets()

                # 3) 신호 계산 + 주문 실행
                markets_state: Dict[str, Any] = {}
                actions: List[Tuple[str, str, float, str]] = []  # (market, side, price, reason)

                for m in selected:
                    feats = features_map.get(m, {})
                    if "price" not in feats:
                        continue

                    # 포지션 보유 여부
                    has_pos = m in positions

                    # 진입 신호
                    if (not has_pos) and entry_signal(feats):
                        # 쿨다운 체크
                        if cooldown_ok(m, positions):
                            actions.append((m, "buy", float(feats["price"]), "entry_signal"))
                            markets_state[m] = {"note": "buy_signal", **feats}
                        else:
                            markets_state[m] = {"note": "cooldown", **feats}
                        continue

                    # 청산 신호
                    if has_pos:
                        ok, reason = exit_signal(feats, positions[m])
                        if ok:
                            actions.append((m, "sell", float(feats["price"]), reason))
                            markets_state[m] = {"note": f"sell_signal:{reason}", **feats, "pos": positions[m]}
                        else:
                            markets_state[m] = {"note": "hold", **feats, "pos": positions[m]}
                    else:
                        markets_state[m] = {"note": "watch", **feats}

                # 실행(매수/매도)
                # 매수는 MAX_POSITIONS 고려해야 해서 순서대로
                for m, side, p, reason in actions:
                    if side == "buy":
                        if count_positions(positions) >= MAX_POSITIONS:
                            continue
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

                # 2) 실시간 PnL 계산
                positions = load_positions()
                pnl = calc_pnl(positions)
                risk = load_risk()

                msg = "✅ 실행중"
                msg += " (실거래 가능)" if can_trade_live() else " (모의/제한 상태)"

                save_state(msg, selected, features_map, markets_state, positions, risk, pnl, self.last_trade, self.last_error, crash_info=crash_msg)
                time.sleep(INTERVAL_SEC)

            except Exception as e:
                self.last_error = str(e)
                positions = load_positions()
                risk = load_risk()
                pnl = calc_pnl(positions)
                save_state(f"❌ BOT ERROR: {e}", [], {}, {}, positions, risk, pnl, self.last_trade, self.last_error)
                time.sleep(5)
