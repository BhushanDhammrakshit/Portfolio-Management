import os
from dotenv import load_dotenv

# Load .env variables
load_dotenv()

AZURE_TABLE_CONN_STR = os.getenv("AZURE_TABLE_CONN_STR")
USER_INFO_TABLE = os.getenv("USER_INFO_TABLE")
USER_STOCKS_TABLE = os.getenv("USER_STOCKS_TABLE")
USER_MF_TABLE = os.getenv("USER_MF_TABLE", "UserMutualFunds")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ENDPOINT = os.getenv("OPENAI_ENDPOINT")

# ── RAG / embeddings ──────────────────────────────────────────────────
# Full URL of the Azure OpenAI embeddings deployment, e.g.
#   https://<resource>.openai.azure.com/openai/deployments/text-embedding-3-small/embeddings?api-version=2024-02-15-preview
# When unset, RAG falls back to keyword-only retrieval (still useful, no AI calls).
OPENAI_EMBED_ENDPOINT = os.getenv("OPENAI_EMBED_ENDPOINT", "")
RAG_DOCS_TABLE = os.getenv("RAG_DOCS_TABLE", "RagDocs")
RAG_EMBED_TABLE = os.getenv("RAG_EMBED_TABLE", "RagEmbed")
RAG_META_TABLE = os.getenv("RAG_META_TABLE", "RagMeta")
RAG_RETENTION_DAYS = int(os.getenv("RAG_RETENTION_DAYS", "90"))
RAG_INGEST_HOUR_IST = int(os.getenv("RAG_INGEST_HOUR_IST", "7"))
RAG_ENABLE_SCHEDULER = (os.getenv("RAG_ENABLE_SCHEDULER", "1") == "1")

# ── Email verification ────────────────────────────────────────────────
# Provider: 'brevo' (production) | 'console' (dev — prints OTP to terminal)
EMAIL_PROVIDER = (os.getenv("EMAIL_PROVIDER") or "console").lower()
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "HM2 Portfolio Manager")
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "")
APP_NAME = os.getenv("APP_NAME", "HM2 Portfolio Manager")
EMAIL_VERIFICATION_TABLE = os.getenv("EMAIL_VERIFICATION_TABLE", "EmailVerifications")
OTP_TTL_MINUTES = int(os.getenv("OTP_TTL_MINUTES", "10"))
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_COOLDOWN_SECONDS = int(os.getenv("OTP_RESEND_COOLDOWN_SECONDS", "60"))

# ── Market data provider ──────────────────────────────────────────────
# Primary provider for prices / history / quotes.
#   "fyers"    → Fyers API v3 (requires FYERS_APP_ID + FYERS_ACCESS_TOKEN)
#   "dhan"     → DhanHQ v2 REST API (requires DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN
#                AND a paid Data APIs subscription on the Dhan account)
#   "yfinance" → Yahoo Finance via yfinance package (unlicensed, dev only)
MARKET_DATA_PROVIDER = (os.getenv("MARKET_DATA_PROVIDER") or "yfinance").lower()
# When the primary provider fails or doesn't support a call (e.g. search,
# company info), fall back to this provider. Set to "" / "none" to disable.
MARKET_DATA_FALLBACK = (os.getenv("MARKET_DATA_FALLBACK") or "yfinance").lower()

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")

FYERS_APP_ID = os.getenv("FYERS_APP_ID", "")
FYERS_ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN", "")
FYERS_SECRET_KEY = os.getenv("FYERS_SECRET_KEY", "")

# ── Redis / shared cache ──────────────────────────────────────────────
# When REDIS_URL is unset the app falls back to in-process caches (the
# behaviour we had before). For prod with >1 gunicorn worker or >1 dyno,
# set REDIS_URL to enable a shared cache + per-user precompute store.
#   Heroku:   provision heroku-redis, REDIS_URL is set automatically.
#   Azure:    rediss://:<key>@<name>.redis.cache.windows.net:6380/0
#   Upstash:  rediss://default:<token>@<host>:<port>
REDIS_URL = os.getenv("REDIS_URL", "")
CACHE_KEY_PREFIX = os.getenv("CACHE_KEY_PREFIX", "hm2:")
CACHE_DEFAULT_TIMEOUT = int(os.getenv("CACHE_DEFAULT_TIMEOUT", "60"))

# Daily command budget — Upstash free tier allows 10,000 commands/day.
# When the running total crosses ``REDIS_DAILY_COMMAND_LIMIT`` we stop
# talking to Redis for the rest of the UTC day and fall back to the
# in-process cache. Set to 0 to disable the guard (e.g. on paid plans).
REDIS_DAILY_COMMAND_LIMIT = int(os.getenv("REDIS_DAILY_COMMAND_LIMIT", "9500"))
# Sync the local counter to a shared Redis counter every N ops so all
# gunicorn workers share the same budget view. Higher = cheaper but
# coarser; lower = closer to real-time but more Redis traffic.
REDIS_BUDGET_SYNC_EVERY = int(os.getenv("REDIS_BUDGET_SYNC_EVERY", "25"))

# How long precomputed payloads stay fresh in Redis.
QUOTE_CACHE_TTL = int(os.getenv("QUOTE_CACHE_TTL", "15"))      # market quotes
HEATMAP_CACHE_TTL = int(os.getenv("HEATMAP_CACHE_TTL", "15"))  # heatmap payload
META_CACHE_TTL = int(os.getenv("META_CACHE_TTL", "1800"))      # name/sector/mcap
USER_CACHE_TTL = int(os.getenv("USER_CACHE_TTL", "30"))        # per-user portfolio
USER_ANALYTICS_TTL = int(os.getenv("USER_ANALYTICS_TTL", "60"))

# Background refreshers (precompute). Set ENABLE_PRECOMPUTE=0 to disable
# them entirely (e.g. local dev where you don't want network calls).
ENABLE_PRECOMPUTE = (os.getenv("ENABLE_PRECOMPUTE", "1") == "1")
MARKET_REFRESH_SECONDS = int(os.getenv("MARKET_REFRESH_SECONDS", "10"))
USER_REFRESH_SECONDS = int(os.getenv("USER_REFRESH_SECONDS", "30"))
# A user is considered "active" (worth refreshing) if they hit any
# authenticated endpoint within this many seconds.
USER_ACTIVE_WINDOW_SECONDS = int(os.getenv("USER_ACTIVE_WINDOW_SECONDS", "300"))
