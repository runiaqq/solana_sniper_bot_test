from aiogram import Bot
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

bot = Bot(token=TELEGRAM_BOT_TOKEN)

async def send_message(text: str):
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

def send_signal_to_telegram(text: str):
    import asyncio
    asyncio.run(send_message(text))