import os
import threading
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
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Finance Candle")
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "")
APP_NAME = os.getenv("APP_NAME", "Finance Candle")
EMAIL_VERIFICATION_TABLE = os.getenv("EMAIL_VERIFICATION_TABLE", "EmailVerifications")
OTP_TTL_MINUTES = int(os.getenv("OTP_TTL_MINUTES", "10"))
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_COOLDOWN_SECONDS = int(os.getenv("OTP_RESEND_COOLDOWN_SECONDS", "60"))

# ── Market data provider ──────────────────────────────────────────────
# Primary provider for prices / history / quotes.
#   "fyers"    → Fyers API v3 (requires FYERS_APP_ID + FYERS_ACCESS_TOKEN)
#   "dhan"     → DhanHQ v2 REST API (requires DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN
#                AND a paid Data APIs subscription on the Dhan account)
#   "truedata" → TrueData licensed feed (requires TRUEDATA_USERNAME + PASSWORD;
#                stable login, no daily token expiry — recommended for prod)
#   "yfinance" → Yahoo Finance via yfinance package (unlicensed, dev only)
MARKET_DATA_PROVIDER = (os.getenv("MARKET_DATA_PROVIDER") or "yfinance").lower()
# When the primary provider fails or doesn't support a call (e.g. search,
# company info), fall back to this provider. Set to "" / "none" to disable.
MARKET_DATA_FALLBACK = (os.getenv("MARKET_DATA_FALLBACK") or "yfinance").lower()

# ── TrueData (licensed market-data vendor) ────────────────────────────
# Username / password issued by TrueData on subscription. Used by the
# truedata_provider (REST history + quotes) and the live option-chain
# streamer. No daily token dance — these credentials are long-lived.
TRUEDATA_USERNAME = os.getenv("TRUEDATA_USERNAME", "")
TRUEDATA_PASSWORD = os.getenv("TRUEDATA_PASSWORD", "")
TRUEDATA_LIVE_PORT = int(os.getenv("TRUEDATA_LIVE_PORT", "8082"))

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")
DHAN_PIN = os.getenv("DHAN_PIN", "")
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET", "")

FYERS_APP_ID = os.getenv("FYERS_APP_ID", "")
FYERS_ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN", "")
FYERS_SECRET_KEY = os.getenv("FYERS_SECRET_KEY", "")

# Optional additional Fyers apps for round-robin REST quotas. Each Fyers
# app gets its own 10k-req/day quota. Register up to 4 more apps under the
# same Fyers account and add their credentials here to multiply capacity.
# Empty values are ignored.
FYERS_APP_ID_2 = os.getenv("FYERS_APP_ID_2", "")
FYERS_ACCESS_TOKEN_2 = os.getenv("FYERS_ACCESS_TOKEN_2", "")
FYERS_SECRET_KEY_2 = os.getenv("FYERS_SECRET_KEY_2", "")
FYERS_APP_ID_3 = os.getenv("FYERS_APP_ID_3", "")
FYERS_ACCESS_TOKEN_3 = os.getenv("FYERS_ACCESS_TOKEN_3", "")
FYERS_SECRET_KEY_3 = os.getenv("FYERS_SECRET_KEY_3", "")
FYERS_APP_ID_4 = os.getenv("FYERS_APP_ID_4", "")
FYERS_ACCESS_TOKEN_4 = os.getenv("FYERS_ACCESS_TOKEN_4", "")
FYERS_SECRET_KEY_4 = os.getenv("FYERS_SECRET_KEY_4", "")
FYERS_APP_ID_5 = os.getenv("FYERS_APP_ID_5", "")
FYERS_ACCESS_TOKEN_5 = os.getenv("FYERS_ACCESS_TOKEN_5", "")
FYERS_SECRET_KEY_5 = os.getenv("FYERS_SECRET_KEY_5", "")

