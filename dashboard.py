import os
import json
import requests
from flask import Flask, jsonify, redirect

from bot import (
    TradingBot, STATE_FILE,
    ARM_FILE, PAUSE_FILE, FORCE_FILE,
    set_flag
)

app = Flask(__name__)
bot = TradingBot()
bot.start()


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"message": "state.json 없음(봇 시작 전)", "markets": {}, "positions": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"message": f"state 읽기 실패: {e}", "markets": {}, "positions": {}}


@app.route("/")
def home():
    s = load_state()

    risk = s.get("risk") if isinstance(s.get("risk"), dict) else {}
    pnl = s.get("pnl") if isinstance(s.get("pnl"), dict) else {}
    last_trade = s.get("last_trade")
    last_error = s.get("last_error")

    selected = s.get("selected_markets", [])
    feats = s.get("features", {})
    markets_state = s.get("markets", {})
    positions = s.get("positions", {})

    crash_info = s.get("crash_info")

    html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <meta http-equiv="refresh" content="5">
      <title>Auto Trading Dashboard</title>
      <style>
        body {{ font-family: Arial; padding: 20px; }}
        .box {{ background:#f5f5f5; padding:12px; border-radius:10px; margin-bottom:14px; }}
        button {{ padding:8px 12px; margin-right:8px; }}
        .danger {{ background:red; color:white; }}
        .ok {{ background:seagreen; color:white; }}
        pre {{ white-space: pre-wrap; word-break: break-word; }}
        .row {{ display:flex; gap:12px; flex-wrap: wrap; }}
        .card {{ flex:1; min-width: 320px; }}
      </style>
    </head>
    <body>
      <h1>📊 Auto Trading Dashboard</h1>

      <div class="box">
        <p><b>시간:</b> {s.get("time")}</p>
        <p><b>상태:</b> {s.get("message")}</p>
        <p><b>PAUSED:</b> {s.get("paused")} / <b>ARMED:</b> {s.get("armed")}</p>
        <p><b>LIVE_TRADING:</b> {s.get("live_trading")} / <b>can_trade_live:</b> {s.get("can_trade_live")}</p>
        <p><b>CRASH INFO:</b> {crash_info}</p>

        <div style="margin-top:10px;">
          <a href="/pause"><button>⏸ Pause</button></a>
          <a href="/resume"><button>▶ Resume</button></a>
          <a href="/arm"><button class="ok">✅ Arm</button></a>
          <a href="/disarm"><button>🧪 Disarm</button></a>
          <a href="/force"><button class="danger">🔴 Force Sell All</button></a>
          <a href="/ip"><button>🌐 서버 IP 확인</button></a>
        </div>
      </div>

      <div class="row">
        <div class="box card">
          <h2>🧠 1) AI식 코인 선정(스코어링)</h2>
          <pre>{json.dumps(selected, ensure_ascii=False, indent=2)}</pre>
          <h3>상위 코인 스코어/피처</h3>
          <pre>{json.dumps({m:feats.get(m) for m in selected}, ensure_ascii=False, indent=2)}</pre>
        </div>

        <div class="box card">
          <h2>💰 2) 실시간 수익률(추정)</h2>
          <pre>{json.dumps(pnl, ensure_ascii=False, indent=2)}</pre>
        </div>
      </div>

      <div class="row">
        <div class="box card">
          <h2>🛡 리스크 상태</h2>
          <pre>{json.dumps(risk, ensure_ascii=False, indent=2)}</pre>
        </div>

        <div class="box card">
          <h2>🧾 최근 거래</h2>
          <pre>{json.dumps(last_trade, ensure_ascii=False, indent=2)}</pre>
          <h2>⚠️ 최근 에러</h2>
          <pre>{json.dumps(last_error, ensure_ascii=False, indent=2)}</pre>
        </div>
      </div>

      <div class="box">
        <h2>📌 봇 포지션(봇이 추적)</h2>
        <pre>{json.dumps(positions, ensure_ascii=False, indent=2)}</pre>
      </div>

      <div class="box">
        <h2>📈 3) 고급전략 신호/상태</h2>
        <pre>{json.dumps(markets_state, ensure_ascii=False, indent=2)}</pre>
      </div>

      <p><a href="/json">/json</a></p>
    </body>
    </html>
    """
    return html


@app.route("/json")
def json_view():
    return jsonify(load_state())


@app.route("/pause")
def pause():
    set_flag(PAUSE_FILE, True)
    return redirect("/")


@app.route("/resume")
def resume():
    set_flag(PAUSE_FILE, False)
    return redirect("/")


@app.route("/arm")
def arm():
    set_flag(ARM_FILE, True)
    return redirect("/")


@app.route("/disarm")
def disarm():
    set_flag(ARM_FILE, False)
    return redirect("/")


@app.route("/force")
def force():
    set_flag(FORCE_FILE, True)
    return redirect("/")


@app.route("/ip")
def get_server_ip():
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
        return f"Server Public IP: {ip}"
    except Exception as e:
        return f"IP 확인 실패: {e}"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
