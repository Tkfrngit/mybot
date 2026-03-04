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

BOT_STARTED = False


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "time": None,
            "message": "봇 상태 수신 대기중",
            "markets": {},
            "positions": {}
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"message": "state 읽기 실패"}


# ==============================
# BOT 자동 실행
# ==============================
def start_bot():
    global BOT_STARTED

    if BOT_STARTED:
        return

    BOT_STARTED = True

    try:
        subprocess.Popen(["python", "bot.py"])
        print("BOT STARTED")
    except Exception as e:
        print("BOT START FAILED:", e)


@app.before_request
def boot():
    start_bot()


# ==============================
# 메인 대시보드
# ==============================
@app.route("/")
def home():

    s = load_state()

    armed = os.path.exists(ARM_FILE)
    paused = os.path.exists(PAUSE_FILE)

    html = f"""
    <html>
    <head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="5">
    </head>

    <body style="font-family:Arial;padding:30px;">

    <h1>📊 Auto Trading Dashboard</h1>

    <p><b>시간:</b> {s.get("time")}</p>

    <p><b>상태:</b> {s.get("message")}</p>

    <p><b>PAUSED:</b> {paused}</p>
    <p><b>ARMED:</b> {armed}</p>

    <br>

    <a href="/pause"><button>⏸ Pause</button></a>
    <a href="/resume"><button>▶ Resume</button></a>

    <a href="/arm"><button style="background:red;color:white;">Arm</button></a>
    <a href="/disarm"><button>Disarm</button></a>

    <a href="/force"><button style="background:black;color:white;">Force Sell All</button></a>

    <hr>

    <h2>📌 봇 포지션</h2>

    <pre>{json.dumps(s.get("positions", {}), indent=2, ensure_ascii=False)}</pre>

    <hr>

    <h2>📊 마켓 상태</h2>

    <pre>{json.dumps(s.get("markets", {}), indent=2, ensure_ascii=False)}</pre>

    <hr>

    <p>
    <a href="/json">JSON 상태 보기</a>
    </p>

    </body>
    </html>
    """

    return html


# ==============================
# JSON 상태
# ==============================
@app.route("/json")
def json_state():
    return jsonify(load_state())


# ==============================
# Pause
# ==============================
@app.route("/pause")
def pause():

    open(PAUSE_FILE, "w").close()

    return redirect("/")


# ==============================
# Resume
# ==============================
@app.route("/resume")
def resume():

    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)

    return redirect("/")


# ==============================
# Arm
# ==============================
@app.route("/arm")
def arm():

    open(ARM_FILE, "w").close()

    return redirect("/")


# ==============================
# Disarm
# ==============================
@app.route("/disarm")
def disarm():

    if os.path.exists(ARM_FILE):
        os.remove(ARM_FILE)

    return redirect("/")


# ==============================
# Force Sell
# ==============================
@app.route("/force")
def force():

    open(FORCE_FILE, "w").close()

    return redirect("/")


# ==============================
# 서버 IP 확인 기능
# ==============================
@app.route("/ip")
def get_server_ip():

    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text
        return f"Server IP: {ip}"

    except Exception as e:
        return f"IP 확인 실패: {e}"


# ==============================
# 실행
# ==============================
if __name__ == "__main__":

    start_bot()

    port = int(os.environ.get("PORT", 8080))

    app.run(host="0.0.0.0", port=port)
