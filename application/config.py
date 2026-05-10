import os
from dotenv import load_dotenv

# Load .env variables
load_dotenv()

AZURE_TABLE_CONN_STR = os.getenv("AZURE_TABLE_CONN_STR")
USER_INFO_TABLE = os.getenv("USER_INFO_TABLE")
USER_STOCKS_TABLE = os.getenv("USER_STOCKS_TABLE")
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
