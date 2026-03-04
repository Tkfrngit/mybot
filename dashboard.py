from flask import Flask, jsonify, redirect, send_file, Response
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
        return {
            "time": None,
            "message": "봇 상태 수신 대기중",
            "armed": False,
            "live_trading": False,
            "can_trade_live": False,
            "balance_error": None,
            "portfolio": None,
            "daily_pnl_est_krw": None,
            "markets": {},
            "positions": {},
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"message": f"state 읽기 실패: {e}", "markets": {}, "positions": {}}


def start_bot_once():
    # gunicorn/재시작 등으로 대시보드 프로세스가 여러번 뜰 수 있어서
    # 파일 플래그로 봇은 1번만 실행
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

      <p><b>PAUSED:</b> {paused}</p>
      <p><b>ARMED:</b> {armed}</p>

      <p><b>LIVE_TRADING(환경변수):</b> {s.get("live_trading")}</p>
      <p><b>실거래 가능(can_trade_live):</b> {s.get("can_trade_live")}</p>

      <p><b>잔고 조회 에러:</b> {s.get("balance_error")}</p>

      <div style="display:flex; gap:8px; flex-wrap:wrap; margin:10px 0;">
        <a href="/pause"><button style="padding:8px 12px;">⏸ Pause</button></a>
        <a href="/resume"><button style="padding:8px 12px;">▶ Resume</button></a>
        <a href="/arm"><button style="padding:8px 12px; background:crimson; color:white;">✅ Arm</button></a>
        <a href="/disarm"><button style="padding:8px 12px;">🧪 Disarm</button></a>
        <a href="/force"><button style="padding:8px 12px; background:red; color:white;">🔴 Force Sell All</button></a>
        <a href="/ip"><button style="padding:8px 12px;">🌐 서버 IP 확인</button></a>
      </div>

      <hr/>

      <h2>💰 포트폴리오</h2>
      <pre style="background:#f5f5f5; padding:10px;">{json.dumps(s.get("portfolio", {}), ensure_ascii=False, indent=2)}</pre>

      <hr/>
      <h2>📌 봇 추적 포지션</h2>
      <pre style="background:#f5f5f5; padding:10px;">{json.dumps(s.get("positions", {}), ensure_ascii=False, indent=2)}</pre>

      <hr/>
      <h2>🪙 마켓 상태</h2>
      <pre style="background:#f5f5f5; padding:10px; max-height:240px; overflow:auto;">
{json.dumps(s.get("markets", {}), ensure_ascii=False, indent=2)}</pre>

      <p>
        <a href="/json">/json</a>
      </p>
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
    # ✅ Railway 서버가 밖으로 나갈 때 쓰는 “공인 IP” 확인
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
        return f"Server Public IP: {ip}"
    except Exception as e:
        return f"IP 확인 실패: {e}"


if __name__ == "__main__":
    start_bot_once()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
