# Autonomous MEXC scanner → Telegram (FastAPI + Scheduler)
# STRIKT + Safe-Entry (Fib 0.5–0.618 Pullback, 5m) + S/R-Ziele (15m/1h/4h) + Scan alle 15 min

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

# 70 liquide Spot-Paare (MEXC-Notation "XXX/USDT")
SYMBOLS = [
    # bestehende 25
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
    "TON/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT","LINK/USDT",
    "TRX/USDT","DOT/USDT","MATIC/USDT","SHIB/USDT","PEPE/USDT",
    "LTC/USDT","BCH/USDT","ATOM/USDT","NEAR/USDT","APT/USDT",
    "ARB/USDT","OP/USDT","SUI/USDT","INJ/USDT","FIL/USDT",

    # +25 neue
    "AAVE/USDT","APE/USDT","ARKM/USDT","BLUR/USDT","CHZ/USDT",
    "COMP/USDT","CRV/USDT","DYDX/USDT","EGLD/USDT","FET/USDT",
    "GALA/USDT","GMX/USDT","GRT/USDT","ICP/USDT","IMX/USDT",
    "LDO/USDT","MKR/USDT","PYTH/USDT","RNDR/USDT","RUNE/USDT",
    "SEI/USDT","STX/USDT","TIA/USDT","UNI/USDT","ONDO/USDT",

    # +20 weitere (insgesamt 70)
    "ALGO/USDT","ANKR/USDT","BAT/USDT","COTI/USDT","DASH/USDT",
    "ENJ/USDT","FLOW/USDT","FTM/USDT","HNT/USDT","JTO/USDT",
    "KAVA/USDT","KAS/USDT","KLAY/USDT","MANA/USDT","MINA/USDT",
    "NTRN/USDT","OCEAN/USDT","QNT/USDT","ROSE/USDT","ZIL/USDT",
]

# ====== Analyse & Scan ======
TF_TRIGGER = "5m"                  # Signal-TF
TF_FILTERS = ["15m","1h","4h"]     # Trendfilter (streng)
LOOKBACK = 300
SCAN_INTERVAL_S = 15 * 60          # alle 15 Minuten

# ====== Safe-Entry (Pullback in Fib-Zone auf 5m) ======
SAFE_ENTRY_REQUIRED = True         # Safe-Entry ist KO-Kriterium
PIVOT_LEFT_TRIG = 3                # Pivots für die Impuls-Erkennung (5m)
PIVOT_RIGHT_TRIG = 3
FIB_TOL_PCT = 0.10 / 100.0         # ±0.10 % Toleranz rund um 0.5–0.618

# ====== S/R (Timeframes für TPs & SL) ======
SR_TF_TP1 = "15m"                  # TP1 aus 15m
SR_TF_TP2 = "1h"                   # TP2 aus 1h (wenn möglich)
SR_TF_TP3 = "4h"                   # TP3 aus 4h (optional)
PIVOT_LEFT = 3                     # Pivot-Breite für Swings
PIVOT_RIGHT = 3
CLUSTER_PCT = 0.15 / 100.0         # Cluster-Toleranz (±0.15 %)
MIN_STRENGTH = 3                   # min. Anzahl an Swings für „starkes“ Level
TP2_FACTOR = 1.20                  # Fallback: TP2 = Entry + 1.2*(TP1-Entry)

# ====== ATR-Fallback (nur wenn S/R nicht verfügbar) ======
ATR_SL  = 1.5
TP1_ATR = 1.0
TP2_ATR = 1.8
TP3_ATR = 2.6

# ====== STRIKTE Checklisten-Settings ======
MIN_ATR_PCT        = 0.20       # min ATR% vom Preis
VOL_SPIKE_FACTOR   = 1.30       # Volumen > 1.30× MA20 (Pflicht)
REQUIRE_VOL_SPIKE  = True       # Volumenspike = KO-Kriterium
PROB_MIN           = 60         # mind. 60% Wahrscheinlichkeit
COOLDOWN_S         = 300        # Anti-Spam pro Symbol/Richtung

# === NEW: 24h-Mute pro Coin nach gesendetem Signal ===
DAILY_SILENCE_S    = 24 * 60 * 60  # 24 Stunden

# ====== Anzeige-Settings ======
COMPACT_SIGNALS = True   # True = nur Kerninfos (Richtung, Entry, TP1–TP3, SL, Prob%)

# ====== Init ======
if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID environment variables.")

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="MEXC Auto Scanner → Telegram (STRIKT + Safe-Entry + S/R)")

ex = ccxt.mexc({"enableRateLimit": True})

last_signal: Dict[str, float] = {}
last_scan_report: Dict[str, Any] = {"ts": None, "symbols": {}}
# NEW: Merker für 24h-Sperre pro Coin
daily_mute: Dict[str, float] = {}  # key = "SYM" -> timestamp des letzten gesendeten Signals

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

