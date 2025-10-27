import os, asyncio, math
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Any, Optional

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
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY")  # optional
COINGLASS_URL     = "https://open-api-v4.coinglass.com"
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET")     # optional für /tv-telegram-relay

if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID environment variables.")

# 25 + erweiterte Liste (Duplikate werden unten entfernt)
SYMBOLS = [
    # ==== Ursprüngliche 25 ====
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
    "TON/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT","LINK/USDT",
    "TRX/USDT","DOT/USDT","MATIC/USDT","SHIB/USDT","PEPE/USDT",
    "LTC/USDT","BCH/USDT","ATOM/USDT","NEAR/USDT","APT/USDT",
    "ARB/USDT","OP/USDT","SUI/USDT","INJ/USDT","FIL/USDT",

    # ==== Erweiterte 50 ====
    "HBAR/USDT","CRV/USDT","CAKE/USDT","WLD/USDT","XLM/USDT",
    "TIA/USDT","CFX/USDT","WIF/USDT","TRUMP/USDT","BONK/USDT",
    "SEI/USDT","GALA/USDT","RNDR/USDT","AR/USDT","JUP/USDT",
    "PYTH/USDT","MEME/USDT","NOT/USDT","MEW/USDT","ZRO/USDT",
    "ENA/USDT","ORDI/USDT","SATS/USDT","JTO/USDT","HNT/USDT",
    "BLUR/USDT","DEGEN/USDT","OPUL/USDT","ALT/USDT","ACE/USDT",
    "RUNE/USDT","SKL/USDT","DYDX/USDT","SFP/USDT",
    "CETUS/USDT","TURBO/USDT","CHEEMS/USDT","CHZ/USDT","AERO/USDT",
    "PENDLE/USDT","STG/USDT","AKT/USDT","KAS/USDT","NTRN/USDT",
    "VET/USDT","FTM/USDT","EOS/USDT","FLOW/USDT","1INCH/USDT",
    "XTZ/USDT","MINA/USDT","MTL/USDT","MASK/USDT"
]
_seen = set()
SYMBOLS = [s for s in SYMBOLS if not (s in _seen or _seen.add(s))]

# ====== Analyse & Scan ======
LOOKBACK        = 300
SCAN_INTERVAL_S = 5 * 60   # Intervall (min)

# ====== Entry-Logik ======
# Pflicht 1: M15 30MA-Break + Volumen (mit Slope + RSI, siehe analyze_trigger_m15)
# Pflicht 2: Safe-Entry per Fib-Retest (0.5–0.618) auf FIB_CONFIRM_TF (mit richtungs-sensitivem Impuls)
FIB_CONFIRM_TF            = "5m"          # "5m" oder "15m"
SAFE_ENTRY_REQUIRED       = True          # Fib-Retest Pflicht (False = nur optional)
FIB_TOL_PCT               = 0.10 / 100.0  # ±0.10 % Toleranz
FIB_REQUIRE_CANDLE_CLOSE  = True          # Fib-Confirm NUR mit geschlossener Candle

PIVOT_LEFT_TRIG  = 3
PIVOT_RIGHT_TRIG = 3

# ====== S/R (1h) Settings ======
SR_TF = "1h"
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
CLUSTER_PCT = 0.15 / 100.0
MIN_STRENGTH = 3
TP2_FACTOR = 1.20  # für erweitertes S/R-Ziel (ehem. TP2)

# ====== ATR-Fallback ======
ATR_SL  = 1.5
TP1_ATR = 1.0
TP2_ATR = 1.8
TP3_ATR = 2.6     # wird jetzt als einziger ATR-TP benutzt

# ====== Checklist Settings ======
MIN_ATR_PCT      = 0.20
VOL_SPIKE_FACTOR = 1.15
PROB_MIN         = 60
COOLDOWN_S       = 5000

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="MEXC Auto Scanner → Telegram (M15 30MA Break + Fib-Retest + S/R)")

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

