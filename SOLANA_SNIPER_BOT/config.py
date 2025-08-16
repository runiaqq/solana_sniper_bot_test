TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
ADMIN_CHAT_ID = 0  # int chat id; или отправьте /watch боту

# Scoring
MIN_SCORE = 60
MODE = "Normal"  # Aggressive | Normal | Conservative

# Helius
HELIUS_API_KEY = "YOUR_HELIUS_API_KEY"

# OpenAI (для AI-строки анализа)
OPENAI_API_KEY = "sk-proj-..."  # или sk-...

# Twitter API (опционально). Если пусто — twitter-обогащение отключено
TWITTER_BEARER_TOKEN = ""  # e.g. "AAAAAAAA..."
TWITTER_MAX_RPS = 10  # глобальный лимит запросов в минуту
TWITTER_PER_TOKEN_TTL_SEC = 45  # не чаще 1 запрос/45s на один mint
TWITTER_CACHE_TTL_SEC = 15 * 60  # 15 минут кэш

# Storage
SQLITE_PATH = "data/bot.db"

# Gates (мягкие значения — под задачу раннего обнаружения)
GATE_MIN_MC_USD = 7000
GATE_MIN_LIQ_USD = 1000
GATE_MIN_PAIRS = 1
GATE_MIN_BUY_RATIO = 1.1
GATE_MIN_UNIQUE_BUYERS_5M = 8
GATE_MAX_TOP5_HOLDERS_PCT = 60  # если доступно быстро

# Dynamic threshold window
DYNAMIC_WINDOW_RECENT = 300  # последние N токенов для вычисления порогов