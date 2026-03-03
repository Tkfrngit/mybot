import json
import os
import subprocess
import threading
from flask import Flask, jsonify, redirect

app = Flask(__name__)

STATE_FILE = "state.json"
ARM_FILE = "armed.flag"
PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "time": None,
            "message": "봇 상태 수신 대기중",
            "armed": False,
            "dry_run": True,
            "balance_krw": None,
            "balance_error": None,
            "total_pnl_krw": 0,
            "markets": {},
            "positions": {}
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"message": f"state.json 읽기 실패: {e}", "markets": {}, "positions": {}}


@app.route("/")
def home():
    s = load_state()

    armed = os.path.exists(ARM_FILE)
    paused = os.path.exists(PAUSE_FILE)

    html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <meta http-equiv="refresh" content="5">
      <title>Auto Trading Dashboard</title>
    </head>
    <body style="font-family:Arial; padding:20px;">
      <h1>📊 Auto Trading Dashboard</h1>

      <p><b>시간:</b> {s.get("time")}</p>
      <p><b>상태:</b> {s.get("message")}</p>

      <p><b>DRY_RUN:</b> {s.get("dry_run")}</p>
      <p><b>ARMED(실매매 승인):</b> {armed}</p>
      <p><b>일시정지:</b> {paused}</p>

      <hr/>

      <div style="display:flex; gap:8px; flex-wrap:wrap;">
        <a href="/pause"><button style="padding:8px 12px;">⏸ Pause</button></a>
        <a href="/resume"><button style="padding:8px 12px;">▶ Resume</button></a>
        <a href="/arm"><button style="padding:8px 12px; background:crimson; color:white;">✅ Arm</button></a>
        <a href="/disarm"><button style="padding:8px 12px;">🧪 Disarm</button></a>
        <a href="/force"><button style="padding:8px 12px; background:red; color:white;">🔴 Force Sell All</button></a>
      </div>

      <hr/>

      <h2>💰 잔고/손익</h2>
      <p><b>KRW 잔고:</b> {s.get("balance_krw")}</p>
      <p><b>잔고 조회 에러:</b> {s.get("balance_error")}</p>
      <p><b>오늘 손익(추정):</b> {s.get("total_pnl_krw")}</p>

      <hr/>

      <h2>📌 보유 포지션</h2>
      <pre style="background:#f5f5f5; padding:10px;">{json.dumps(s.get("positions", {}), ensure_ascii=False, indent=2)}</pre>

      <hr/>

      <h2>🪙 마켓 상태</h2>
      <pre style="background:#f5f5f5; padding:10px;">{json.dumps(s.get("markets", {}), ensure_ascii=False, indent=2)}</pre>

      <p><a href="/json">/json 보기</a></p>
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


def start_bot():
    print("BOT STARTING...")
    subprocess.Popen(["python", "bot.py"])


if __name__ == "__main__":
    threading.Thread(target=start_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