# ── Automatic daily token refresh (TOTP) ──────────────────────────────
# Personal Fyers credentials needed for headless TOTP login (same for all
# 5 apps because the trading account is one). See fyers_auth.py for the
# flow. When all four of these are set the scheduler runs the refresh
# every weekday at FYERS_TOKEN_REFRESH_HOUR_IST:MINUTE_IST and no manual
# /fyers/callback dance is needed.
FYERS_FY_ID = os.getenv("FYERS_FY_ID", "")
FYERS_PIN = os.getenv("FYERS_PIN", "")
FYERS_TOTP_SECRET = os.getenv("FYERS_TOTP_SECRET", "")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "")
# Per-app redirect override (defaults to FYERS_REDIRECT_URI when blank).
# Only set these if a particular app was registered with a different URL.
FYERS_REDIRECT_URI_1 = os.getenv("FYERS_REDIRECT_URI_1", "")
FYERS_REDIRECT_URI_2 = os.getenv("FYERS_REDIRECT_URI_2", "")
FYERS_REDIRECT_URI_3 = os.getenv("FYERS_REDIRECT_URI_3", "")
FYERS_REDIRECT_URI_4 = os.getenv("FYERS_REDIRECT_URI_4", "")
FYERS_REDIRECT_URI_5 = os.getenv("FYERS_REDIRECT_URI_5", "")
FYERS_TOKEN_REFRESH_HOUR_IST = int(os.getenv("FYERS_TOKEN_REFRESH_HOUR_IST", "7"))
FYERS_TOKEN_REFRESH_MINUTE_IST = int(os.getenv("FYERS_TOKEN_REFRESH_MINUTE_IST", "30"))


# ── Upstox API (market data provider) ─────────────────────────────────
# Upstox v2/v3 REST. Access tokens expire daily at 03:30 IST. The token
# is obtained via the OAuth code exchange (/callback/upstox) or the
# headless TOTP login in upstox_auth.py. Personal creds (mobile/pin/totp)
# enable the automated daily refresh.
UPSTOX_API_KEY = os.getenv("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET = os.getenv("UPSTOX_API_SECRET", "")
UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "")
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
UPSTOX_TOTP_SECRET = os.getenv("UPSTOX_TOTP_SECRET", "")
UPSTOX_MOBILE = os.getenv("UPSTOX_MOBILE", "")
UPSTOX_PIN = os.getenv("UPSTOX_PIN", "")
UPSTOX_TOKEN_REFRESH_HOUR_IST = int(os.getenv("UPSTOX_TOKEN_REFRESH_HOUR_IST", "7"))
UPSTOX_TOKEN_REFRESH_MINUTE_IST = int(os.getenv("UPSTOX_TOKEN_REFRESH_MINUTE_IST", "15"))

# Runtime store for the Upstox access token (set by upstox_auth on refresh
# or by the OAuth callback). Prefer this over the env-var value so refreshes
# propagate without a restart.
_upstox_runtime_token: dict[str, str] = {}
_upstox_token_lock = threading.Lock()


def set_upstox_token(token: str) -> None:
    if not token:
        return
    with _upstox_token_lock:
        _upstox_runtime_token["token"] = token


def upstox_access_token() -> str:
    """Active Upstox access token — runtime value wins over env var."""
    with _upstox_token_lock:
        tok = _upstox_runtime_token.get("token")
    return tok or UPSTOX_ACCESS_TOKEN


# ── Runtime token store (populated by fyers_auth.refresh_all_tokens) ──
# Lets the daily refresher inject fresh tokens without restarting the
# app. ``fyers_app_pool()`` prefers runtime tokens over the env-var
# values, so the round-robin picks them up on the very next request.
_runtime_tokens: dict[str, str] = {}
_runtime_tokens_lock = threading.Lock()


def set_runtime_token(app_id: str, token: str) -> None:
    if not app_id or not token:
        return
    with _runtime_tokens_lock:
        _runtime_tokens[app_id] = token


def get_runtime_token(app_id: str) -> str:
    with _runtime_tokens_lock:
        return _runtime_tokens.get(app_id, "")


