import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from datetime import datetime, timezone, timedelta
from config import TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, MIN_SCORE
from config import BITQUERY_TOKEN, OPENAI_API_KEY

# NEW IMPORTS
import json
import websockets
from openai import OpenAI


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
# GeckoTerminal public API for newly created pools on Solana (no API key required)
GECKO_BASE_URL: str = "https://api.geckoterminal.com/api/v2"
GECKO_NEW_POOLS_URL: str = f"{GECKO_BASE_URL}/networks/solana/new_pools"
# Token info endpoint
GECKO_TOKEN_URL_TPL: str = f"{GECKO_BASE_URL}/networks/solana/tokens/{{mint}}"

# Dexscreener token endpoint
DEXSCREENER_TOKEN_URL_TPL: str = "https://api.dexscreener.com/latest/dex/tokens/{mint}"

# Bitquery streaming
BITQUERY_WS_URL: str = "wss://streaming.bitquery.io/graphql"
# Pump.fun create instruction subscription (new token signal)
BITQUERY_PUMPFUN_SUB: str = (
	"subscription {\n"
	"  Solana {\n"
	"    TokenSupplyUpdates(\n"
	"      where: {Instruction: {Program: {Address: {is: \"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P\"}, Method: {is: \"create\"}}}}\n"
	"    ) {\n"
	"      Block { Time }\n"
	"      Transaction { Signer }\n"
	"      TokenSupplyUpdate {\n"
	"        Currency {\n"
	"          Symbol\n"
	"          Name\n"
	"          MintAddress\n"
	"          Uri\n"
	"        }\n"
	"      }\n"
	"    }\n"
	"  }\n"
	"}\n"
)

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

# New runtime queues/sets
new_token_events_queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
ws_reconnect_backoff_seconds: float = 3.0


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


def first_present(mapping: Dict[str, Any], *keys: str) -> Optional[Any]:
	for k in keys:
		if k in mapping and mapping[k] is not None:
			return mapping[k]
	return None


def contains_suspicious(text: Optional[str]) -> bool:
	if not text:
		return False
	lowered = str(text).lower()
	for bad in ("rug", "scam", "test", "fake"):
		if bad in lowered:
			return True
	return False


def _parse_created_at(value: Any) -> Optional[datetime]:
	try:
		if value is None:
			return None
		if isinstance(value, (int, float)):
			return datetime.fromtimestamp(float(value), tz=timezone.utc)
		if isinstance(value, str):
			text = value.replace("Z", "+00:00")
			return datetime.fromisoformat(text)
	except Exception:
		return None
	return None


def _extract_metrics(attrs: Dict[str, Any]) -> Dict[str, Optional[float]]:
	liquidity = first_present(attrs, "liquidity_usd", "reserve_in_usd", "reserve_usd", "liquidity")
	vol = None
	vol_usd_obj = attrs.get("volume_usd")
	if isinstance(vol_usd_obj, dict):
		vol = first_present(vol_usd_obj, "h24", "h_24", "24h")
	elif isinstance(attrs.get("volume_usd_24h"), (int, float)):
		vol = attrs.get("volume_usd_24h")
	mcap = first_present(attrs, "market_cap_usd", "market_cap", "fdv_usd", "fdv")
	price = first_present(attrs, "price_usd", "base_token_price_usd")
	pairs_count = attrs.get("pairs_count") or attrs.get("pools_count") or attrs.get("pool_count")
	created_at = attrs.get("pool_created_at") or attrs.get("created_at")
	return {
		"liquidity": float(liquidity) if isinstance(liquidity, (int, float)) else None,
		"volume_24h": float(vol) if isinstance(vol, (int, float)) else None,
		"market_cap": float(mcap) if isinstance(mcap, (int, float)) else None,
		"price_usd": float(price) if isinstance(price, (int, float)) else None,
		"pairs_count": int(pairs_count) if isinstance(pairs_count, (int, float)) else None,
		"created_at_ts": _parse_created_at(created_at),
	}


