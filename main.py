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
    "HBAR/USDT","CRV/USDT","CAKE/USDT","WLD/USDT",
    "TIA/USDT","CFX/USDT","WIF/USDT","TRUMP/USDT",
    "SUI/USDT","SAPIEN/USDT","ASTAR/USDT"  # Tippfehler gefixt
]
_seen = set()
SYMBOLS = [s for s in SYMBOLS if not (s in _seen or _seen.add(s))]

# ====== Analyse & Scan ======
LOOKBACK        = 300
SCAN_INTERVAL_S = 5 * 60   # Intervall (Sek.)

# ====== Entry-Logik ======
# Pflicht 1: M15 30MA-Break + Volumen (mit Slope + RSI + Strength-Delay)
# Pflicht 2: Safe-Entry per Fib-Retest (0.5–0.618) auf FIB_CONFIRM_TF (mit richtungs-sensitivem Impuls)
FIB_CONFIRM_TF            = "5m"          # "5m" oder "15m"
SAFE_ENTRY_REQUIRED       = True          # Fib-Retest Pflicht (False = nur optional)
FIB_TOL_PCT               = 0.10 / 100.0  # ±0.10 % Toleranz (legacy, weiterhin geloggt)
FIB_REQUIRE_CANDLE_CLOSE  = True          # (legacy Flag; neuer Modus siehe FIB_CONFIRM_MODE)

# ====== Fibonacci – Präzisions-Settings (NEU) ======
FIB_BAND                = "CLASSIC"   # "CLASSIC"(0.5–0.618), "GOLDEN"(0.618–0.65), "DEEP"(0.618–0.786), "SHALLOW"(0.382–0.5), "CUSTOM"
FIB_BAND_CUSTOM         = (0.5, 0.618) # nur wenn FIB_BAND="CUSTOM"

FIB_TOL_MODE            = "pct"       # "pct" oder "atr"
FIB_TOL_ATR_MULT        = 0.25        # z.B. 0.25 * ATR(15) als halbe Bandbreite (nur bei "atr")

FIB_CONFIRM_MODE        = "close"     # "close" (Schlusskurs in Zone), "wick" (Docht reicht), "2bar" (2 Schlüsse in Zone)
FIB_MIN_TOUCHES         = 1           # wie oft Zone (je nach MODE) in den letzten FIB_RECENT_BARS berührt werden muss
FIB_USE_LOG_SCALE       = False       # True = log-Fibonacci (für sehr große Spannen)

# ====== Strength-Delay ======
BREAK_DELAY_BARS = 2  # Anzahl bestätigender M15-Kerzen nach dem Cross (z.B. 2 oder 3)

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

# ====== Trendfilter (EMA200) – Pflicht mit Toleranz ======
EMA200_STRICT  = True
EMA200_TOL_PCT = 0.20   # bis zu 0.20% unter/über EMA200 zulassen

# ====== Fib-Recent Settings ======
FIB_RECENT_BARS = 6
MAX_DIST_FROM_ZONE_ATR = 1.0

# ====== 5m MA30-Filter (NEU) ======
MA30_5M_FILTER = True   # Long nur über 5m-MA30, Short nur darunter

# ====== CoinGlass Sweep-Filter Settings ======
CG_RANGE = "12h"                 # Zeitfenster wie im UI
CG_TOP_PCT = 90                  # signifikante Levels = Top-10% Volumina
CG_NEAR_BAND_PCT = 0.35 / 100.0  # „nah“ = <= 0.35% Entfernung vom Preis
CG_ATR_MULT_NEAR = 0.6           # „nah“ = <= 0.6 × ATR15
CG_DOM_RATIO = 1.35              # Dominanzschwelle (Top3 oben vs. unten)
CG_TIMEOUT = 5                   # HTTP-Timeout (Sek.)

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

