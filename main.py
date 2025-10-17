import os, asyncio, time, math
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Any

import pandas as pd
import pandas_ta as ta
import ccxt
import requests
from fastapi import FastAPI, Request, HTTPException
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ====== Config ======
TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY")
COINGLASS_URL     = "https://open-api-v4.coinglass.com"
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET")  # optional für /tv-telegram-relay

if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID environment variables.")
# COINGLASS_API_KEY ist optional; wenn nicht gesetzt, wird die Heatmap nicht gefiltert.

# 25 liquid Spot pairs (MEXC notation "XXX/USDT")
SYMBOLS = [
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
    "TON/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT","LINK/USDT",
    "TRX/USDT","DOT/USDT","MATIC/USDT","SHIB/USDT","PEPE/USDT",
    "LTC/USDT","BCH/USDT","ATOM/USDT","NEAR/USDT","APT/USDT",
    "ARB/USDT","OP/USDT","SUI/USDT","INJ/USDT","FIL/USDT",
]

# ====== Analyse & Scan ======
TF_TRIGGER = "5m"                  # Haupt-Timeframe für Entry
TF_FILTERS = ["5m","15m"]          # Nur 5m + 15m für Trendrichtung
LOOKBACK = 300
SCAN_INTERVAL_S = 15 * 60          # alle 15 Minuten

# ====== Safe-Entry (Disabled, using 30MA break instead) ======
SAFE_ENTRY_REQUIRED = False        # Disable fib pullback requirement

# ====== S/R (1h) Settings ======
SR_TF = "1h"                       # Use 1h for support/resistance pivots
PIVOT_LEFT = 3                    # Pivot width for swings (1h)
PIVOT_RIGHT = 3
CLUSTER_PCT = 0.15 / 100.0        # Cluster tolerance (±0.15 %)
MIN_STRENGTH = 3                  # Minimum number of swings for a strong level
TP2_FACTOR = 1.20                 # TP2 = Entry + 1.2*(TP1-Entry) (symmetrically for shorts)

# ====== ATR-Fallback (if S/R not available) ======
ATR_SL  = 1.5
TP1_ATR = 1.0
TP2_ATR = 1.8
TP3_ATR = 2.6

# ====== Strict Checklist Settings ======
MIN_ATR_PCT        = 0.20       # min ATR% of price (20%)
VOL_SPIKE_FACTOR   = 1.30       # Volume > 1.30× MA20 (required)
REQUIRE_VOL_SPIKE  = True       # Vol spike is KO criterion
PROB_MIN           = 60         # min 60% probability (score)
COOLDOWN_S         = 300        # Anti-spam per symbol/direction

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="MEXC Auto Scanner → Telegram (30MA Break + 12h Liquidity Heatmap + S/R)")

ex = ccxt.mexc({"enableRateLimit": True})
last_signal: Dict[str, float] = {}
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

def fetch_df(symbol: str, timeframe: str, limit: int = LOOKBACK) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","volume"])
    return df

# ====== Trigger & Trend ======
def compute_trend_ok(df: pd.DataFrame) -> Tuple[bool,bool,bool]:
    ema200 = ema(df["close"], 200)
    up = df["close"].iloc[-1] > ema200.iloc[-1]
    down = df["close"].iloc[-1] < ema200.iloc[-1]
    return up, down, not math.isnan(df["close"].iloc[-1])

def analyze_trigger(df: pd.DataFrame) -> Dict[str, Any]:
    # Add moving averages and RSI
    df["ema50"]  = ema(df.close, 50)
    df["ema100"] = ema(df.close, 100)
    df["ema200"] = ema(df.close, 200)
    df["rsi"]    = rsi(df.close, 14)
    df["atr"]    = atr(df.high, df.low, df.close, 14)
    df["volma"]  = vol_sma(df.volume, 20)
    df["sma30"]  = ta.sma(df.close, 30)  # 30-period simple MA

    o, h, l, c, v = df.open, df.high, df.low, df.close, df.volume

    # Traditional triggers
    long_fast  = (c.iloc[-1] > df.ema50.iloc[-1] > df.ema100.iloc[-1]) and (df.rsi.iloc[-1] <= 65)
    short_fast = (c.iloc[-1] < df.ema50.iloc[-1] < df.ema100.iloc[-1]) and (df.rsi.iloc[-1] >= 35)
    bull   = bullish_engulf(o, h, l, c)
    bear   = bearish_engulf(o, h, l, c)
    vol_ok = v.iloc[-1] > (VOL_SPIKE_FACTOR * df.volma.iloc[-1])

    # 30 MA breakout triggers
    bull30 = (c.iloc[-2] < df.sma30.iloc[-2]) and (c.iloc[-1] > df.sma30.iloc[-1]) and vol_ok
    bear30 = (c.iloc[-2] > df.sma30.iloc[-2]) and (c.iloc[-1] < df.sma30.iloc[-1]) and vol_ok

    return {
        "bull": bull, "bear": bear,
        "long_fast": long_fast, "short_fast": short_fast,
        "bull30": bull30, "bear30": bear30,
        "vol_ok": bool(vol_ok),
        "atr": float(df.atr.iloc[-1]),
        "price": float(c.iloc[-1]),
        "ema200_up": (c.iloc[-1] > df.ema200.iloc[-1]),
        "ema200_dn": (c.iloc[-1] < df.ema200.iloc[-1]),
        "rsi": float(df.rsi.iloc[-1]),
    }

