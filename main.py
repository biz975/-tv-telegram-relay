# main.py — MEXC Auto-Scanner (Final 3.0 Checkliste)
# Safe-Entry 0.25%, Fib 0.5–0.618, ATR%/Volumen-Pflicht, Danger-Zone, Prob≥75,
# TP1=15% & TP2=30% (auf Margin @20x), SL=10% (Margin) -> Follow-up: TP1 ⇒ SL=BE

import os, asyncio, time, math
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Dict, Any

from fastapi import FastAPI
from telegram import Bot
import ccxt

# ========= ENV =========
TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID")

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="MEXC Auto Scanner → Telegram (Final 3.0)")

# ========= MEXC (ccxt) =========
ex = ccxt.mexc({"enableRateLimit": True})

# ========= SETTINGS =========
SYMBOLS = [
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
    "TON/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT","LINK/USDT"
]

TF              = "5m"                    # Trigger TF
FILTER_TFS      = ["15m", "1h", "4h"]     # HTF-Filter
LOOKBACK        = 300
SCAN_INTERVAL_S = 60                      # wie oft scannen

# Leverage & Zielgrößen (auf Margin)
LEVERAGE         = 20
TP_MARGIN_PCTS   = [15.0, 30.0]           # TP1/TP2 auf Margin
SL_MARGIN_PCT    = 10.0                   # SL auf Margin
TP_PCTS          = [p / LEVERAGE for p in TP_MARGIN_PCTS]   # in Preis-%
SL_PCT           = SL_MARGIN_PCT / LEVERAGE                 # in Preis-%

# Safe-Entry & Filter
SAFE_ENTRY_PCT   = 0.25                   # Pullback vom aktuellen Preis
MIN_ATR_PCT      = 0.20                   # min. ATR% auf 5m
VOL_SPIKE_FACTOR = 1.30                   # Volumen ≥ 1.30× MA20 (Pflicht)
DANGER_ZONE_PCT  = 0.20                   # Nähe zu HTF Extremes -> Skip
MIN_PROB         = 75                     # Punkte-Schwelle

# Pending/Anti-Spam
PENDING_TTL_MIN  = 180                    # max. Wartezeit auf Safe-Entry
COOLDOWN_MIN     = 5                      # pro Symbol+Richtung

# ========= STATE =========
_last_scan_at: datetime | None = None
_last_error: str | None = None
_signals_sent: int = 0

_pending: Dict[str, Dict[str, Any]] = {}       # key="SYM:LONG|SHORT" -> setup + ts
_last_sent_at: Dict[str, datetime] = {}        # cooldown
_active: Dict[str, Dict[str, Any]] = {}        # aktive Positionen für TP1→BE
_last_scan_report: Dict[str, Any] = {"ts": None, "symbols": {}}