# ====== Trigger (M15) — inkl. Strength-Delay ======
def analyze_trigger_m15(df: pd.DataFrame) -> Dict[str, Any]:
    df = df.copy()
    df["ema200"] = ema(df.close, 200)
    df["rsi"]    = rsi(df.close, 14)
    df["atr"]    = atr(df.high, df.low, df.close, 14)
    df["volma"]  = vol_sma(df.volume, 20)
    df["sma30"]  = sma(df.close, 30)

    slope = df.sma30.diff()
    slope_ok_up   = slope.iloc[-1] > 0 and slope.iloc[-2] > 0 and slope.iloc[-3] > 0
    slope_ok_down = slope.iloc[-1] < 0 and slope.iloc[-2] < 0 and slope.iloc[-3] < 0

    c, v = df.close, df.volume
    s    = df.sma30
    vol_ok = v.iloc[-1] > (VOL_SPIKE_FACTOR * df.volma.iloc[-1])
    r = float(df.rsi.iloc[-1])

    # --- Strength-Delay Logic ---
    n = max(1, BREAK_DELAY_BARS)

    def confirmed_long() -> bool:
        if len(c) < n + 2:
            return False
        last_ok = all(c.iloc[-i] > s.iloc[-i] for i in range(1, n+1))
        pre_cross = c.iloc[-(n+1)] <= s.iloc[-(n+1)]
        return last_ok and pre_cross and vol_ok and slope_ok_up and (r >= 50.0)

    def confirmed_short() -> bool:
        if len(c) < n + 2:
            return False
        last_ok = all(c.iloc[-i] < s.iloc[-i] for i in range(1, n+1))
        pre_cross = c.iloc[-(n+1)] >= s.iloc[-(n+1)]
        return last_ok and pre_cross and vol_ok and slope_ok_down and (r <= 50.0)

    bull30 = confirmed_long()
    bear30 = confirmed_short()

    return {
        "bull30": bool(bull30),
        "bear30": bool(bear30),
        "vol_ok": bool(vol_ok),
        "atr": float(df.atr.iloc[-1]),
        "price": float(c.iloc[-1]),
        "ema200_val": float(df.ema200.iloc[-1]),
        "ema200_up": (c.iloc[-1] > df.ema200.iloc[-1]),
        "ema200_dn": (c.iloc[-1] < df.ema200.iloc[-1]),
        "rsi": r,
        "ma30_slope_up": bool(slope_ok_up),
        "ma30_slope_dn": bool(slope_ok_down),
    }

# ====== Fib-Retest – Pivot Finder ======
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

# Richtungs-sensitiver Impuls
def directional_impulse(df: pd.DataFrame, want: str) -> Optional[Tuple[Tuple[int,float], Tuple[int,float]]]:
    highs = df["high"].values
    lows  = df["low"].values
    hi_idx = _find_pivots(list(highs), PIVOT_LEFT_TRIG, PIVOT_RIGHT_TRIG, True)
    lo_idx = _find_pivots(list(lows ), PIVOT_LEFT_TRIG, PIVOT_RIGHT_TRIG, False)
    if not hi_idx or not lo_idx:
        return None
    if want.upper() == "LONG":
        pairs = [(lo, hi) for lo in lo_idx for hi in hi_idx if lo < hi]
        if not pairs: return None
        hi_i = max([p[1] for p in pairs])
        lo_candidates = [lo for (lo, hi_) in pairs if hi_ == hi_i and lo < hi_i]
        if not lo_candidates: return None
        lo_i = max(lo_candidates)
        return ((lo_i, lows[lo_i]), (hi_i, highs[hi_i]))
    else:
        pairs = [(hi, lo) for hi in hi_idx for lo in lo_idx if hi < lo]
        if not pairs: return None
        lo_i = max([p[1] for p in pairs])
        hi_candidates = [hi for (hi, lo_) in pairs if lo_ == lo_i and hi < lo_i]
        if not hi_candidates: return None
        hi_i = max(hi_candidates)
        return ((lo_i, lows[lo_i]), (hi_i, highs[hi_i]))

# ====== Fibonacci-Helper (NEU) ======
def _fib_band_tuple() -> Tuple[float, float]:
    band = FIB_BAND.upper()
    if band == "CLASSIC":
        return (0.5, 0.618)
    if band == "GOLDEN":
        return (0.618, 0.65)
    if band == "DEEP":
        return (0.618, 0.786)
    if band == "SHALLOW":
        return (0.382, 0.5)
    lo, hi = FIB_BAND_CUSTOM  # CUSTOM fallback
    return (min(lo, hi), max(lo, hi))

