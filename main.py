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
    "FTM/USDT","TIA/USDT","SEI/USDT","TAO/USDT" , "ETC/USDT",
    "ALGO/USDT","GRT/USDT","KAS/USDT","FLOW/USDT","SAND/USDT",
    "MATIC/USDT","ROSE/USDT","CFX/USDT","EGLD/USDT","STX/USDT",
    "DYDX/USDT","SKL/USDT","PENDLE/USDT","AR/USDT","AXS/USDT"
]
_seen = set()
SYMBOLS = [s for s in SYMBOLS if not (s in _seen or _seen.add(s))]

# ====== Analyse & Scan ======
LOOKBACK        = 300
SCAN_INTERVAL_S = 5 * 60   # Intervall (min)

# ====== Entry-Logik ======
# Pflicht 1: M15 30MA-Break + Volumen (mit Slope + RSI + Strength-Delay)
# Pflicht 2: Safe-Entry per Fib-Retest (0.5–0.618) auf FIB_CONFIRM_TF (mit richtungs-sensitivem Impuls)
FIB_CONFIRM_TF            = "5m"          # "5m" oder "15m"
SAFE_ENTRY_REQUIRED       = True          # Fib-Retest Pflicht (False = nur optional)
FIB_TOL_PCT               = 0.10 / 100.0  # ±0.10 % Toleranz
FIB_REQUIRE_CANDLE_CLOSE  = True          # Fib-Confirm NUR mit geschlossener Candle

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
EMA200_TOL_PCT = 0.20   # bis zu 0.05% unter/über EMA200 zulassen

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

def fib_zone_ok(price: float, impulse: Tuple[Tuple[int,float], Tuple[int,float]], direction: str) -> Tuple[bool, Tuple[float,float]]:
    (lo_i, lo_v), (hi_i, hi_v) = impulse
    if hi_i == lo_i:
        return (False, (0.0, 0.0))
    if hi_i > lo_i:
        fib50  = lo_v + 0.5   * (hi_v - lo_v)
        fib618 = lo_v + 0.618 * (hi_v - lo_v)
        zmin, zmax = sorted([fib50, fib618])
        zmin *= (1 - FIB_TOL_PCT); zmax *= (1 + FIB_TOL_PCT)
        ok = (direction == "LONG") and (zmin <= price <= zmax)
        return (ok, (zmin, zmax))
    else:
        fib50  = hi_v - 0.5   * (hi_v - lo_v)
        fib618 = hi_v - 0.618 * (hi_v - lo_v)
        zmin, zmax = sorted([fib618, fib50])
        zmin *= (1 - FIB_TOL_PCT); zmax *= (1 + FIB_TOL_PCT)
        ok = (direction == "SHORT") and (zmin <= price <= zmax)
        return (ok, (zmin, zmax))

# ====== Fib "jetzt oder kürzlich" ======
def fib_zone_for_direction(df_fib: pd.DataFrame, direction: str) -> Optional[Tuple[float, float]]:
    imp = directional_impulse(df_fib, direction)
    if imp is None:
        return None
    _, zone = fib_zone_ok(price=0.0, impulse=imp, direction=direction)
    return zone

def fib_ok_now_or_recent(df_fib: pd.DataFrame, direction: str, price_now: float, atr15: float) -> bool:
    zone = fib_zone_for_direction(df_fib, direction)
    if not zone:
        return False
    zmin, zmax = zone

    if FIB_REQUIRE_CANDLE_CLOSE:
        price_chk = float(df_fib["close"].iloc[-2]) if len(df_fib) >= 2 else float(df_fib["close"].iloc[-1])
    else:
        price_chk = float(df_fib["close"].iloc[-1])
    if zmin <= price_chk <= zmax:
        return True

    closes = df_fib["close"].iloc[-(FIB_RECENT_BARS+1):-1] if len(df_fib) > 1 else pd.Series([], dtype=float)
    if len(closes) == 0:
        return False
    touched = any((zmin <= c <= zmax) for c in closes)
    if not touched:
        return False

    if direction.upper() == "SHORT":
        if price_now >= zmin: return False
        dist = zmin - price_now
    else:
        if price_now <= zmax: return False
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

# ====== Checkliste (inkl. EMA200 Toleranz) ======
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
        else:                return (False, ok, [f"Kein bes
