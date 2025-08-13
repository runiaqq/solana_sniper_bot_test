import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from config import TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
logging.basicConfig(
	format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
	level=logging.INFO,
)
logger = logging.getLogger("solana_sniper_bot")


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
CHECK_INTERVAL_SECONDS: int = 60
# Primary endpoint: pairs for Solana to focus on new listings in this chain.
DEXSCREENER_PAIRS_SOLANA_URL: str = "https://api.dexscreener.com/latest/dex/pairs/solana"
# Secondary endpoint (per task requirement). Used as a fallback if needed.
DEXSCREENER_TOKENS_URL: str = "https://api.dexscreener.com/latest/dex/tokens"

# Exclude common stable/bluechip symbols and wrapped variants
EXCLUDED_SYMBOLS: Set[str] = {
	"USDT", "USDC", "USD", "DAI", "TUSD", "USDD", "LUSD", "FRAX", "PYUSD", "USDE", "FDUSD", "USDP", "USDS",
	"SOL", "WSOL", "MSOL", "BSOL", "JITOSOL",
}


# ----------------------------------------------------------------------------
# In-memory runtime state
# ----------------------------------------------------------------------------
subscribed_chat_ids: Set[int] = set()
seen_token_addresses: Set[str] = set()


# ----------------------------------------------------------------------------
# Utilities
# ----------------------------------------------------------------------------

def abbreviate_usd(value: Optional[float]) -> str:
	if value is None:
		return "N/A"
	try:
		num = float(value)
	except Exception:
		return "N/A"
	abs_num = abs(num)
	if abs_num >= 1_000_000_000:
		return f"${num/1_000_000_000:.2f}B"
	if abs_num >= 1_000_000:
		return f"${num/1_000_000:.2f}M"
	if abs_num >= 1_000:
		return f"${num/1_000:.2f}K"
	return f"${num:.2f}"


def safe_get_nested(mapping: Dict[str, Any], *keys: str) -> Optional[Any]:
	cursor: Any = mapping
	for key in keys:
		if not isinstance(cursor, dict) or key not in cursor:
			return None
		cursor = cursor[key]
	return cursor


# ----------------------------------------------------------------------------
# Dexscreener fetching/parsing
# ----------------------------------------------------------------------------

async def fetch_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
	try:
		async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
			if resp.status != 200:
				logger.warning("Dexscreener returned non-200 status %s for %s", resp.status, url)
				return None
			return await resp.json()
	except Exception as exc:
		logger.exception("Failed to fetch %s: %s", url, exc)
		return None


def is_excluded_symbol(symbol: Optional[str]) -> bool:
	if not symbol:
		return True
	return symbol.upper() in EXCLUDED_SYMBOLS


def extract_pairs_from_tokens_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
	# Some Dexscreener endpoints return a top-level 'pairs'. If it's tokens-based, adapt as needed.
	pairs = payload.get("pairs")
	if isinstance(pairs, list):
		return pairs
	# Try 'tokens' list -> each may include bestPair or pairs
	tokens = payload.get("tokens")
	if isinstance(tokens, list):
		collected: List[Dict[str, Any]] = []
		for token in tokens:
			best_pair = token.get("bestPair")
			if isinstance(best_pair, dict):
				collected.append(best_pair)
			pairs_list = token.get("pairs")
			if isinstance(pairs_list, list):
				collected.extend([p for p in pairs_list if isinstance(p, dict)])
		return collected
	return []