def _fib_levels_from_swing(lo_v: float, hi_v: float) -> Dict[float, float]:
    """Gibt ein dict {ratio: level} zurück. Unterstützt lineare und log-Skala."""
    if not FIB_USE_LOG_SCALE:
        span = hi_v - lo_v
        return {
            0.382: lo_v + 0.382 * span,
            0.5:   lo_v + 0.5   * span,
            0.618: lo_v + 0.618 * span,
            0.65:  lo_v + 0.65  * span,
            0.705: lo_v + 0.705 * span,
            0.786: lo_v + 0.786 * span,
        }
    else:
        L, H = math.log(max(lo_v, 1e-12)), math.log(max(hi_v, 1e-12))
        span = H - L
        def lf(r): return math.exp(L + r * span)
        return {r: lf(r) for r in (0.382, 0.5, 0.618, 0.65, 0.705, 0.786)}

def _apply_tolerance(zmin: float, zmax: float, atr_ref: float) -> Tuple[float, float]:
    if FIB_TOL_MODE.lower() == "atr":
        tol = max(atr_ref * FIB_TOL_ATR_MULT, 0.0)
        return (zmin - tol, zmax + tol)
    # default: pct (legacy FIB_TOL_PCT weiter verfügbar)
    return (zmin * (1 - FIB_TOL_PCT), zmax * (1 + FIB_TOL_PCT))

def _build_fib_zone(lo_v: float, hi_v: float, direction: str, atr_ref: float) -> Tuple[float, float]:
    lvls = _fib_levels_from_swing(lo_v, hi_v)
    r1, r2 = _fib_band_tuple()
    if r1 not in lvls or r2 not in lvls:  # linearer Fallback bei Custom
        span = hi_v - lo_v
        a = lo_v + r1 * span
        b = lo_v + r2 * span
    else:
        a, b = lvls[r1], lvls[r2]
    zmin, zmax = (min(a, b), max(a, b))
    # SHORT: falls Swing invertiert – Absicherung
    if direction.upper() == "SHORT" and hi_v < lo_v:
        lo_v, hi_v = min(lo_v, hi_v), max(lo_v, hi_v)
        span = hi_v - lo_v
        a = lo_v + r1 * span
        b = lo_v + r2 * span
        zmin, zmax = (min(a, b), max(a, b))
    return _apply_tolerance(zmin, zmax, atr_ref)

# ====== Fib-API (ersetzt alte Funktionen; Signaturen bleiben kompatibel) ======
def fib_zone_ok(price: float, impulse: Tuple[Tuple[int,float], Tuple[int,float]], direction: str, atr_ref: float = 0.0) -> Tuple[bool, Tuple[float,float]]:
    (lo_i, lo_v), (hi_i, hi_v) = impulse
    if hi_i == lo_i:
        return (False, (0.0, 0.0))
    zmin, zmax = _build_fib_zone(lo_v, hi_v, direction, atr_ref)
    ok = (zmin <= price <= zmax)
    return (ok and direction.upper() in ("LONG","SHORT"), (zmin, zmax))

def fib_zone_for_direction(df_fib: pd.DataFrame, direction: str, atr_ref: float) -> Optional[Tuple[float, float]]:
    imp = directional_impulse(df_fib, direction)
    if imp is None:
        return None
    _, zone = fib_zone_ok(price=float("nan"), impulse=imp, direction=direction, atr_ref=atr_ref)
    return zone

def _price_hit_zone(pr_open: float, pr_high: float, pr_low: float, pr_close: float, zmin: float, zmax: float) -> bool:
    mode = FIB_CONFIRM_MODE.lower()
    if mode == "wick":
        return (pr_low <= zmax and pr_high >= zmin)  # Docht berührt Zone
    return (zmin <= pr_close <= zmax)               # "close" / "2bar" via Close