# ====== Safe-Entry via Fib (disabled now) ======
def find_pivot_indices(values: List[float], left: int, right: int, is_high: bool) -> List[int]:
    idxs = []
    n = len(values)
    for i in range(left, n - right):
        left_side = values[i-left:i]
        right_side = values[i+1:i+1+right]
        if is_high:
            if all(values[i] > v for v in left_side) and all(values[i] > v for v in right_side):
                idxs.append(i)
        else:
            if all(values[i] < v for v in left_side) and all(values[i] < v for v in right_side):
                idxs.append(i)
    return idxs

def last_impulse(df: pd.DataFrame) -> Tuple[Tuple[int,float], Tuple[int,float]] | None:
    highs = df["high"].values
    lows  = df["low"].values
    hi_idx = find_pivot_indices(list(highs), PIVOT_LEFT, PIVOT_RIGHT, True)
    lo_idx = find_pivot_indices(list(lows ), PIVOT_LEFT, PIVOT_RIGHT, False)
    if not hi_idx or not lo_idx:
        return None
    last_hi_i = hi_idx[-1]
    last_lo_i = lo_idx[-1]
    if last_lo_i < last_hi_i:
        return ((last_lo_i, lows[last_lo_i]), (last_hi_i, highs[last_hi_i]))
    elif last_hi_i < last_lo_i:
        return ((last_lo_i, lows[last_lo_i]), (last_hi_i, highs[last_hi_i]))
    return None

def fib_zone_ok(price: float, impulse: Tuple[Tuple[int,float], Tuple[int,float]], direction: str) -> Tuple[bool, Tuple[float,float]]:
    (lo_i, lo_v), (hi_i, hi_v) = impulse
    if hi_i == lo_i:
        return (False, (0.0, 0.0))
    if hi_i > lo_i:
        fib50  = lo_v + 0.5  * (hi_v - lo_v)
        fib618 = lo_v + 0.618 * (hi_v - lo_v)
        zmin, zmax = sorted([fib50, fib618])
        zmin *= (1 - 0.001)
        zmax *= (1 + 0.001)
        ok = (direction == "LONG") and (zmin <= price <= zmax)
        return (ok, (zmin, zmax))
    else:
        fib50  = hi_v - 0.5  * (hi_v - lo_v)
        fib618 = hi_v - 0.618 * (hi_v - lo_v)
        zmin, zmax = sorted([fib618, fib50])
        zmin *= (1 - 0.001)
        zmax *= (1 + 0.001)
        ok = (direction == "SHORT") and (zmin <= price <= zmax)
        return (ok, (zmin, zmax))

# ====== S/R Levels (1h) ======
def find_pivots_levels(df: pd.DataFrame) -> Tuple[List[Tuple[float,int]], List[Tuple[float,int]]]:
    highs = df["high"].values
    lows  = df["low"].values
    n = len(df)
    swing_highs, swing_lows = [], []
    for i in range(PIVOT_LEFT, n - PIVOT_RIGHT):
        left  = highs[i-PIVOT_LEFT:i]; right = highs[i+1:i+1+PIVOT_RIGHT]
        if all(highs[i] > left) and all(highs[i] > right):
            swing_highs.append(highs[i])
        left  = lows[i-PIVOT_LEFT:i]; right = lows[i+1:i+1+PIVOT_RIGHT]
        if all(lows[i] < left) and all(lows[i] < right):
            swing_lows.append(lows[i])

    def cluster_levels(values: List[float], tol_pct: float) -> List[Tuple[float,int]]:
        values = sorted(values)
        clusters = []
        if not values:
            return clusters
        cluster = [values[0]]
        for x in values[1:]:
            center = sum(cluster)/len(cluster)
            if abs(x - center) / center <= tol_pct:
                cluster.append(x)
            else:
                clusters.append((sum(cluster)/len(cluster), len(cluster)))
                cluster = [x]
        clusters.append((sum(cluster)/len(cluster), len(cluster)))
        clusters.sort(key=lambda t: (t[1], t[0]), reverse=True)
        return clusters

    res_clusters = cluster_levels(swing_highs, CLUSTER_PCT)
    sup_clusters = cluster_levels(swing_lows , CLUSTER_PCT)
    return res_clusters, sup_clusters

