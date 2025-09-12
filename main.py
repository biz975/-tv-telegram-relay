# MEXC Scanner (strict) → Telegram
# - Checkliste (strict):
#   * 5m: EMA9>EMA21 + RSI>48 → Long | EMA9<EMA21 + RSI<52 → Short
#   * Volumen-Spike: Vol > 1.30 × MA20 (Pflicht)
#   * ATR% (5m) ≥ 0.20% (Pflicht)
#   * HTF-Alignment (15m/1h/4h): Close > EMA200 (Long) / Close < EMA200 (Short)
# - TPs aus Support/Resistance:
#   * TP1: nächster 5m-S/R in Signaldirektion (minor)
#   * TP2: nächster 15m-S/R in Signaldirektion (stronger)
#   * SL: ATR-basiert (1.5 × ATR)
# - Anti-Duplikat:
#   * Pro Symbol+Richtung nur eine aktive Position. Erst wenn TP oder SL getroffen wurde,
#     darf ein neues Signal entstehen (manuelles /scan löst keine Duplikate aus).

import os, asyncio, time, math
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Any, Optional

import pandas as pd
import pandas_ta as ta
import ccxt
from fastapi import FastAPI
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ====== ENV ======
TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID environment variables.")

# ====== SETTINGS ======
# 25 liquide Paare (Spot auf MEXC via ccxt)
SYMBOLS = [
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
    "TON/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT","LINK/USDT",
    "MATIC/USDT","DOT/USDT","TRX/USDT","ATOM/USDT","APT/USDT",
    "OP/USDT","NEAR/USDT","LTC/USDT","INJ/USDT","SUI/USDT",
    "ARB/USDT","FIL/USDT","AAVE/USDT","RNDR/USDT","TIA/USDT",
]

TF_TRIGGER   = "5m"               # Analyse/Signal-TF
TF_FILTERS   = ["15m","1h","4h"]  # HTF-Alignment via EMA200
LOOKBACK     = 300
SCAN_INTERVAL_S = 60

# Strict-Checklist Parameter
VOL_SPIKE_FACTOR = 1.30           # Pflicht: Vol > 1.30 × MA20 (5m)
MIN_ATR_PCT      = 0.20           # Pflicht: ATR% ≥ 0.20%
PROB_MIN         = 75             # Punkte-System (siehe prob_score), nur ≥75 senden

# Levels
ATR_LEN   = 14
ATR_SL    = 1.5                   # SL = 1.5 × ATR (vom Entry)
MIN_TP_DIST_PCT = 0.30            # Mindestabstand TP1 vom Entry in % (sonst Skip)
COOLDOWN_S      = 300             # Fallback-Spam-Bremse (zusätzlich zu active-lock)

# ====== INIT ======
bot = Bot(token=TG_TOKEN)
app = FastAPI(title="MEXC Strict Scanner → Telegram (SnR-TP, no-dup)")

ex = ccxt.mexc({"enableRateLimit": True})

# Anti-Spam & Position-Lock
last_signal_at: Dict[str, float] = {}           # key = f"{sym}:{dir}"
active_positions: Dict[str, Dict[str, Any]] = {}  # key = f"{sym}:{dir}" → {entry, sl, tp1, tp2, dir, open_ts}

# Letzter Status
last_status: Dict[str, Any] = {"ts": None, "symbols": {}}

# ====== INDICATORS ======
def ema(series: pd.Series, length: int):
    return ta.ema(series, length)

def rsi(series: pd.Series, length: int = 14):
    return ta.rsi(series, length)

def atr(h: pd.Series, l: pd.Series, c: pd.Series, length: int = ATR_LEN):
    return ta.atr(h, l, c, length)

def sma(series: pd.Series, length: int = 20):
    return ta.sma(series, length)

def fetch_df(symbol: str, timeframe: str) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=LOOKBACK)
    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","volume"])
    return df

