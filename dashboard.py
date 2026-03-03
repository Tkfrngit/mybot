from flask import Flask, redirect
import json
import os

STATE_FILE = "state.json"
HISTORY_FILE = "pnl_history.json"
ARM_FILE = "armed.flag"

app = Flask(__name__)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"markets": {}, "message": "state.json 없음 - bot.py가 아직 안 돌고 있어요"}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"markets": {}, "message": f"state.json 읽기 실패: {e}"}


@app.route("/")
def home():
    s = load(STATE_FILE)
    history = load(HISTORY_FILE) if os.path.exists(HISTORY_FILE) else []

    pnl_values = [h["pnl"] for h in history]
    labels = [h["time"] for h in history]

    armed = os.path.exists(ARM_FILE)

    return f"""
    <html>
    <head>
      <meta http-equiv="refresh" content="5">
      <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body style="font-family:Arial;padding:20px;">

    <h1>📊 고급 자동매매 대시보드</h1>

    <p><b>현재 KRW 잔고:</b> {s.get("balance",0)}</p>
    <p><b>총 누적 손익:</b> {s.get("total_pnl",0)}</p>

    <a href="/arm"><button style="background:red;color:white;">실매매 승인</button></a>
    <a href="/disarm"><button>안전모드</button></a>

    <hr/>

    <canvas id="chart" width="600" height="200"></canvas>

    <script>
    const ctx = document.getElementById('chart');
    new Chart(ctx, {{
        type: 'line',
        data: {{
            labels: {labels},
            datasets: [{{
                label: '누적 손익',
                data: {pnl_values},
                borderColor: 'blue',
                fill: false
            }}]
        }}
    }});
    </script>

    </body>
    </html>
    """


@app.route("/arm")
def arm():
    open(ARM_FILE, "w").close()
    return redirect("/")


@app.route("/disarm")
def disarm():
    if os.path.exists(ARM_FILE):
        os.remove(ARM_FILE)
    return redirect("/")


import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