def prob_score(vol_ok: bool, ema200_align: bool, safe_ok: bool) -> int:
    base = 70
    base += 5 if vol_ok else 0
    base += 5 if ema200_align else 0
    base += 5 if safe_ok else 0
    return min(base, 90)

def fetch_df(symbol: str, timeframe: str, limit: int = LOOKBACK) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","volume"])
    return df

# ====== Trigger (M15) — Punkt 1: Slope + RSI in den Break integrieren ======
def analyze_trigger_m15(df: pd.DataFrame) -> Dict[str, Any]:
    df = df.copy()
    df["ema200"] = ema(df.close, 200)
    df["rsi"]    = rsi(df.close, 14)
    df["atr"]    = atr(df.high, df.low, df.close, 14)
    df["volma"]  = vol_sma(df.volume, 20)
    df["sma30"]  = sma(df.close, 30)

    # MA30-Slope über die letzten 3 Kerzen
    slope = df.sma30.diff()
    slope_ok_up   = slope.iloc[-1] > 0 and slope.iloc[-2] > 0 and slope.iloc[-3] > 0
    slope_ok_down = slope.iloc[-1] < 0 and slope.iloc[-2] < 0 and slope.iloc[-3] < 0

    c, v = df.close, df.volume
    vol_ok = v.iloc[-1] > (VOL_SPIKE_FACTOR * df.volma.iloc[-1])

    # RSI-Basiswert
    r = float(df.rsi.iloc[-1])

    # Break + Vol + Slope + RSI-Richtung
    bull30 = (c.iloc[-2] <= df.sma30.iloc[-2]) and (c.iloc[-1] > df.sma30.iloc[-1]) and vol_ok and slope_ok_up and (r >= 50.0)
    bear30 = (c.iloc[-2] >= df.sma30.iloc[-2]) and (c.iloc[-1] < df.sma30.iloc[-1]) and vol_ok and slope_ok_down and (r <= 50.0)

    return {
        "bull30": bool(bull30),
        "bear30": bool(bear30),
        "vol_ok": bool(vol_ok),
        "atr": float(df.atr.iloc[-1]),
        "price": float(c.iloc[-1]),
        "ema200_up": (c.iloc[-1] > df.ema200.iloc[-1]),
        "ema200_dn": (c.iloc[-1] < df.ema200.iloc[-1]),
        "rsi": r,
        "ma30_slope_up": bool(slope_ok_up),
        "ma30_slope_dn": bool(slope_ok_down),
    }

# ====== Fib-Retest auf FIB_CONFIRM_TF ======
def _find_pivots(values: List[float], left: int, right: int, is_high: bool) -> List[int]:
    idxs, n = [], len(values)
    for i in range(left, n - right):
        L = values[i-left:i]
        R = values[i+1:i+1+right]
        if is_high:
            if all(values[i] > x for x in L) and all(values[i] > x for x in R):
                idxs.append(i)
        else:
            if all(values[i] < x for x in L) and all(values[i] < x for x in R):
                idxs.append(i)
    return idxs

# (beibehalten; wird nicht mehr direkt genutzt)
def last_impulse(df: pd.DataFrame) -> Optional[Tuple[Tuple[int,float], Tuple[int,float]]]:
    highs = df["high"].values
    lows  = df["low"].values
    hi_idx = _find_pivots(list(highs), PIVOT_LEFT_TRIG, PIVOT_RIGHT_TRIG, True)
    lo_idx = _find_pivots(list(lows ), PIVOT_LEFT_TRIG, PIVOT_RIGHT_TRIG, False)
    if not hi_idx or not lo_idx:
        return None
    last_hi_i = hi_idx[-1]
    last_lo_i = lo_idx[-1]
    if last_lo_i < last_hi_i:
        return ((last_lo_i, lows[last_lo_i]), (last_hi_i, highs[last_hi_i]))
    elif last_hi_i < last_lo_i:
        return ((last_lo_i, lows[last_lo_i]), (last_hi_i, highs[last_hi_i]))
    return None