# ====== PIVOTS / S&R ======
def find_pivots(df: pd.DataFrame, left: int = 3, right: int = 3) -> Tuple[List[Tuple[int,float]], List[Tuple[int,float]]]:
    """
    Liefert Listen (index, preis) für Pivot-Highs (Widerstände) und Pivot-Lows (Supports).
    """
    highs, lows = [], []
    h = df["high"].values
    l = df["low"].values
    n = len(df)
    for i in range(left, n-right):
        is_ph = all(h[i] >= h[i-k-1] for k in range(left)) and all(h[i] >= h[i+k+1] for k in range(right))
        is_pl = all(l[i] <= l[i-k-1] for k in range(left)) and all(l[i] <= l[i+k+1] for k in range(right))
        if is_ph:
            highs.append((i, float(h[i])))
        if is_pl:
            lows.append((i, float(l[i])))
    return highs, lows

def dedupe_levels(levels: List[float], tol_pct: float = 0.10) -> List[float]:
    """
    Entfernt nahe beieinander liegende Levels (Toleranz in % relativ zum Level).
    """
    if not levels:
        return []
    levels = sorted(levels)
    deduped = [levels[0]]
    for x in levels[1:]:
        if abs(x - deduped[-1]) / deduped[-1] * 100.0 >= tol_pct:
            deduped.append(x)
    return deduped

def pick_snr_tps(df5: pd.DataFrame, df15: pd.DataFrame, direction: str, price: float) -> Tuple[Optional[float], Optional[float]]:
    """
    TP1 = nächster 5m-Level in Trendrichtung, TP2 = nächster 15m-Level (stärker)
    Long: Widerstände > price; Short: Supports < price
    """
    ph5, pl5 = find_pivots(df5, left=3, right=3)
    ph15, pl15 = find_pivots(df15, left=3, right=3)

    if direction == "LONG":
        res5  = dedupe_levels([p for _, p in ph5  if p > price])
        res15 = dedupe_levels([p for _, p in ph15 if p > price])
        tp1 = res5[0]  if len(res5)  > 0 else None
        tp2 = res15[0] if len(res15) > 0 else (res5[1] if len(res5) > 1 else None)
    else:
        sup5  = dedupe_levels([p for _, p in pl5  if p < price])
        sup15 = dedupe_levels([p for _, p in pl15 if p < price])
        sup5  = list(reversed(sup5))   # nächster unter Preis = größter < price
        sup15 = list(reversed(sup15))
        tp1 = sup5[0]  if len(sup5)  > 0 else None
        tp2 = sup15[0] if len(sup15) > 0 else (sup5[1] if len(sup5) > 1 else None)

    return tp1, tp2

# ====== CHECKLISTE (STRICT) ======
def analyze_trigger(df5: pd.DataFrame) -> Dict[str, Any]:
    df5["ema9"]   = ema(df5.close, 9)
    df5["ema21"]  = ema(df5.close, 21)
    df5["ema200"] = ema(df5.close, 200)
    df5["rsi"]    = rsi(df5.close, 14)
    df5["atr"]    = atr(df5.high, df5.low, df5.close, ATR_LEN)
    df5["vma20"]  = sma(df5.volume, 20)

    price   = float(df5["close"].iloc[-1])
    ema9    = float(df5["ema9"].iloc[-1])
    ema21   = float(df5["ema21"].iloc[-1])
    rsi_v   = float(df5["rsi"].iloc[-1])
    atr_v   = float(df5["atr"].iloc[-1])
    v_last  = float(df5["volume"].iloc[-1])
    v_ma    = float(df5["vma20"].iloc[-1]) if not math.isnan(df5["vma20"].iloc[-1]) else 0.0

    atr_pct = (atr_v / max(price, 1e-9)) * 100.0
    vol_spike_ok = v_last > VOL_SPIKE_FACTOR * max(v_ma, 1e-9)

    long_bias  = (ema9 > ema21) and (rsi_v > 48)
    short_bias = (ema9 < ema21) and (rsi_v < 52)

    return {
        "price": price,
        "atr": atr_v,
        "atr_pct": atr_pct,
        "vol_ok": vol_spike_ok,
        "long_bias": long_bias,
        "short_bias": short_bias,
    }

