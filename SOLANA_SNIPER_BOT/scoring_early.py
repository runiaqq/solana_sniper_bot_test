from typing import Any, Dict, Tuple
from config import (
	GATE_MIN_MC_USD,
	GATE_MIN_LIQ_USD,
	GATE_MIN_PAIRS,
	GATE_MIN_BUY_RATIO,
	GATE_MIN_UNIQUE_BUYERS_5M,
	GATE_MAX_TOP5_HOLDERS_PCT,
)

SUSPICIOUS = ("rug", "scam", "test", "fake")


def contains_bad(text: str) -> bool:
	if not text:
		return False
	return any(b in text.lower() for b in SUSPICIOUS)


def gate1_onchain(onc: Dict[str, Any]) -> Tuple[bool, str]:
	mc = onc.get("market_cap_usd") or onc.get("fdv_usd")
	liq = onc.get("liquidity_usd")
	pairs = onc.get("pairs_count") or 0
	price = onc.get("price_usd")
	buy_ratio = onc.get("buy_ratio_10m") or onc.get("buy_ratio_5m")
	uniq = onc.get("unique_buyers_5m") or onc.get("unique_buyers_10m")

	if (mc is None or mc < GATE_MIN_MC_USD) and (price is None):
		return False, "no mc>min and no price"
	if (liq is not None and liq >= GATE_MIN_LIQ_USD and pairs >= GATE_MIN_PAIRS):
		pass
	else:
		vol5 = onc.get("volume_5m_raw")
		if not (vol5 and uniq and uniq >= GATE_MIN_UNIQUE_BUYERS_5M):
			return False, "no volume/uniq gate"
	if buy_ratio is not None and buy_ratio < GATE_MIN_BUY_RATIO:
		return False, "buy_ratio too low"
	return True, "ok"


def gate2_safety(safety: Dict[str, Any]) -> Tuple[bool, str, int]:
	# returns (ok, reason, penalty)
	penalty = 0
	freeze = safety.get("freeze_authority")
	mint_auth = safety.get("mint_authority")
	lp_lock = safety.get("lp_lock_pct")
	lp_lock_time_h = safety.get("lp_lock_time_h")
	if freeze not in (None, "renounced", "none"):
		penalty -= 10
	if mint_auth not in (None, "renounced", "none"):
		penalty -= 10
	if isinstance(lp_lock, (int, float)):
		if lp_lock >= 80:
			penalty += 8
		elif lp_lock < 50:
			penalty -= 10
	if isinstance(lp_lock_time_h, (int, float)) and lp_lock_time_h < 24:
		penalty -= 5
	ok = penalty > -20
	return ok, "ok" if ok else "suspicious", penalty


def score_multifactor(onc: Dict[str, Any], tw: Dict[str, Any], safety: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
	score = 0
	details: Dict[str, Any] = {"components": []}
	def add(pts: int, why: str):
		nonlocal score
		score += pts
		details["components"].append((pts, why))

	mc = onc.get("market_cap_usd") or onc.get("fdv_usd")
	if isinstance(mc, (int, float)) and mc > 7000:
		add(10, "mc>7k")
	liq = onc.get("liquidity_usd")
	if isinstance(liq, (int, float)):
		if 1000 <= liq <= 30000:
			add(8, "liq in [1k,30k]")
		elif liq > 30000:
			add(3, ">30k early")
	pairs = onc.get("pairs_count") or 0
	if pairs >= 2:
		add(10, "pairs>=2")
	elif pairs >= 1:
		add(6, "pairs>=1")
	if isinstance(onc.get("price_usd"), (int, float)) and onc["price_usd"] > 0:
		add(5, "has price")
		if onc["price_usd"] < 0.001:
			add(2, "meme pricing")
	buy_ratio = onc.get("buy_ratio_10m") or onc.get("buy_ratio_5m")
	if isinstance(buy_ratio, (int, float)):
		if buy_ratio >= 1.5:
			add(12, "buy/sell>=1.5")
		elif buy_ratio >= 1.2:
			add(8, "buy/sell>=1.2")
	uniq = onc.get("unique_buyers_5m") or onc.get("unique_buyers_10m")
	if isinstance(uniq, (int, float)):
		if uniq >= 20:
			add(12, "unique_buyers>=20")
		elif uniq >= 12:
			add(8, "unique_buyers>=12")
	holder_growth = onc.get("holder_growth_10m")
	if isinstance(holder_growth, (int, float)) and holder_growth >= 0.2:
		add(8, "holder_growth>=20%")
	top5 = onc.get("top5_holders_pct")
	if isinstance(top5, (int, float)):
		if top5 <= 35:
			add(10, "top5<=35%")
		elif top5 <= 50:
			add(6, "top5<=50%")
	pool_age_min = onc.get("pool_age_min")
	if isinstance(pool_age_min, (int, float)) and pool_age_min < 30:
		add(5, "pool<30m")

	# Safety
	ok2, _, penalty = gate2_safety(safety)
	add(penalty, "safety")

	# Twitter/meta
	followers = tw.get("followers")
	if isinstance(followers, (int, float)):
		if followers > 20000:
			add(14, ">20k followers")
		elif followers > 5000:
			add(10, ">5k followers")
		elif followers > 1000:
			add(6, ">1k followers")
	er = tw.get("er")
	if isinstance(er, (int, float)):
		if er >= 0.02:
			add(10, "ER>=2%")
		elif er >= 0.01:
			add(6, "ER>=1%")
	mentions = tw.get("mentions_count")
	uniq_auth = tw.get("mentions_unique_authors")
	infl = tw.get("influencers") or []
	if isinstance(mentions, (int, float)) and isinstance(uniq_auth, (int, float)):
		if mentions >= 20 and uniq_auth >= 10:
			add(8, "mentions/unique authors")
	if infl:
		add(6, "influencers present")
	sent = tw.get("sentiment")
	if sent == "positive":
		add(4, "pos sentiment")
	elif sent == "toxic":
		add(-6, "toxic sentiment")

	score = max(0, min(100, score))
	details["score"] = score
	return score, details