import json
import os
import subprocess
import threading
from flask import Flask, jsonify

app = Flask(__name__)

STATE_FILE = "state.json"


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "balance": None,
            "markets": {},
            "message": "봇 상태 수신 대기중",
            "time": None,
            "total_pnl": None
        }

    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {
            "balance": None,
            "markets": {},
            "message": "state 파일 오류",
            "time": None,
            "total_pnl": None
        }


@app.route("/")
def home():
    s = load_state()

    html = f"""
    <html>
    <head>
    <title>Auto Trading Dashboard</title>
    </head>

    <body style="font-family:Arial;background:#111;color:white;padding:40px">

    <h1>🚀 Auto Trading Bot</h1>

    <h2>상태</h2>
    <p>{s.get("message")}</p>

    <h2>잔고</h2>
    <p>{s.get("balance")}</p>

    <h2>총 수익</h2>
    <p>{s.get("total_pnl")}</p>

    <h2>코인 상태</h2>
    <pre>{json.dumps(s.get("markets"), indent=2)}</pre>

    <p>업데이트 시간: {s.get("time")}</p>

    </body>
    </html>
    """

    return html


@app.route("/json")
def api():
    return jsonify(load_state())


def start_bot():
    print("BOT STARTING...")
    subprocess.Popen(["python", "bot.py"])


if __name__ == "__main__":

    # bot 실행
    threading.Thread(target=start_bot).start()

    # Railway 포트 대응
    port = int(os.environ.get("PORT", 8080))

    app.run(host="0.0.0.0", port=port)
