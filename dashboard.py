from flask import Flask, jsonify, redirect
import os
import json
import requests
import subprocess

app = Flask(__name__)

STATE_FILE = "state.json"

ARM_FILE = "armed.flag"
PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"
BOT_STARTED_FLAG = "bot_started.flag"


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"message": "state.json 없음(봇 시작 전)", "markets": {}, "positions": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"message": f"state 읽기 실패: {e}", "markets": {}, "positions": {}}


def start_bot_once():
    if os.path.exists(BOT_STARTED_FLAG):
        return
    open(BOT_STARTED_FLAG, "w").close()
    try:
        subprocess.Popen(["python", "bot.py"])
        print("BOT STARTED")
    except Exception as e:
        print("BOT START FAILED:", e)


@app.before_request
def boot():
    start_bot_once()


@app.route("/")
def home():
    s = load_state()

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
        <a href="/arm"><button class="warn">✅ Arm</button></a>
        <a href="/disarm"><button>🧪 Disarm</button></a>
        <a href="/force"><button class="danger">🔴 Force Sell All</button></a>
        <a href="/ip"><button>🌐 서버 IP 확인</button></a>
      </div>

      <hr/>

      <h2>🛡 리스크 상태</h2>
      <div class="box">
        <pre>{json.dumps(s.get("risk", {}), ensure_ascii=False, indent=2)}</pre>
      </div>

      <h2>🧾 최근 거래</h2>
      <div class="box">
        <pre>{json.dumps(s.get("last_trade", {}), ensure_ascii=False, indent=2)}</pre>
      </div>

      <h2>⚠️ 최근 에러</h2>
      <div class="box">
        <pre>{json.dumps(s.get("last_error", None), ensure_ascii=False, indent=2)}</pre>
      </div>

      <hr/>

      <h2>📌 봇 포지션</h2>
      <div class="box">
        <pre>{json.dumps(s.get("positions", {}), ensure_ascii=False, indent=2)}</pre>
      </div>

      <hr/>

      <h2>🪙 마켓 상태</h2>
      <div class="box" style="max-height:320px; overflow:auto;">
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
    open(PAUSE_FILE, "w").close()
    return redirect("/")


@app.route("/resume")
def resume():
    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
    return redirect("/")


@app.route("/arm")
def arm():
    open(ARM_FILE, "w").close()
    return redirect("/")


@app.route("/disarm")
def disarm():
    if os.path.exists(ARM_FILE):
        os.remove(ARM_FILE)
    return redirect("/")


@app.route("/force")
def force():
    open(FORCE_FILE, "w").close()
    return redirect("/")


@app.route("/ip")
def get_server_ip():
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
        return f"Server Public IP: {ip}"
    except Exception as e:
        return f"IP 확인 실패: {e}"


if __name__ == "__main__":
    start_bot_once()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
