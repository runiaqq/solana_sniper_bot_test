import requests
from config import TWITTER_BEARER_TOKEN

def search_token_mentions(token_symbol: str) -> int:
    url = f"https://api.twitter.com/2/tweets/search/recent?query=${token_symbol}&max_results=10"
    headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            tweets = r.json().get("meta", {}).get("result_count", 0)
            return tweets
    except Exception as e:
        print(f"Ошибка Twitter: {e}")
    return 0