import os
import json
import sqlite3
import threading
import subprocess
from flask import Flask, jsonify, redirect, send_file, Response

app = Flask(__name__)

STATE_FILE = "state.json"
DB_FILE = "trades.db"

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
            "daily_pnl_est_krw": 0,
            "markets": {},
            "positions": {}
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"message": f"state.json 읽기 실패: {e}", "markets": {}, "positions": {}, "portfolio": None}


def db_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_last_events(limit=100):
    if not os.path.exists(DB_FILE):
        return []
    conn = db_conn()
    rows = conn.execute(
        "SELECT ts, level, message, data FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "ts": r["ts"],
            "level": r["level"],
            "message": r["message"],
            "data": r["data"],
        })
    return out


def db_last_trades(limit=200):
    if not os.path.exists(DB_FILE):
        return []
    conn = db_conn()
    rows = conn.execute(
        "SELECT ts, mode, market, side, qty, price, krw, fee, reason, order_uuid, realized_pnl_krw "
        "FROM trades ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({k: r[k] for k in r.keys()})
    return out


def db_pnl_series(limit=2000):
    """
    누적 실현손익(SELL에서 계산된 realized_pnl_krw 합)
    """
    if not os.path.exists(DB_FILE):
        return {"labels": [], "values": []}

    conn = db_conn()
    rows = conn.execute(
        "SELECT ts, COALESCE(realized_pnl_krw, 0) AS rp FROM trades ORDER BY id ASC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    labels = []
    values = []
    cum = 0.0
    for r in rows:
        rp = float(r["rp"] or 0)
        cum += rp
        labels.append(r["ts"])
        values.append(round(cum, 0))
    return {"labels": labels, "values": values}


def portfolio_table(p):
    if not p:
        return "<p>포트폴리오 정보 없음</p>"

    rows = p.get("portfolio", [])
    html = f"""
    <p><b>KRW 잔고:</b> {p.get("krw_balance")}</p>
    <p><b>총 매입(추정):</b> {p.get("total_cost_krw")}</p>
    <p><b>총 평가:</b> {p.get("total_eval_krw")}</p>
    <p><b>총 평가손익:</b> {p.get("total_pnl_krw")}</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">
      <tr>
        <th>마켓</th><th>수량</th><th>평단</th><th>현재가</th><th>평가금액</th><th>평가손익</th><th>수익률%</th>
      </tr>
    """
    for r in rows:
        html += f"""
        <tr>
          <td>{r.get("market")}</td>
          <td>{r.get("qty")}</td>
          <td>{r.get("avg_buy_price")}</td>
          <td>{r.get("price")}</td>
          <td>{r.get("eval_krw")}</td>
          <td>{r.get("pnl_krw")}</td>
          <td>{r.get("pnl_rate")}</td>
        </tr>
        """
    html += "</table>"
    return html


def trades_table(trades):
    if not trades:
        return "<p>거래 로그 없음</p>"
    html = """
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; font-size:12px;">
      <tr>
        <th>시간</th><th>MODE</th><th>마켓</th><th>BUY/SELL</th>
        <th>수량</th><th>가격</th><th>금액</th><th>수수료</th><th>실현손익</th><th>사유</th>
      </tr>
    """
    for t in trades:
        html += f"""
        <tr>
          <td>{t.get("ts")}</td>
          <td>{t.get("mode")}</td>
          <td>{t.get("market")}</td>
          <td>{t.get("side")}</td>
          <td>{t.get("qty")}</td>
          <td>{t.get("price")}</td>
          <td>{t.get("krw")}</td>
          <td>{t.get("fee")}</td>
          <td>{t.get("realized_pnl_krw")}</td>
          <td>{(t.get("reason") or "")[:80]}</td>
        </tr>
        """
    html += "</table>"
    return html


def events_table(events):
    if not events:
        return "<p>이벤트 로그 없음</p>"
    html = """
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; font-size:12px;">
      <tr><th>시간</th><th>레벨</th><th>메시지</th><th>데이터</th></tr>
    """
    for e in events:
        html += f"""
        <tr>
          <td>{e.get("ts")}</td>
          <td>{e.get("level")}</td>
          <td>{e.get("message")}</td>
          <td style="max-width:520px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{e.get("data") or ""}</td>
        </tr>
        """
    html += "</table>"
    return html


@app.route("/")
def home():
    s = load_state()
    armed = os.path.exists(ARM_FILE)
    paused = os.path.exists(PAUSE_FILE)

    trades = db_last_trades(100)
    events = db_last_events(50)

    html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <meta http-equiv="refresh" content="5">
      <title>Auto Trading Dashboard</title>
      <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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
        <a href="/download_db"><button style="padding:8px 12px;">⬇ DB 다운로드</button></a>
      </div>

      <hr/>

      <h2>📈 누적 실현손익(SELL 기준)</h2>
      <canvas id="pnlChart" width="900" height="280"></canvas>
      <script>
        async function drawPnl() {{
          const res = await fetch('/api/pnl_series');
          const data = await res.json();
          const ctx = document.getElementById('pnlChart').getContext('2d');
          new Chart(ctx, {{
            type: 'line',
            data: {{
              labels: data.labels,
              datasets: [{{ label: 'Cumulative Realized PnL (KRW)', data: data.values }}]
            }},
            options: {{
              responsive: true,
              plugins: {{ legend: {{ display: true }} }},
              scales: {{ x: {{ display: false }} }}
            }}
          }});
        }}
        drawPnl();
      </script>

      <hr/>

      <h2>💰 포트폴리오(업비트 계좌 기준)</h2>
      {portfolio_table(s.get("portfolio"))}

      <hr/>
      <h2>📌 봇 추적 포지션</h2>
      <pre style="background:#f5f5f5; padding:10px;">{json.dumps(s.get("positions", {}), ensure_ascii=False, indent=2)}</pre>

      <hr/>
      <h2>🪙 마켓 상태</h2>
      <pre style="background:#f5f5f5; padding:10px; max-height:240px; overflow:auto;">{json.dumps(s.get("markets", {}), ensure_ascii=False, indent=2)}</pre>

      <hr/>
      <h2>🧾 최근 거래(로그)</h2>
      {trades_table(trades)}

      <hr/>
      <h2>🛠 최근 이벤트(디버깅)</h2>
      {events_table(events)}

      <p>
        <a href="/json">/json</a> |
        <a href="/api/trades">/api/trades</a> |
        <a href="/api/events">/api/events</a>
      </p>
    </body>
    </html>
    """
    return html


@app.route("/json")
def json_view():
    return jsonify(load_state())


@app.route("/api/trades")
def api_trades():
    return jsonify(db_last_trades(300))


@app.route("/api/events")
def api_events():
    return jsonify(db_last_events(300))


@app.route("/api/pnl_series")
def api_pnl_series():
    return jsonify(db_pnl_series(2000))


@app.route("/download_db")
def download_db():
    if not os.path.exists(DB_FILE):
        return Response("DB 파일이 아직 없어요", status=404)
    return send_file(DB_FILE, as_attachment=True)


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


def start_bot_once():
    """
    Railway에서 gunicorn이 여러 worker면 봇이 여러 번 켜질 수 있음.
    그래서 파일 플래그로 1회만 실행.
    """
    if os.path.exists(BOT_STARTED_FLAG):
        return
    open(BOT_STARTED_FLAG, "w").close()
    subprocess.Popen(["python", "bot.py"])


@app.before_request
def _boot():
    # 첫 요청 들어오면 봇 시작
    start_bot_once()


if __name__ == "__main__":
    # 로컬/단독 실행용
    start_bot_once()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
