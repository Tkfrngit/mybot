import os
import json
import requests
from flask import Flask, jsonify, redirect
from bot import TradingBot, STATE_FILE, ARM_FILE, PAUSE_FILE, FORCE_FILE, set_flag, flag_exists

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

    # 안전하게 dict 보장
    risk = s.get("risk") if isinstance(s.get("risk"), dict) else {}
    last_trade = s.get("last_trade")
    last_error = s.get("last_error")

    html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <meta http-equiv="refresh" content="5">
      <title>Auto Trading Dashboard</title>
      <style>
        body {{ font-family: Arial; padding: 20px; }}
        .box {{ background:#f5f5f5; padding:10px; border-radius:8px; }}
        button {{ padding:8px 12px; margin-right:8px; }}
        .danger {{ background:red; color:white; }}
        .warn {{ background:crimson; color:white; }}
        .ok {{ background:seagreen; color:white; }}
        pre {{ white-space: pre-wrap; word-break: break-word; }}
      </style>
    </head>
    <body>
      <h1>📊 Auto Trading Dashboard</h1>

      <p><b>시간:</b> {s.get("time")}</p>
      <p><b>상태:</b> {s.get("message")}</p>

      <p><b>PAUSED:</b> {s.get("paused")}</p>
      <p><b>ARMED:</b> {s.get("armed")}</p>
      <p><b>LIVE_TRADING(환경변수):</b> {s.get("live_trading")}</p>
      <p><b>실거래 가능(can_trade_live):</b> {s.get("can_trade_live")}</p>

      <hr/>

      <div>
        <a href="/pause"><button>⏸ Pause</button></a>
        <a href="/resume"><button>▶ Resume</button></a>
        <a href="/arm"><button class="ok">✅ Arm</button></a>
        <a href="/disarm"><button>🧪 Disarm</button></a>
        <a href="/force"><button class="danger">🔴 Force Sell All</button></a>
        <a href="/ip"><button>🌐 서버 IP 확인</button></a>
      </div>

      <hr/>

      <h2>🛡 리스크 상태</h2>
      <div class="box">
        <pre>{json.dumps(risk, ensure_ascii=False, indent=2)}</pre>
      </div>

      <h2>🧾 최근 거래</h2>
      <div class="box">
        <pre>{json.dumps(last_trade, ensure_ascii=False, indent=2)}</pre>
      </div>

      <h2>⚠️ 최근 에러</h2>
      <div class="box">
        <pre>{json.dumps(last_error, ensure_ascii=False, indent=2)}</pre>
      </div>

      <hr/>

      <h2>📌 봇 포지션(잔고조회 없이 봇이 추적)</h2>
      <div class="box">
        <pre>{json.dumps(s.get("positions", {}), ensure_ascii=False, indent=2)}</pre>
      </div>

      <hr/>

      <h2>🪙 마켓 상태(자동선정 TOP)</h2>
      <div class="box" style="max-height:360px; overflow:auto;">
        <pre>{json.dumps(s.get("markets", {}), ensure_ascii=False, indent=2)}</pre>
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
