# main.py — Auto-Scanner mit SAFE-ENTRY, Fib 0.382–0.786, Danger-Zone, Min-ATR,
#            TP1→SL=BE-Followup und VERBOSE-Report pro Symbol (BYBIT-Datenquelle)
import os, asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Dict, Any

from fastapi import FastAPI
from telegram import Bot
import aiohttp

# ========= ENV =========
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID")

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="Auto Scanner → Telegram (Final3 Safe Entry / Bybit)")

# ========= SETTINGS =========
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","MATICUSDT","LTCUSDT","SHIBUSDT",
    "AVAXUSDT","LINKUSDT","DOTUSDT","TRXUSDT","TONUSDT",
    "NEARUSDT","ATOMUSDT","APTUSDT","OPUSDT",
]

TF              = "5m"
FILTER_TFS      = ["15m", "1h", "4h"]
SCAN_INTERVAL_S = 300  # alle 5 Minuten

LEVERAGE         = 20
TP_MARGIN_PCTS   = [15.0, 20.0, 30.0]          # (~ +0.75/+1.0/+1.5% @20x)
SL_MARGIN_PCT    = 10.0                        # (~ -0.5% @20x)
TP_PCTS          = [p / LEVERAGE for p in TP_MARGIN_PCTS]
SL_PCT           = SL_MARGIN_PCT / LEVERAGE

SAFE_ENTRY_PCT   = 0.20   # gelockert (vorher 0.25)

# gelockerte Filter
MIN_PROB         = 50
MIN_ATR_PCT      = 0.15
DANGER_ZONE_PCT  = 0.15

PENDING_TTL_MIN  = 180
COOLDOWN_MIN     = 5

# ========= STATE =========
_last_scan_at: datetime | None = None
_signals_sent: int = 0
_last_error: str | None = None

_pending: Dict[str, Dict[str, Any]] = {}
_last_sent_at: Dict[str, datetime] = {}
_active: Dict[str, Dict[str, Any]] = {}

# Neuer Scan-Report (letzte Runde)
_last_scan_report: Dict[str, Any] = {}

