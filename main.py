Autonomous MEXC scanner → Telegram (FastAPI + Scheduler)

MODE: Locker, aber ≥70% — Safe-Entry optional (mit Volumen-Bestätigung), Volumen-Spike nur Bonus, PROB_MIN=70

S/R-Ziele (15m/1h/4h), Scan alle 15 Minuten

Early-STRIKT (BOS+Retest, EMA-Stack, Micro-Fib) bleibt aktiv

import os, asyncio, time, math
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Any

import pandas as pd
import pandas_ta as ta
import ccxt
from fastapi import FastAPI
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

====== Config ======

TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

70 liquide Spot-Paare (MEXC-Notation "XXX/USDT")

SYMBOLS = [
# ==== Top Marketcap & Liquidität ====
"BTC/USDT",   # ✔ MEXC Spot (Beispielseite). 0
"ETH/USDT",   # ✔ MEXC Spot. 1
"BNB/USDT",   # ✔ MEXC Spot. 2
"SOL/USDT",   # ✔ MEXC Spot. 3
"XRP/USDT",   # ✔ MEXC Spot. 4
"ADA/USDT",   # ✔ MEXC Spot. 5
"AVAX/USDT",  # ✔ MEXC Spot. 6
"DOT/USDT",   # ✔ MEXC Spot. 7
"TRX/USDT",   # ✔ MEXC Spot. 8
"LINK/USDT",  # ✔ MEXC Spot. 9
"POL/USDT",   # (ehem. MATIC) ✔ MEXC Spot. 10
"ATOM/USDT",  # ✔ MEXC Spot. 11
"NEAR/USDT",  # ✔ MEXC Spot. 12
"APT/USDT",   # ✔ MEXC Spot. 13
"ARB/USDT",   # ✔ MEXC Spot. 14

# ==== High Volatility / Movers ====  
"DOGE/USDT",  # ✔ MEXC Spot. 15  
"SHIB/USDT",  # ✔ MEXC Spot. 16  
"PEPE/USDT",  # ✔ MEXC Spot. 17  
"INJ/USDT",   # ✔ MEXC Spot. 18  
"OP/USDT",    # ✔ MEXC Spot. 19  
"SUI/USDT",   # ✔ MEXC Spot. 20  
"SEI/USDT",   # ✔ MEXC Spot. 21  
"TIA/USDT",   # ✔ MEXC Spot. 22  
"STX/USDT",   # ✔ MEXC Spot. 23  
"ONDO/USDT",  # ✔ MEXC Spot. 24  

# ==== Volatile + Trending Alts ====  
"PYTH/USDT",    # ✔ MEXC Spot. 25  
"RENDER/USDT",  # ✔ MEXC Spot (RNDR→RENDER). 26  
"LDO/USDT",     # ✔ MEXC Spot. 27  
"IMX/USDT",     # ✔ MEXC Spot. 28  
"FET/USDT",     # ✔ MEXC Spot. 29

]

====== Analyse & Scan ======

TF_TRIGGER = "5m"                  # Signal-TF (Bias/Vol/RSI/EMAs)
TF_FILTERS = ["15m","1h","4h"]     # Trendfilter (streng)
LOOKBACK = 500
SCAN_INTERVAL_S = 15 * 60          # alle 15 Minuten

====== Safe-Entry (Pullback in Fib-Zone auf 15m) ======

SAFE_ENTRY_REQUIRED = False        # <— Locker: Safe-Entry ist KEIN KO-Kriterium
SAFE_ENTRY_TF = "15m"              # Fib-/Impulse-Check auf 15m
PIVOT_LEFT_TRIG = 3                # Pivots für die Impuls-Erkennung
PIVOT_RIGHT_TRIG = 3
FIB_TOL_PCT = 0.10 / 100.0         # ±0.10 % Toleranz rund um 0.5–0.618

Volumen-Bestätigung direkt am Safe-Entry (15m)

ENTRY_VOL_FACTOR = 1.15            # Volumen > MA20 × Faktor (z.B. 1.10–1.30)
REQUIRE_ENTRY_VOL = True           # <— bleibt: Entry nur mit Volumen-Bestätigung (Qualitätsanker)

