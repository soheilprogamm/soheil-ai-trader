import os, sqlite3, asyncio, logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional

import ccxt
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("soheil-trader")

DB_PATH = os.getenv("DB_PATH", "trader.db")
EXCHANGE_ID = os.getenv("EXCHANGE_ID", "binance")
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT").split(",") if s.strip()]
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
POLL_SECONDS = max(60, int(os.getenv("POLL_SECONDS", "300")))
START_BALANCE = float(os.getenv("START_BALANCE", "1000"))
RISK_PCT = min(2.0, max(0.1, float(os.getenv("RISK_PCT", "1")))) / 100
STOP_ATR = float(os.getenv("STOP_ATR", "1.5"))
TAKE_PROFIT_R = float(os.getenv("TAKE_PROFIT_R", "2.0"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))

exchange_class = getattr(ccxt, EXCHANGE_ID, None)
if exchange_class is None:
    raise RuntimeError(f"Unsupported exchange: {EXCHANGE_ID}")
exchange = exchange_class({"enableRateLimit": True, "timeout": 20000})

def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK(id=1), balance REAL NOT NULL,
            starting_balance REAL NOT NULL)""")
        c.execute("""INSERT OR IGNORE INTO state(id,balance,starting_balance)
            VALUES(1,?,?)""", (START_BALANCE, START_BALANCE))
        c.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            side TEXT NOT NULL, entry REAL NOT NULL, quantity REAL NOT NULL,
            stop REAL NOT NULL, target REAL NOT NULL, status TEXT NOT NULL,
            opened_at TEXT NOT NULL, closed_at TEXT, exit REAL, pnl REAL,
            reason TEXT)""")