# ========= TA (leichtgewichtig) =========
def ema(values: List[float], period: int) -> List[float]:
    k = 2 / (period + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out

def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) < period + 1:
        return [50.0] * len(values)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(values)):
        chg = values[i] - values[i-1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    rsis = [50.0] * len(values)
    for i in range(period, len(values)):
        ag = sum(gains[i-period+1:i+1]) / period
        al = sum(losses[i-period+1:i+1]) / period
        rs = (ag / al) if al != 0 else 1e9
        rsis[i] = 100 - (100 / (1 + rs))
    return rsis

def atr_pct(op: List[float], hi: List[float], lo: List[float], cl: List[float], period: int = 14) -> float:
    trs = []
    for i in range(1, len(cl)):
        tr = max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    atr = sum(trs[-period:]) / period
    price = cl[-1]
    return (atr / price) * 100.0 if price > 0 else 0.0

def sma(vals: List[float], period: int) -> List[float]:
    out = []
    run = 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= period: run -= vals[i-period]
        out.append( (run/period) if i >= period-1 else float('nan') )
    return out

def fmt(n: float) -> str:
    if n >= 100: return f"{n:,.2f}"
    if n >= 1:   return f"{n:,.4f}"
    return f"{n:.8f}"

def make_safe_entry(entry: float, is_long: bool) -> float:
    p = SAFE_ENTRY_PCT / 100.0
    return entry * (1 - p) if is_long else entry * (1 + p)

def touched_safe(safe: float, last_low: float, last_high: float, is_long: bool) -> bool:
    return (last_low <= safe) if is_long else (last_high >= safe)

def calc_targets_from_price(entry_now: float, is_long: bool) -> Tuple[float, float, float]:
    # TPs nach Preis-% (TP1=15%/20x=0.75%, TP2=30%/20x=1.5%)
    step1 = TP_PCTS[0] / 100.0
    step2 = TP_PCTS[1] / 100.0
    if is_long:
        tp1 = entry_now * (1 + step1)
        tp2 = entry_now * (1 + step2)
    else:
        tp1 = entry_now * (1 - step1)
        tp2 = entry_now * (1 - step2)
    return tp1, tp2, 0.0  # TP3 optional nicht verwendet

def calc_sl_from_price(entry_now: float, is_long: bool) -> float:
    step = SL_PCT / 100.0
    return entry_now * (1 - step) if is_long else entry_now * (1 + step)

def rr(entry: float, tp: float, sl: float, is_long: bool) -> float:
    if is_long: reward, risk = tp - entry, entry - sl
    else:       reward, risk = entry - tp, sl - entry
    return round(reward / risk, 2) if risk > 0 else 0.0

# ========= SWING, FIB, DANGER =========
def swing_high_low(series: List[float], lookback: int = 50) -> Tuple[float, float]:
    look = series[-lookback:] if len(series) >= lookback else series[:]
    return max(look), min(look)

def fib_zone_ok(entry_safe: float, swing_low: float, swing_high: float, is_long: bool) -> bool:
    if swing_high <= 0 or swing_low <= 0 or swing_high == swing_low:
        return False
    diff = swing_high - swing_low
    if is_long:
        f50  = swing_low + 0.5   * diff
        f618 = swing_low + 0.618 * diff
        lo, hi = (min(f50, f618), max(f50, f618))
        return lo <= entry_safe <= hi
    else:
        f50  = swing_high - 0.5   * diff
        f618 = swing_high - 0.618 * diff
        lo, hi = (min(f50, f618), max(f50, f618))
        return lo <= entry_safe <= hi

def too_close_to_htf_extremes(price: float, htf_high: float, htf_low: float) -> bool:
    if htf_high <= 0 or htf_low <= 0:
        return False
    up_dist = abs(htf_high - price) / price * 100.0
    dn_dist = abs(price - htf_low)  / price * 100.0
    return (up_dist < DANGER_ZONE_PCT) or (dn_dist < DANGER_ZONE_PCT)

# ========= DATA (MEXC via ccxt) =========
def fetch_ohlcv(symbol: str, timeframe: str, limit: int = LOOKBACK):
    # ccxt liefert: [ms, open, high, low, close, volume]
    ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    ts   = [x[0] for x in ohlcv]
    o    = [float(x[1]) for x in ohlcv]
    h    = [float(x[2]) for x in ohlcv]
    l    = [float(x[3]) for x in ohlcv]
    c    = [float(x[4]) for x in ohlcv]
    v    = [float(x[5]) for x in ohlcv]
    return ts, o, h, l, c, v

def indicators(symbol: str, timeframe: str) -> Dict[str, Any] | None:
    ts, o, h, l, c, v = fetch_ohlcv(symbol, timeframe, LOOKBACK)
    if len(c) < 50:
        return None
    ema9  = ema(c, 9)[-1]
    ema21 = ema(c, 21)[-1]
    rsi14 = rsi(c, 14)[-1]
    atrp  = atr_pct(o, h, l, c, 14)
    vma20 = sma(v, 20)[-1]
    vol_spike = (v[-1] >= VOL_SPIKE_FACTOR * vma20) if (vma20 == vma20) else False  # nan-check
    return {
        "ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v,
        "close": c[-1],
        "ema9": ema9, "ema21": ema21, "rsi": rsi14,
        "atr_pct": atrp,
        "vol_spike": bool(vol_spike),
        "vma20": vma20
    }

def probability(main: dict, filters: List[dict], is_long: bool) -> int:
    ema_ok = (main["ema9"] > main["ema21"]) if is_long else (main["ema9"] < main["ema21"])
    rsi_ok = (main["rsi"] >= 48) if is_long else (main["rsi"] <= 52)
    htf_ok = all((f["ema9"] > f["ema21"]) if is_long else (f["ema9"] < f["ema21"]) for f in filters)
    vol_ok = main["vol_spike"]
    score = 0
    score += 25 if ema_ok else 0
    score += 25 if rsi_ok else 0
    score += 25 if htf_ok else 0
    score += 10 if vol_ok else 0
    return min(100, score)

# ========= CORE ANALYSE (Final 3.0) =========
def analyze_symbol(symbol: str):
    # Haupt-TF (5m)
    main = indicators(symbol, TF)
    if not main:
        return None, ["insufficient candles"]

    notes = []

    # ATR% Pflicht
    notes.append(f"ATR%={main['atr_pct']:.3f} (min {MIN_ATR_PCT})")
    if main["atr_pct"] < MIN_ATR_PCT:
        return None, notes + [f"REJECT: ATR%<{MIN_ATR_PCT}"]

    # Volumen-Spike Pflicht
    notes.append(f"VolSpike={'YES' if main['vol_spike'] else 'NO'} (≥ {VOL_SPIKE_FACTOR:.2f}×MA20)")
    if not main["vol_spike"]:
        return None, notes + [f"REJECT: kein Volumen-Spike (≥ {VOL_SPIKE_FACTOR:.2f}×)"]

    # HTF Filter (15m/1h/4h)
    filt = []
    for tf in FILTER_TFS:
        x = indicators(symbol, tf)
        if not x:
            return None, notes + [f"REJECT: missing {tf}"]
        filt.append(x)

    # Richtung (EMA9/21 + RSI Bias)
    is_long  = (main["ema9"] > main["ema21"]) and (main["rsi"] >= 48)
    is_short = (main["ema9"] < main["ema21"]) and (main["rsi"] <= 52)
    dir_txt  = "LONG" if is_long else ("SHORT" if is_short else "NONE")
    notes.append(f"direction={dir_txt} (EMA9/21 & RSI)")
    if not (is_long or is_short):
        return None, notes + ["REJECT: no clear bias"]

    # Danger-Zone (HTF 1h Extremes, 120 Kerzen)
    htf1h = filt[1] if len(filt) >= 2 else filt[-1]
    htf_high, htf_low = max(htf1h["h"][-120:]), min(htf1h["l"][-120:])
    if too_close_to_htf_extremes(main["close"], htf_high, htf_low):
        return None, notes + [f"REJECT: danger-zone < {DANGER_ZONE_PCT}%"]

    # Probability
    prob = probability(main, filt, is_long)
    notes.append(f"prob={prob}% (min {MIN_PROB}%)")
    if prob < MIN_PROB:
        return None, notes + [f"REJECT: prob<{MIN_PROB}%"]

    # Safe-Entry + Fib(0.5–0.618)
    entry_now = main["close"]
    safe      = make_safe_entry(entry_now, is_long)
    sw_high, sw_low = swing_high_low(main["c"], lookback=50)
    in_fib = fib_zone_ok(safe, sw_low, sw_high, is_long)
    notes.append(f"fib(0.5–0.618)={'OK' if in_fib else 'NO'}")
    if not in_fib:
        return None, notes + ["REJECT: safe-entry not in fib zone"]

    # Targets & SL (Preis-% anhand Margin%)
    tp1, tp2, _ = calc_targets_from_price(entry_now, is_long)
    sl = calc_sl_from_price(entry_now, is_long)
    rr1 = rr(safe, tp1, sl, is_long)
    rr2 = rr(safe, tp2, sl, is_long)

    setup = {
        "symbol": symbol, "display": symbol.replace("/USDT","/USDT"),
        "direction": "LONG" if is_long else "SHORT",
        "is_long": is_long,
        "entry_now": entry_now,
        "safe": safe, "sl": sl,
        "tp1": tp1, "tp2": tp2,
        "prob": prob,
        "rr1": rr1, "rr2": rr2,
        "last_low": main["l"][-1], "last_high": main["h"][-1],
    }
    return setup, notes + ["ACCEPT"]

async def send_signal_safe_triggered(s: Dict[str, Any]):
    text = (
        f"⚡ *Signal* {s['display']} ({TF}) — *SAFE ENTRY getriggert*\n"
        f"➡️ *{s['direction']}*\n"
        f"🎯 Entry (Safe): `{fmt(s['safe'])}`  (Pullback ≈ {SAFE_ENTRY_PCT}%)\n"
        f"🛡 SL: `{fmt(s['sl'])}`  (≈ -{SL_MARGIN_PCT}% @20x)\n"
        f"🏁 TP1: `{fmt(s['tp1'])}` (≈ +{TP_MARGIN_PCTS[0]}% @20x, R:R {s['rr1']})\n"
        f"🏁 TP2: `{fmt(s['tp2'])}` (≈ +{TP_MARGIN_PCTS[1]}% @20x, R:R {s['rr2']})\n"
        f"📊 Prob.: *{s['prob']}%*  | HTF: 15m/1h/4h aligned\n"
        f"✅ Final3.0: EMA9/21 & RSI, Vol≥{VOL_SPIKE_FACTOR:.2f}×MA20, ATR≥{MIN_ATR_PCT}%, Fib 0.5–0.618, Safe-Entry, no Danger-Zone\n"
        f"📌 Regel: Nach *TP1* → *SL auf BE*."
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

async def send_followup_be(sym_display: str, direction: str):
    msg = f"🔔 *Follow-up* {sym_display} — *TP1 erreicht* → SL jetzt auf **BE** setzen."
    await bot.send_message(chat_id=TG_CHAT_ID, text=msg, parse_mode="Markdown")

# ========= SCAN RUN =========
def cooldown_ready(key: str, now: datetime) -> bool:
    last_t = _last_sent_at.get(key)
    return (not last_t) or ((now - last_t) >= timedelta(minutes=COOLDOWN_MIN))

async def scan_once() -> int:
    global _last_scan_at, _last_error, _signals_sent, _last_scan_report
    sent_now = 0
    _last_scan_report = {"ts": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    try:
        now = datetime.now(timezone.utc)

        # 1) Neue Setups einsammeln
        for sym in SYMBOLS:
            try:
                setup, notes = analyze_symbol(sym)
                _last_scan_report["symbols"][sym] = {"notes": notes}
                if isinstance(setup, dict):
                    key = f"{setup['symbol']}:{setup['direction']}"
                    keep = _pending.get(key)
                    if keep and now - keep["ts"] < timedelta(minutes=PENDING_TTL_MIN):
                        pass
                    else:
                        _pending[key] = {**setup, "ts": now}
                # RateLimit schonen
                time.sleep(ex.rateLimit/1000)
            except Exception as e:
                _last_scan_report["symbols"][sym] = {"error": str(e)}
                time.sleep(ex.rateLimit/1000)

        # 2) Pending: hat der Preis Safe-Entry berührt?
        to_delete = []
        for key, s in list(_pending.items()):
            if now - s["ts"] > timedelta(minutes=PENDING_TTL_MIN):
                to_delete.append(key)
                continue
            try:
                # nur die letzten 2 Kerzen abrufen
                _, o, h, l, c, v = fetch_ohlcv(s["symbol"], TF, limit=2)
            except Exception:
                continue
            last_low, last_high = l[-1], h[-1]
            hit = touched_safe(s["safe"], last_low, last_high, s["is_long"])
            if hit and cooldown_ready(key, now):
                await send_signal_safe_triggered(s)
                _last_sent_at[key] = now
                to_delete.append(key)
                _signals_sent += 1
                sent_now += 1
                _active[key] = {
                    "entry": s["safe"], "tp1": s["tp1"], "is_long": s["is_long"],
                    "display": s["display"], "direction": s["direction"],
                    "be_sent": False
                }
            time.sleep(ex.rateLimit/1000)

        # 3) Follow-up: TP1 erreicht? → „SL = BE“
        for key, pos in list(_active.items()):
            try:
                _, o, h, l, c, v = fetch_ohlcv(key.split(":")[0], TF, limit=2)
            except Exception:
                continue
            last_high, last_low = h[-1], l[-1]
            if not pos["be_sent"]:
                tp_hit = (last_high >= pos["tp1"]) if pos["is_long"] else (last_low <= pos["tp1"])
                if tp_hit:
                    await send_followup_be(pos["display"], pos["direction"])
                    pos["be_sent"] = True
            time.sleep(ex.rateLimit/1000)

        for k in to_delete:
            _pending.pop(k, None)

        _last_scan_at = datetime.now(timezone.utc)
        _last_error = None
    except Exception as e:
        _last_error = str(e)
    return sent_now

async def scan_loop():
    while True:
        await scan_once()
        await asyncio.sleep(SCAN_INTERVAL_S)

# ========= API =========
@app.on_event("startup")
async def on_start():
    asyncio.create_task(scan_loop())

@app.get("/")
async def root():
    return {
        "ok": True,
        "mode": "scanner_final3.0",
        "tf": TF, "filters": FILTER_TFS,
        "leverage": LEVERAGE,
        "tp_margin_pcts": TP_MARGIN_PCTS, "sl_margin_pct": SL_MARGIN_PCT,
        "safe_entry_pct": SAFE_ENTRY_PCT,
        "min_atr_pct": MIN_ATR_PCT,
        "vol_spike_factor": VOL_SPIKE_FACTOR,
        "danger_zone_pct": DANGER_ZONE_PCT,
        "min_prob": MIN_PROB,
        "pending": len(_pending),
        "active": len(_active),
        "last_scan_at": _last_scan_at.isoformat() if _last_scan_at else None,
        "signals_sent": _signals_sent,
        "last_error": _last_error,
    }

@app.get("/scan")
async def manual_scan():
    n = await scan_once()
    # kompaktes Resümee pro Symbol (letzte Note)
    compact = {s: (info["notes"][-1] if isinstance(info, dict) and info.get("notes") else "")
               for s, info in _last_scan_report.get("symbols", {}).items()}
    return {"ok": True, "sent_now": n, "pending": len(_pending), "active": len(_active),
            "last_error": _last_error, "summary": compact, "ts": _last_scan_report.get("ts")}

@app.get("/status")
async def status():
    return {"ok": True, "report": _last_scan_report}

@app.get("/test")
async def test():
    # Simulierter LONG auf BTC mit deinen Settings (nur Telegram-Test)
    entry = 25000.0
    safe  = make_safe_entry(entry, True)
    tp1, tp2, _ = calc_targets_from_price(entry, True)
    sl = calc_sl_from_price(entry, True)
    text = (
        f"✅ Test — Final 3.0 Format\n"
        f"➡️ *LONG* BTC/USDT ({TF})\n"
        f"🎯 Entry (Safe): `{fmt(safe)}`  (Pullback ≈ {SAFE_ENTRY_PCT}%)\n"
        f"🛡 SL: `{fmt(sl)}`  (≈ -{SL_MARGIN_PCT}% @20x)\n"
        f"🏁 TP1: `{fmt(tp1)}` (≈ +{TP_MARGIN_PCTS[0]}% @20x)\n"
        f"🏁 TP2: `{fmt(tp2)}` (≈ +{TP_MARGIN_PCTS[1]}% @20x)\n"
        f"📌 Follow-up: Bei TP1 → SL auf BE."
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
    return {"ok": True, "test": True}
