import os
from fastapi import FastAPI
from pydantic import BaseModel
from telegram import Bot

TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

if not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Missing TG_TOKEN or TG_CHAT_ID env vars. Set them in your hosting dashboard.")

bot = Bot(token=TG_TOKEN)
app = FastAPI(title="TradingView → Telegram Relay")

class Signal(BaseModel):
    symbol: str
    timeframe: str
    direction: str
    entry: float
    sl: float
    tp1: float
    tp2: float | None = None
    tp3: float | None = None
    probability: int | None = None
    checklist: list[str] = []
    comment: str | None = None

@app.get("/")
async def root():
    return {"ok": True, "info": "POST /hook with your TradingView alert JSON."}

@app.post("/hook")
async def hook(s: Signal):
    text = (
        f"⚡️ *Signal* {s.symbol} {s.timeframe}\n"
        f"➡️ *{s.direction}*\n"
        f"🎯 Entry: `{s.entry}`\n"
        f"🛡 SL: `{s.sl}`\n"
        f"🏁 TP1: `{s.tp1}`"
        + (f"\n🏁 TP2: `{s.tp2}`" if s.tp2 is not None else "")
        + (f"\n🏁 TP3: `{s.tp3}`" if s.tp3 is not None else "")
        + (f"\n📈 Prob.: *{s.probability}%*" if s.probability is not None else "")
        + (f"\n✅ {', '.join(s.checklist)}" if s.checklist else "")
        + (f"\n📝 {s.comment}" if s.comment else "")
    )
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
    return {"ok": True}
