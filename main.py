# Autonomous MEXC scanner → Telegram (FastAPI + Scheduler)
# STRIKTER Build: härtere Checkliste + Start-Banner | 25 Coins

import os, asyncio, time, math
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Any

import pandas as pd
import pandas_ta as ta
import ccxt
from fastapi import FastAPI
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ====== Config ======
TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# 25 liquide Spot-Paare (MEXC-Notation "XXX/USDT")
SYMBOLS = [
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
    "TON/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT","LINK/USDT",
    "TRX/USDT","DOT/USDT","MATIC/USDT","SHIB/USDT","PEPE/USDT",
    "LTC/USDT","BCH/USDT","ATOM/USDT","NEAR/USDT","APT/USDT",
    "ARB/USDT","OP/USDT","SUI/USDT","INJ/USDT","FIL/USDT",
]

# Timeframes
TF_TRIGGER = "5m"                 # Signal-TF
TF_FILTERS = ["15m","1h","4h"]    # Trendfilter

LOOKBACK = 300
SCAN_INTERVAL_S = 60

# ATR-basierte Levels (Chart-ATR, nicht Margin %)
ATR_SL  = 1.5
TP1_ATR = 1.0
TP2_ATR = 1.8
TP3_ATR = 2.6

# ====== STRIKTE Checklisten-Settings ======
MIN_ATR_PCT        = 0.20      # min ATR% vom Preis
VOL_SPIKE_FACTOR   = 1.30      # Volumen > 1.30× MA20 (Pflicht)
REQUIRE_VOL_SPIKE  = True      # Volumenspike = KO-Kriterium
PROB_MIN           = 60        # mind. 60% Wahrscheinlichkeit
COOLDOWN_S         = 300       # Anti-Spam pro Symbol/Richtung

# ====== Init ======
if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID environment variables.")

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="MEXC Auto Scanner → Telegram (STRIKT)")

# CCXT Exchange
ex = ccxt.mexc({"enableRateLimit": True})

# Anti-Spam Merker (symbol+richtung → letzte Zeit)
last_signal: Dict[str, float] = {}

# Letzter Analyse-Report (für /status)
last_scan_report: Dict[str, Any] = {"ts": None, "symbols": {}}

# ====== TA Helpers ======
def ema(series: pd.Series, length: int):
    return ta.ema(series, length)

def rsi(series: pd.Series, length: int = 14):
    return ta.rsi(series, length)

def atr(h, l, c, length: int = 14):
    return ta.atr(h, l, c, length)

def vol_sma(v, length: int = 20):
    return ta.sma(v, length)

def bullish_engulf(o, h, l, c) -> bool:
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
        sl  = round(entry - ATR_SL  * atrv, 6)
        tp1 = round(entry + TP1_ATR * atrv, 6)
        tp2 = round(entry + TP2_ATR * atrv, 6)
        tp3 = round(entry + TP3_ATR * atrv, 6)
    else:
        sl  = round(entry + ATR_SL  * atrv, 6)
        tp1 = round(entry - TP1_ATR * atrv, 6)
        tp2 = round(entry - TP2_ATR * atrv, 6)
        tp3 = round(entry - TP3_ATR * atrv, 6)
    return entry, sl, tp1, tp2, tp3

def need_throttle(key: str, now: float, cool_s: int = COOLDOWN_S) -> bool:
    t = last_signal.get(key, 0.0)
    if now - t < cool_s:
        return True
    last_signal[key] = now
    return False