def nearest_level(levels: List[Tuple[float,int]], ref_price: float, direction: str, min_strength: int) -> float | None:
    candidates = []
    for price, strength in levels:
        if strength < min_strength: continue
        if direction == "LONG" and price > ref_price:
            candidates.append(price)
        elif direction == "SHORT" and price < ref_price:
            candidates.append(price)
    if not candidates: return None
    return min(candidates) if direction == "LONG" else max(candidates)

def make_levels_sr(direction: str, entry: float, atrv: float, df_sr: pd.DataFrame) -> Tuple[float,float,float,float,float,bool]:
    res_lvls, sup_lvls = find_pivots_levels(df_sr)
    if direction == "LONG":
        tp1_sr = nearest_level(res_lvls, entry, "LONG", MIN_STRENGTH)
        sl_sr  = nearest_level(sup_lvls, entry, "SHORT", MIN_STRENGTH)
        if tp1_sr is not None and sl_sr is not None and tp1_sr > entry and sl_sr < entry:
            tp2 = round(entry + (tp1_sr - entry) * TP2_FACTOR, 6)
            return entry, round(sl_sr,6), round(tp1_sr,6), tp2, None, True
    else:
        tp1_sr = nearest_level(sup_lvls, entry, "SHORT", MIN_STRENGTH)
        sl_sr  = nearest_level(res_lvls, entry, "LONG", MIN_STRENGTH)
        if tp1_sr is not None and sl_sr is not None and tp1_sr < entry and sl_sr > entry:
            tp2 = round(entry - (entry - tp1_sr) * TP2_FACTOR, 6)
            return entry, round(sl_sr,6), round(tp1_sr,6), tp2, None, True

    # Fallback zu ATR
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
    return entry, sl, tp1, tp2, tp3, False

# ====== Strict Checklist ======
def build_checklist_for_dir(direction: str, trig: Dict[str, Any], up_all: bool, dn_all: bool) -> Tuple[bool, List[str], List[str]]:
    ok, warn = [], []

    atr_pct = (trig["atr"] / max(trig["price"], 1e-9)) * 100.0
    if atr_pct >= MIN_ATR_PCT:
        ok.append(f"ATR≥{MIN_ATR_PCT}% ({atr_pct:.2f}%)")
    else:
        return (False, ok, [f"ATR<{MIN_ATR_PCT}% ({atr_pct:.2f}%)"])

    if (up_all if direction=="LONG" else dn_all):
        ok.append("Trend HTF aligniert")
    else:
        return (False, ok, ["HTF nicht aligned"])

    # 30MA-Break zählt als Bias
    if direction == "LONG":
        bias_ok = trig["bull"] or trig["long_fast"] or trig["bull30"]
        if bias_ok:
            labels = []
            if trig["bull"]: labels.append("Engulf")
            if trig["long_fast"]: labels.append("EMA-Stack")
            if trig["bull30"]: labels.append("30MA-Break")
            ok.append("/".join(labels) if labels else "Bullish Signal")
        else:
            return (False, ok, ["Kein bullisches Setup"])
    else:
        bias_ok = trig["bear"] or trig["short_fast"] or trig["bear30"]
        if bias_ok:
            labels = []
            if trig["bear"]: labels.append("Bearish Engulf")
            if trig["short_fast"]: labels.append("EMA-Stack")
            if trig["bear30"]: labels.append("30MA-Break")
            ok.append("/".join(labels) if labels else "Bearish Signal")
        else:
            return (False, ok, ["Kein bearishes Setup"])

    ema200_ok = trig["ema200_up"] if direction=="LONG" else trig["ema200_dn"]
    if ema200_ok:
        ok.append("EMA200 ok")
    else:
        return (False, ok, ["EMA200 gegen Setup"])

    if trig["vol_ok"]:
        ok.append(f"Vol>{VOL_SPIKE_FACTOR:.2f}×MA20 (Pflicht)")
    else:
        return (False, ok, [f"kein Vol-Spike (≥{VOL_SPIKE_FACTOR:.2f}× Pflicht)"])

    # RSI nur Hinweis
    if direction=="LONG":
        if trig["rsi"] > 67:
            warn.append(f"RSI hoch ({trig['rsi']:.1f})")
        else:
            ok.append(f"RSI ok ({trig['rsi']:.1f})")
    else:
        if trig["rsi"] < 33:
            warn.append(f"RSI tief ({trig['rsi']:.1f})")
        else:
            ok.append(f"RSI ok ({trig['rsi']:.1f})")

    return (True, ok, warn)

