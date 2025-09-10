# Autonomous MEXC Scanner → Telegram (FastAPI + APScheduler + CCXT)
import os, asyncio, time, math
from datetime import datetime, timezone
from typing import List, Dict, Tuple

import pandas as pd
import pandas_ta as ta
import ccxt
from fastapi import FastAPI
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ========= Konfiguration =========
TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# 10 MEXC-Spot-Paare
SYMBOLS = [
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
    "TON/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT","LINK/USDT"
]

TF_TRIGGER = "5m"                 # Signale auf 5m
TF_FILTERS = ["15m","1h","4h"]    # HTF-Trendfilter
LOOKBACK = 300                    # Kerzen je TF
SCAN_INTERVAL_S = 60              # Scan-Frequenz (Sekunden)

# Levels (ATR-Multiplikatoren) – kannst du hier anpassen
ATR_SL  = 1.5
TP1_ATR = 1.0
TP2_ATR = 1.8
TP3_ATR = 2.6

# Mindest-Qualität für Signale
MIN_PROB = 75  # % -> nur senden, wenn prob >= MIN_PROB

# ========= Init =========
if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID environment variables.")

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="MEXC Auto Scanner → Telegram")

# CCXT Exchange
ex = ccxt.mexc({"enableRateLimit": True})

# Anti-Spam: merke letztes Signal je (symbol+dir)
last_signal_ts: Dict[str, float] = {}

# ========= Indikator-Helfer =========
def ema(s: pd.Series, length: int): return ta.ema(s, length)
def rsi(s: pd.Series, length: int = 14): return ta.rsi(s, length)
def atr(h, l, c, length: int = 14): return ta.atr(h, l, c, length)
def vol_sma(v, length: int = 20): return ta.sma(v, length)

def bullish_engulf(o, h, l, c) -> bool:
    return (c.iloc[-1] > o.iloc[-1]) and (o.iloc[-1] <= l.iloc[-2]) and (c.iloc[-1] >= h.iloc[-2])

def bearish_engulf(o, h, l, c) -> bool:
    return (c.iloc[-1] < o.iloc[-1]) and (o.iloc[-1] >= h.iloc[-2]) and (c.iloc[-1] <= l.iloc[-2])

def prob_score(long_ok: bool, short_ok: bool, vol_ok: bool, trend_ok: bool, ema200_align: bool) -> int:
    base = 70 if (long_ok or short_ok) else 0
    if base == 0: return 0
    if vol_ok: base += 5
    if trend_ok: base += 5
    if ema200_align: base += 5
    return min(base, 90)

def make_levels(direction: str, price: float, atrv: float) -> Tuple[float,float,float,float,float]:
    entry = float(price)
    if direction == "LONG":
        sl  = round(entry - ATR_SL * atrv, 6)
        tp1 = round(entry + TP1_ATR * atrv, 6)
        tp2 = round(entry + TP2_ATR * atrv, 6)
        tp3 = round(entry + TP3_ATR * atrv, 6)
    else:
        sl  = round(entry + ATR_SL * atrv, 6)
        tp1 = round(entry - TP1_ATR * atrv, 6)
        tp2 = round(entry - TP2_ATR * atrv, 6)
        tp3 = round(entry - TP3_ATR * atrv, 6)
    return entry, sl, tp1, tp2, tp3

def throttle(key: str, now: float, cool_s: int = 300) -> bool:
    t = last_signal_ts.get(key, 0.0)
    if now - t < cool_s:
        return True
    last_signal_ts[key] = now
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
    return pd.DataFrame(ohlcv, columns=["time","open","high","low","close","volume"])

def compute_trend_ok(df: pd.DataFrame) -> Tuple[bool,bool]:
    ema200 = ema(df["close"], 200).iloc[-1]
    c = df["close"].iloc[-1]
    return c > ema200, c < ema200

def analyze_trigger(df: pd.DataFrame) -> Dict[str, any]:
    df["ema50"]  = ema(df.close, 50)
    df["ema100"] = ema(df.close, 100)
    df["ema200"] = ema(df.close, 200)
    df["rsi"]    = rsi(df.close, 14)
    df["atr"]    = atr(df.high, df.low, df.close, 14)
    df["volma"]  = vol_sma(df.volume, 20)

    o, h, l, c, v = df.open, df.high, df.low, df.close, df.volume
    long_fast  = (c.iloc[-1] > df.ema50.iloc[-1] > df.ema100.iloc[-1]) and (df.rsi.iloc[-1] < 65)
    short_fast = (c.iloc[-1] < df.ema50.iloc[-1] < df.ema100.iloc[-1]) and (df.rsi.iloc[-1] > 35)
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
            # Trigger-TF (5m)
            df5 = fetch_df(sym, TF_TRIGGER)
            t = analyze_trigger(df5)

            # Trendfilter (15m / 1h / 4h): alle müssen alignen
            up_all, dn_all = True, True
            for tf in TF_FILTERS:
                df_tf = fetch_df(sym, tf)
                up, dn = compute_trend_ok(df_tf)
                up_all &= up
                dn_all &= dn
                time.sleep(ex.rateLimit/1000)

            long_ok  = up_all and (t["bull"] or t["long_fast"])  and t["vol_ok"]
            short_ok = dn_all and (t["bear"] or t["short_fast"]) and t["vol_ok"]

            if long_ok or short_ok:
                direction = "LONG" if long_ok else "SHORT"
                prob = prob_score(long_ok, short_ok, t["vol_ok"], up_all or dn_all,
                                  t["ema200_up"] if long_ok else t["ema200_dn"])

                # 🔒 Mindestwahrscheinlichkeit
                if prob < MIN_PROB:
                    continue

                entry, sl, tp1, tp2, tp3 = make_levels(direction, t["price"], t["atr"])
                key = f"{sym}:{direction}"
                if throttle(key, now):  # Anti-Spam 5 min
                    continue
                checklist = [
                    "HTF align (15m/1h/4h)",
                    "Engulf/EMA-Stack",
                    "Vol>MA",
                    "EMA200 ok"
                ]
                await send_signal(sym, TF_TRIGGER, direction, entry, sl, tp1, tp2, tp3, prob, checklist)

        except Exception as e:
            print(f"[scan] {sym} error: {e}")
        finally:
            time.sleep(ex.rateLimit/1000)

async def runner():
    sched = AsyncIOScheduler()
    sched.add_job(scan_once, "interval", seconds=SCAN_INTERVAL_S, next_run_time=datetime.now(timezone.utc))
    sched.start()
    while True:
        await asyncio.sleep(3600)

@app.on_event("startup")
async def _startup():
    asyncio.create_task(runner())

@app.get("/")
async def root():
    return {"ok": True, "mode": "scanner", "tf": TF_TRIGGER, "filters": TF_FILTERS, "min_prob": MIN_PROB}

@app.get("/scan")
async def manual_scan():
    await scan_once()
    return {"ok": True, "manual": True}
