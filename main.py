import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.filters import Command

BOT_TOKEN = "7811527010:AAFG987e-Eh5En99veWo2FmZm2vzZHRMYNs"  # замени на токен

dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)

@dp.message(Command("test"))
async def send_signal(message: Message):
    # Здесь позже — реальный fetch с API по контракту
    symbol = "MEN"
    contract = "3neBuVfPsd38xXrEybBkLCCLzFMjaj1ERSmoGWqPbonk"
    marketcap = "$₂₄₂K"  # placeholder
    liquidity = "$₅₅K"
    holders = "≈ 1 230"
    mentions = "45 за 1 ч"
    score = "7.8 / 10"

    text = (
        "<b>🚀 Новый токен найден!</b>\n\n"
        f"💡 <b>Symbol:</b> <code>{symbol}</code>\n"
        f"🧾 <b>Contract:</b> <code>{contract}</code>\n"
        f"📊 <b>Market Cap:</b> {marketcap}\n"
        f"🔒 <b>Liquidity:</b> {liquidity}\n"
        f"👥 <b>Holders:</b> {holders}\n"
        f"💬 <b>Twitter mentions:</b> {mentions}\n"
        f"⭐ <b>Score:</b> {score}\n\n"
        "🔗 <b>Ссылки:</b>\n"
        f"• <a href='https://dexscreener.com/solana/{contract}'>Dexscreener</a>\n"
        f"• <a href='https://matcha.xyz/tokens/solana/{contract}'>Matcha</a>\n"
        f"• <a href='https://twitter.com/search?q={symbol}'>Twitter</a>"
    )
    await message.answer(text)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())