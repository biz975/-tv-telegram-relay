# Autonomous MEXC scanner → Telegram (FastAPI + Scheduler)
import os, asyncio, time, math
from datetime import datetime, timezone
from typing import List, Dict, Tuple

import pandas as pd
import pandas_ta as ta
import ccxt
from fastapi import FastAPI
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ====== Config ======
TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# 10 Coins (Spot). Für Perps: z.B. "BTC/USDT:USDT" bei manchen Börsen; MEXC ccxt-Spot bleibt "BTC/USDT".
SYMBOLS = ["BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
           "TON/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT","LINK/USDT"]

# Timeframes
TF_TRIGGER = "5m"       # Signal-TF
TF_FILTERS = ["15m","1h","4h"]  # Trendfilter TFs

LOOKBACK = 300          # Candles zum Rechnen
SCAN_INTERVAL_S = 60    # wie oft scannen (Sekunden)

ATR_SL = 1.5
TP1_ATR = 1.0
TP2_ATR = 1.8
TP3_ATR = 2.6

# ====== Init ======
if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID environment variables.")

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="MEXC Auto Scanner → Telegram")

# CCXT Exchange
ex = ccxt.mexc({"enableRateLimit": True})

# Merker gegen Spam (symbol+tf → letzte Signal-Zeit)
last_signal: Dict[str, float] = {}

def ema(series: pd.Series, length: int):
    return ta.ema(series, length)

def rsi(series: pd.Series, length: int = 14):
    return ta.rsi(series, length)

def atr(h, l, c, length: int = 14):
    return ta.atr(h, l, c, length)

def vol_sma(v, length: int = 20):
    return ta.sma(v, length)

def bullish_engulf(o, h, l, c) -> bool:
    # bull candle, und umschließt den vorherigen Körper
    return (c.iloc[-1] > o.iloc[-1]) and (o.iloc[-1] <= l.iloc[-2]) and (c.iloc[-1] >= h.iloc[-2])

def bearish_engulf(o, h, l, c) -> bool:
    return (c.iloc[-1] < o.iloc[-1]) and (o.iloc[-1] >= h.iloc[-2]) and (c.iloc[-1] <= l.iloc[-2])

def prob_score(long_ok: bool, short_ok: bool, vol_ok: bool, trend_ok: bool, ema200_align: bool) -> int:
    base = 70 if (long_ok or short_ok) else 0
    if base == 0: return 0
    base += 5 if vol_ok else 0
    base += 5 if trend_ok else 0
    base += 5 if ema200_align else 0
    return min(base, 90)

def make_levels(direction: str, price: float, atrv: float) -> Tuple[float,float,float,float,float]:
    entry = float(price)
    if direction == "LONG":
        sl = round(entry - ATR_SL * atrv, 6)
        tp1 = round(entry + TP1_ATR * atrv, 6)
        tp2 = round(entry + TP2_ATR * atrv, 6)
        tp3 = round(entry + TP3_ATR * atrv, 6)
    else:
        sl = round(entry + ATR_SL * atrv, 6)
        tp1 = round(entry - TP1_ATR * atrv, 6)
        tp2 = round(entry - TP2_ATR * atrv, 6)
        tp3 = round(entry - TP3_ATR * atrv, 6)
    return entry, sl, tp1, tp2, tp3

def need_throttle(key: str, now: float, cool_s: int = 300) -> bool:
    # unterdrückt gleiche Signale 5min lang
    t = last_signal.get(key, 0.0)
    if now - t < cool_s:
        return True
    last_signal[key] = now
    return False

async def send_signal(symbol: str, tf: str, direction: str, entry: float, sl: float, tp1: float, tp2: float, tp3: float, prob: int, checklist: List[str]):
    text = (
        f"⚡️ *Signal* {symbol} {tf}\n"
        f"➡️ *{direction}*\n"
        f"🎯 Entry: `{entry}`\n"
        f"🛡 SL: `{sl}`\n"
        f"🏁 TP1: `{tp1}`\n"
        f"🏁 TP2: `{tp2}`\n"
        f"🏁 TP3: `{tp3}`\n"
        f"📈 Prob.: *{prob}%*\n"
        + (f"✅ {', '.join(checklist)}" if checklist else "")
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

def fetch_df(symbol: str, timeframe: str) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=LOOKBACK)
    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","volume"])
    return df

