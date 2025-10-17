import os, asyncio, math
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Any

import pandas as pd
import pandas_ta as ta
import ccxt
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ====== Config ======
TG_TOKEN          = os.getenv("TG_TOKEN")
TG_CHAT_ID        = os.getenv("TG_CHAT_ID")
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY")
COINGLASS_URL     = "https://open-api-v4.coinglass.com"
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET")  # optional für /tv-telegram-relay

if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID environment variables.")

# 25 liquid Spot pairs (MEXC notation "XXX/USDT")
SYMBOLS = [
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
    "TON/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT","LINK/USDT",
    "TRX/USDT","DOT/USDT","MATIC/USDT","SHIB/USDT","PEPE/USDT",
    "LTC/USDT","BCH/USDT","ATOM/USDT","NEAR/USDT","APT/USDT",
    "ARB/USDT","OP/USDT","SUI/USDT","INJ/USDT","FIL/USDT",
]

# ====== Analyse & Scan ======
LOOKBACK        = 300
SCAN_INTERVAL_S = 15 * 60   # alle 15 Minuten

# ====== S/R (1h) Settings ======
SR_TF = "1h"
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
CLUSTER_PCT = 0.15 / 100.0
MIN_STRENGTH = 3
TP2_FACTOR = 1.20

# ====== ATR-Fallback ======
ATR_SL  = 1.5
TP1_ATR = 1.0
TP2_ATR = 1.8
TP3_ATR = 2.6

# ====== Checklist Settings ======
MIN_ATR_PCT      = 0.20
VOL_SPIKE_FACTOR = 1.30
PROB_MIN         = 60
COOLDOWN_S       = 300

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="MEXC Auto Scanner → Telegram (M15 30MA Break + Volume + S/R)")

ex = ccxt.mexc({"enableRateLimit": True})
last_signal: Dict[str, float] = {}
last_scan_report: Dict[str, Any] = {"ts": None, "symbols": {}}

# ====== TA Helpers ======
def ema(series: pd.Series, length: int):
    return ta.ema(series, length)

def sma(series: pd.Series, length: int):
    return ta.sma(series, length)

def rsi(series: pd.Series, length: int = 14):
    return ta.rsi(series, length)

def atr(h, l, c, length: int = 14):
    return ta.atr(h, l, c, length)

def vol_sma(v, length: int = 20):
    return ta.sma(v, length)

def prob_score(vol_ok: bool, ema200_align: bool) -> int:
    base = 70
    base += 5 if vol_ok else 0
    base += 5 if ema200_align else 0
    return min(base, 90)

def fetch_df(symbol: str, timeframe: str, limit: int = LOOKBACK) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","volume"])
    return df

# ====== Trigger (M15) ======
def analyze_trigger_m15(df: pd.DataFrame) -> Dict[str, Any]:
    """
    ENTRY-Pflicht: 15m 30MA Break + Volumen-Spike.
    Weitere Filter: EMA200 (auf 15m), ATR, RSI (nur Hinweis).
    """
    df["ema200"] = ema(df.close, 200)
    df["rsi"]    = rsi(df.close, 14)
    df["atr"]    = atr(df.high, df.low, df.close, 14)
    df["volma"]  = vol_sma(df.volume, 20)
    df["sma30"]  = sma(df.close, 30)

    c, v = df.close, df.volume
    vol_ok = v.iloc[-1] > (VOL_SPIKE_FACTOR * df.volma.iloc[-1])

    bull30 = (c.iloc[-2] < df.sma30.iloc[-2]) and (c.iloc[-1] > df.sma30.iloc[-1]) and vol_ok
    bear30 = (c.iloc[-2] > df.sma30.iloc[-2]) and (c.iloc[-1] < df.sma30.iloc[-1]) and vol_ok

    return {
        "bull30": bool(bull30),
        "bear30": bool(bear30),
        "vol_ok": bool(vol_ok),
        "atr": float(df.atr.iloc[-1]),
        "price": float(c.iloc[-1]),
        "ema200_up": (c.iloc[-1] > df.ema200.iloc[-1]),
        "ema200_dn": (c.iloc[-1] < df.ema200.iloc[-1]),
        "rsi": float(df.rsi.iloc[-1]),
    }

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