def need_throttle(key: str, now: float, cool_s: int = COOLDOWN_S) -> bool:
    t = last_signal.get(key, 0.0)
    if now - t < cool_s:
        return True
    last_signal[key] = now
    return False

async def send_signal(symbol: str, tf: str, direction: str,
                      entry: float, sl: float, tp1: float, tp2: float, tp3: float | None,
                      prob: int, checklist_ok: List[str], checklist_warn: List[str], used_sr: bool):
    checks_line = ""
    if checklist_ok:   checks_line += f"✅ {', '.join(checklist_ok)}\n"
    if checklist_warn: checks_line += f"⚠️ {', '.join(checklist_warn)}\n"
    sr_note = f"S/R {SR_TF}" if used_sr else "ATR-Fallback"

    text = (
        f"🛡 *Scanner Signal* — {symbol} ({tf})\n"
        f"➡️ *{direction}*  ({sr_note})\n"
        f"🎯 Entry: `{entry}`\n"
        f"🛡 SL: `{sl}`\n"
        f"🏁 TP1: `{tp1}`\n"
        f"🏁 TP2: `{tp2}`\n"
        + (f"🏁 TP3: `{tp3}`\n" if tp3 is not None else "")
        + f"📈 Wahrscheinlichkeit: *{prob}%*\n"
        f"{checks_line}".strip()
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

async def send_mode_banner():
    text = (
        "🛡 *Scanner gestartet – MODUS: 30MA + 12h Heatmap + S/R (1h)*\n"
        f"• Scan alle 15 Minuten\n"
        "• Entry: Breakout der 30MA mit Volumen (5m)\n"
        "• HTF: Streng align (5m/15m/1h/4h)\n"
        f"• Volumen: Pflicht ≥ {VOL_SPIKE_FACTOR:.2f}× MA20\n"
        f"• ATR%-Schwelle aktiv (≥ {MIN_ATR_PCT:.2f}%)\n"
        "• TP/SL über 1h Support/Resistance (TP2 = +20% Distanz). Fallback: ATR\n"
        + (f"\n• CoinGlass Heatmap 12h: Richtung check" if COINGLASS_API_KEY else "")
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

# ====== Scan ======
async def scan_once():
    global last_scan_report
    now = time.time()
    round_ts = datetime.now(timezone.utc).isoformat()
    last_scan_report = {"ts": round_ts, "symbols": {}}

    for sym in SYMBOLS:
        try:
            # Trigger timeframe analysis
            df5  = fetch_df(sym, TF_TRIGGER)
            trig = analyze_trigger(df5)
            price = trig["price"]

            # Direction via CoinGlass 12h liquidity heatmap (optional)
            cg_dir = None
            if COINGLASS_API_KEY:
                base = sym.split("/")[0]
                params = {"symbol": base, "range": "12h"}
                try:
                    resp = requests.get(
                        f"{COINGLASS_URL}/api/futures/liquidation/aggregated-heatmap/model1",
                        headers={"CG-API-KEY": COINGLASS_API_KEY},
                        params=params,
                        timeout=5
                    )
                    data = resp.json()
                    if data.get("code") == 0 and "data" in data:
                        y_axis = data["data"].get("y_axis", [])
                        liq = data["data"].get("liquidation_leverage_data", [])
                        if y_axis and liq:
                            sum_by_y = {}
                            for x_idx, y_idx, vol in liq:
                                sum_by_y[y_idx] = sum_by_y.get(y_idx, 0) + vol
                            if sum_by_y:
                                max_idx = max(sum_by_y, key=sum_by_y.get)
                                if 0 <= max_idx < len(y_axis):
                                    max_price = float(y_axis[max_idx])
                                    cg_dir = "LONG" if max_price > price else "SHORT"
                except Exception:
                    cg_dir = None

            # Trend filter across timeframes
            up_all, dn_all = True, True
            for tf in TF_FILTERS:
                df_tf = fetch_df(sym, tf)
                up, dn, _ = compute_trend_ok(df_tf)
                up_all &= up
                dn_all &= dn
                time.sleep(ex.rateLimit/1000)

            # Evaluate signals
            passes = []
            for direction in ("LONG","SHORT"):
                passed, ok_tags, warn_tags = build_checklist_for_dir(direction, trig, up_all, dn_all)
                if passed:
                    prob = prob_score(direction=="LONG", direction=="SHORT", trig["vol_ok"], up_all or dn_all,
                                      trig["ema200_up"] if direction=="LONG" else trig["ema200_dn"])
                    passes.append((direction, prob, ok_tags, warn_tags))

            # Filter by CoinGlass direction if available
            if cg_dir:
                passes = [p for p in passes if p[0] == cg_dir]

            if passes:
                direction, prob, ok_tags, warn_tags = sorted(passes, key=lambda x: x[1], reverse=True)[0]
                if prob >= PROB_MIN:
                    # Load S/R from 1h for targets
                    df_sr = fetch_df(sym, SR_TF, limit=LOOKBACK)
                    entry, sl, tp1, tp2, maybe_tp3, used_sr = make_levels_sr(direction, price, trig["atr"], df_sr)

                    key = f"{sym}:{direction}"
                    throttled = need_throttle(key, now)

                    last_scan_report["symbols"][sym] = {
                        "direction": direction, "prob": prob, "throttled": throttled,
                        "ok": ok_tags, "warn": warn_tags,
                        "price": price, "atr": trig["atr"],
                        "sr_used": used_sr, "heatmap_dir": cg_dir
                    }
                    if not throttled:
                        await send_signal(sym, TF_TRIGGER, direction, entry, sl, tp1, tp2, maybe_tp3, prob, ok_tags, warn_tags, used_sr)
                else:
                    last_scan_report["symbols"][sym] = {"skip": f"Prob. {prob}% < {PROB_MIN}%"}
            else:
                reason = "Kein Setup"
                if cg_dir and not passes:
                    reason = f"Headmap ({cg_dir}) vs Signal mismatch"
                last_scan_report["symbols"][sym] = {"skip": reason}

        except Exception as e:
            last_scan_report["symbols"][sym] = {"error": str(e)}
        finally:
            time.sleep(ex.rateLimit/1000)

# ====== Runner / API ======
async def runner():
    sched = AsyncIOScheduler()
    sched.add_job(scan_once, "interval", seconds=SCAN_INTERVAL_S, next_run_time=datetime.now(timezone.utc))
    sched.start()
    while True:
        await asyncio.sleep(3600)

@app.on_event("startup")
async def _startup():
    await send_mode_banner()
    asyncio.create_task(runner())

# ====== TEST & RELAY ENDPOINTS ======

@app.get("/test")
async def test():
    text = "✅ Test: Bot & Telegram OK — Mode: 30MA + Heatmap + S/R"
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
    return {"ok": True, "test": True}

@app.get("/scan")
async def manual_scan():
    await scan_once()
    return {"ok": True, "ran": True, "ts": last_scan_report.get("ts")}

@app.get("/status")
async def status():
    return {"ok": True, "mode": "30MA_heatmap", "report": last_scan_report}

@app.get("/")
async def root():
    return {"ok": True, "mode": "30MA_heatmap"}

@app.post("/tv-telegram-relay")
async def tv_telegram_relay(req: Request):
    """
    TradingView Webhook → Telegram.
    Erwartetes JSON:
    {
      "secret": "DEIN_SECRET",      # optional, wenn WEBHOOK_SECRET gesetzt
      "symbol": "BTCUSDT",
      "timeframe": "5m",
      "direction": "LONG",
      "message": "Text ..."
    }
    """
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if WEBHOOK_SECRET:
        if not isinstance(data, dict) or data.get("secret") != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized (bad secret)")

    symbol    = data.get("symbol")
    tf        = data.get("timeframe")
    direction = data.get("direction")
    msg       = data.get("message") or data.get("text") or data.get("alert") or ""

    parts = ["📡 *TradingView Alert*"]
    if symbol:    parts.append(f"• Symbol: `{symbol}`")
    if tf:        parts.append(f"• TF: `{tf}`")
    if direction: parts.append(f"• Richtung: *{direction}*")
    if msg:       parts.append(f"\n{msg}")
    text = "\n".join(parts)

    try:
        await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Telegram send failed: {e}")

    return {"ok": True, "forwarded": True}
