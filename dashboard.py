from flask import Flask, redirect, request, jsonify
import os
import time

PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"
ARM_FILE = "armed.flag"

app = Flask(__name__)

# ✅ 봇이 보내주는 최신 상태를 여기 저장 (메모리)
LATEST_STATE = {
    "time": None,
    "balance": None,
    "total_pnl": None,
    "markets": {},
    "message": "봇 상태 수신 대기중..."
}

@app.route("/ingest", methods=["POST"])
def ingest():
    global LATEST_STATE
    data = request.get_json(force=True, silent=True) or {}
    data["received_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    LATEST_STATE = data
    return jsonify({"ok": True})

@app.route("/")
def home():
    s = LATEST_STATE
    armed = os.path.exists(ARM_FILE)
    paused = os.path.exists(PAUSE_FILE)

    arm_text = "✅ 실매매 승인됨" if armed else "🧪 안전모드(실매매 OFF)"
    pause_text = "⏸ 일시정지 중" if paused else "▶ 실행 중"

    html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <meta http-equiv="refresh" content="5">
      <title>Auto Trading Dashboard</title>
    </head>
    <body style="font-family:Arial; padding:20px;">
      <h1>📊 자동매매 대시보드</h1>

      <p><b>상태:</b> {pause_text} / {arm_text}</p>
      <p><b>마지막 수신:</b> {s.get("received_at")}</p>
      <p><b>메시지:</b> {s.get("message","")}</p>

      <div style="margin:10px 0;">
        <a href="/pause"><button style="padding:8px 12px;">⏸ 일시정지</button></a>
        <a href="/resume"><button style="padding:8px 12px;">▶ 재개</button></a>
        <a href="/force"><button style="padding:8px 12px; background:red; color:white;">🔴 강제청산</button></a>
        <a href="/arm"><button style="padding:8px 12px; background:crimson; color:white;">실매매 승인</button></a>
        <a href="/disarm"><button style="padding:8px 12px;">안전모드</button></a>
      </div>

      <hr/>

      <h2>💰 잔고/손익</h2>
      <p><b>KRW 잔고:</b> {s.get("balance")}</p>
      <p><b>총 누적 손익:</b> {s.get("total_pnl")}</p>

      <hr/>
      <h2>🪙 코인 상태</h2>
    """

    markets = s.get("markets", {})
    if not markets:
        html += "<p>아직 봇 데이터가 없어요. bot 서비스가 /ingest로 보내는지 확인하세요.</p>"
    else:
        for market, data in markets.items():
            pnl = data.get("pnl", 0)
            color = "green" if pnl >= 0 else "red"
            html += f"""
            <div style="border:1px solid #ddd; padding:10px; margin:10px 0;">
              <b>{market}</b><br/>
              현재가: {data.get("price")}<br/>
              RSI: {data.get("rsi")}<br/>
              보유중: {data.get("position")}<br/>
              <span style="color:{color}; font-weight:bold;">누적손익: {pnl}</span>
            </div>
            """

    html += """
    </body></html>
    """
    return html

@app.route("/pause")
def pause():
    open(PAUSE_FILE, "w").close()
    return redirect("/")

@app.route("/resume")
def resume():
    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
    return redirect("/")

@app.route("/force")
def force():
    open(FORCE_FILE, "w").close()
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

@app.route("/json")
def json_view():
    return LATEST_STATE

if __name__ == "__main__":
    import subprocess
    import threading

    def run_bot():
        subprocess.Popen(["python", "bot.py"])

    threading.Thread(target=run_bot).start()

    app.run(host="0.0.0.0", port=8080)