def compute_trend_ok(df: pd.DataFrame) -> Tuple[bool,bool,bool]:
    # EMA200-Regel je TF: close > ema200 => up
    up = df["close"].iloc[-1] > ema(df["close"], 200).iloc[-1]
    down = df["close"].iloc[-1] < ema(df["close"], 200).iloc[-1]
    return up, down, not math.isnan(df["close"].iloc[-1])

def analyze_trigger(df: pd.DataFrame) -> Dict[str, any]:
    # Indikatoren auf Trigger-TF (5m)
    df["ema50"] = ema(df.close, 50)
    df["ema100"] = ema(df.close, 100)
    df["ema200"] = ema(df.close, 200)
    df["rsi"] = rsi(df.close, 14)
    df["atr"] = atr(df.high, df.low, df.close, 14)
    df["volma"] = vol_sma(df.volume, 20)

    o, h, l, c, v = df.open, df.high, df.low, df.close, df.volume
    long_fast  = c.iloc[-1] > df.ema50.iloc[-1] > df.ema100.iloc[-1] and df.rsi.iloc[-1] < 65
    short_fast = c.iloc[-1] < df.ema50.iloc[-1] < df.ema100.iloc[-1] and df.rsi.iloc[-1] > 35

    bull = bullish_engulf(o, h, l, c)
    bear = bearish_engulf(o, h, l, c)
    vol_ok = v.iloc[-1] > df.volma.iloc[-1]

    return {
        "bull": bull, "bear": bear,
        "long_fast": long_fast, "short_fast": short_fast,
        "vol_ok": vol_ok,
        "atr": float(df.atr.iloc[-1]),
        "price": float(c.iloc[-1]),
        "ema200_up": c.iloc[-1] > df.ema200.iloc[-1],
        "ema200_dn": c.iloc[-1] < df.ema200.iloc[-1],
    }

async def scan_once():
    now = time.time()
    for sym in SYMBOLS:
        try:
            # Trigger-TF
            df5 = fetch_df(sym, TF_TRIGGER)
            trig = analyze_trigger(df5)

            # Trendfilter auf 15m/1h/4h -> Higher TF must align
            up_all, dn_all = True, True
            for tf in TF_FILTERS:
                df_tf = fetch_df(sym, tf)
                up, dn, _ = compute_trend_ok(df_tf)
                up_all &= up
                dn_all &= dn
                # Ratelimit beachten
                time.sleep(ex.rateLimit/1000)

            long_ok = up_all and (trig["bull"] or trig["long_fast"]) and trig["vol_ok"]
            short_ok = dn_all and (trig["bear"] or trig["short_fast"]) and trig["vol_ok"]

            if long_ok or short_ok:
                direction = "LONG" if long_ok else "SHORT"
                prob = prob_score(long_ok, short_ok, trig["vol_ok"], up_all or dn_all,
                                  trig["ema200_up"] if long_ok else trig["ema200_dn"])
                entry, sl, tp1, tp2, tp3 = make_levels(direction, trig["price"], trig["atr"])
                key = f"{sym}:{direction}"
                if need_throttle(key, now):  # anti-spam 5min
                    continue
                checklist = [
                    "HTF align (15m/1h/4h)",
                    "Engulf/EMA-Stack",
                    "Vol>MA",
                    "EMA200 ok"
                ]
                await send_signal(sym, TF_TRIGGER, direction, entry, sl, tp1, tp2, tp3, prob, checklist)

        except Exception as e:
            # Optional: Logging minimal
            print(f"[scan] {sym} error: {e}")
        finally:
            time.sleep(ex.rateLimit/1000)

async def runner():
    sched = AsyncIOScheduler()
    sched.add_job(scan_once, "interval", seconds=SCAN_INTERVAL_S, next_run_time=datetime.now(timezone.utc))
    sched.start()
    # Background loop to keep running
    while True:
        await asyncio.sleep(3600)

@app.on_event("startup")
async def _startup():
    asyncio.create_task(runner())

@app.get("/")
async def root():
    return {"ok": True, "mode": "scanner", "info": "Background scanner active. TradingView not required."}

# Optional: Manual trigger (GET /scan)
@app.get("/scan")
async def manual_scan():
    await scan_once()
    return {"ok": True, "ran": True}