====== S/R (Timeframes für TPs & SL) ======

SR_TF_TP1 = "15m"                  # TP1 aus 15m
SR_TF_TP2 = "1h"                   # TP2 aus 1h (wenn möglich)
SR_TF_TP3 = "4h"                   # TP3 aus 4h (optional)
PIVOT_LEFT = 3                     # Pivot-Breite für Swings
PIVOT_RIGHT = 3
CLUSTER_PCT = 0.25 / 100.0         # Cluster-Toleranz (±0.15 %)
MIN_STRENGTH = 2                   # min. Anzahl an Swings für „starkes“ Level
TP2_FACTOR = 1.20                  # Fallback: TP2 = Entry + 1.2*(TP1-Entry)

====== ATR-Fallback (nur wenn S/R nicht verfügbar) ======

ATR_SL  = 1.7
TP1_ATR = 1.2
TP2_ATR = 2.4
TP3_ATR = 3.6

--- TP-Abstandsregeln ---

TP1_MIN_ATR = 1.0          # TP1 muss >= 1.0 × ATR vom Entry entfernt sein
PREFER_1H_IF_CLOSE = True  # wenn 15m-Level zu nah, nimm 1h als TP1

====== Checklisten-Settings (gelockert) ======

MIN_ATR_PCT        = 0.40
VOL_SPIKE_FACTOR   = 1.5
REQUIRE_VOL_SPIKE  = True         # <— Locker: Vol-Spike nicht Pflicht, nur Bonus
PROB_MIN           = 84            # <— Mindestens 70% Wahrscheinlichkeit
COOLDOWN_S         = 900

=== 24h-Mute pro Coin nach gesendetem Signal ===

DAILY_SILENCE_S    = 24 * 60 * 60

====== Anzeige-Settings ======

COMPACT_SIGNALS = True

====== Early-STRIKT (optional, gleiche Qualität wie Hauptsignal) ======

EARLY_WARN_ENABLED     = True
EARLY_STRICT_MODE      = False
EARLY_COOLDOWN_S       = 300
EARLY_PROB_MIN         = 82
EARLY_VOL_FACTOR       = 1.40
EARLY_WICK_MIN_PCT     = 45.0
EARLY_BOS_LOOKBACK     = 30
EARLY_RETEST_MAX_BARS  = 4

Micro-Fib (5m) Qualität des ersten Pullbacks nach BOS

MICRO_FIB_ENABLED      = True
MICRO_FIB_TOL_PCT      = 0.15 / 100.0
MICRO_FIB_PIVOT_L      = 2
MICRO_FIB_PIVOT_R      = 2

====== HEATMAP START ======

Orderbook-Heatmap (MEXC gratis, REST via ccxt)

HEATMAP_ENABLED: bool = True        # Daten erfassen (REST via ccxt)
HEATMAP_USE_IN_ANALYSIS: bool = True  # Heatmap in Probability/Checkliste einfließen lassen
HEATMAP_LIMIT:   int  = 1000        # Tiefe des Orderbuchs
HEATMAP_BIN_PCT: float = 0.02/100.0 # Preis-Bin-Größe (z.B. 0.02%)
HEATMAP_TOP_N:   int  = 5           # Top-N Bins je Seite
HEATMAP_NEAR_PCT:float = 0.08/100.0 # „nahe“ = ±0.05% vom Entry
HEATMAP_BONUS_STRONG: int = 6       # Bonus % wenn nahe Wall stark ist
HEATMAP_BONUS_WEAK:   int = 3       # Bonus % wenn nahe Wall schwächer ist

def _calc_notional(price: float, qty: float) -> float:
try:
return float(price) * float(qty)
except Exception:
return 0.0

def fetch_orderbook(symbol: str, limit: int = HEATMAP_LIMIT) -> Dict[str, List[Tuple[float, float, float]]]:
"""Holt das MEXC-Orderbuch via ccxt und gibt Listen von (price, qty, notional) für bids/asks zurück."""
ob = ex.fetch_order_book(symbol, limit=limit)
bids_raw = ob.get("bids", [])
asks_raw = ob.get("asks", [])
bids = [(float(p), float(q), _calc_notional(p, q)) for p, q in bids_raw]
asks = [(float(p), float(q), _calc_notional(p, q)) for p, q in asks_raw]
bids.sort(key=lambda x: x[0], reverse=True)
asks.sort(key=lambda x: x[0])
return {"bids": bids, "asks": asks}

