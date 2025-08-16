import aiosqlite
import json
from typing import Any, Dict, Optional, List
from config import SQLITE_PATH

INIT_SQL = """
CREATE TABLE IF NOT EXISTS tokens (
	mint TEXT PRIMARY KEY,
	ts INTEGER,
	name TEXT,
	symbol TEXT,
	score INTEGER,
	sent INTEGER,
	mode TEXT,
	threshold_used INTEGER
);
CREATE TABLE IF NOT EXISTS features (
	mint TEXT PRIMARY KEY,
	onchain_json TEXT,
	twitter_json TEXT,
	safety_json TEXT,
	hype_json TEXT
);
CREATE TABLE IF NOT EXISTS score_history (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	mint TEXT,
	score INTEGER,
	ts INTEGER,
	details_json TEXT
);
CREATE TABLE IF NOT EXISTS settings (
	key TEXT PRIMARY KEY,
	value TEXT
);
CREATE TABLE IF NOT EXISTS twitter_cache (
	key TEXT PRIMARY KEY,
	value_json TEXT,
	ts INTEGER,
	ttl_sec INTEGER
);
"""

async def init_db() -> None:
	async with aiosqlite.connect(SQLITE_PATH) as db:
		await db.executescript(INIT_SQL)
		await db.commit()

async def save_features(mint: str, onchain: Dict[str, Any], twitter: Dict[str, Any], safety: Dict[str, Any], hype: Dict[str, Any]) -> None:
	async with aiosqlite.connect(SQLITE_PATH) as db:
		await db.execute(
			"REPLACE INTO features(mint,onchain_json,twitter_json,safety_json,hype_json) VALUES (?,?,?,?,?)",
			(mint, json.dumps(onchain), json.dumps(twitter), json.dumps(safety), json.dumps(hype)),
		)
		await db.commit()

async def save_token_row(mint: str, ts: int, name: Optional[str], symbol: Optional[str], score: int, sent: int, mode: str, threshold: int) -> None:
	async with aiosqlite.connect(SQLITE_PATH) as db:
		await db.execute(
			"REPLACE INTO tokens(mint,ts,name,symbol,score,sent,mode,threshold_used) VALUES (?,?,?,?,?,?,?,?)",
			(mint, ts, name or "", symbol or "", score, sent, mode, threshold),
		)
		await db.commit()

async def save_score_history(mint: str, ts: int, score: int, details: Dict[str, Any]) -> None:
	async with aiosqlite.connect(SQLITE_PATH) as db:
		await db.execute(
			"INSERT INTO score_history(mint,score,ts,details_json) VALUES (?,?,?,?)",
			(mint, score, ts, json.dumps(details)),
		)
		await db.commit()

async def get_recent_scores(limit: int) -> List[int]:
	async with aiosqlite.connect(SQLITE_PATH) as db:
		async with db.execute("SELECT score FROM score_history ORDER BY ts DESC LIMIT ?", (limit,)) as cur:
			rows = await cur.fetchall()
			return [int(r[0]) for r in rows if r and r[0] is not None]

async def get_setting(key: str) -> Optional[str]:
	async with aiosqlite.connect(SQLITE_PATH) as db:
		async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
			row = await cur.fetchone()
			return row[0] if row else None

async def set_setting(key: str, value: str) -> None:
	async with aiosqlite.connect(SQLITE_PATH) as db:
		await db.execute("REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
		await db.commit()