# NEW: Helper für 24h-Mute
def is_daily_muted(symbol: str, now_ts: float) -> Tuple[bool, float]:
    """
    True + Restsekunden, wenn 24h-Sperre aktiv.
    """
    last_ts = daily_mute.get(symbol, 0.0)
    elapsed = now_ts - last_ts
    if elapsed < DAILY_SILENCE_S:
        return True, DAILY_SILENCE_S - elapsed
    return False, 0.0

# ====== Trigger & Trend ======
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

# ====== Safe-Entry (letzte Impulsbewegung per Pivots, 5m) ======
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
    """Gibt ((lo_idx, lo_val),(hi_idx, hi_val)) der letzten aufeinanderfolgenden Low→High oder High→Low Sequenz zurück."""
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
    """Prüft, ob price in der 0.5–0.618 Fib-Zone (±Toleranz) liegt. Liefert (ok, (z_min, z_max))."""
    (lo_i, lo_v), (hi_i, hi_v) = impulse
    if hi_i == lo_i:
        return (False, (0.0, 0.0))
    if hi_i > lo_i:
        # Aufwärts-Impuls (long): low -> high
        fib50  = lo_v + 0.5  * (hi_v - lo_v)
        fib618 = lo_v + 0.618 * (hi_v - lo_v)
        zmin, zmax = sorted([fib50, fib618])
        zmin *= (1 - FIB_TOL_PCT)
        zmax *= (1 + FIB_TOL_PCT)
        ok = (direction == "LONG") and (zmin <= price <= zmax)
        return (ok, (zmin, zmax))
    else:
        # Abwärts-Impuls (short): high -> low
        fib50  = hi_v - 0.5  * (hi_v - lo_v)
        fib618 = hi_v - 0.618 * (hi_v - lo_v)
        zmin, zmax = sorted([fib618, fib50])  # bei Shorts liegt 0.618 unter 0.5
        zmin *= (1 - FIB_TOL_PCT)
        zmax *= (1 + FIB_TOL_PCT)
        ok = (direction == "SHORT") and (zmin <= price <= zmax)
        return (ok, (zmin, zmax))

# ====== S/R-Tools (für 15m/1h/4h) ======
def find_pivots_levels(df: pd.DataFrame) -> Tuple[List[Tuple[float,int]], List[Tuple[float,int]]]:
    """Gibt (res_levels, sup_levels) als Listen von (level_price, strength) zurück."""
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
                clusters.append( (sum(cluster)/len(cluster), len(cluster)) )
                cluster = [x]
        clusters.append( (sum(cluster)/len(cluster), len(cluster)) )
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
    """
    TP1 aus 15m, TP2 aus 1h, TP3 aus 4h (optional). SL aus 15m Gegenseite.
    Fallback: ATR-Levels, wenn TP1+SL nicht sauber vorhanden.
    """
    res_15, sup_15 = get_sr_levels(symbol, SR_TF_TP1)
    res_1h, sup_1h = get_sr_levels(symbol, SR_TF_TP2)
    res_4h, sup_4h = get_sr_levels(symbol, SR_TF_TP3)

    if direction == "LONG":
        tp1 = nearest_level(res_15, entry, "LONG", MIN_STRENGTH)
        sl  = nearest_level(sup_15, entry, "SHORT", MIN_STRENGTH)
        if tp1 is not None and sl is not None and tp1 > entry and sl < entry:
            tp2 = nearest_level(res_1h, tp1, "LONG", MIN_STRENGTH)
            if tp2 is None:
                tp2 = round(entry + (tp1 - entry) * TP2_FACTOR, 6)
            tp3 = nearest_level(res_4h, tp2, "LONG", MIN_STRENGTH)
            return entry, round(sl,6), round(tp1,6), round(tp2,6), (round(tp3,6) if tp3 is not None else None), True
    else:
        tp1 = nearest_level(sup_15, entry, "SHORT", MIN_STRENGTH)
        sl  = nearest_level(res_15, entry, "LONG", MIN_STRENGTH)
        if tp1 is not None and sl is not None and tp1 < entry and sl > entry:
            tp2 = nearest_level(sup_1h, tp1, "SHORT", MIN_STRENGTH)
            if tp2 is None:
                tp2 = round(entry - (entry - tp1) * TP2_FACTOR, 6)
            tp3 = nearest_level(sup_4h, tp2, "SHORT", MIN_STRENGTH)
            return entry, round(sl,6), round(tp1,6), round(tp2,6), (round(tp3,6) if tp3 is not None else None), True

    # Fallback: ATR
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