# Punkt 3: richtungs-sensitiver Impuls (LONG = lo→hi, SHORT = hi→lo)
def directional_impulse(df: pd.DataFrame, want: str) -> Optional[Tuple[Tuple[int,float], Tuple[int,float]]]:
    highs = df["high"].values
    lows  = df["low"].values
    hi_idx = _find_pivots(list(highs), PIVOT_LEFT_TRIG, PIVOT_RIGHT_TRIG, True)
    lo_idx = _find_pivots(list(lows ), PIVOT_LEFT_TRIG, PIVOT_RIGHT_TRIG, False)
    if not hi_idx or not lo_idx:
        return None

    if want.upper() == "LONG":
        # finde letztes Paar mit lo < hi
        pairs = [(lo, hi) for lo in lo_idx for hi in hi_idx if lo < hi]
        if not pairs:
            return None
        # nimm das Paar, dessen hi am jüngsten ist
        hi_i = max([p[1] for p in pairs])
        lo_candidates = [lo for (lo, hi_) in pairs if hi_ == hi_i and lo < hi_i]
        if not lo_candidates:
            return None
        lo_i = max(lo_candidates)
        return ((lo_i, lows[lo_i]), (hi_i, highs[hi_i]))
    else:
        # SHORT: hi < lo
        pairs = [(hi, lo) for hi in hi_idx for lo in lo_idx if hi < lo]
        if not pairs:
            return None
        lo_i = max([p[1] for p in pairs])
        hi_candidates = [hi for (hi, lo_) in pairs if lo_ == lo_i and hi < lo_i]
        if not hi_candidates:
            return None
        hi_i = max(hi_candidates)
        return ((lo_i, lows[lo_i]), (hi_i, highs[hi_i]))

def fib_zone_ok(price: float, impulse: Tuple[Tuple[int,float], Tuple[int,float]], direction: str) -> Tuple[bool, Tuple[float,float]]:
    (lo_i, lo_v), (hi_i, hi_v) = impulse
    if hi_i == lo_i:
        return (False, (0.0, 0.0))
    if hi_i > lo_i:
        # Up-Impuls
        fib50  = lo_v + 0.5   * (hi_v - lo_v)
        fib618 = lo_v + 0.618 * (hi_v - lo_v)
        zmin, zmax = sorted([fib50, fib618])
        zmin *= (1 - FIB_TOL_PCT); zmax *= (1 + FIB_TOL_PCT)
        ok = (direction == "LONG") and (zmin <= price <= zmax)
        return (ok, (zmin, zmax))
    else:
        # Down-Impuls
        fib50  = hi_v - 0.5   * (hi_v - lo_v)
        fib618 = hi_v - 0.618 * (hi_v - lo_v)
        zmin, zmax = sorted([fib618, fib50])
        zmin *= (1 - FIB_TOL_PCT); zmax *= (1 + FIB_TOL_PCT)
        ok = (direction == "SHORT") and (zmin <= price <= zmax)
        return (ok, (zmin, zmax))

# ====== S/R Levels (1h) ======
def find_pivots_levels(df: pd.DataFrame) -> Tuple[List[Tuple[float,int]], List[Tuple[float,int]]]:
    highs = df["high"].values
    lows  = df["low"].values
    n = len(df)
    swing_highs, swing_lows = [], []
    for i in range(PIVOT_LEFT, n - PIVOT_RIGHT):
        Lh = highs[i-PIVOT_LEFT:i]; Rh = highs[i+1:i+1+PIVOT_RIGHT]
        if all(highs[i] > x for x in Lh) and all(highs[i] > x for x in Rh):
            swing_highs.append(highs[i])
        Ll = lows[i-PIVOT_LEFT:i]; Rl = lows[i+1:i+1+PIVOT_RIGHT]
        if all(lows[i] < x for x in Ll) and all(lows[i] < x for x in Rl):
            swing_lows.append(lows[i])

    def cluster(values: List[float], tol_pct: float) -> List[Tuple[float,int]]:
        values = sorted(values)
        if not values: return []
        clusters, cur = [], [values[0]]
        for x in values[1:]:
            center = sum(cur)/len(cur)
            if abs(x - center)/center <= tol_pct:
                cur.append(x)
            else:
                clusters.append((sum(cur)/len(cur), len(cur))); cur = [x]
        clusters.append((sum(cur)/len(cur), len(cur)))
        clusters.sort(key=lambda t: (t[1], t[0]), reverse=True)
        return clusters

    return cluster(swing_highs, CLUSTER_PCT), cluster(swing_lows, CLUSTER_PCT)

