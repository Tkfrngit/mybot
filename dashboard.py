from flask import Flask, redirect
import json
import os

STATE_FILE = "state.json"
HISTORY_FILE = "pnl_history.json"  # 없으면 그냥 빈 그래프
PAUSE_FILE = "pause.flag"
FORCE_FILE = "force_sell.flag"
ARM_FILE = "armed.flag"

app = Flask(__name__)

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        # 파일이 깨졌거나 읽기 실패해도 페이지는 뜨게
        return default | {"_error": f"{path} 읽기 실패: {e}"} if isinstance(default, dict) else default

@app.route("/")
def home():
    s = load_json(STATE_FILE, {"markets": {}, "message": "state.json 없음 - bot.py가 아직 안 돌고 있어요"})
    history = load_json(HISTORY_FILE, [])

    armed = os.path.exists(ARM_FILE)
    paused = os.path.exists(PAUSE_FILE)

    # 그래프 데이터 (없으면 빈 배열)
    labels = [h.get("time") for h in history][-50:]
    pnl_values = [h.get("pnl") for h in history][-50:]

    # 버튼 상태 텍스트
    arm_text = "✅ 실매매 승인됨" if armed else "🧪 안전모드(실매매 OFF)"
    pause_text = "⏸ 일시정지 중" if paused else "▶ 실행 중"

    # 간단 HTML
    html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <meta http-equiv="refresh" content="5">
      <title>Auto Trading Dashboard</title>
      <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body style="font-family:Arial; padding:20px;">
      <h1>📊 자동매매 대시보드</h1>

      <p><b>상태:</b> {pause_text} / {arm_text}</p>
      <p><b>메시지:</b> {s.get("message","")}</p>
      {"<p style='color:red'><b>오류:</b> " + str(s.get("_error")) + "</p>" if s.get("_error") else ""}

      <div style="margin:10px 0;">
        <a href="/pause"><button style="padding:8px 12px;">⏸ 일시정지</button></a>
        <a href="/resume"><button style="padding:8px 12px;">▶ 재개</button></a>
        <a href="/force"><button style="padding:8px 12px; background:red; color:white;">🔴 강제청산</button></a>
        <a href="/arm"><button style="padding:8px 12px; background:crimson; color:white;">실매매 승인</button></a>
        <a href="/disarm"><button style="padding:8px 12px;">안전모드</button></a>
      </div>

      <hr/>

      <h2>💰 잔고/손익</h2>
      <p><b>KRW 잔고:</b> {s.get("balance","(bot에서 제공 안 함)")}</p>
      <p><b>총 누적 손익:</b> {s.get("total_pnl","(bot에서 제공 안 함)")}</p>

      <hr/>

      <h2>📈 손익 그래프</h2>
      <canvas id="chart" width="800" height="220"></canvas>

      <script>
        const labels = {labels};
        const data = {pnl_values};
        const ctx = document.getElementById('chart');

        new Chart(ctx, {{
          type: 'line',
          data: {{
            labels: labels,
            datasets: [{{
              label: '누적 손익',
              data: data,
              borderWidth: 2,
              fill: false
            }}]
          }},
          options: {{
            responsive: true,
            animation: false
          }}
        }});
      </script>

      <hr/>

      <h2>🪙 코인 상태</h2>
    """

    markets = s.get("markets", {})
    if not markets:
        html += "<p>표시할 코인이 없어요. bot.py가 state.json에 markets를 써주면 보입니다.</p>"
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
    </body>
    </html>
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
    return load_json(STATE_FILE, {"markets": {}, "message": "state.json 없음"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