def calculate_score(token_data: Dict[str, Any]) -> int:
	attrs = token_data.get("attributes") or {}
	m = _extract_metrics(attrs)
	score = 0
	if m["liquidity"] is not None and m["liquidity"] > 10_000:
		score += 20
	if m["volume_24h"] is not None and m["volume_24h"] > 5_000:
		score += 20
	if m["market_cap"] is not None and m["market_cap"] > 50_000:
		score += 20
	if m["price_usd"] is not None and m["price_usd"] > 0.0001:
		score += 10
	if isinstance(m["pairs_count"], int) and m["pairs_count"] > 1:
		score += 10
	created_at = m["created_at_ts"]
	if isinstance(created_at, datetime):
		now = datetime.now(timezone.utc)
		if (now - created_at) < timedelta(hours=24):
			score += 20
	return max(0, min(100, score))


def is_token_valid(token_data: Dict[str, Any]) -> bool:
	base = token_data.get("baseToken") or {}
	name = base.get("name")
	symbol = base.get("symbol")
	if contains_suspicious(name) or contains_suspicious(symbol):
		return False
	attrs = token_data.get("attributes") or {}
	m = _extract_metrics(attrs)
	# required numeric fields must exist and price > 0
	required_present = all([
		m["liquidity"] is not None,
		m["volume_24h"] is not None,
		m["market_cap"] is not None,
		m["price_usd"] is not None,
	])
	if not required_present:
		return False
	if m["price_usd"] is not None and m["price_usd"] <= 0:
		return False
	return True


# ----------------------------------------------------------------------------
# GeckoTerminal fetching/parsing
# ----------------------------------------------------------------------------

async def fetch_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
	try:
		async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
			if resp.status != 200:
				logger.warning("HTTP %s for %s", resp.status, url)
				return None
			return await resp.json()
	except Exception as exc:
		logger.exception("Failed to fetch %s: %s", url, exc)
		return None


async def http_get_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, retries: int = 3, delay: float = 1.0) -> Optional[Dict[str, Any]]:
	for attempt in range(1, retries + 1):
		try:
			async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
				if resp.status != 200:
					logger.warning("GET %s -> %s", url, resp.status)
					text = await resp.text()
					logger.debug("Response text: %s", text[:500])
					await asyncio.sleep(delay)
					continue
				return await resp.json()
		except Exception as exc:
			logger.exception("GET failed %s (attempt %d/%d): %s", url, attempt, retries, exc)
			await asyncio.sleep(delay)
	return None


async def fetch_dexscreener_by_mint(session: aiohttp.ClientSession, mint: str) -> Dict[str, Any]:
	url = DEXSCREENER_TOKEN_URL_TPL.format(mint=mint)
	data = await http_get_json(session, url)
	result: Dict[str, Any] = {"liquidity_usd": None, "volume_24h_usd": None, "market_cap_usd": None, "pairs_count": None, "price_usd": None}
	if not data:
		return result
	pairs = data.get("pairs") or []
	if not isinstance(pairs, list) or not pairs:
		return result
	# Aggregate metrics across pairs
	liq = 0.0
	vol = 0.0
	mc = 0.0
	price = None
	for p in pairs:
		try:
			l = p.get("liquidity") or {}
			liq += float(l.get("usd") or 0)
			v = p.get("volume") or {}
			vol += float(v.get("h24") or 0)
			mc = max(mc, float(p.get("fdv") or p.get("marketCap") or 0))
			if price is None and p.get("priceUsd") is not None:
				price = float(p.get("priceUsd"))
		except Exception:
			continue
	result["liquidity_usd"] = liq if liq > 0 else None
	result["volume_24h_usd"] = vol if vol > 0 else None
	result["market_cap_usd"] = mc if mc > 0 else None
	result["pairs_count"] = len(pairs)
	result["price_usd"] = price
	return result


