# Intraday Tools — User Guide

The **Intraday Tools** hub is built for traders who enter and exit positions inside a single session (9:15 AM – 3:30 PM IST). Every tool here is tuned for short time-frames (5–60 min candles), live cache TTLs, and signals that decay quickly.

> **Access:** Sidebar → *Intraday → Intraday Tools*
> **URL:** `/intraday-tools`
> **Best window:** Use most tools after **9:30 AM** (so opening-range data is filled) and before **3:00 PM** (so signals have time to play out).

---

## 1. ORB Scanner — *Opening Range Breakout*

**What it measures:** Stocks that break **above the high** (or below the low) of the **first 15 minutes** of trading.

**What it indicates:**
- A clean breakout from the morning consolidation = institutional commitment.
- Breakout direction = bias for the rest of the day.

**How it helps you:**
- Gives a watch-list of *fresh* momentum names by ~9:35 AM.
- The "volume confirmation" column tells you if the breakout has real participation (vol × > 1.5) or is a false move.
- Use the **ORB High / ORB Low** as natural stop-loss levels.

**Typical playbook:** Buy on breakout above ORB high *with* volume confirmation, stop-loss = ORB low, target = 1× ORB range projected upward.

---

## 2. RVOL Heatmap — *Relative Volume*

**What it measures:** Today's traded volume vs the **20-day average** for the same time of day.

**What it indicates:**
- RVOL > **2.0** → unusual interest; news, event, or institutional flow is in play.
- RVOL < **0.7** → dead stock today; avoid for intraday.

**How it helps you:**
- Filters the noise. A stock can be up 3% on low volume (weak) or up 1% on RVOL 4× (strong — accumulation).
- Pair this with a price-action tool (ORB, Momentum Burst) before entering.

---

## 3. Gappers

**What it measures:** Stocks that opened **>2% gap-up** or **gap-down** vs yesterday's close.

**What it indicates:**
- **Gap-and-go:** price holds the gap → trend day in gap direction.
- **Gap-fill:** price reverses to fill the gap → fade trade.

**How it helps you:**
- Pre-market preparation list — know your candidates before the bell.
- Combined with news-sentiment (Tool #9) you can separate "earnings gap" (continuation likely) from "rumor gap" (fade likely).

---

## Need more explaination 
## 4. Pivots — *Classic Daily Pivots*

**What it shows:** Daily Pivot (P), Support 1/2/3 and Resistance 1/2/3 calculated from previous day's H/L/C.

**What they indicate:**
- **Above P** → bullish bias for the day.
- **Below P** → bearish bias.
- S1/R1 are intraday targets; S2/R2 are stretch targets / reversal zones.

**How it helps you:**
- Pre-decided levels remove emotion. Use R1 as profit-booking zone for a long, S1 as initial stop.

---

## 5. Momentum Burst

**What it measures:** Stocks that have moved **>1.5% in the last 15 minutes** with rising volume.

**What it indicates:**
- A sudden buying/selling thrust — often the **start** of a new intraday leg.

**How it helps you:**
- Catch in-progress moves rather than waiting for a clean setup.
- Cross-check with RVOL: a burst on RVOL > 2 is *real*, on RVOL < 1 is *fakeout*.

**Caution:** Late bursts (after 2:30 PM) often fade — be quick on profit-booking.

---

## 6. Index Basis Monitor

**What it shows:** Spot price of NIFTY / BANKNIFTY vs **theoretical fair value** of current-month futures (cost-of-carry model).

**What it indicates:**
- **Futures > Fair Value (positive basis)** → bullish positioning, longs are paying premium.
- **Futures < Fair Value (negative basis / discount)** → bearish positioning, hedging pressure.

**How it helps you:**
- A leading proxy for institutional sentiment. Big discount in BANKNIFTY futures often precedes a sell-off in PSU/private banks.
- Helps decide overall directional bias before placing any individual stock trade.

> **Note:** The displayed fair value uses ~7% annual carry. For live basis you should compare against your broker's actual futures LTP.

---

## 7. VWAP Deviation Scanner

**What it measures:** Stocks trading **>2%** above or below their **intraday VWAP** (Volume-Weighted Average Price) with volume confirmation.

**What it indicates:**
- **Above VWAP + volume × > 1.5** → trend-continuation (institutional buying).
- **Below VWAP + volume × > 1.5** → trend-continuation (institutional selling).
- Otherwise: **mean-reversion candidate** — the stock is stretched and may snap back.

**How it helps you:**
- VWAP is the line institutions watch all day. Trading *with* the deviation = momentum trade; trading *against* it = reversion trade.
- The **Signal** column tells you which playbook is appropriate.

**Tunable:** Adjust the **Min |Dev| %** input (default 2.0) — lower for choppy days, higher for trend days.

---

## 8. Sector Rotation Ticker

**What it measures:** Live performance of **10 NSE sector indices** (NIFTY, BANKNIFTY, IT, AUTO, PHARMA, FMCG, METAL, REALTY, ENERGY, FIN SERVICES) with **1-hour momentum**.

**What it indicates:**
- **Leader sector** = where money is flowing in *right now*.
- **Laggard sector** = where money is leaving.
- **Rotation Spread** (leader − laggard %) — wide spread = stock-pickers' market; narrow spread = index-driven day.

**How it helps you:**
- Always trade **stocks in the leading sector** for long ideas and stocks in lagging sectors for shorts. You're swimming *with* the tide.
- The **1-Hour Momentum** column catches *fresh* rotation — a sector that was flat in the morning but turning green at 12:30 PM is a high-probability afternoon trade.

---

## 9. Live News Sentiment Tagger

**What it measures:** Aggregates RAG-indexed news from the **last 48 hours** per ticker and scores it bullish / bearish / neutral via keyword sentiment.

**What it indicates:**
- **Bullish verdict** (score > 0.4) → positive news flow, upside bias.
- **Bearish verdict** (score < −0.4) → negative news flow, downside risk.
- High **news count** (3+) means the move is *news-driven*, not technical.

**How it helps you:**
- Before going long on a momentum-burst signal, check: is there *positive news* backing it? If yes, conviction is higher.
- Avoid shorts in stocks with **multiple bullish news items** in the last 48h (re-rating risk).
- The **Latest Headline** column is clickable — read the source before sizing up.

> **Requires:** RAG ingest pipeline must be running (see `services/rag/ingest/runner.py`).

---

## How to combine the tools (sample workflow)

| Time | Action | Tools to use |
|------|--------|--------------|
| 9:00–9:15 | Pre-market prep | Gappers, News Sentiment, Index Basis |
| 9:15–9:30 | Form bias | Sector Rotation, Index Basis |
| 9:30–9:45 | Find candidates | ORB Scanner, Momentum Burst, RVOL |
| 9:45 onwards | Filter & confirm | VWAP Deviation + Sector Rotation + News |
| Throughout | Risk management | Pivots (for stops & targets) |

**Golden rule:** *Never* trade an intraday signal without checking volume (RVOL or volume ratio column). A signal without volume is a coin flip.