def fib_ok_now_or_recent(df_fib: pd.DataFrame, direction: str, price_now: float, atr15: float) -> bool:
    zone = fib_zone_for_direction(df_fib, direction, atr_ref=atr15)
    if not zone:
        return False
    zmin, zmax = zone

    closes = df_fib["close"]
    highs  = df_fib["high"]
    lows   = df_fib["low"]
    opens  = df_fib["open"]

    nbars = max(1, FIB_RECENT_BARS)
    # geschlossene Kerzen betrachten (letzte laufende Candle raus)
    start = max(0, len(df_fib) - 1 - nbars)
    idxs = list(range(start, len(df_fib) - 1))

    mode = FIB_CONFIRM_MODE.lower()
    if mode == "2bar":
        seq = 0
        for i in idxs:
            if zmin <= float(closes.iloc[i]) <= zmax:
                seq += 1
                if seq >= 2:
                    return True
            else:
                seq = 0
    else:
        touches = 0
        for i in idxs:
            if _price_hit_zone(
                pr_open=float(opens.iloc[i]),
                pr_high=float(highs.iloc[i]),
                pr_low =float(lows.iloc[i]),
                pr_close=float(closes.iloc[i]),
                zmin=zmin, zmax=zmax
            ):
                touches += 1
                if touches >= FIB_MIN_TOUCHES:
                    return True

    # Kein Recent-Touch → Distanz-Check (falls Preis bereits weg ist)
    if direction.upper() == "SHORT":
        if price_now >= zmin:
            return False
        dist = zmin - price_now
    else:
        if price_now <= zmax:
            return False
        dist = price_now - zmax

    return dist <= (MAX_DIST_FROM_ZONE_ATR * atr15)

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
            if abs(x - center)/max(center,1e-12) <= tol_pct:
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
    res_lvls, sup_lvls = find_pivots_levels(df_sr)
    if direction == "LONG":
        tp1_sr = nearest_level(res_lvls, entry, "LONG", MIN_STRENGTH)
        sl_sr  = nearest_level(sup_lvls, entry, "SHORT", MIN_STRENGTH)
        if tp1_sr is not None and sl_sr is not None and tp1_sr > entry and sl_sr < entry:
            tp_ext = round(entry + (tp1_sr - entry) * TP2_FACTOR, 6)
            return entry, round(sl_sr,6), tp_ext, True
    else:
        tp1_sr = nearest_level(sup_lvls, entry, "SHORT", MIN_STRENGTH)
        sl_sr  = nearest_level(res_lvls, entry, "LONG", MIN_STRENGTH)
        if tp1_sr is not None and sl_sr is not None and tp1_sr < entry and sl_sr > entry:
            tp_ext = round(entry - (entry - tp1_sr) * TP2_FACTOR, 6)
            return entry, round(sl_sr,6), tp_ext, True

    if direction == "LONG":
        sl = round(entry - ATR_SL * atrv, 6)
        tp = round(entry + TP3_ATR * atrv, 6)
    else:
        sl = round(entry + ATR_SL * atrv, 6)
        tp = round(entry - TP3_ATR * atrv, 6)
    return entry, sl, tp, False

# ====== CoinGlass Sweep-Filter Helpers ======
def _cg_fetch_levels(symbol_base: str):
    if not COINGLASS_API_KEY:
        return None
    try:
        resp = requests.get(
            f"{COINGLASS_URL}/api/futures/liquidation/aggregated-heatmap/model1",
            headers={"CG-API-KEY": COINGLASS_API_KEY},
            params={"symbol": symbol_base, "range": CG_RANGE},
            timeout=CG_TIMEOUT
        )
        if not resp.ok:
            return None
        data = resp.json()
        if data.get("code") != 0:
            return None
        y_axis = data["data"].get("y_axis", [])
        liq = data["data"].get("liquidation_leverage_data", [])
        if not y_axis or not liq:
            return None
        # Summe Volumen je y_idx (Preis-Level)
        agg = {}
        for _x, y_idx, vol in liq:
            agg[y_idx] = agg.get(y_idx, 0) + float(vol)
        levels = []
        for y_idx, vol in agg.items():
            if 0 <= y_idx < len(y_axis):
                try:
                    price_lvl = float(y_axis[y_idx])
                except Exception:
                    continue
                levels.append((price_lvl, vol))
        if not levels:
            return None
        # Signifikanz-Schwelle (Top-10% Volumina)
        vols = [v for _, v in levels]
        vols_sorted = sorted(vols)
        idx = max(0, int(len(vols_sorted) * CG_TOP_PCT / 100.0) - 1)
        thresh = vols_sorted[idx]
        return {"levels": levels, "vol_thresh": thresh}
    except Exception:
        return None