def _bin_key(p: float, bin_pct: float, anchor: float) -> float:
step = max(bin_pct * max(anchor, 1e-12), 1e-12)
offset = (p - anchor) / step
snapped = round(offset)
return round(anchor + snapped * step, 12)

def aggregate_walls_binned(orderbook: Dict[str, List[Tuple[float, float, float]]],
ref_price: float,
bin_pct: float = HEATMAP_BIN_PCT) -> Dict[str, List[Tuple[float, float, float]]]:
out = {"bids": {}, "asks": {}}
for side in ("bids", "asks"):
for price, qty, notional in orderbook.get(side, []):
key = _bin_key(price, bin_pct, ref_price)
if key not in out[side]:
out[side][key] = [0.0, 0.0]
out[side][key][0] += qty
out[side][key][1] += notional

def _topn(d: Dict[float, List[float]], n: int) -> List[Tuple[float, float, float]]:  
    rows = [(k, v[0], v[1]) for k, v in d.items()]  
    rows.sort(key=lambda t: t[2], reverse=True)  
    return rows[:n]  

return {"bids": _topn(out["bids"], HEATMAP_TOP_N), "asks": _topn(out["asks"], HEATMAP_TOP_N)}

def nearest_walls(orderbook: Dict[str, List[Tuple[float, float, float]]],
ref_price: float) -> Dict[str, Tuple[float, float, float] | None]:
nearest_bid = None
nearest_ask = None
bids_near = [(p,q,n) for (p,q,n) in orderbook.get("bids", []) if p <= ref_price]
asks_near = [(p,q,n) for (p,q,n) in orderbook.get("asks", []) if p >= ref_price]
if bids_near:
bids_near.sort(key=lambda t: (abs((ref_price - t[0]) / max(ref_price,1e-12)), -t[2]))
nearest_bid = bids_near[0]
if asks_near:
asks_near.sort(key=lambda t: (abs((t[0] - ref_price) / max(ref_price,1e-12)), -t[2]))
nearest_ask = asks_near[0]
return {"bid": nearest_bid, "ask": nearest_ask}

def is_within_pct(level_price: float, ref_price: float, pct: float) -> bool:
return abs(level_price - ref_price) / max(ref_price, 1e-12) <= pct

====== HEATMAP END ======

====== Init ======

if not TG_TOKEN or not TG_CHAT_ID:
raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID environment variables.")

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="MEXC Auto Scanner → Telegram (Locker ≥70% + S/R)")
ex = ccxt.mexc({"enableRateLimit": True})

last_signal: Dict[str, float] = {}
early_last_signal: Dict[str, float] = {}  # separater Cooldown-Speicher
last_scan_report: Dict[str, Any] = {"ts": None, "symbols": {}}
daily_mute: Dict[str, float] = {}  # key="SYM" -> timestamp letztes Signal

====== TA Helpers ======

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
return (c.iloc[-1] < o.iloc[-1]) and (o.iloc[-1] >= l.iloc[-2]) and (c.iloc[-1] <= l.iloc[-2])

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

====== Helper 24h-Mute ======

def is_daily_muted(symbol: str, now_ts: float) -> Tuple[bool, float]:
last_ts = daily_mute.get(symbol, 0.0)
elapsed = now_ts - last_ts
if elapsed < DAILY_SILENCE_S:
return True, DAILY_SILENCE_S - elapsed
return False, 0.0

====== Trigger & Trend ======

def compute_trend_ok(df: pd.DataFrame) -> Tuple[bool,bool,bool]:
ema200 = ema(df["close"], 200)
up = df["close"].iloc[-1] > ema200.iloc[-1]
down = df["close"].iloc[-1] < ema200.iloc[-1]
return up, down, not math.isnan(df["close"].iloc[-1])

def analyze_trigger(df: pd.DataFrame) -> Dict[str, Any]:
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

