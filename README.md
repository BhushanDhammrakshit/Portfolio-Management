# PManagement

A Flask-based personal portfolio manager with live price refresh (DhanHQ / yfinance), Azure Table Storage backend, AI-powered insights (Azure OpenAI), Bloomberg-style advanced analytics, sector heatmaps, and tender tracking.

## Features

- **Dashboard** — portfolio summary, allocation pie/bar charts, top movers, P/L by stock, sector breakdown, holdings list.
- **Add / Edit / Delete stocks** — with yfinance symbol mapping for live prices.
- **Refresh prices** — one-click batch refresh via yfinance.
- **Portfolio analysis** — per-stock AI insights on demand.
- **Advanced dashboard** — Sharpe / Sortino / Treynor / Calmar ratios, beta vs NIFTY 50, VaR/CVaR, drawdown, monthly returns heatmap, HHI concentration.
- **Sector heatmap** — Nifty 50 + Bank Nifty live performance, with bullish/bearish sector strip.
- **Tenders** — scrape and view government tender listings.

## Tech stack

- Python 3.10+ / Flask 3
- Azure Table Storage (`azure-data-tables`)
- yfinance, pandas, numpy
- Azure OpenAI (chat completions)
- Bootstrap 5, Chart.js, Font Awesome

## Setup

```powershell
# 1. Clone
git clone https://github.com/BhushanDhammrakshit/PManagement.git
cd PManagement

# 2. Create venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
Copy-Item .env.example .env
# Edit .env and fill in real values
```

## Environment variables (.env)

| Variable | Purpose |
| --- | --- |
| `AZURE_TABLE_CONN_STR` | Azure Storage account connection string |
| `USER_INFO_TABLE` | Table name for user accounts |
| `USER_STOCKS_TABLE` | Table name for portfolio entries |
| `OPENAI_API_KEY` | Azure OpenAI API key |
| `OPENAI_ENDPOINT` | Full Azure OpenAI chat completions endpoint URL |
| `SECRET_KEY` | Flask session signing key |
| `MARKET_DATA_PROVIDER` | `dhan` or `yfinance` (default `yfinance`) |
| `MARKET_DATA_FALLBACK` | Provider used when primary lacks data (default `yfinance`); set `none` to disable |
| `DHAN_CLIENT_ID` | DhanHQ client id (only when provider = `dhan`) |
| `DHAN_ACCESS_TOKEN` | DhanHQ v2 access token (only when provider = `dhan`) |

See `.env.example` for the template.

### Switching to DhanHQ as the live-data provider

1. Log in to https://web.dhan.co → Profile → DhanHQ Trading APIs → generate an **Access Token** (24-hour or long-lived).
2. Add to `.env`:
   ```
   MARKET_DATA_PROVIDER=dhan
   DHAN_CLIENT_ID=<your client id>
   DHAN_ACCESS_TOKEN=<your access token>
   ```
3. On first call the app downloads Dhan's instrument master CSV (~24 MB) to `application/_dhan_scrip_master.csv` and caches it for 24 hours.
4. Symbols stay in Yahoo format (`RELIANCE.NS`, `^NSEI`) — the abstraction maps them to Dhan `security_id` automatically.

## Run

```powershell
python run.py
```

App starts on http://127.0.0.1:5000

## Project structure

```
application/
  __init__.py        # Flask app + blueprint registration
  config.py          # Loads env vars
  routes/            # Blueprints: heatmap, advanced analytics, AI, tenders, etc.
  services/          # Azure Tables + AI client
  static/            # CSS, JS
  templates/         # Jinja2 templates
run.py               # Entry point
requirements.txt
Procfile             # For gunicorn-based hosts (Render, Railway, Heroku)
```

## Deployment

The included `Procfile` starts the app with `gunicorn run:app`. Set the same env vars in your hosting provider's dashboard. Compatible with Render, Railway, Azure App Service (with custom startup command), and similar PaaS hosts.

## License

Personal project. All rights reserved unless stated otherwise.