def _cg_analyze(price_now: float, atr15: float, cg):
    levels = cg["levels"]; thresh = cg["vol_thresh"]
    above = []
    below = []
    for p, v in levels:
        dist_abs = abs(p - price_now)
        dist_pct = dist_abs / max(price_now, 1e-9)
        dist_atr = dist_abs / max(atr15, 1e-9) if atr15 > 0 else float("inf")
        rec = (p, v, dist_pct, dist_atr)
        if p >= price_now:
            above.append(rec)
        else:
            below.append(rec)

    # Signifikante Levels
    sig_above = [r for r in above if r[1] >= thresh]
    sig_below = [r for r in below if r[1] >= thresh]

    # Nächster signifikanter Pool je Seite
    near_above = min(sig_above, key=lambda r: r[2], default=None)
    near_below = min(sig_below, key=lambda r: r[2], default=None)

    # Mid-Chop, wenn oben & unten sehr nahe Pools liegen
    mid_chop = False
    if near_above and near_below:
        if (near_above[2] <= CG_NEAR_BAND_PCT and near_below[2] <= CG_NEAR_BAND_PCT):
            mid_chop = True

    # Bias über Summe der Top3-Volumina pro Seite
    top3_above = sorted(sig_above, key=lambda r: r[1], reverse=True)[:3]
    top3_below = sorted(sig_below, key=lambda r: r[1], reverse=True)[:3]
    sum_above = sum(r[1] for r in top3_above) if top3_above else 0.0
    sum_below = sum(r[1] for r in top3_below) if top3_below else 0.0
    if sum_above > CG_DOM_RATIO * max(sum_below, 1e-9):
        bias = "UP"
    elif sum_below > CG_DOM_RATIO * max(sum_above, 1e-9):
        bias = "DOWN"
    else:
        bias = "NEUTRAL"

    return {
        "near_above": near_above,  # (price, vol, dist_pct, dist_atr)
        "near_below": near_below,
        "bias": bias,
        "mid_chop": mid_chop
    }

def coinglass_sweep_filter(price_now: float, atr15: float, direction: str, cg) -> Tuple[bool, str]:
    """
    True = PASS (Trade erlaubt), False = REJECT (skip), plus Grund (string)
    Regeln:
      1) Mid-Chop: nahe Pools oben & unten -> skip
      2) Proximity Risk: signifikanter Gegenpool nahe (prozentual ODER ATR-basiert) -> skip
      3) Bias gegen Setup -> skip
    """
    if not cg:
        return True, "CG:kein-daten"

    an = _cg_analyze(price_now, atr15, cg)

    # (1) Mid-Chop
    if an["mid_chop"]:
        return False, "CG:mid-chop"

    # (2) Proximity Risk
    opp = an["near_below"] if direction.upper() == "LONG" else an["near_above"]
    if opp:
        _, _, dist_pct, dist_atr = opp
        if (dist_pct <= CG_NEAR_BAND_PCT) or (dist_atr <= CG_ATR_MULT_NEAR):
            return False, f"CG:opp-pool-near (pct={dist_pct*100:.3f}%, atr={dist_atr:.2f}x)"

    # (3) Bias
    if (direction.upper() == "LONG" and an["bias"] == "DOWN") or \
       (direction.upper() == "SHORT" and an["bias"] == "UP"):
        return False, f"CG:bias-gegen ({an['bias']})"

    return True, "CG:ok"

