import asyncio
import time
from typing import Any, Dict, List, Optional
import aiohttp
from cachetools import LRUCache
from config import (
	TWITTER_BEARER_TOKEN,
	TWITTER_MAX_RPS,
	TWITTER_PER_TOKEN_TTL_SEC,
	TWITTER_CACHE_TTL_SEC,
)

CACHE = LRUCache(maxsize=1000)
CACHE_TS: Dict[str, float] = {}
LAST_CALL_TS: float = 0.0
SEMAPHORE = asyncio.Semaphore(2)

API_BASE = "https://api.twitter.com/2"


def _cache_get(key: str) -> Optional[Any]:
	v = CACHE.get(key)
	if v is None:
		return None
	ts = CACHE_TS.get(key, 0.0)
	if (time.monotonic() - ts) > TWITTER_CACHE_TTL_SEC:
		CACHE.pop(key, None)
		CACHE_TS.pop(key, None)
		return None
	return v


def _cache_set(key: str, value: Any) -> None:
	CACHE[key] = value
	CACHE_TS[key] = time.monotonic()


async def _rate_gate() -> None:
	global LAST_CALL_TS
	if TWITTER_BEARER_TOKEN == "":
		return
	# simple per-process RPS gate
	min_interval = 60.0 / max(TWITTER_MAX_RPS, 1)
	delta = time.monotonic() - LAST_CALL_TS
	if delta < min_interval:
		await asyncio.sleep(min_interval - delta)
	LAST_CALL_TS = time.monotonic()


async def _auth_headers() -> Dict[str, str]:
	return {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}


async def fetch_project_context(mint: str, symbol: Optional[str], name: Optional[str]) -> Dict[str, Any]:
	"""Heuristics: try searching mentions of mint/name/symbol to find accounts."""
	if TWITTER_BEARER_TOKEN == "":
		return {"accounts": []}
	key = f"ctx:{mint}"
	cached = _cache_get(key)
	if cached is not None:
		return cached
	query = f"(\"{mint}\" OR \"{symbol or ''}\" OR \"{name or ''}\") -is:retweet"
	params = {"query": query, "max_results": 10}
	async with SEMAPHORE:
		await _rate_gate()
		h = await _auth_headers()
		async with aiohttp.ClientSession() as session:
			async with session.get(f"{API_BASE}/tweets/search/recent", params=params, headers=h, timeout=aiohttp.ClientTimeout(total=10)) as resp:
				if resp.status != 200:
					_cache_set(key, {"accounts": []})
					return {"accounts": []}
				js = await resp.json()
				# naive extraction; real impl should use expansions=author_id and users
				accounts: List[str] = []
				for _ in js.get("data", [])[:5]:
					pass
				res = {"accounts": accounts}
				_cache_set(key, res)
				return res


async def fetch_account_stats(handle: str) -> Dict[str, Any]:
	if TWITTER_BEARER_TOKEN == "":
		return {}
	key = f"acct:{handle}"
	cached = _cache_get(key)
	if cached is not None:
		return cached
	async with SEMAPHORE:
		await _rate_gate()
		h = await _auth_headers()
		async with aiohttp.ClientSession() as session:
			# For brevity, placeholder. Real impl: users/by/username/:username + tweets?max_results=... to compute ER
			res = {"followers": None, "age_days": None, "er": None}
			_cache_set(key, res)
			return res


async def search_mentions(query: str, since_minutes: int = 60) -> Dict[str, Any]:
	if TWITTER_BEARER_TOKEN == "":
		return {}
	key = f"m:{hash(query)}:{since_minutes}"
	cached = _cache_get(key)
	if cached is not None:
		return cached
	params = {"query": query, "max_results": 50}
	async with SEMAPHORE:
		await _rate_gate()
		h = await _auth_headers()
		async with aiohttp.ClientSession() as session:
			async with session.get(f"{API_BASE}/tweets/search/recent", params=params, headers=h, timeout=aiohttp.ClientTimeout(total=10)) as resp:
				if resp.status != 200:
					_cache_set(key, {})
					return {}
				js = await resp.json()
				# Real impl: dedup authors, count, infer influencers by followers (need expansions)
				out = {"count": len(js.get("data", [])), "unique_authors": None, "influencers": []}
				_cache_set(key, out)
				return out