# ====== Checkliste (M15 Break ist Pflicht) ======
def build_checklist(direction: str, trig15: Dict[str, Any]) -> Tuple[bool, List[str], List[str]]:
    ok, warn = [], []

    atr_pct = (trig15["atr"] / max(trig15["price"], 1e-9)) * 100.0
    if atr_pct >= MIN_ATR_PCT: ok.append(f"ATR≥{MIN_ATR_PCT}% ({atr_pct:.2f}%)")
    else:                      return (False, ok, [f"ATR<{MIN_ATR_PCT}% ({atr_pct:.2f}%)"])

    # M15: 30MA Breakout mit Volumen (Pflicht)
    if direction == "LONG":
        if trig15["bull30"]: ok.append("M15 30MA Break ↑ (mit Volumen)")
        else:                return (False, ok, ["Kein M15 30MA Break↑"])
    else:
        if trig15["bear30"]: ok.append("M15 30MA Break ↓ (mit Volumen)")
        else:                return (False, ok, ["Kein M15 30MA Break↓"])

    # EMA200 Richtung (M15)
    ema200_ok = trig15["ema200_up"] if direction=="LONG" else trig15["ema200_dn"]
    if ema200_ok: ok.append("EMA200 ok")
    else:         return (False, ok, ["EMA200 gegen Setup"])

    # Volumen (zusätzlich abprüfen)
    if trig15["vol_ok"]: ok.append(f"Vol>{VOL_SPIKE_FACTOR:.2f}×MA20")
    else:                return (False, ok, [f"kein Vol-Spike (≥{VOL_SPIKE_FACTOR:.2f}× Pflicht)"])

    # RSI nur Hinweis
    if direction=="LONG":
        if trig15["rsi"] > 67: warn.append(f"RSI hoch ({trig15['rsi']:.1f})")
        else:                  ok.append(f"RSI ok ({trig15['rsi']:.1f})")
    else:
        if trig15["rsi"] < 33: warn.append(f"RSI tief ({trig15['rsi']:.1f})")
        else:                  ok.append(f"RSI ok ({trig15['rsi']:.1f})")

    return (True, ok, warn)

def need_throttle(key: str, now: float, cool_s: int = COOLDOWN_S) -> bool:
    t = last_signal.get(key, 0.0)
    if now - t < cool_s:
        return True
    last_signal[key] = now
    return False

