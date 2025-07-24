import requests
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

BONKBOT_URL = "https://api.bonkbot.io/api/v1/tokenlist"  # публичный эндпоинт

def fetch_new_tokens(limit: int = 20) -> List[Dict]:
    try:
        resp = requests.get(BONKBOT_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Ошибка при получении токенов с bonkbot.io: {e}")
        return []

    tokens = data.get("tokens", [])
    tokens = tokens[:limit]

    new_tokens = []
    for t in tokens:
        token_info = {
            "mint": t.get("mintAddress"),
            "name": t.get("name"),
            "symbol": t.get("symbol"),
            "decimals": t.get("decimals"),
            "metadata_url": t.get("metadataUri"),
            "creator": t.get("creatorAddress"),
            "update_authority": None  # нет в bonkbot API
        }
        new_tokens.append(token_info)

    return new_tokens