async def fetch_gecko_token(session: aiohttp.ClientSession, mint: str) -> Dict[str, Any]:
	url = GECKO_TOKEN_URL_TPL.format(mint=mint)
	params = {"include": "pools"}
	data = await http_get_json(session, url, params=params)
	result: Dict[str, Any] = {"pool_created_at": None, "holders": None, "liquidity_usd": None, "volume_24h_usd": None, "price_usd": None}
	if not data:
		return result
	data_list = data.get("data")
	if isinstance(data_list, list) and data_list:
		attrs = data_list[0].get("attributes", {}) or {}
		# Try top-level attributes first
		result["price_usd"] = attrs.get("price_usd") or attrs.get("base_token_price_usd")
		vol = attrs.get("volume_usd") or {}
		if isinstance(vol, dict):
			result["volume_24h_usd"] = vol.get("h24") or vol.get("24h")
		liq = attrs.get("liquidity_usd") or attrs.get("reserve_in_usd")
		result["liquidity_usd"] = liq
		result["holders"] = attrs.get("holders") or attrs.get("holders_count")
		result["pool_created_at"] = attrs.get("pool_created_at") or attrs.get("created_at")
	return result


def is_excluded_symbol(symbol: Optional[str]) -> bool:
	if not symbol:
		return True
	return symbol.upper() in EXCLUDED_SYMBOLS


def _resolve_token(included: List[Dict[str, Any]], rel: Dict[str, Any]) -> Optional[Dict[str, Any]]:
	if not isinstance(rel, dict):
		return None
	rel_data = rel.get("data")
	if not isinstance(rel_data, dict):
		return None
	rel_id = rel_data.get("id")
	if not rel_id:
		return None
	for item in included or []:
		if item.get("id") == rel_id:
			attrs = item.get("attributes", {}) or {}
			return {
				"address": attrs.get("address") or attrs.get("id") or item.get("id"),
				"symbol": attrs.get("symbol"),
				"name": attrs.get("name"),
			}
	return None