# ====== Checkliste (inkl. EMA200 Toleranz & Fib-Detail) ======
def build_checklist(direction: str, trig15: Dict[str, Any], fib_ok: bool) -> Tuple[bool, List[str], List[str]]:
    ok, warn = [], []

    atr_pct = (trig15["atr"] / max(trig15["price"], 1e-9)) * 100.0
    if atr_pct >= MIN_ATR_PCT: ok.append(f"ATR≥{MIN_ATR_PCT}% ({atr_pct:.2f}%)")
    else:                      return (False, ok, [f"ATR<{MIN_ATR_PCT}% ({atr_pct:.2f}%)"])

    if direction == "LONG":
        if trig15["bull30"]: ok.append(f"M15 30MA Break ↑ bestätigt ({BREAK_DELAY_BARS} Bars)")
        else:                return (False, ok, [f"Kein bestätigter Break↑ ({BREAK_DELAY_BARS})"])
    else:
        if trig15["bear30"]: ok.append(f"M15 30MA Break ↓ bestätigt ({BREAK_DELAY_BARS} Bars)")
        else:                return (False, ok, [f"Kein bestätigter Break↓ ({BREAK_DELAY_BARS})"])

    # EMA200 mit Toleranz
    ema200_val = trig15.get("ema200_val", None)
    if ema200_val is None:
        return (False, ok, ["EMA200 Daten fehlen"])

    price_now = trig15["price"]
    if direction == "LONG":
        if trig15["ema200_up"]:
            ok.append("EMA200 ok")
        else:
            dist_pct = (ema200_val - price_now) / max(ema200_val,1e-9) * 100.0
            if EMA200_STRICT and dist_pct <= EMA200_TOL_PCT:
                ok.append(f"EMA200 knapp (Dip −{dist_pct:.2f}% ≤ {EMA200_TOL_PCT:.2f}%)")
            elif EMA200_STRICT:
                return (False, ok, [f"EMA200 gegen Setup (−{dist_pct:.2f}%)"])
            else:
                warn.append(f"Unter EMA200 (−{dist_pct:.2f}%)")
    else:
        if trig15["ema200_dn"]:
            ok.append("EMA200 ok")
        else:
            dist_pct = (price_now - ema200_val) / max(ema200_val,1e-9) * 100.0
            if EMA200_STRICT and dist_pct <= EMA200_TOL_PCT:
                ok.append(f"EMA200 knapp (+{dist_pct:.2f}% ≤ {EMA200_TOL_PCT:.2f}%)")
            elif EMA200_STRICT:
                return (False, ok, [f"EMA200 gegen Setup (+{dist_pct:.2f}%)"])
            else:
                warn.append(f"Ober EMA200 (+{dist_pct:.2f}%)")

    # Volumen Pflicht
    if trig15["vol_ok"]: ok.append(f"Vol>{VOL_SPIKE_FACTOR:.2f}×MA20")
    else:                return (False, ok, [f"kein Vol-Spike (≥{VOL_SPIKE_FACTOR:.2f}× Pflicht)"])

    # Safe-Entry per Fib-Retest (falls Pflicht)
    if SAFE_ENTRY_REQUIRED:
        if fib_ok: ok.append(f"Fib ok ({FIB_BAND}, {FIB_CONFIRM_MODE})")
        else:      return (False, ok, [f"Kein Fib-Retest ({FIB_BAND}, {FIB_CONFIRM_MODE})"])
    else:
        if fib_ok: ok.append(f"Fib optional erfüllt ({FIB_BAND}, {FIB_CONFIRM_MODE})")
        else:      warn.append("Fib optional nicht erfüllt")

    # RSI Hinweis (leicht)
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

# ---------- Break-Fail-Sperre ----------
def break_failed_recently(df15: pd.DataFrame) -> bool:
    s = sma(df15.close, 30)
    c = df15.close
    if len(c) < 3:
        return False
    last3 = c.iloc[-3:]
    last3_ma = s.iloc[-3:]
    up_break = (last3.iloc[-3] <= last3_ma.iloc[-3]) and (last3.iloc[-2] > last3_ma.iloc[-2])
    fail_up  = up_break and (last3.iloc[-1] < last3_ma.iloc[-1])
    dn_break = (last3.iloc[-3] >= last3_ma.iloc[-3]) and (last3.iloc[-2] < last3_ma.iloc[-2])
    fail_dn  = dn_break and (last3.iloc[-1] > last3_ma.iloc[-1])
    return bool(fail_up or fail_dn)