def nearest_level(levels: List[Tuple[float,int]], ref_price: float, direction: str, min_strength: int) -> Optional[float]:
    cands = []
    for price, strength in levels:
        if strength < min_strength: continue
        if direction == "LONG" and price > ref_price:  cands.append(price)
        if direction == "SHORT" and price < ref_price: cands.append(price)
    if not cands: return None
    return min(cands) if direction == "LONG" else max(cands)

# ---------- NUR 1 TP zurückgeben ----------
def make_levels_sr(direction: str, entry: float, atrv: float, df_sr: pd.DataFrame) -> Tuple[float,float,float,bool]:
    """
    Liefert nur noch EIN TP:
      • S/R: erweitertes Ziel (früher TP2 via TP2_FACTOR)
      • ATR-Fallback: früherer TP3 (TP3_ATR)
    Rückgabe: (entry, sl, tp, used_sr)
    """
    res_lvls, sup_lvls = find_pivots_levels(df_sr)

    # S/R bevorzugt
    if direction == "LONG":
        tp1_sr = nearest_level(res_lvls, entry, "LONG", MIN_STRENGTH)
        sl_sr  = nearest_level(sup_lvls, entry, "SHORT", MIN_STRENGTH)
        if tp1_sr is not None and sl_sr is not None and tp1_sr > entry and sl_sr < entry:
            tp_ext = round(entry + (tp1_sr - entry) * TP2_FACTOR, 6)  # früherer TP2
            return entry, round(sl_sr,6), tp_ext, True
    else:
        tp1_sr = nearest_level(sup_lvls, entry, "SHORT", MIN_STRENGTH)
        sl_sr  = nearest_level(res_lvls, entry, "LONG", MIN_STRENGTH)
        if tp1_sr is not None and sl_sr is not None and tp1_sr < entry and sl_sr > entry:
            tp_ext = round(entry - (entry - tp1_sr) * TP2_FACTOR, 6)  # früherer TP2
            return entry, round(sl_sr,6), tp_ext, True

    # ATR Fallback → nimm früheren TP3
    if direction == "LONG":
        sl = round(entry - ATR_SL * atrv, 6)
        tp = round(entry + TP3_ATR * atrv, 6)
    else:
        sl = round(entry + ATR_SL * atrv, 6)
        tp = round(entry - TP3_ATR * atrv, 6)
    return entry, sl, tp, False