def _normalize_gecko_pool(pool: Dict[str, Any], included: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
	attrs = pool.get("attributes", {}) or {}
	rels = pool.get("relationships", {}) or {}
	base_tok = _resolve_token(included, rels.get("base_token"))
	quote_tok = _resolve_token(included, rels.get("quote_token"))
	candidate = None
	# Prefer non-stable token as the base for alerts
	if base_tok and not is_excluded_symbol(base_tok.get("symbol")):
		candidate = base_tok
	elif quote_tok and not is_excluded_symbol(quote_tok.get("symbol")):
		candidate = quote_tok
	else:
		return None

	fdv = first_present(attrs, "fdv_usd", "fdv", "fully_diluted_valuation_usd")
	market_cap = first_present(attrs, "market_cap_usd", "market_cap")
	volume_24h = None
	vol_usd_obj = attrs.get("volume_usd")
	if isinstance(vol_usd_obj, dict):
		volume_24h = first_present(vol_usd_obj, "h24", "h_24", "24h")
	elif isinstance(attrs.get("volume_usd_24h"), (int, float)):
		volume_24h = attrs.get("volume_usd_24h")

	liquidity = first_present(attrs, "reserve_in_usd", "liquidity_usd", "reserve_usd")

	pool_address = attrs.get("address") or pool.get("id")
	gecko_url = f"https://www.geckoterminal.com/solana/pools/{pool_address}" if pool_address else "https://www.geckoterminal.com/solana"
	# Also provide a Dexscreener token page link via token mint address (works as a generic token view)
	dex_url = f"https://dexscreener.com/solana/{candidate.get('address')}" if candidate.get("address") else gecko_url

	return {
		"chainId": "solana",
		"baseToken": {
			"name": candidate.get("name") or "Unknown",
			"symbol": candidate.get("symbol") or "?",
			"address": candidate.get("address") or "?",
		},
		"fdv": fdv,
		"marketCap": market_cap,
		"volume": {"h24": volume_24h},
		"liquidity": {"usd": liquidity},
		"url": dex_url,
		"geckoUrl": gecko_url,
		"attributes": attrs,
	}


async def get_latest_solana_pairs(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
	# GeckoTerminal: request recent new pools and map them to a Dexscreener-like structure
	params = {"include": "base_token,quote_token", "page": 1}
	payload = await fetch_json(session, GECKO_NEW_POOLS_URL, params=params)
	if not payload:
		return []
	data = payload.get("data") or []
	included = payload.get("included") or []
	normalized: List[Dict[str, Any]] = []
	for pool in data:
		if not isinstance(pool, dict):
			continue
		item = _normalize_gecko_pool(pool, included)
		if item:
			normalized.append(item)
	return normalized


# ----------------------------------------------------------------------------
# Telegram bot handlers
# ----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	text = (
		"Привет! Я бот для отслеживания новых токенов в сети Solana.\n\n"
		"Команды:\n"
		"/watch — включить слежение\n"
		"/stop — выключить слежение\n\n"
		"Я проверяю новые пулы на GeckoTerminal каждые 60 секунд и присылаю новые токены без дубликатов."
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


def build_markdown_message(pair: Dict[str, Any], score: int) -> str:
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
	gecko_url = pair.get("geckoUrl") or "https://www.geckoterminal.com/solana"

	mc_text = abbreviate_usd(market_cap if market_cap is not None else fdv)
	fdv_text = abbreviate_usd(fdv)
	vol_text = abbreviate_usd(volume_24h)
	liq_text = abbreviate_usd(liq_usd)

	lines: List[str] = []
	lines.append(f"🔥 New Token Alert (Score: {score}/100)")
	lines.append(f"*Новый токен Solana*")
	lines.append("")
	lines.append(f"Название: {base_name} ({base_symbol})")
	lines.append(f"FDV: {fdv_text}")
	lines.append(f"Маркеткап: {mc_text}")
	lines.append(f"Объём 24ч: {vol_text}")
	lines.append(f"Ликвидность: {liq_text}")
	lines.append(f"[GeckoTerminal]({gecko_url}) | [Dexscreener]({url})")
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

		# Extended validation and scoring
		if not is_token_valid(pair):
			continue
		score = calculate_score(pair)
		if score < int(MIN_SCORE):
			continue

		if base_address in seen_token_addresses:
			continue
		seen_token_addresses.add(base_address)

		message = build_markdown_message(pair, score)
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
	# Auto-subscribe ADMIN_CHAT_ID if provided (>0)
	if isinstance(ADMIN_CHAT_ID, int) and ADMIN_CHAT_ID > 0:
		subscribed_chat_ids.add(ADMIN_CHAT_ID)
	await prefill_seen_tokens()
	logger.info("Subscribed chat IDs at startup: %s", subscribed_chat_ids)
	# Launch background consumers
	asyncio.create_task(bitquery_ws_consumer())
	# Create a simple context proxy for worker
	class _Ctx:
		def __init__(self, bot):
			self.bot = bot
	context_proxy = _Ctx(app.bot)
	asyncio.create_task(process_new_tokens_worker(context_proxy))


async def bitquery_ws_consumer() -> None:
	global ws_reconnect_backoff_seconds
	headers = {
		"Authorization": f"Bearer {BITQUERY_TOKEN}",
		"Content-Type": "application/json",
	}
	payload = json.dumps({"type": "connection_init", "payload": {}})

	while True:
		try:
			logger.info("Connecting to Bitquery WS...")
			async with websockets.connect(BITQUERY_WS_URL, extra_headers=headers, ping_interval=20, ping_timeout=20) as ws:
				# init connection (Apollo protocol)
				await ws.send(payload)
				# start subscription
				sub_msg = json.dumps({
					"id": "pumpfun_create",
					"type": "start",
					"payload": {"query": BITQUERY_PUMPFUN_SUB},
				})
				await ws.send(sub_msg)
				logger.info("Bitquery WS subscribed to Pump.fun create")

				async for raw in ws:
					try:
						msg = json.loads(raw)
						if msg.get("type") not in ("data",):
							continue
						payload_data = (((msg.get("payload") or {}).get("data") or {}).get("Solana") or {})
						updates = payload_data.get("TokenSupplyUpdates") or []
						for upd in updates:
							currency = (((upd.get("TokenSupplyUpdate") or {}).get("Currency")) or {})
							mint = currency.get("MintAddress")
							symbol = currency.get("Symbol")
							name = currency.get("Name")
							if not mint:
								continue
							if is_excluded_symbol(symbol):
								continue
							if mint in seen_token_addresses:
								continue
							evt = {"mint": mint, "symbol": symbol, "name": name}
							await new_token_events_queue.put(evt)
							logger.info("Enqueued new token from Bitquery: %s %s", symbol, mint)
					except Exception:
						logger.exception("Failed to parse Bitquery message")
			ws_reconnect_backoff_seconds = 3.0
		except Exception:
			logger.exception("Bitquery WS disconnected; retrying in %.1fs", ws_reconnect_backoff_seconds)
			await asyncio.sleep(ws_reconnect_backoff_seconds)
			ws_reconnect_backoff_seconds = min(ws_reconnect_backoff_seconds * 2, 60.0)


# AI START
_openai_client: Optional[OpenAI] = None

def get_openai_client() -> OpenAI:
	global _openai_client
	if _openai_client is None:
		_openai_client = OpenAI(api_key=OPENAI_API_KEY)
	return _openai_client


async def ai_short_analysis(score: int, metrics: Dict[str, Any]) -> str:
	try:
		client = get_openai_client()
		prompt = (
			"Сформулируй короткое (1–2 предложения, до 150 символов) заключение по токену на основе метрик и score. "
			"Не используй технические детали. Используй эмодзи 🟢🟡🔴 по уместности.\n"
			f"Score: {score}. Метрики: {json.dumps(metrics, ensure_ascii=False)[:800]}"
		)
		# Use the Chat Completions API of the new OpenAI SDK
		resp = await asyncio.get_event_loop().run_in_executor(
			None,
			lambda: client.chat.completions.create(
				model="gpt-4o-mini",
				messages=[{"role": "user", "content": prompt}],
				max_tokens=60,
				temperature=0.3,
			),
		)
		text = (resp.choices[0].message.content or "").strip()
		if len(text) > 150:
			text = text[:147] + "..."
		return text
	except Exception:
		logger.exception("OpenAI analysis failed")
		# Fallback by score
		if score >= 80:
			return "🟢 Перспективный токен: отличная ликвидность и активные торги."
		if score >= 60:
			return "🟡 Средний потенциал, стоит следить за объёмом."
		return "🔴 Высокий риск: слабые метрики."
# AI END


# SCORE START

def compute_score_from_metrics(metrics: Dict[str, Any]) -> int:
	score = 0
	liq = metrics.get("liquidity_usd")
	vol = metrics.get("volume_24h_usd")
	mcap = metrics.get("market_cap_usd")
	price = metrics.get("price_usd")
	pairs = metrics.get("pairs_count")
	created_at = metrics.get("pool_created_at_dt")
	if isinstance(liq, (int, float)) and liq > 10_000:
		score += 20
	if isinstance(vol, (int, float)) and vol > 5_000:
		score += 20
	if isinstance(mcap, (int, float)) and mcap > 50_000:
		score += 20
	if isinstance(price, (int, float)) and price > 0.0001:
		score += 10
	if isinstance(pairs, int) and pairs > 1:
		score += 10
	if isinstance(created_at, datetime):
		now = datetime.now(timezone.utc)
		if (now - created_at) < timedelta(hours=24):
			score += 20
	return max(0, min(100, score))
# SCORE END


async def process_new_tokens_worker(context: ContextTypes.DEFAULT_TYPE) -> None:
	async with aiohttp.ClientSession() as session:
		while True:
			evt = await new_token_events_queue.get()
			mint = evt.get("mint")
			symbol = evt.get("symbol")
			name = evt.get("name")
			if not mint:
				continue
			try:
				logger.info("Processing mint %s (%s) from queue", mint, symbol)
				# Fetch metrics from Dexscreener and Gecko
				ds = await fetch_dexscreener_by_mint(session, mint)
				gt = await fetch_gecko_token(session, mint)

				# Merge metrics
				metrics: Dict[str, Any] = {
					"symbol": symbol,
					"name": name,
					"mint": mint,
					"liquidity_usd": None,
					"volume_24h_usd": None,
					"market_cap_usd": None,
					"pairs_count": None,
					"price_usd": None,
					"pool_created_at": gt.get("pool_created_at"),
					"holders": gt.get("holders"),
					"gecko_url": f"https://www.geckoterminal.com/solana/tokens/{mint}",
					"dexs_url": f"https://dexscreener.com/solana/{mint}",
				}
				# Prefer Dexscreener for liquidity/volume/marketcap/pairs/price
				metrics["liquidity_usd"] = ds.get("liquidity_usd") if ds.get("liquidity_usd") is not None else gt.get("liquidity_usd")
				metrics["volume_24h_usd"] = ds.get("volume_24h_usd") if ds.get("volume_24h_usd") is not None else gt.get("volume_24h_usd")
				metrics["market_cap_usd"] = ds.get("market_cap_usd")
				metrics["pairs_count"] = ds.get("pairs_count")
				metrics["price_usd"] = ds.get("price_usd") if ds.get("price_usd") is not None else gt.get("price_usd")

				# Parse created_at
				created_at_dt = _parse_created_at(metrics.get("pool_created_at"))
				metrics["pool_created_at_dt"] = created_at_dt

				# Filtering rules
				if metrics["liquidity_usd"] is None or metrics["volume_24h_usd"] is None or metrics["price_usd"] is None:
					logger.info("Filtered out due to missing core metrics: %s", mint)
					continue
				if isinstance(metrics["price_usd"], (int, float)) and metrics["price_usd"] <= 0:
					logger.info("Filtered out due to zero price: %s", mint)
					continue
				if contains_suspicious(symbol) or contains_suspicious(name):
					logger.info("Filtered out due to suspicious name/symbol: %s %s", name, symbol)
					continue

				# Compute score
				score = compute_score_from_metrics(metrics)
				logger.info("Score for %s: %d", mint, score)
				if score < int(MIN_SCORE):
					logger.info("Filtered out due to low score (<%s): %s", MIN_SCORE, mint)
					continue

				# AI analysis
				ai_text = await ai_short_analysis(score, metrics)

				# Build and send message
				price_text = abbreviate_usd(metrics["price_usd"]) if metrics["price_usd"] is not None else "N/A"
				liq_text = abbreviate_usd(metrics["liquidity_usd"]) if metrics["liquidity_usd"] is not None else "N/A"
				vol_text = abbreviate_usd(metrics["volume_24h_usd"]) if metrics["volume_24h_usd"] is not None else "N/A"
				mc_text = abbreviate_usd(metrics["market_cap_usd"]) if metrics["market_cap_usd"] is not None else "N/A"

				lines: List[str] = []
				lines.append(f"🔥 New Token Alert (Score: {score}/100)")
				lines.append(f"{name or 'Unknown'} ({symbol or '?'})")
				lines.append(f"Цена: {price_text}")
				lines.append(f"Маркеткап: {mc_text}")
				lines.append(f"Ликвидность: {liq_text}")
				lines.append(f"Объём 24ч: {vol_text}")
				lines.append(f"AI Analysis: {ai_text}")
				lines.append(f"[Dexscreener]({metrics['dexs_url']})")
				lines.append(f"[Geckoterminal]({metrics['gecko_url']})")
				lines.append(f"Contract: `{mint}`")
				msg = "\n".join(lines)

				# Send to subscribers
				for chat_id in list(subscribed_chat_ids):
					try:
						await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=False)
						logger.info("Message sent to %s for %s", chat_id, mint)
					except Exception:
						logger.exception("Failed sending message to %s", chat_id)

				# Mark as seen to avoid duplicates
				seen_token_addresses.add(mint)
			except Exception:
				logger.exception("Failed to process event for %s", mint)


def main() -> None:
	application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

	# Commands
	application.add_handler(CommandHandler("start", start))
	application.add_handler(CommandHandler("watch", watch))
	application.add_handler(CommandHandler("stop", stop))

	# Startup hook
	application.post_init = on_startup

	logger.info("Bot is starting polling...")
	application.run_polling(close_loop=False)


if __name__ == "__main__":
	main()