async def get_latest_solana_pairs(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
	# Prefer Solana pairs endpoint to focus on new listings for this chain.
	primary = await fetch_json(session, DEXSCREENER_PAIRS_SOLANA_URL)
	pairs: List[Dict[str, Any]] = []
	if primary:
		pairs = primary.get("pairs") or []
		if isinstance(pairs, list):
			pairs = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"]
			return pairs

	# Fallback to tokens endpoint (per requirement), filter chainId == solana
	secondary = await fetch_json(session, DEXSCREENER_TOKENS_URL)
	if secondary:
		pairs = extract_pairs_from_tokens_payload(secondary)
		pairs = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"]
	return pairs


# ----------------------------------------------------------------------------
# Telegram bot handlers
# ----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	text = (
		"Привет! Я бот для отслеживания новых токенов в сети Solana.\n\n"
		"Команды:\n"
		"/watch — включить слежение\n"
		"/stop — выключить слежение\n\n"
		"Я проверяю Dexscreener каждые 60 секунд и присылаю новые токены без дубликатов."
	)
	await update.message.reply_text(text)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	chat_id = update.effective_chat.id
	subscribed_chat_ids.add(chat_id)
	await update.message.reply_text("Слежение включено. Буду присылать новые токены Solana.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	chat_id = update.effective_chat.id
	if chat_id in subscribed_chat_ids:
		subscribed_chat_ids.remove(chat_id)
		await update.message.reply_text("Слежение остановлено.")
	else:
		await update.message.reply_text("Слежение уже выключено.")


def build_markdown_message(pair: Dict[str, Any]) -> str:
	base = pair.get("baseToken", {}) or {}
	base_name = base.get("name") or "Unknown"
	base_symbol = base.get("symbol") or "?"
	base_address = base.get("address") or "?"

	fdv = pair.get("fdv")
	market_cap = pair.get("marketCap")  # may be absent; fall back to fdv
	volume_24h = safe_get_nested(pair, "volume", "h24")
	liq_usd = safe_get_nested(pair, "liquidity", "usd")
	if liq_usd is None:
		# some payloads may expose liquidity directly
		liq_usd = pair.get("liquidity") if isinstance(pair.get("liquidity"), (int, float)) else None

	url = pair.get("url") or "https://dexscreener.com/solana"

	mc_text = abbreviate_usd(market_cap if market_cap is not None else fdv)
	fdv_text = abbreviate_usd(fdv)
	vol_text = abbreviate_usd(volume_24h)
	liq_text = abbreviate_usd(liq_usd)

	lines: List[str] = []
	lines.append(f"*Новый токен Solana*")
	lines.append("")
	lines.append(f"Название: {base_name} ({base_symbol})")
	lines.append(f"FDV: {fdv_text}")
	lines.append(f"Маркеткап: {mc_text}")
	lines.append(f"Объём 24ч: {vol_text}")
	lines.append(f"Ликвидность: {liq_text}")
	lines.append(f"[Dexscreener]({url})")
	lines.append(f"CA: `{base_address}`")
	return "\n".join(lines)


async def scan_and_notify(context: ContextTypes.DEFAULT_TYPE) -> None:
	if not subscribed_chat_ids:
		return

	async with aiohttp.ClientSession() as session:
		pairs = await get_latest_solana_pairs(session)

	new_messages: List[str] = []
	for pair in pairs:
		if pair.get("chainId") != "solana":
			continue
		base = pair.get("baseToken") or {}
		base_symbol: Optional[str] = base.get("symbol")
		base_address: Optional[str] = base.get("address")
		if not base_address or is_excluded_symbol(base_symbol):
			continue

		if base_address in seen_token_addresses:
			continue
		seen_token_addresses.add(base_address)

		message = build_markdown_message(pair)
		new_messages.append(message)

	if not new_messages:
		return

	for chat_id in list(subscribed_chat_ids):
		for msg in new_messages:
			try:
				await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=False)
			except Exception:
				logger.exception("Failed to send message to chat %s", chat_id)


async def prefill_seen_tokens() -> None:
	async with aiohttp.ClientSession() as session:
		pairs = await get_latest_solana_pairs(session)
	addresses: Set[str] = set()
	for pair in pairs:
		if pair.get("chainId") != "solana":
			continue
		base = pair.get("baseToken") or {}
		addr = base.get("address")
		if isinstance(addr, str) and addr:
			addresses.add(addr)
	if addresses:
		seen_token_addresses.update(addresses)
		logger.info("Prefilled %d existing token addresses to avoid initial spam", len(addresses))


async def on_startup(app: Application) -> None:
	# Auto-subscribe ADMIN_CHAT_ID if provided (>0) so that bot starts reporting immediately after launch
	if isinstance(ADMIN_CHAT_ID, int) and ADMIN_CHAT_ID > 0:
		subscribed_chat_ids.add(ADMIN_CHAT_ID)
	await prefill_seen_tokens()
	logger.info("Subscribed chat IDs at startup: %s", subscribed_chat_ids)


def main() -> None:
	application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

	# Commands
	application.add_handler(CommandHandler("start", start))
	application.add_handler(CommandHandler("watch", watch))
	application.add_handler(CommandHandler("stop", stop))

	# Periodic job every 60s
	application.job_queue.run_repeating(scan_and_notify, interval=CHECK_INTERVAL_SECONDS, first=3)

	# Startup hook
	application.post_init = on_startup

	logger.info("Bot is starting polling...")
	application.run_polling(close_loop=False)


if __name__ == "__main__":
	main()