def _all_app_slots() -> list[tuple[str, str, str, str]]:
    """Return (app_id, env_access_token, secret_key, redirect_override) for
    each numbered slot, including empties. Internal helper.
    """
    return [
        (FYERS_APP_ID,   FYERS_ACCESS_TOKEN,   FYERS_SECRET_KEY,   FYERS_REDIRECT_URI_1),
        (FYERS_APP_ID_2, FYERS_ACCESS_TOKEN_2, FYERS_SECRET_KEY_2, FYERS_REDIRECT_URI_2),
        (FYERS_APP_ID_3, FYERS_ACCESS_TOKEN_3, FYERS_SECRET_KEY_3, FYERS_REDIRECT_URI_3),
        (FYERS_APP_ID_4, FYERS_ACCESS_TOKEN_4, FYERS_SECRET_KEY_4, FYERS_REDIRECT_URI_4),
        (FYERS_APP_ID_5, FYERS_ACCESS_TOKEN_5, FYERS_SECRET_KEY_5, FYERS_REDIRECT_URI_5),
    ]


def fyers_app_pool() -> list[tuple[str, str]]:
    """Return all configured Fyers (app_id, access_token) pairs.

    Runtime tokens (set by the daily refresher) take precedence over the
    FYERS_ACCESS_TOKEN env vars so refreshes propagate without restart.
    Apps with no token in either store are skipped.
    """
    out: list[tuple[str, str]] = []
    with _runtime_tokens_lock:
        for app_id, env_tok, _secret, _redir in _all_app_slots():
            if not app_id:
                continue
            tok = _runtime_tokens.get(app_id) or env_tok
            if tok:
                out.append((app_id, tok))
    return out


def fyers_app_credentials() -> list[tuple[str, str, str]]:
    """Return (app_id, secret_key, redirect_uri) for every app that has
    enough info to perform the automated token refresh. Used by
    ``fyers_auth.refresh_all_tokens``.
    """
    default_redirect = FYERS_REDIRECT_URI
    out: list[tuple[str, str, str]] = []
    for app_id, _tok, secret, redir in _all_app_slots():
        if not (app_id and secret):
            continue
        out.append((app_id, secret, redir or default_redirect))
    return out

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
# How often the background job rebuilds the NIFTY option-chain snapshot
# during market hours. Needed so OI deltas / gap-outlook keep accumulating
# even when nobody has the options page open in a browser.
OPTION_CHAIN_REFRESH_SECONDS = int(os.getenv("OPTION_CHAIN_REFRESH_SECONDS", "30"))
# A user is considered "active" (worth refreshing) if they hit any
# authenticated endpoint within this many seconds.
USER_ACTIVE_WINDOW_SECONDS = int(os.getenv("USER_ACTIVE_WINDOW_SECONDS", "300"))

# ── Azure Table OHLC cache ────────────────────────────────────────────
# Daily candles are immutable once the trading day closes, so we persist
# them in Azure Tables and only fetch missing dates from the provider.
# Cuts provider history calls by ~99% across scanners.
OHLC_TABLE = os.getenv("OHLC_TABLE", "OhlcDaily")
OHLC_CACHE_ENABLED = (os.getenv("OHLC_CACHE_ENABLED", "1") == "1")
# Skip the cache for symbols whose first cache miss was less than this
# many seconds ago — avoids hammering the provider when a symbol simply
# has no data (delisted / typo).
OHLC_NEGATIVE_TTL = int(os.getenv("OHLC_NEGATIVE_TTL", "900"))

# ── Swing-tools precompute (overnight scan warmer) ────────────────────
# When 1, the background scheduler runs all heavy swing scans once at
# ``SWING_PRECOMPUTE_HOUR_IST`` (default 08:00 IST Mon-Fri) and stores
# results in Redis under the same keys the live scanners use, so users
# hitting the swing-tools page pay zero broker calls for the first scan
# of the day.
SWING_PRECOMPUTE_ENABLED = (os.getenv("SWING_PRECOMPUTE_ENABLED", "1") == "1")
SWING_PRECOMPUTE_HOUR_IST = int(os.getenv("SWING_PRECOMPUTE_HOUR_IST", "8"))
SWING_PRECOMPUTE_MINUTE_IST = int(os.getenv("SWING_PRECOMPUTE_MINUTE_IST", "0"))

# ── Payments (Razorpay) ───────────────────────────────────────────────
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_CURRENCY = os.getenv("RAZORPAY_CURRENCY", "INR")