====== Safe-Entry (letzte Impulsbewegung per Pivots, SAFE_ENTRY_TF) ======

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
hi_idx = find_pivot_indices(list(highs), PIVOT_LEFT_TRIG, PIVOT_RIGHT_TRIG, True)
lo_idx = find_pivot_indices(list(lows ), PIVOT_LEFT_TRIG, PIVOT_RIGHT_TRIG, False)
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
zmin *= (1 - FIB_TOL_PCT)
zmax *= (1 + FIB_TOL_PCT)
ok = (direction == "LONG") and (zmin <= price <= zmax)
return (ok, (zmin, zmax))
else:
fib50  = hi_v - 0.5  * (hi_v - lo_v)
fib618 = hi_v - 0.618 * (hi_v - lo_v)
zmin, zmax = sorted([fib618, fib50])
zmin *= (1 - FIB_TOL_PCT)
zmax *= (1 + FIB_TOL_PCT)
ok = (direction == "SHORT") and (zmin <= price <= zmax)
return (ok, (zmin, zmax))

====== S/R-Tools (für 15m/1h/4h) ======

def find_pivots_levels(df: pd.DataFrame) -> Tuple[List[Tuple[float,int]], List[Tuple[float,int]]]:
highs = df["high"].values
lows  = df["low"].values
n = len(df)

swing_highs, swing_lows = [], []  
for i in range(PIVOT_LEFT, n - PIVOT_RIGHT):  
    left  = highs[i-PIVOT_LEFT:i]  
    right = highs[i+1:i+1+PIVOT_RIGHT]  
    if all(highs[i] > left) and all(highs[i] > right):  
        swing_highs.append(highs[i])  
    left  = lows[i-PIVOT_LEFT:i]  
    right = lows[i+1:i+1+PIVOT_RIGHT]  
    if all(lows[i] < left) and all(lows[i] < right):  
        swing_lows.append(lows[i])  

def cluster_levels(values: List[float], tol_pct: float) -> List[Tuple[float,int]]:  
    values = sorted(values)  
    clusters = []  
    if not values:  
        return clusters  
    cluster = [values[0]]  
    for x in values[1:]:  
        center = sum(cluster) / len(cluster)  
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
if strength < min_strength:
continue
if direction == "LONG" and price > ref_price:
candidates.append(price)
elif direction == "SHORT" and price < ref_price:
candidates.append(price)
if not candidates:
return None
return min(candidates) if direction == "LONG" else max(candidates)

def get_sr_levels(symbol: str, timeframe: str) -> Tuple[List[Tuple[float,int]], List[Tuple[float,int]]]:
df = fetch_df(symbol, timeframe, limit=LOOKBACK)
return find_pivots_levels(df)

def make_levels_sr_mtf(symbol: str, direction: str, entry: float, atrv: float) -> Tuple[float,float,float,float,float,bool]:
# Level sammeln
res_15, sup_15 = get_sr_levels(symbol, SR_TF_TP1)
res_1h, sup_1h = get_sr_levels(symbol, SR_TF_TP2)
res_4h, sup_4h = get_sr_levels(symbol, SR_TF_TP3)

def candidates_above(levels: List[Tuple[float,int]], ref: float, min_strength: int) -> List[float]:  
    return sorted([p for (p,s) in levels if s >= min_strength and p > ref])  

def candidates_below(levels: List[Tuple[float,int]], ref: float, min_strength: int) -> List[float]:  
    return sorted([p for (p,s) in levels if s >= min_strength and p < ref], reverse=True)  

used_sr = False  