# ====== Checkliste ======
def build_checklist(direction: str, trig15: Dict[str, Any], fib_ok: bool) -> Tuple[bool, List[str], List[str]]:
    ok, warn = [], []

    atr_pct = (trig15["atr"] / max(trig15["price"], 1e-9)) * 100.0
    if atr_pct >= MIN_ATR_PCT: ok.append(f"ATR≥{MIN_ATR_PCT}% ({atr_pct:.2f}%)")
    else:                      return (False, ok, [f"ATR<{MIN_ATR_PCT}% ({atr_pct:.2f}%)"])

    # M15: 30MA Breakout mit Volumen (Pflicht)
    if direction == "LONG":
        if trig15["bull30"]: ok.append("M15 30MA Break ↑ (Vol + Slope + RSI)")
        else:                return (False, ok, ["Kein M15 30MA Break↑"])
    else:
        if trig15["bear30"]: ok.append("M15 30MA Break ↓ (Vol + Slope + RSI)")
        else:                return (False, ok, ["Kein M15 30MA Break↓"])

    # EMA200 Richtung (M15)
    ema200_ok = trig15["ema200_up"] if direction=="LONG" else trig15["ema200_dn"]
    if ema200_ok: ok.append("EMA200 ok")
    else:         return (False, ok, ["EMA200 gegen Setup"])

    # Volumen Pflicht
    if trig15["vol_ok"]: ok.append(f"Vol>{VOL_SPIKE_FACTOR:.2f}×MA20")
    else:                return (False, ok, [f"kein Vol-Spike (≥{VOL_SPIKE_FACTOR:.2f}× Pflicht)"])

    # Safe-Entry per Fib-Retest (falls Pflicht)
    if SAFE_ENTRY_REQUIRED:
        if fib_ok: ok.append(f"Safe-Entry Fib 0.5–0.618 ({FIB_CONFIRM_TF})")
        else:      return (False, ok, [f"Kein Fib-Retest (0.5–0.618) auf {FIB_CONFIRM_TF}"])

    # RSI Hinweis (informativ)
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

# ---------- Punkt 6: Break-Fail-Sperre (schneller Fakeout-Filter) ----------
def break_failed_recently(df15: pd.DataFrame) -> bool:
    """Verwirft Setups, wenn nach einem frischen Break sofort ein Re-Close zurück über/unter MA30 kommt."""
    s = sma(df15.close, 30)
    c = df15.close
    if len(c) < 3:
        return False
    last3 = c.iloc[-3:]
    last3_ma = s.iloc[-3:]
    # Up-Break gefolgt von Close < MA30
    up_break = (last3.iloc[-3] <= last3_ma.iloc[-3]) and (last3.iloc[-2] > last3_ma.iloc[-2])
    fail_up  = up_break and (last3.iloc[-1] < last3_ma.iloc[-1])
    # Down-Break gefolgt von Close > MA30
    dn_break = (last3.iloc[-3] >= last3_ma.iloc[-3]) and (last3.iloc[-2] < last3_ma.iloc[-2])
    fail_dn  = dn_break and (last3.iloc[-1] > last3_ma.iloc[-1])
    return bool(fail_up or fail_dn)

# ---------- Minimalistisches Signal ----------
async def send_signal(symbol: str, direction: str, entry: float, sl: float, tp: float,
                      prob: int, checklist_ok: List[str], checklist_warn: List[str], used_sr: bool):
    # Richtung & Icon
    arrow = "🟢 LONG" if direction.upper() == "LONG" else "🔴 SHORT"

    # Kompakter, stylischer Signaltext
    text = (
        f"🛡 *Scanner Signal* — {symbol}\n"
        f"➡️ {arrow}\n"
        f"🎯 Entry: `{entry}`\n"
        f"🏁 TP: `{tp}`\n"
        f"🛡 SL: `{sl}`\n"
        f"📈 Wahrscheinlichkeit: *{prob}%*"
    )

    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