# ====== Checkliste (streng) ======
def build_checklist_for_dir(direction: str, trig: Dict[str, Any], up_all: bool, dn_all: bool,
                            safe_ok_long: bool, safe_ok_short: bool) -> Tuple[bool, List[str], List[str]]:
    ok, warn = [], []

    atr_pct = (trig["atr"] / max(trig["price"], 1e-9)) * 100.0
    if atr_pct >= MIN_ATR_PCT: ok.append(f"ATR≥{MIN_ATR_PCT}% ({atr_pct:.2f}%)")
    else:                      return (False, ok, [f"ATR<{MIN_ATR_PCT}% ({atr_pct:.2f}%)"])

    if (up_all if direction=="LONG" else dn_all): ok.append("HTF align (15m/1h/4h)")
    else:                                         return (False, ok, ["HTF nicht aligned"])

    bias_ok = (trig["bull"] or trig["long_fast"]) if direction=="LONG" else (trig["bear"] or trig["short_fast"])
    if bias_ok: ok.append("Engulf/EMA-Stack")
    else:       return (False, ok, ["Kein Engulf/EMA-Stack"])

    ema200_ok = trig["ema200_up"] if direction=="LONG" else trig["ema200_dn"]
    if ema200_ok: ok.append("EMA200 ok")
    else:         return (False, ok, ["EMA200 gegen Setup"])

    if trig["vol_ok"]: ok.append(f"Vol>{VOL_SPIKE_FACTOR:.2f}×MA (Pflicht)")
    else:              return (False, ok, [f"kein Vol-Spike (≥{VOL_SPIKE_FACTOR:.2f}× Pflicht)"])

    # Safe-Entry (Fib-Zone) als hartes Kriterium
    if SAFE_ENTRY_REQUIRED:
        safe_ok = safe_ok_long if direction=="LONG" else safe_ok_short
        if safe_ok:
            ok.append("Safe-Entry: Fib 0.5–0.618 (5m)")
        else:
            return (False, ok, ["Kein Safe-Entry (Fib 0.5–0.618)"])

    # RSI nur Hinweis
    if direction=="LONG":
        if trig["rsi"] > 67: warn.append(f"RSI hoch ({trig['rsi']:.1f})")
        else: ok.append(f"RSI ok ({trig['rsi']:.1f})")
    else:
        if trig["rsi"] < 33: warn.append(f"RSI tief ({trig['rsi']:.1f})")
        else: ok.append(f"RSI ok ({trig['rsi']:.1f})")

    return (True, ok, warn)

def need_throttle(key: str, now: float, cool_s: int = COOLDOWN_S) -> bool:
    t = last_signal.get(key, 0.0)
    if now - t < cool_s:
        return True
    last_signal[key] = now
    return False

# ====== KOMPAKTE / DETAILLIERTE SIGNAL-NACHRICHT ======
async def send_signal(symbol: str, tf: str, direction: str,
                      entry: float, sl: float, tp1: float, tp2: float, tp3: float | None,
                      prob: int, checklist_ok: List[str], checklist_warn: List[str], used_sr: bool):

    if COMPACT_SIGNALS:
        # Kompakt: nur Kerninfos
        lines = [
            f"🛡 *STRIKT* — {symbol} {tf}",
            f"➡️ *{direction}*",
            f"🎯 Entry: `{entry}`",
            f"🏁 TP1: `{tp1}`",
            f"🏁 TP2: `{tp2}`",
            *( [f"🏁 TP3: `{tp3}`"] if tp3 is not None else [] ),
            f"🛡 SL: `{sl}`",
            f"📈 Prob.: *{prob}%*",
        ]
        text = "\n".join(lines)
    else:
        # Detailmodus wie zuvor
        checks_line = ""
        if checklist_ok:   checks_line += f"✅ {', '.join(checklist_ok)}\n"
        if checklist_warn: checks_line += f"⚠️ {', '.join(checklist_warn)}\n"
        sr_note = "S/R 15m/1h/4h" if used_sr else "ATR-Fallback"
        text = (
            f"🛡 *STRIKT* — Signal {symbol} {tf}\n"
            f"➡️ *{direction}*  ({sr_note})\n"
            f"🎯 Entry: `{entry}`\n"
            f"🛡 SL: `{sl}`\n"
            f"🏁 TP1: `{tp1}`\n"
            f"🏁 TP2: `{tp2}`\n"
            + (f"🏁 TP3: `{tp3}`\n" if tp3 is not None else "")
            + f"📈 Prob.: *{prob}%*\n"
            f"{checks_line}".strip()
        )

    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