if direction == "LONG":  
    # Kandidatenliste aufbauen  
    c15 = candidates_above(res_15, entry, MIN_STRENGTH)  
    c1h = candidates_above(res_1h, entry, MIN_STRENGTH)  
    c4h = candidates_above(res_4h, entry, MIN_STRENGTH)  

    # wähle TP1 aus 15m, aber nicht zu nah  
    tp1 = c15[0] if c15 else None  
    if tp1 is not None and (tp1 - entry) < TP1_MIN_ATR * atrv and PREFER_1H_IF_CLOSE:  
        # nimm direkt das nächste 1h-Level als TP1  
        tp1 = c1h[0] if c1h else tp1  

    # falls 15m-TP1 immer noch zu nah, versuche nächstes 15m-Cluster  
    i = 0  
    while tp1 is not None and (tp1 - entry) < TP1_MIN_ATR * atrv and i+1 < len(c15):  
        i += 1  
        tp1 = c15[i]  

    # SL von 15m-Support  
    sl_list = candidates_below(sup_15, entry, MIN_STRENGTH)  
    sl = sl_list[0] if sl_list else None  

    if tp1 is not None and sl is not None and tp1 > entry and sl < entry:  
        used_sr = True  
        # TP2: 1h oder ATR-Faktor  
        tp2 = c1h[0] if c1h and c1h[0] > tp1 else round(entry + (tp1 - entry) * TP2_FACTOR, 6)  
        # TP3: 4h optional  
        tp3 = c4h[0] if c4h and c4h[0] > tp2 else None  
        return entry, round(sl,6), round(tp1,6), round(tp2,6), (round(tp3,6) if tp3 else None), used_sr  

else:  # SHORT  
    c15 = candidates_below(sup_15, entry, MIN_STRENGTH)  
    c1h = candidates_below(sup_1h, entry, MIN_STRENGTH)  
    c4h = candidates_below(sup_4h, entry, MIN_STRENGTH)  

    tp1 = c15[0] if c15 else None  
    if tp1 is not None and (entry - tp1) < TP1_MIN_ATR * atrv and PREFER_1H_IF_CLOSE:  
        tp1 = c1h[0] if c1h else tp1  

    i = 0  
    while tp1 is not None and (entry - tp1) < TP1_MIN_ATR * atrv and i+1 < len(c15):  
        i += 1  
        tp1 = c15[i]  

    sl_list = candidates_above(res_15, entry, MIN_STRENGTH)  
    sl = sl_list[0] if sl_list else None  

    if tp1 is not None and sl is not None and tp1 < entry and sl > entry:  
        used_sr = True  
        tp2 = c1h[0] if c1h and c1h[0] < tp1 else round(entry - (entry - tp1) * TP2_FACTOR, 6)  
        tp3 = c4h[0] if c4h and c4h[0] < tp2 else None  
        return entry, round(sl,6), round(tp1,6), round(tp2,6), (round(tp3,6) if tp3 else None), used_sr  

# --- ATR Fallback ---  
if direction == "LONG":  
    sl  = round(entry - ATR_SL  * atrv, 6)  
    tp1 = round(entry + max(TP1_ATR, TP1_MIN_ATR) * atrv, 6)  
    tp2 = round(entry + TP2_ATR * atrv, 6)  
    tp3 = round(entry + TP3_ATR * atrv, 6)  
else:  
    sl  = round(entry + ATR_SL  * atrv, 6)  
    tp1 = round(entry - max(TP1_ATR, TP1_MIN_ATR) * atrv, 6)  
    tp2 = round(entry - TP2_ATR * atrv, 6)  
    tp3 = round(entry - TP3_ATR * atrv, 6)  
return entry, sl, tp1, tp2, tp3, False

====== Checkliste (gelockert, ≥70%) ======

def build_checklist_for_dir(direction: str, trig: Dict[str, Any], up_all: bool, dn_all: bool,
safe_ok_long: bool, safe_ok_short: bool) -> Tuple[bool, List[str], List[str]]:
ok, warn = [], []

# ATR-Volatilität als KO  
atr_pct = (trig["atr"] / max(trig["price"], 1e-9)) * 100.0  
if atr_pct >= MIN_ATR_PCT: ok.append(f"ATR≥{MIN_ATR_PCT}% ({atr_pct:.2f}%)")  
else:                      return (False, ok, [f"ATR<{MIN_ATR_PCT}% ({atr_pct:.2f}%)"])  

# HTF-Alignment (KO)  
if (up_all if direction=="LONG" else dn_all): ok.append("HTF align (15m/1h/4h)")  
else:                                         return (False, ok, ["HTF nicht aligned"])  

# Bias durch Engulf oder EMA-S