def candles(symbol):
    raw = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
    if not raw or len(raw) < 30:
        raise RuntimeError(f"Not enough candles for {symbol}")
    df = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"])
    df["ema_fast"] = df.close.ewm(span=9, adjust=False).mean()
    df["ema_slow"] = df.close.ewm(span=21, adjust=False).mean()
    delta = df.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))
    prev_close = df.close.shift(1)
    tr = pd.concat([(df.high-df.low), (df.high-prev_close).abs(),
                    (df.low-prev_close).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    return df.dropna()

def get_state():
    with db() as c:
        row = c.execute("SELECT * FROM state WHERE id=1").fetchone()
        return dict(row)

def open_positions():
    with db() as c:
        rows = c.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC").fetchall()
        return [dict(x) for x in rows]

def trade_signal(df):
    prev, last = df.iloc[-2], df.iloc[-1]
    crossed_up = prev.ema_fast <= prev.ema_slow and last.ema_fast > last.ema_slow
    crossed_down = prev.ema_fast >= prev.ema_slow and last.ema_fast < last.ema_slow
    if crossed_up and 50 <= last.rsi <= 70:
        return "BUY"
    if crossed_down and 30 <= last.rsi <= 50:
        return "SELL"
    return "HOLD"

def execute_paper_entry(symbol, side, price, atr):
    state = get_state()
    positions = open_positions()
    if len(positions) >= MAX_OPEN_POSITIONS:
        return
    if any(p["symbol"] == symbol for p in positions):
        return
    risk_cash = state["balance"] * RISK_PCT
    stop_distance = max(atr * STOP_ATR, price * 0.002)
    qty = risk_cash / stop_distance
    notional = qty * price
    # Never allocate more than 25% of current paper balance to one position.
    qty = min(qty, state["balance"] * 0.25 / price)
    if qty <= 0 or qty * price < 5:
        return
    stop = price - stop_distance if side == "BUY" else price + stop_distance
    target = price + stop_distance * TAKE_PROFIT_R if side == "BUY" else price - stop_distance * TAKE_PROFIT_R
    with db() as c:
        c.execute("""INSERT INTO trades(symbol,side,entry,quantity,stop,target,status,opened_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (symbol, side, price, qty, stop, target, "OPEN", datetime.now(timezone.utc).isoformat()))

def close_paper_trade(trade, price, reason):
    direction = 1 if trade["side"] == "BUY" else -1
    pnl = (price - trade["entry"]) * trade["quantity"] * direction
    with db() as c:
        c.execute("UPDATE state SET balance=balance+? WHERE id=1", (pnl,))
        c.execute("""UPDATE trades SET status='CLOSED',closed_at=?,exit=?,pnl=?,reason=? WHERE id=?""",
            (datetime.now(timezone.utc).isoformat(), price, pnl, reason, trade["id"]))

def scan_once():
    results = []
    for symbol in SYMBOLS:
        try:
            df = candles(symbol)
            last = df.iloc[-1]
            price, atr = float(last.close), float(last.atr)
            signal = trade_signal(df)
            for position in open_positions():
                if position["symbol"] != symbol:
                    continue
                if position["side"] == "BUY":
                    if price <= position["stop"]:
                        close_paper_trade(position, price, "STOP_LOSS")
                    elif price >= position["target"]:
                        close_paper_trade(position, price, "TAKE_PROFIT")
                    elif signal == "SELL":
                        close_paper_trade(position, price, "OPPOSITE_SIGNAL")
                else:
                    if price >= position["stop"]:
                        close_paper_trade(position, price, "STOP_LOSS")
                    elif price <= position["target"]:
                        close_paper_trade(position, price, "TAKE_PROFIT")
                    elif signal == "BUY":
                        close_paper_trade(position, price, "OPPOSITE_SIGNAL")
            if signal in ("BUY", "SELL"):
                execute_paper_entry(symbol, signal, price, atr)
            results.append({"symbol": symbol, "price": price, "rsi": round(float(last.rsi),2),
                            "ema_fast": round(float(last.ema_fast),6),
                            "ema_slow": round(float(last.ema_slow),6),
                            "signal": signal, "time": datetime.now(timezone.utc).isoformat()})
        except Exception as e:
            log.exception("Scan failed for %s", symbol)
            results.append({"symbol": symbol, "error": str(e), "signal": "ERROR"})
    return results

async def scan_loop():
    while True:
        try:
            await asyncio.to_thread(scan_once)
        except Exception:
            log.exception("Background scan failed")
        await asyncio.sleep(POLL_SECONDS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(scan_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="Soheil AI Trader", version="1.0.0", lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(DASHBOARD)

@app.get("/health")
def health():
    return {"ok": True, "mode": "PAPER", "exchange": EXCHANGE_ID}

@app.get("/api/status")
def status():
    state = get_state()
    positions = open_positions()
    with db() as c:
        closed = c.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl FROM trades WHERE status='CLOSED'").fetchone()
    return {"mode":"PAPER", "exchange":EXCHANGE_ID, "timeframe":TIMEFRAME,
            "symbols":SYMBOLS, "balance":round(state["balance"],2),
            "starting_balance":state["starting_balance"],
            "total_pnl":round(state["balance"]-state["starting_balance"],2),
            "open_positions":len(positions), "closed_trades":closed["n"],
            "closed_pnl":round(closed["pnl"],2), "risk_per_trade_pct":RISK_PCT*100}

@app.get("/api/positions")
def positions():
    return open_positions()

@app.get("/api/trades")
def trades(limit: int = 50):
    limit = min(max(limit,1),200)
    with db() as c:
        rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
        return [dict(x) for x in rows]

@app.get("/api/scan")
def scan():
    return scan_once()

@app.post("/api/reset")
def reset():
    with db() as c:
        open_count = c.execute("SELECT COUNT(*) n FROM trades WHERE status='OPEN'").fetchone()["n"]
        if open_count:
            raise HTTPException(409, "ابتدا همه موقعیت‌های باز را ببندید.")
        c.execute("DELETE FROM trades")
        c.execute("UPDATE state SET balance=starting_balance WHERE id=1")
    return {"ok":True,"message":"حساب آزمایشی بازنشانی شد."}

DASHBOARD = r"""
<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Soheil AI Trader</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#e8eefc;font-family:Tahoma,Arial}
main{max-width:1050px;margin:auto;padding:22px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:25px;margin:4px 0}.muted{color:#94a3b8;font-size:13px}.badge{background:#123b2d;color:#75e0ad;padding:8px 12px;border-radius:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:22px 0}
.card{background:#151f32;border:1px solid #26344c;border-radius:14px;padding:17px}.label{color:#9aabc5;font-size:13px}.value{font-size:25px;font-weight:bold;margin-top:10px}
section{background:#151f32;border:1px solid #26344c;border-radius:14px;padding:16px;margin:14px 0}
h2{font-size:17px;margin:0 0 14px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:right;padding:11px 8px;border-bottom:1px solid #29364b;white-space:nowrap}
.green{color:#62dda0}.red{color:#ff7d89}.yellow{color:#ffd166}button{border:0;border-radius:9px;padding:10px 15px;background:#4778f5;color:white;font-weight:bold}
.note{line-height:1.9;color:#aab8ce;font-size:12px}.scroll{overflow:auto}
</style></head><body><main>
<div class="top"><div><h1>🤖 Soheil AI Trader</h1><div class="muted">ربات تحلیل و معامله‌گر آزمایشی</div></div><span class="badge">● حالت آزمایشی (بدون پول واقعی)</span></div>
<div class="grid">
<div class="card"><div class="label">موجودی حساب آزمایشی</div><div class="value" id="balance">—</div></div>
<div class="card"><div class="label">سود / زیان کل</div><div class="value" id="pnl">—</div></div>
<div class="card"><div class="label">موقعیت‌های باز</div><div class="value" id="open">—</div></div>
<div class="card"><div class="label">معاملات بسته‌شده</div><div class="value" id="closed">—</div></div>
</div>
<section><h2>📡 تحلیل بازار</h2><div class="scroll"><table><thead><tr><th>ارز</th><th>قیمت</th><th>RSI</th><th>سیگنال</th></tr></thead><tbody id="signals"><tr><td colspan="4">در حال دریافت اطلاعات…</td></tr></tbody></table></div></section>
<section><h2>📂 موقعیت‌های باز</h2><div class="scroll"><table><thead><tr><th>ارز</th><th>جهت</th><th>ورود</th><th>حد ضرر</th><th>حد سود</th></tr></thead><tbody id="positions"></tbody></table></div></section>
<section><h2>🧾 آخرین معاملات</h2><div class="scroll"><table><thead><tr><th>ارز</th><th>جهت</th><th>ورود</th><th>خروج</th><th>سود/زیان</th><th>وضعیت</th></tr></thead><tbody id="trades"></tbody></table></div></section>
<section><h2>تنظیمات و توضیحات</h2><button onclick="refreshAll()">به‌روزرسانی</button>
<p class="note">استراتژی فعلی: تقاطع EMA 9 و EMA 21 همراه با فیلتر RSI؛ حد ضرر بر اساس ATR و حد سود با نسبت ریسک به بازده ۱ به ۲. ربات فقط داده عمومی بازار را می‌خواند و سفارش واقعی ارسال نمی‌کند. این نسخه تضمین‌کننده سود نیست و برای ارزیابی اولیه است.</p></section>
</main><script>
const fmt=n=>Number(n||0).toLocaleString('en-US',{maximumFractionDigits:4});
async function refreshAll(){
try{
const [s,p,t]=await Promise.all([fetch('/api/status').then(r=>r.json()),fetch('/api/positions').then(r=>r.json()),fetch('/api/trades').then(r=>r.json())]);
document.getElementById('balance').textContent=fmt(s.balance)+' USDT';
const pnl=document.getElementById('pnl');pnl.textContent=(s.total_pnl>=0?'+':'')+fmt(s.total_pnl)+' USDT';pnl.className='value '+(s.total_pnl>=0?'green':'red');
document.getElementById('open').textContent=s.open_positions;document.getElementById('closed').textContent=s.closed_trades;
document.getElementById('positions').innerHTML=p.map(x=>`<tr><td>${x.symbol}</td><td>${x.side}</td><td>${fmt(x.entry)}</td><td>${fmt(x.stop)}</td><td>${fmt(x.target)}</td></tr>`).join('')||'<tr><td colspan="5">موقعیت بازی وجود ندارد</td></tr>';
document.getElementById('trades').innerHTML=t.map(x=>`<tr><td>${x.symbol}</td><td>${x.side}</td><td>${fmt(x.entry)}</td><td>${x.exit?fmt(x.exit):'—'}</td><td class="${(x.pnl||0)>=0?'green':'red'}">${x.pnl==null?'—':fmt(x.pnl)}</td><td>${x.status}</td></tr>`).join('')||'<tr><td colspan="6">هنوز معامله‌ای ثبت نشده</td></tr>';
const scans=await fetch('/api/scan').then(r=>r.json());
document.getElementById('signals').innerHTML=scans.map(x=>`<tr><td>${x.symbol}</td><td>${x.price?fmt(x.price):'—'}</td><td>${x.rsi??'—'}</td><td class="${x.signal==='BUY'?'green':x.signal==='SELL'?'red':'yellow'}">${x.signal}${x.error?' - '+x.error:''}</td></tr>`).join('');
}catch(e){console.error(e)}
}
refreshAll();setInterval(refreshAll,60000);
</script></body></html>
"""