async def send_mode_banner():
    text = (
        "🛡 *Scanner gestartet – MODUS: STRENG + Safe-Entry + S/R-Ziele*\n"
        f"• Scan alle 15 Minuten\n"
        "• Safe-Entry: Pullback in Fib 0.5–0.618 (5m) — Pflicht\n"
        "• HTF strikt aligned (15m/1h/4h)\n"
        f"• Volumen: Pflicht ≥ {VOL_SPIKE_FACTOR:.2f}× MA20\n"
        f"• ATR%-Schwelle aktiv (≥ {MIN_ATR_PCT:.2f}%)\n"
        f"• Wahrscheinlichkeit ≥ {PROB_MIN}%\n"
        "• TP1=15m, TP2=1h, TP3=4h (sofern vorhanden). Fallback: ATR\n"
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
            # NEW: 24h-Mute check pro Coin
            muted, rest = is_daily_muted(sym, now)
            if muted:
                hrs = int(rest // 3600)
                mins = int((rest % 3600) // 60)
                last_scan_report["symbols"][sym] = {"skip": f"24h-Mute aktiv – noch {hrs}h {mins}m"}
                time.sleep(ex.rateLimit/1000)
                continue

            # Trigger-TF
            df5  = fetch_df(sym, TF_TRIGGER)
            trig = analyze_trigger(df5)

            # Safe-Entry Check (Fib-Zone auf 5m)
            imp = last_impulse(df5)
            safe_ok_long, safe_ok_short = False, False
            if imp is not None:
                long_ok, _zoneL = fib_zone_ok(trig["price"], imp, "LONG")
                short_ok, _zoneS = fib_zone_ok(trig["price"], imp, "SHORT")
                safe_ok_long, safe_ok_short = long_ok, short_ok

            # Trendfilter
            up_all, dn_all = True, True
            for tf in TF_FILTERS:
                df_tf = fetch_df(sym, tf)
                up, dn, _ = compute_trend_ok(df_tf)
                up_all &= up
                dn_all &= dn
                time.sleep(ex.rateLimit/1000)

            # Richtung(en) prüfen
            passes = []
            for direction in ("LONG","SHORT"):
                passed, ok_tags, warn_tags = build_checklist_for_dir(
                    direction, trig, up_all, dn_all, safe_ok_long, safe_ok_short
                )
                if passed:
                    prob = prob_score(direction=="LONG", direction=="SHORT", trig["vol_ok"], up_all or dn_all,
                                      trig["ema200_up"] if direction=="LONG" else trig["ema200_dn"])
                    passes.append( (direction, prob, ok_tags, warn_tags) )

            if passes:
                direction, prob, ok_tags, warn_tags = sorted(passes, key=lambda x: x[1], reverse=True)[0]
                if prob >= PROB_MIN:
                    # S/R-Ziele multi-TF (15m/1h/4h)
                    entry, sl, tp1, tp2, maybe_tp3, used_sr = make_levels_sr_mtf(sym, direction, trig["price"], trig["atr"])
                    key = f"{sym}:{direction}"
                    throttled = need_throttle(key, now)

                    last_scan_report["symbols"][sym] = {
                        "direction": direction, "prob": prob, "throttled": throttled,
                        "ok": ok_tags, "warn": warn_tags,
                        "price": trig["price"], "atr": trig["atr"],
                        "sr_used": used_sr, "safe_entry": (safe_ok_long if direction=="LONG" else safe_ok_short)
                    }
                    if not throttled:
                        await send_signal(sym, TF_TRIGGER, direction, entry, sl, tp1, tp2, maybe_tp3, prob, ok_tags, warn_tags, used_sr)
                        # NEW: nach Send Coin für 24h stummschalten
                        daily_mute[sym] = now
                else:
                    last_scan_report["symbols"][sym] = {"skip": f"prob {prob}% < {PROB_MIN}%"}
            else:
                last_scan_report["symbols"][sym] = {"skip": "Checkliste nicht bestanden"}

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

@app.get("/")
async def root():
    return {
        "ok": True,
        "mode": "streng_sr_safe",
        "symbols": SYMBOLS,
        "trigger_tf": TF_TRIGGER,
        "filters": TF_FILTERS,
        "scan_interval_s": SCAN_INTERVAL_S,
        "sr_tps": {"tp1": SR_TF_TP1, "tp2": SR_TF_TP2, "tp3": SR_TF_TP3},
        "info": "Background scanner active. TradingView not required."
    }

@app.get("/scan")
async def manual_scan():
    await scan_once()
    return {"ok": True, "ran": True, "ts": last_scan_report.get("ts")}

@app.get("/status")
async def status():
    return {"ok": True, "mode": "streng_sr_safe", "report": last_scan_report}

@app.get("/test")
async def test():
    text = "✅ Test: Bot & Telegram OK — Mode: *STRENG + Safe-Entry (Fib) + S/R (15m/1h/4h)*, Scan: alle 15 Min."
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
    return {"ok": True, "test": True, "mode": "streng_sr_safe"}