# ---------- Telegram Signal ----------
async def send_signal(symbol: str, direction: str, entry: float, sl: float, tp: float,
                      prob: int, checklist_ok: List[str], checklist_warn: List[str], used_sr: bool):
    arrow = "🟢 LONG" if direction.upper() == "LONG" else "🔴 SHORT"
    text = (
        f"🛡 *Scanner Signal* — {symbol}\n"
        f"➡️ {arrow}\n"
        f"🎯 Entry: `{entry}`\n"
        f"🏁 TP: `{tp}` {'(S/R)' if used_sr else '(ATR)'}\n"
        f"🛡 SL: `{sl}`\n"
        f"📈 Wahrscheinlichkeit: *{prob}%*\n"
        f"OK: " + " | ".join(checklist_ok) + ("\n" if checklist_ok else "") +
        (f"WARN: " + " | ".join(checklist_warn) if checklist_warn else "")
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

            # Break-Fail-Check
            if break_failed_recently(df15):
                last_scan_report["symbols"][sym] = {"skip": "Break-Fail (MA30 Re-Close)"}
                await asyncio.sleep(getattr(ex, "rateLimit", 200) / 1000)
                continue

            # ---- CoinGlass Levels (einmal pro Symbol laden) ----
            base = sym.split("/")[0]
            cg_levels = _cg_fetch_levels(base) if COINGLASS_API_KEY else None

            # Fib-Check (now or recent) + 5m MA30-Filter
            df_fib = fetch_df(sym, FIB_CONFIRM_TF)
            price_fib_now = float(df_fib["close"].iloc[-1])
            fib_ok_L = fib_ok_now_or_recent(df_fib, "LONG",  price_fib_now, trig15["atr"])
            fib_ok_S = fib_ok_now_or_recent(df_fib, "SHORT", price_fib_now, trig15["atr"])

            if MA30_5M_FILTER:
                df_fib["sma30"] = sma(df_fib["close"], 30)
                ma30_now = float(df_fib["sma30"].iloc[-1])
                if math.isnan(ma30_now):
                    fib_ok_L = fib_ok_S = False
                else:
                    fib_ok_L = fib_ok_L and (price_fib_now > ma30_now)
                    fib_ok_S = fib_ok_S and (price_fib_now < ma30_now)

            # Kandidaten sammeln
            candidates = []
            skip_reasons = []

            for direction in ("LONG", "SHORT"):
                fib_ok = fib_ok_L if direction=="LONG" else fib_ok_S

                # --- CoinGlass Sweep-Filter pro Richtung ---
                if COINGLASS_API_KEY:
                    cg_pass, cg_reason = coinglass_sweep_filter(price, trig15["atr"], direction, cg_levels)
                    if not cg_pass:
                        skip_reasons.append(cg_reason + f" [{direction}]")
                        continue

                passed, ok_tags, warn_tags = build_checklist(direction, trig15, fib_ok)
                if not passed:
                    continue

                ema_align = trig15["ema200_up"] if direction=="LONG" else trig15["ema200_dn"]
                prob = prob_score(trig15["vol_ok"], ema_align, fib_ok)
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
                        "sr_used": used_sr,
                        "fib_tf": FIB_CONFIRM_TF, "fib_close": FIB_REQUIRE_CANDLE_CLOSE,
                        "fib_band": FIB_BAND, "fib_mode": FIB_CONFIRM_MODE,
                        "cg_filter": "enabled" if COINGLASS_API_KEY else "disabled"
                    }
                    if not throttled:
                        await send_signal(sym, direction, entry, sl, tp, prob, ok_tags, warn_tags, used_sr)
                else:
                    last_scan_report["symbols"][sym] = {"skip": f"Prob. {prob}% < {PROB_MIN}%"}
            else:
                reason = "Kein Setup (Pflichten nicht erfüllt)"
                if skip_reasons:
                    reason += " | " + "; ".join(skip_reasons)
                last_scan_report["symbols"][sym] = {"skip": reason}

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
    asyncio.create_task(runner())

# ====== TEST & RELAY ENDPOINTS ======
@app.get("/test")
async def test():
    text = (
        "✅ Test: Bot & Telegram OK — Mode: M15 30MA Break + Fib-Retest + S/R (1 TP) "
        f"+ EMA200 Toleranz + 5m MA30-Filter | FIB {FIB_BAND} ({FIB_CONFIRM_MODE}, tol={FIB_TOL_MODE})"
    )
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

# Auto-Link-Seite
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