async def send_signal(symbol: str, direction: str, entry: float, sl: float, tp1: float, tp2: float, tp3: float | None,
                      prob: int, checklist_ok: List[str], checklist_warn: List[str], used_sr: bool):
    checks_line = ""
    if checklist_ok:   checks_line += f"✅ {', '.join(checklist_ok)}\n"
    if checklist_warn: checks_line += f"⚠️ {', '.join(checklist_warn)}\n"
    sr_note = f"S/R {SR_TF}" if used_sr else "ATR-Fallback"

    text = (
        f"🛡 *Scanner Signal* — {symbol} (M15 30MA Break + Vol)\n"
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
        "🛡 *Scanner gestartet – MODUS: M15 30MA Breakout (mit Volumen) + S/R (1h)*\n"
        f"• Scan alle 15 Minuten\n"
        "• Entry: M15 30MA Breakout mit Volumen (Pflicht)\n"
        f"• Volumen: Pflicht ≥ {VOL_SPIKE_FACTOR:.2f}× MA20\n"
        f"• ATR%-Schwelle aktiv (≥ {MIN_ATR_PCT:.2f}%)\n"
        "• TP/SL über 1h Support/Resistance (TP2 = +20% Distanz). Fallback: ATR\n"
        + (f"\n• CoinGlass Heatmap 12h (optional): Richtung muss matchen" if COINGLASS_API_KEY else "")
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

# ====== Scan ======
async def scan_once():
    global last_scan_report
    now = asyncio.get_event_loop().time()
    round_ts = datetime.now(timezone.utc).isoformat()
    last_scan_report = {"ts": round_ts, "symbols": {}}

    for sym in SYMBOLS:
        try:
            # M15 Trigger (Entry)
            df15   = fetch_df(sym, "15m")
            trig15 = analyze_trigger_m15(df15)
            price  = trig15["price"]

            # CoinGlass 12h Heatmap (optional Richtungsfilter)
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

            # Kandidaten nur basierend auf M15 Break Richtung (+ optional Heatmap)
            candidates = []
            for direction in ("LONG", "SHORT"):
                passed, ok_tags, warn_tags = build_checklist(direction, trig15)
                if not passed:
                    continue
                if cg_dir and cg_dir != direction:
                    continue
                prob = prob_score(trig15["vol_ok"], trig15["ema200_up"] if direction=="LONG" else trig15["ema200_dn"])
                candidates.append((direction, prob, ok_tags, warn_tags))

            if candidates:
                direction, prob, ok_tags, warn_tags = sorted(candidates, key=lambda x: x[1], reverse=True)[0]
                if prob >= PROB_MIN:
                    df_sr = fetch_df(sym, SR_TF, limit=LOOKBACK)
                    entry, sl, tp1, tp2, maybe_tp3, used_sr = make_levels_sr(direction, price, trig15["atr"], df_sr)

                    key = f"{sym}:{direction}"
                    throttled = need_throttle(key, now)

                    last_scan_report["symbols"][sym] = {
                        "direction": direction, "prob": prob, "throttled": throttled,
                        "ok": ok_tags, "warn": warn_tags,
                        "price": price, "atr": trig15["atr"],
                        "sr_used": used_sr, "heatmap_dir": cg_dir
                    }
                    if not throttled:
                        await send_signal(sym, direction, entry, sl, tp1, tp2, maybe_tp3, prob, ok_tags, warn_tags, used_sr)
                else:
                    last_scan_report["symbols"][sym] = {"skip": f"Prob. {prob}% < {PROB_MIN}%"}
            else:
                last_scan_report["symbols"][sym] = {"skip": "Kein Setup (M15 Break mit Volumen nicht erfüllt)"}

        except Exception as e:
            last_scan_report["symbols"][sym] = {"error": str(e)}
        finally:
            await asyncio.sleep(ex.rateLimit / 1000 if hasattr(ex, "rateLimit") else 0.2)

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
    text = "✅ Test: Bot & Telegram OK — Mode: M15 30MA Break + Vol + S/R"
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
    return {"ok": True, "test": True}

@app.get("/scan")
async def manual_scan():
    await scan_once()
    return {"ok": True, "ran": True, "ts": last_scan_report.get("ts")}

@app.get("/status")
async def status():
    return {"ok": True, "mode": "m15_break_only", "report": last_scan_report}

@app.get("/")
async def root():
    return {"ok": True, "mode": "m15_break_only"}

@app.post("/tv-telegram-relay")
async def tv_telegram_relay(req: Request):
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if WEBHOOK_SECRET and (not isinstance(data, dict) or data.get("secret") != WEBHOOK_SECRET):
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

# Auto-Link-Seite (klickbare, korrekte Links zu deiner Domain)
@app.get("/links")
async def links(request: Request):
    base = str(request.base_url).rstrip("/")
    html = f"""
    <html><body style="font-family: system-ui; line-height:1.5; padding:16px">
      <h2>✅ Deine Endpunkte (auto-generiert)</h2>
      <ul>
        <li><a href="{base}/">Root / Health</a></li>
        <li><a href="{base}/test">Telegram Test</a></li>
        <li><a href="{base}/scan">Sofort-Scan</a></li>
        <li><a href="{base}/status">Letzter Report</a></li>
        <li><a href="{base}/tv-telegram-relay">TV → Telegram Relay (POST)</a></li>
        <li><a href="{base}/links">Diese Link-Seite</a></li>
      </ul>
      <h3>cURL-Beispiel für Relay</h3>
      <pre>curl -X POST "{base}/tv-telegram-relay" -H "Content-Type: application/json" -d '{{"secret":"DEIN_SECRET","symbol":"BTCUSDT","timeframe":"15m","direction":"LONG","message":"Relay-Test"}}'</pre>
    </body></html>
    """
    return HTMLResponse(content=html)