async def send_signal(symbol: str, tf: str, direction: str, entry: float, sl: float, tp1: float, tp2: float, tp3: float, prob: int, checklist_ok: List[str], checklist_warn: List[str]):
    checks_line = ""
    if checklist_ok:   checks_line += f"✅ {', '.join(checklist_ok)}\n"
    if checklist_warn: checks_line += f"⚠️ {', '.join(checklist_warn)}\n"
    text = (
        f"🛡 *STRIKT* — Signal {symbol} {tf}\n"
        f"➡️ *{direction}*\n"
        f"🎯 Entry: `{entry}`\n"
        f"🛡 SL: `{sl}`\n"
        f"🏁 TP1: `{tp1}`\n"
        f"🏁 TP2: `{tp2}`\n"
        f"🏁 TP3: `{tp3}`\n"
        f"📈 Prob.: *{prob}%*\n"
        f"{checks_line}".strip()
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

async def send_mode_banner():
    text = (
        "🛡 *Scanner gestartet – MODUS: STRENG*\n"
        "• HTF strikt aligned (15m/1h/4h)\n"
        f"• Volumen: Pflicht ≥ {VOL_SPIKE_FACTOR:.2f}× MA20\n"
        f"• ATR%-Schwelle aktiv (≥ {MIN_ATR_PCT:.2f}%)\n"
        f"• Wahrscheinlichkeit ≥ {PROB_MIN}%\n"
        "• TP1/2/3 & SL ATR-basiert\n"
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

def fetch_df(symbol: str, timeframe: str) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=LOOKBACK)
    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","volume"])
    return df

def compute_trend_ok(df: pd.DataFrame) -> Tuple[bool,bool,bool]:
    ema200 = ema(df["close"], 200)
    up = df["close"].iloc[-1] > ema200.iloc[-1]
    down = df["close"].iloc[-1] < ema200.iloc[-1]
    return up, down, not math.isnan(df["close"].iloc[-1])

def analyze_trigger(df: pd.DataFrame) -> Dict[str, any]:
    df["ema50"]  = ema(df.close, 50)
    df["ema100"] = ema(df.close, 100)
    df["ema200"] = ema(df.close, 200)
    df["rsi"]    = rsi(df.close, 14)
    df["atr"]    = atr(df.high, df.low, df.close, 14)
    df["volma"]  = vol_sma(df.volume, 20)

    o, h, l, c, v = df.open, df.high, df.low, df.close, df.volume
    long_fast  = (c.iloc[-1] > df.ema50.iloc[-1] > df.ema100.iloc[-1]) and (df.rsi.iloc[-1] <= 65)
    short_fast = (c.iloc[-1] < df.ema50.iloc[-1] < df.ema100.iloc[-1]) and (df.rsi.iloc[-1] >= 35)

    bull   = bullish_engulf(o, h, l, c)
    bear   = bearish_engulf(o, h, l, c)
    vol_ok = v.iloc[-1] > (VOL_SPIKE_FACTOR * df.volma.iloc[-1])

    return {
        "bull": bull, "bear": bear,
        "long_fast": long_fast, "short_fast": short_fast,
        "vol_ok": bool(vol_ok),
        "atr": float(df.atr.iloc[-1]),
        "price": float(c.iloc[-1]),
        "ema200_up": (c.iloc[-1] > df.ema200.iloc[-1]),
        "ema200_dn": (c.iloc[-1] < df.ema200.iloc[-1]),
        "rsi": float(df.rsi.iloc[-1]),
    }

# ====== Strikte Checkliste ======
def build_checklist_for_dir(direction: str, trig: Dict[str, any], up_all: bool, dn_all: bool) -> Tuple[bool, List[str], List[str]]:
    ok, warn = [], []

    # ATR% (hart)
    atr_pct = (trig["atr"] / max(trig["price"], 1e-9)) * 100.0
    if atr_pct >= MIN_ATR_PCT: ok.append(f"ATR≥{MIN_ATR_PCT}% ({atr_pct:.2f}%)")
    else:                      return (False, ok, [f"ATR<{MIN_ATR_PCT}% ({atr_pct:.2f}%)"])

    # HTF-Alignment (hart)
    if (up_all if direction=="LONG" else dn_all): ok.append("HTF align (15m/1h/4h)")
    else:                                         return (False, ok, ["HTF nicht aligned"])

    # Bias (hart)
    bias_ok = (trig["bull"] or trig["long_fast"]) if direction=="LONG" else (trig["bear"] or trig["short_fast"])
    if bias_ok: ok.append("Engulf/EMA-Stack")
    else:       return (False, ok, ["Kein Engulf/EMA-Stack"])

    # EMA200 in Richtung (hart)
    ema200_ok = trig["ema200_up"] if direction=="LONG" else trig["ema200_dn"]
    if ema200_ok: ok.append("EMA200 ok")
    else:         return (False, ok, ["EMA200 gegen Setup"])

    # Volumenspike (hart)
    if trig["vol_ok"]: ok.append(f"Vol>{VOL_SPIKE_FACTOR:.2f}×MA (Pflicht)")
    else:              return (False, ok, [f"kein Vol-Spike (≥{VOL_SPIKE_FACTOR:.2f}× Pflicht)"])

    # RSI nur Hinweis
    if direction=="LONG":
        if trig["rsi"] > 67: warn.append(f"RSI hoch ({trig['rsi']:.1f})")
        else: ok.append(f"RSI ok ({trig['rsi']:.1f})")
    else:
        if trig["rsi"] < 33: warn.append(f"RSI tief ({trig['rsi']:.1f})")
        else: ok.append(f"RSI ok ({trig['rsi']:.1f})")

    return (True, ok, warn)

# ====== Scan ======
async def scan_once():
    global last_scan_report
    now = time.time()
    round_ts = datetime.now(timezone.utc).isoformat()
    last_scan_report = {"ts": round_ts, "symbols": {}}

    for sym in SYMBOLS:
        try:
            # Trigger-TF
            df5  = fetch_df(sym, TF_TRIGGER)
            trig = analyze_trigger(df5)

            # Trendfilter auf 15m/1h/4h
            up_all, dn_all = True, True
            for tf in TF_FILTERS:
                df_tf = fetch_df(sym, tf)
                up, dn, _ = compute_trend_ok(df_tf)
                up_all &= up
                dn_all &= dn
                time.sleep(ex.rateLimit/1000)

            # Beide Richtungen prüfen
            passes = []
            for direction in ("LONG","SHORT"):
                passed, ok_tags, warn_tags = build_checklist_for_dir(direction, trig, up_all, dn_all)
                if passed:
                    prob = prob_score(direction=="LONG", direction=="SHORT", trig["vol_ok"], up_all or dn_all,
                                      trig["ema200_up"] if direction=="LONG" else trig["ema200_dn"])
                    passes.append( (direction, prob, ok_tags, warn_tags) )

            # Entscheidung
            if passes:
                direction, prob, ok_tags, warn_tags = sorted(passes, key=lambda x: x[1], reverse=True)[0]
                if prob >= PROB_MIN:
                    entry, sl, tp1, tp2, tp3 = make_levels(direction, trig["price"], trig["atr"])
                    key = f"{sym}:{direction}"
                    throttled = need_throttle(key, now)
                    last_scan_report["symbols"][sym] = {
                        "direction": direction, "prob": prob, "throttled": throttled,
                        "ok": ok_tags, "warn": warn_tags,
                        "price": trig["price"], "atr": trig["atr"]
                    }
                    if not throttled:
                        await send_signal(sym, TF_TRIGGER, direction, entry, sl, tp1, tp2, tp3, prob, ok_tags, warn_tags)
                else:
                    last_scan_report["symbols"][sym] = {"skip": f"prob {prob}% < {PROB_MIN}%"}
            else:
                last_scan_report["symbols"][sym] = {"skip": "Checkliste nicht bestanden"}

        except Exception as e:
            last_scan_report["symbols"][sym] = {"error": str(e)}
        finally:
            time.sleep(ex.rateLimit/1000)

async def runner():
    sched = AsyncIOScheduler()
    sched.add_job(scan_once, "interval", seconds=SCAN_INTERVAL_S, next_run_time=datetime.now(timezone.utc))
    sched.start()
    while True:
        await asyncio.sleep(3600)

# ====== FastAPI ======
@app.on_event("startup")
async def _startup():
    await send_mode_banner()
    asyncio.create_task(runner())

@app.get("/")
async def root():
    return {
        "ok": True,
        "mode": "streng",
        "symbols": SYMBOLS,
        "trigger_tf": TF_TRIGGER,
        "filters": TF_FILTERS,
        "scan_interval_s": SCAN_INTERVAL_S,
        "info": "Background scanner active. TradingView not required."
    }

@app.get("/scan")
async def manual_scan():
    await scan_once()
    return {"ok": True, "ran": True, "ts": last_scan_report.get("ts")}

@app.get("/status")
async def status():
    return {"ok": True, "mode": "streng", "report": last_scan_report}

@app.get("/test")
async def test():
    text = "✅ Test: Bot & Telegram OK — MEXC Scanner aktiv. Mode: *STRENG*"
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
    return {"ok": True, "test": True, "mode": "streng"}