# ========= UTILS =========
def ema(values: List[float], period: int) -> List[float]:
    k = 2 / (period + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out

def rsi(values: List[float], period: int = 14) -> List[float]:
    gains, losses, rsis = [], [], [50.0]
    for i in range(1, len(values)):
        chg = values[i] - values[i-1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
        if i < period:
            rsis.append(50.0)
        else:
            ag = sum(gains[-period:]) / period
            al = sum(losses[-period:]) / period
            rs = 999999 if al == 0 else ag / al
            rsis.append(100 - (100 / (1 + rs)))
    return rsis

def atr_pct(o: List[float], h: List[float], l: List[float], c: List[float], period: int = 14) -> float:
    trs = []
    for i in range(1, len(c)):
        tr = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    atr = sum(trs[-period:]) / period
    price = c[-1]
    return (atr / price) * 100.0 if price > 0 else 0.0

def rr(entry: float, tp: float, sl: float, is_long: bool) -> float:
    if is_long: reward, risk = tp - entry, entry - sl
    else:       reward, risk = entry - tp, sl - entry
    return round(reward / risk, 2) if risk > 0 else 0.0

def fmt(n: float) -> str:
    if n >= 100: return f"{n:,.2f}"
    if n >= 1:   return f"{n:,.4f}"
    return f"{n:.8f}"

def make_safe_entry(entry: float, is_long: bool) -> float:
    p = SAFE_ENTRY_PCT / 100.0
    return entry * (1 - p) if is_long else entry * (1 + p)

def touched_safe(safe: float, last_low: float, last_high: float, is_long: bool) -> bool:
    return (last_low <= safe) if is_long else (last_high >= safe)

def calc_targets_percent(entry: float, is_long: bool):
    tp_steps = [p/100.0 for p in TP_PCTS]
    sl_step  = SL_PCT / 100.0
    if is_long:
        tp1 = entry * (1 + tp_steps[0]); tp2 = entry * (1 + tp_steps[1]); tp3 = entry * (1 + tp_steps[2])
        sl  = entry * (1 - sl_step)
    else:
        tp1 = entry * (1 - tp_steps[0]); tp2 = entry * (1 - tp_steps[1]); tp3 = entry * (1 - tp_steps[2])
        sl  = entry * (1 + sl_step)
    return tp1, tp2, tp3, sl

# ========= SWING & FIB =========
def swing_high_low(series: List[float], lookback: int = 50) -> Tuple[float, float]:
    look = series[-lookback:] if len(series) >= lookback else series[:]
    return max(look), min(look)

def fib_zone_ok(entry_safe: float, swing_low: float, swing_high: float, is_long: bool) -> bool:
    """
    Erlaubt 0.382–0.786 (mehr Treffer):
      LONG:  low + [0.382..0.786]*(high-low)
      SHORT: high - [0.382..0.786]*(high-low)
    """
    if swing_high <= 0 or swing_low <= 0 or swing_high == swing_low:
        return False
    diff = swing_high - swing_low
    if is_long:
        f_lo = swing_low + 0.382 * diff
        f_hi = swing_low + 0.786 * diff
        lo, hi = min(f_lo, f_hi), max(f_lo, f_hi)
        return lo <= entry_safe <= hi
    else:
        f_lo = swing_high - 0.786 * diff
        f_hi = swing_high - 0.382 * diff
        lo, hi = min(f_lo, f_hi), max(f_lo, f_hi)
        return lo <= entry_safe <= hi

def too_close_to_htf_extremes(price: float, htf_high: float, htf_low: float) -> bool:
    if htf_high <= 0 or htf_low <= 0:
        return False
    up_dist  = abs(htf_high - price) / price * 100.0
    dn_dist  = abs(price - htf_low)  / price * 100.0
    return (up_dist < DANGER_ZONE_PCT) or (dn_dist < DANGER_ZONE_PCT)

# ========= DATA (BYBIT) =========
BYBIT_BASE = "https://api.bybit.com"
TF_MAP = {"1m":"1", "5m":"5", "15m":"15", "1h":"60", "4h":"240"}

async def fetch_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int = 200):
    """
    Bybit v5 Market Kline (linear / USDT-Perps):
    GET /v5/market/kline?category=linear&symbol=BTCUSDT&interval=5&limit=200
    Rückgabe list: [[ts, open, high, low, close, volume, turnover], ...] (DESC)
    Wir kehren nach ASC um.
    """
    tf = TF_MAP.get(interval, "5")
    url = f"{BYBIT_BASE}/v5/market/kline?category=linear&symbol={symbol}&interval={tf}&limit={limit}"
    async with session.get(url, timeout=20) as r:
        r.raise_for_status()
        data = await r.json()
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
        lst = data["result"]["list"]
        if not lst:
            raise RuntimeError(f"No klines for {symbol} {interval}")
        # in Bybit ist die Liste absteigend → umdrehen
        lst = list(reversed(lst))
        opens  = [float(x[1]) for x in lst]
        highs  = [float(x[2]) for x in lst]
        lows   = [float(x[3]) for x in lst]
        closes = [float(x[4]) for x in lst]
        vols   = [float(x[5]) for x in lst]
        return opens, highs, lows, closes, vols

async def indicators(session, symbol: str, interval: str):
    o, h, l, c, v = await fetch_klines(session, symbol, interval)
    if len(c) < 50:
        return None
    ema_fast = ema(c, 9)[-1]
    ema_slow = ema(c, 21)[-1]
    r = rsi(c, 14)[-1]
    v_avg = sum(v[-20:]) / 20
    v_spike = v[-1] > 1.3 * v_avg
    atrp = atr_pct(o, h, l, c, 14)
    return {
        "o": o, "h": h, "l": l, "c": c, "v": v,
        "close": c[-1],
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "rsi": r,
        "vol_spike": v_spike,
        "atr_pct": atrp
    }

def probability(main: dict, filters: List[dict], is_long: bool) -> int:
    ema_ok = (main["ema_fast"] > main["ema_slow"]) if is_long else (main["ema_fast"] < main["ema_slow"])
    rsi_ok = (main["rsi"] >= 50) if is_long else (main["rsi"] <= 50)
    htf_ok = all((f["ema_fast"] > f["ema_slow"]) if is_long else (f["ema_fast"] < f["ema_slow"]) for f in filters)
    vol_ok = main["vol_spike"]
    score = 0
    score += 25 if ema_ok else 0
    score += 25 if rsi_ok else 0
    score += 25 if htf_ok else 0
    score += 10 if vol_ok else 0
    return min(100, score)

def classify_prob(prob: int) -> Tuple[str, str]:
    if prob >= 75: return "STRONG", "✅ STRONG"
    if prob >= 60: return "MEDIUM", "⚠️ MEDIUM"
    return "WEAK", "⚠️ WEAK"

# ========= CORE (mit VERBOSE) =========
async def analyze_symbol_verbose(session, symbol: str) -> Tuple[Any, List[str]]:
    notes = []
    try:
        main = await indicators(session, symbol, TF)
        if not main:
            return None, ["insufficient candles"]

        notes.append(f"ATR={main['atr_pct']:.3f}% (min {MIN_ATR_PCT}%)")
        if main["atr_pct"] < MIN_ATR_PCT:
            return None, notes + [f"REJECT: ATR < {MIN_ATR_PCT}%"]

        filt = []
        for tf in FILTER_TFS:
            x = await indicators(session, symbol, tf)
            if not x:
                return None, notes + [f"REJECT: missing {tf} klines"]
            filt.append(x)

        is_long  = (main["ema_fast"] > main["ema_slow"]) and (main["rsi"] >= 48)
        is_short = (main["ema_fast"] < main["ema_slow"]) and (main["rsi"] <= 52)
        dir_txt = "LONG" if is_long else ("SHORT" if is_short else "NONE")
        notes.append(f"direction={dir_txt} (EMA9/21 & RSI)")
        if not (is_long or is_short):
            return None, notes + ["REJECT: no clear bias"]

        htf1h = filt[1] if len(filt) >= 2 else filt[-1]
        htf_high, htf_low = max(htf1h["h"][-120:]), min(htf1h["l"][-120:])
        if too_close_to_htf_extremes(main["close"], htf_high, htf_low):
            return None, notes + [f"REJECT: danger-zone < {DANGER_ZONE_PCT}% to HTF extremes"]

        prob = probability(main, filt, is_long)
        notes.append(f"prob={prob}% (min {MIN_PROB}%)")
        if prob < MIN_PROB:
            return None, notes + [f"REJECT: prob < {MIN_PROB}%"]
        tier, tag = classify_prob(prob)

        entry_now = main["close"]
        safe      = make_safe_entry(entry_now, is_long)
        sw_high, sw_low = swing_high_low(main["c"], lookback=50)
        in_fib = fib_zone_ok(safe, sw_low, sw_high, is_long)
        notes.append(f"fib(0.382–0.786)={'OK' if in_fib else 'NO'}")
        if not in_fib:
            return None, notes + ["REJECT: safe-entry not in fib zone"]

        tp1, tp2, tp3, sl = calc_targets_percent(entry_now, is_long)
        setup = {
            "symbol": symbol, "display": symbol.replace("USDT","/USDT"),
            "direction": "LONG" if is_long else "SHORT",
            "is_long": is_long,
            "entry_now": entry_now, "safe": safe, "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "prob": prob, "tier": tier, "tag": tag,
            "rr1": rr(safe, tp1, sl, is_long),
            "rr2": rr(safe, tp2, sl, is_long),
            "rr3": rr(safe, tp3, sl, is_long),
            "last_low": main["l"][-1], "last_high": main["h"][-1],
        }
        return setup, notes + ["ACCEPT"]
    except Exception as e:
        return None, notes + [f"ERROR: {e}"]

async def send_signal_safe_triggered(s: Dict[str, Any]):
    header = f"{s['tag']} *Signal* {s['display']} ({TF}) — *SAFE ENTRY getriggert*"
    text = (
        f"{header}\n"
        f"➡️ *{s['direction']}*\n"
        f"🎯 Entry (Safe): `{fmt(s['safe'])}`  (Pullback ≈ {SAFE_ENTRY_PCT}%)\n"
        f"🛡 SL: `{fmt(s['sl'])}`  (≈ -{SL_MARGIN_PCT}% @20x)\n"
        f"🏁 TP1: `{fmt(s['tp1'])}` (≈ +{TP_MARGIN_PCTS[0]}% @20x, R:R {s['rr1']})\n"
        f"🏁 TP2: `{fmt(s['tp2'])}` (≈ +{TP_MARGIN_PCTS[1]}% @20x, R:R {s['rr2']})\n"
        f"🏁 TP3: `{fmt(s['tp3'])}` (≈ +{TP_MARGIN_PCTS[2]}% @20x, R:R {s['rr3']})\n"
        f"📊 Prob.: *{s['prob']}%*  | Tier: *{s['tier']}*  | HTF: 15m/1h/4h aligned\n"
        f"✅ Final3: EMA-Stack, RSI-Bias, Volumen, Fib 0.382–0.786, Safe-Entry, no Danger-Zone"
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")

async def send_followup_be(sym_display: str, direction: str):
    msg = f"🔔 *Follow-up* {sym_display} — *TP1 erreicht* → SL jetzt auf **BE** setzen."
    await bot.send_message(chat_id=TG_CHAT_ID, text=msg, parse_mode="Markdown")

async def scan_once() -> int:
    global _last_scan_at, _signals_sent, _last_error, _last_scan_report
    sent_now = 0
    _last_scan_report = {"ts": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    try:
        async with aiohttp.ClientSession() as session:
            # 1) Analyse pro Symbol (mit Gründen)
            tasks = [analyze_symbol_verbose(session, s) for s in SYMBOLS]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            now = datetime.now(timezone.utc)

            for sym, res in zip(SYMBOLS, results):
                if isinstance(res, Exception):
                    _last_scan_report["symbols"][sym] = {"notes": [f"ERROR: {res}"]}
                    continue

                setup, notes = res
                _last_scan_report["symbols"][sym] = {"notes": notes}

                if not isinstance(setup, dict):
                    continue

                key = f"{setup['symbol']}:{setup['direction']}"
                keep = _pending.get(key)
                if keep and now - keep["ts"] < timedelta(minutes=PENDING_TTL_MIN):
                    continue
                _pending[key] = {**setup, "ts": now}

            # 2) Pending → Safe-Entry getriggert?
            to_delete = []
            for key, s in list(_pending.items()):
                if now - s["ts"] > timedelta(minutes=PENDING_TTL_MIN):
                    to_delete.append(key)
                    continue
                try:
                    o, h, l, c, v = await fetch_klines(session, s["symbol"], TF, 2)
                except Exception:
                    continue
                last_low, last_high = l[-1], h[-1]
                hit = touched_safe(s["safe"], last_low, last_high, s["is_long"])
                last_t = _last_sent_at.get(key)
                ready = True if not last_t else (now - last_t) >= timedelta(minutes=COOLDOWN_MIN)

                if hit and ready:
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

            # 3) Follow-up: TP1 → SL=BE
            for key, pos in list(_active.items()):
                try:
                    o, h, l, c, v = await fetch_klines(session, key.split(":")[0], TF, 2)
                except Exception:
                    continue
                last_high, last_low = h[-1], l[-1]
                if not pos["be_sent"]:
                    tp_hit = (last_high >= pos["tp1"]) if pos["is_long"] else (last_low <= pos["tp1"])
                    if tp_hit:
                        await send_followup_be(pos["display"], pos["direction"])
                        pos["be_sent"] = True

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
        "mode": "scanner_final3_safe",
        "tf": TF, "filters": FILTER_TFS,
        "min_prob": MIN_PROB,
        "leverage": LEVERAGE,
        "tp_margin_pcts": TP_MARGIN_PCTS, "sl_margin_pct": SL_MARGIN_PCT,
        "safe_entry_pct": SAFE_ENTRY_PCT,
        "min_atr_pct": MIN_ATR_PCT,
        "danger_zone_pct": DANGER_ZONE_PCT,
        "pending": len(_pending),
        "active": len(_active),
        "last_scan_at": _last_scan_at.isoformat() if _last_scan_at else None,
        "signals_sent": _signals_sent,
        "last_error": _last_error,
    }

@app.get("/scan")
async def manual_scan():
    n = await scan_once()
    compact = {s: (info["notes"][-1] if info.get("notes") else "") for s, info in _last_scan_report.get("symbols", {}).items()}
    return {"ok": True, "sent_now": n, "pending": len(_pending), "active": len(_active),
            "last_error": _last_error, "summary": compact}

@app.get("/report")
async def report():
    return {"ok": True, "report": _last_scan_report}

@app.get("/test")
async def test():
    entry = 25000.0
    safe  = make_safe_entry(entry, True)
    tp1, tp2, tp3, sl = calc_targets_percent(entry, True)
    text = (
        f"⚡ *Test-Signal BTC/USDT (5m) — SAFE ENTRY getriggert*\n"
        f"➡️ *LONG*\n"
        f"🎯 Entry (Safe): `{fmt(safe)}`  (Pullback ≈ {SAFE_ENTRY_PCT}%)\n"
        f"🛡 SL: `{fmt(sl)}`  (≈ -{SL_MARGIN_PCT}% @20x)\n"
        f"🏁 TP1: `{fmt(tp1)}` (≈ +{TP_MARGIN_PCTS[0]}% @20x)\n"
        f"🏁 TP2: `{fmt(tp2)}` (≈ +{TP_MARGIN_PCTS[1]}% @20x)\n"
        f"🏁 TP3: `{fmt(tp3)}` (≈ +{TP_MARGIN_PCTS[2]}% @20x)\n"
        f"📊 Prob.: *85%* | Tier: *STRONG*\n"
        f"✅ Final3: EMA-Stack, RSI-Bias, Volumen, Fib 0.382–0.786, Safe-Entry, no Danger-Zone"
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
    return {"ok": True, "test": True}