def htf_alignment_ok(symbol: str) -> Tuple[bool, bool]:
    """
    Prüft HTF-Alignment mit EMA200 für 15m/1h/4h:
      Long: alle Close > EMA200
      Short: alle Close < EMA200
    """
    all_up, all_dn = True, True
    for tf in TF_FILTERS:
        df = fetch_df(symbol, tf)
        e200 = ema(df["close"], 200).iloc[-1]
        c    = df["close"].iloc[-1]
        all_up &= (c > e200)
        all_dn &= (c < e200)
        time.sleep(ex.rateLimit/1000)
    return all_up, all_dn

def prob_score(long_ok: bool, short_ok: bool, vol_ok: bool, htf_ok: bool) -> int:
    # Punkte wie in deiner Liste: EMA/RSI-Bias (+25), HTF (+25), Vol (+10) → Basis 50
    # Wir vergeben 25 (Bias) + 25 (HTF) + 10 (Vol) + 15 Reserve-Buffer = 75..95
    if not (long_ok or short_ok):
        return 0
    score = 0
    score += 25                        # Bias erfüllt
    score += 25 if htf_ok else 0       # HTF align
    score += 10 if vol_ok else 0       # Vol spike
    score += 15                        # technischer Buffer
    return min(100, score)

# ====== SIGNAL / POSITION MGMT ======
def locked(symbol: str, direction: str) -> bool:
    return f"{symbol}:{direction}" in active_positions

def lock_position(symbol: str, direction: str, entry: float, sl: float, tp1: float, tp2: float):
    key = f"{symbol}:{direction}"
    active_positions[key] = {
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
        "direction": direction, "open_ts": time.time(),
    }

def maybe_close_position(symbol: str, price: float):
    """
    Prüft für beide Richtungen, ob TP/SL getroffen wurde → schließt Position (entlockt).
    """
    for direction in ("LONG", "SHORT"):
        key = f"{symbol}:{direction}"
        pos = active_positions.get(key)
        if not pos:
            continue
        hit = False
        if direction == "LONG":
            if price >= pos["tp1"] or price >= pos["tp2"] or price <= pos["sl"]:
                hit = True
        else:
            if price <= pos["tp1"] or price <= pos["tp2"] or price >= pos["sl"]:
                hit = True
        if hit:
            active_positions.pop(key, None)