async def send_mode_banner():
    text = (
        "🛡 *Scanner gestartet – MODUS: M15 30MA Breakout (mit Volumen) + Fib-Retest (0.5–0.618) + S/R (1h)*\n"
        f"• Scan-Intervall: {SCAN_INTERVAL_S//60} Minuten\n"
        f"• Fib-Confirm-TF: {FIB_CONFIRM_TF} (Close={'Yes' if FIB_REQUIRE_CANDLE_CLOSE else 'Live'})\n"
        f"• Volumen: Pflicht ≥ {VOL_SPIKE_FACTOR:.2f}× MA20, ATR% ≥ {MIN_ATR_PCT:.2f}%\n"
        "• Ziel: Einziger TP (S/R: erweitertes Ziel; ATR: früherer TP3)"
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

            # Punkt 6: Break-Fail-Check
            if break_failed_recently(df15):
                last_scan_report["symbols"][sym] = {"skip": "Break-Fail (MA30 Re-Close)"}
                await asyncio.sleep(getattr(ex, "rateLimit", 200) / 1000)
                continue

            # CoinGlass 12h Heatmap (optional)
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

            # Safe-Entry via Fib-Retest auf FIB_CONFIRM_TF
            df_fib  = fetch_df(sym, FIB_CONFIRM_TF)

            # Preisbasis für Fib je nach Close-Policy
            if FIB_REQUIRE_CANDLE_CLOSE:
                price_fib = float(df_fib["close"].iloc[-2]) if len(df_fib) >= 2 else float(df_fib["close"].iloc[-1])
            else:
                price_fib = float(df_fib["close"].iloc[-1])

            # Punkt 3: richtungs-sensitiver Impuls
            impulse_L = directional_impulse(df_fib, "LONG")
            impulse_S = directional_impulse(df_fib, "SHORT")

            fib_ok_L = fib_ok_S = False
            if impulse_L is not None:
                okL, _ = fib_zone_ok(price_fib, impulse_L, "LONG")
                fib_ok_L = bool(okL)
            if impulse_S is not None:
                okS, _ = fib_zone_ok(price_fib, impulse_S, "SHORT")
                fib_ok_S = bool(okS)

            # Kandidaten sammeln
            candidates = []
            for direction in ("LONG", "SHORT"):
                fib_ok = fib_ok_L if direction=="LONG" else fib_ok_S
                passed, ok_tags, warn_tags = build_checklist(direction, trig15, fib_ok)
                if not passed:
                    continue
                if cg_dir and cg_dir != direction:
                    continue
                prob = prob_score(trig15["vol_ok"], trig15["ema200_up"] if direction=="LONG" else trig15["ema200_dn"], fib_ok)
                candidates.append((direction, prob, ok_tags, warn_tags))

            if candidates:
                direction, prob, ok_tags, warn_tags = sorted(candidates, key=lambda x: x[1], reverse=True)[0]
                if prob >= PROB_MIN:
                    df_sr = fetch_df(sym, SR_TF, limit=LOOKBACK)
                    entry, sl, tp, used_sr = make_levels_sr(direction, price, trig15["atr"], df_sr)

                    key = f"{sym}:{direction}"
                    throttled = need_throttle(key, now)

                    last_scan_report["symbols"][sym] = {
                        "direction": direction, "prob": prob, "throttled": throttled,
                        "ok": ok_tags, "warn": warn_tags,
                        "price": price, "atr": trig15["atr"],
                        "sr_used": used_sr, "heatmap_dir": cg_dir,
                        "fib_tf": FIB_CONFIRM_TF, "fib_close": FIB_REQUIRE_CANDLE_CLOSE
                    }
                    if not throttled:
                        await send_signal(sym, direction, entry, sl, tp, prob, ok_tags, warn_tags, used_sr)
                else:
                    last_scan_report["symbols"][sym] = {"skip": f"Prob. {prob}% < {PROB_MIN}%"}
            else:
                last_scan_report["symbols"][sym] = {"skip": "Kein Setup (Pflichten nicht erfüllt)"}

        except Exception as e:
            last_scan_report["symbols"][sym] = {"error": str(e)}
        finally:
            await asyncio.sleep(getattr(ex, "rateLimit", 200) / 1000)

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
    text = "✅ Test: Bot & Telegram OK — Mode: M15 30MA Break + Fib-Retest + S/R (1 TP)"
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
    return {"ok": True, "test": True}

@app.get("/scan")
async def manual_scan():
    await scan_once()
    return {"ok": True, "ran": True, "ts": last_scan_report.get("ts")}

@app.get("/status")
async def status():
    return {"ok": True, "mode": "m15_break_fib_single_tp", "report": last_scan_report}

@app.get("/")
async def root():
    return {"ok": True, "mode": "m15_break_fib_single_tp"}

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