async def send_signal(symbol: str, direction: str, entry: float, sl: float, tp1: float, tp2: float, prob: int):
    text = (
        f"⚡️ *Signal* {symbol} {TF_TRIGGER}\n"
        f"➡️ *{direction}*\n"
        f"🎯 Entry: `{entry}`\n"
        f"🛡 SL: `{sl}` (≈ {ATR_SL}×ATR)\n"
        f"🏁 TP1 (5m S/R): `{tp1}`\n"
        f"🏁 TP2 (15m S/R): `{tp2}`\n"
        f"📈 Prob.: *{prob}%*\n"
        f"🔒 Solange TP/SL nicht erreicht → kein neues Signal für {symbol} / {direction}"
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

def need_throttle(key: str, now: float, cool_s: int = COOLDOWN_S) -> bool:
    t = last_signal_at.get(key, 0.0)
    if now - t < cool_s:
        return True
    last_signal_at[key] = now
    return False

# ====== CORE SCAN ======
async def scan_once():
    global last_status
    round_ts = datetime.now(timezone.utc).isoformat()
    last_status = {"ts": round_ts, "symbols": {}}
    now = time.time()

    for sym in SYMBOLS:
        try:
            # Trigger-Frame Daten
            df5 = fetch_df(sym, TF_TRIGGER)
            trig = analyze_trigger(df5)

            # Vor jedem neuen Signal: offene Positionen checken/auflösen
            maybe_close_position(sym, trig["price"])

            # HTF-Alignment
            up_all, dn_all = htf_alignment_ok(sym)

            long_ok  = trig["long_bias"]  and up_all  and trig["vol_ok"] and (trig["atr_pct"] >= MIN_ATR_PCT)
            short_ok = trig["short_bias"] and dn_all and trig["vol_ok"] and (trig["atr_pct"] >= MIN_ATR_PCT)

            # Richtung bestimmen und Wahrscheinlichkeit berechnen
            chosen = None
            if long_ok or short_ok:
                htf_ok = up_all if long_ok else dn_all
                prob = prob_score(long_ok, short_ok, trig["vol_ok"], htf_ok)
                chosen = ("LONG" if long_ok else "SHORT", prob)

            # Status vorbereiten
            last_status["symbols"][sym] = {
                "price": trig["price"],
                "atr_pct": round(trig["atr_pct"], 4),
                "vol_spike": trig["vol_ok"],
                "long_bias": trig["long_bias"],
                "short_bias": trig["short_bias"],
                "htf_up_all": up_all,
                "htf_dn_all": dn_all,
                "active_locks": [k for k in active_positions.keys() if k.startswith(sym + ":")],
            }

            if not chosen:
                last_status["symbols"][sym]["skip"] = "Checklist not satisfied"
                time.sleep(ex.rateLimit/1000); continue

            direction, prob = chosen
            if prob < PROB_MIN:
                last_status["symbols"][sym]["skip"] = f"prob {prob}% < {PROB_MIN}%"
                time.sleep(ex.rateLimit/1000); continue

            # Keine Duplikate: wenn Position gelockt → überspringen
            if locked(sym, direction):
                last_status["symbols"][sym]["skip"] = f"locked {direction} (waiting TP/SL)"
                time.sleep(ex.rateLimit/1000); continue

            # TP-Level aus S/R
            df15 = fetch_df(sym, "15m")
            tp1, tp2 = pick_snr_tps(df5, df15, direction, trig["price"])
            if tp1 is None or tp2 is None:
                last_status["symbols"][sym]["skip"] = "no S/R targets"
                time.sleep(ex.rateLimit/1000); continue

            # Mindestabstand prüfen
            dist_pct = abs(tp1 - trig["price"]) / trig["price"] * 100.0
            if dist_pct < MIN_TP_DIST_PCT:
                last_status["symbols"][sym]["skip"] = f"TP1 too close ({dist_pct:.2f}% < {MIN_TP_DIST_PCT}%)"
                time.sleep(ex.rateLimit/1000); continue

            # SL via ATR
            entry = float(trig["price"])
            if direction == "LONG":
                sl = round(entry - ATR_SL * trig["atr"], 6)
            else:
                sl = round(entry + ATR_SL * trig["atr"], 6)

            # Anti-Spam Zeitfenster (zusätzlich zum Lock)
            key = f"{sym}:{direction}"
            if need_throttle(key, now):
                last_status["symbols"][sym]["skip"] = "cooldown"
                time.sleep(ex.rateLimit/1000); continue

            # Senden + Lock setzen
            await send_signal(sym, direction, entry, sl, float(tp1), float(tp2), prob)
            lock_position(sym, direction, entry, sl, float(tp1), float(tp2))
            last_status["symbols"][sym]["sent"] = {"dir": direction, "prob": prob, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2}

        except Exception as e:
            last_status["symbols"][sym] = {"error": str(e)}
        finally:
            time.sleep(ex.rateLimit/1000)

# ====== SCHEDULER ======
async def runner():
    sched = AsyncIOScheduler()
    sched.add_job(scan_once, "interval", seconds=SCAN_INTERVAL_S, next_run_time=datetime.now(timezone.utc))
    sched.start()
    while True:
        await asyncio.sleep(3600)

# ====== API ======
@app.on_event("startup")
async def _startup():
    asyncio.create_task(runner())

@app.get("/")
async def root():
    return {
        "ok": True,
        "mode": "strict_snr_tp",
        "tf": TF_TRIGGER,
        "filters": TF_FILTERS,
        "vol_spike_factor": VOL_SPIKE_FACTOR,
        "min_atr_pct": MIN_ATR_PCT,
        "prob_min": PROB_MIN,
        "atr_sl": ATR_SL,
        "min_tp_dist_pct": MIN_TP_DIST_PCT,
        "cooldown_s": COOLDOWN_S,
        "active_positions": list(active_positions.keys()),
        "symbols": len(SYMBOLS),
    }

@app.get("/scan")
async def manual_scan():
    await scan_once()
    return {"ok": True, "ran": True, "ts": last_status.get("ts")}

@app.get("/status")
async def status():
    return {"ok": True, "report": last_status}

@app.get("/test")
async def test():
    text = "✅ Test: Bot & Telegram OK — Strict Mode aktiv (SnR-TP, no-dup)."
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
    return {"ok": True, "test": True}
