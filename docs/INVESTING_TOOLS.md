# Investing Tools — User Guide

The **Investing Tools** hub is built for long-term investors (**6 months to 5+ years**). Focus is on **fundamentals, valuation, governance and quality** rather than price action. Most outputs change quarterly (after results) — you do not need to look at these daily.

> **Access:** Sidebar → *Investing → Investing Tools*
> **URL:** `/investing-tools`
> **Cadence:** Re-check holdings once per quarter (post-results); re-screen the market once per month.

---

## 1. Screener

**What it does:** Filter the full universe by **fundamental metrics** — ROE, ROCE, debt/equity, sales growth, profit growth, P/E, P/B, dividend yield, etc. Pre-built styles: **quality, value, growth, dividend**.

**What it indicates:**
- A shortlist of stocks that mathematically satisfy your investing philosophy.
- "Quality" style → ROE > 15%, low debt, consistent earnings.
- "Value" style → low P/E, P/B, high earnings yield.
- "Growth" style → 3-yr sales/profit CAGR > 20%.

**How it helps you:**
- Replaces hours of manual filtering on screener.in.
- Use as the **funnel-top** of your research. Every other tool here is for *deepening* the analysis on names that passed the screen.

---

## 2. DCF Calculator — *Discounted Cash Flow*

**What it does:** Computes intrinsic value per share using:
- Free Cash Flow (latest TTM)
- Growth rate (years 1–5, terminal)
- Discount rate (WACC, typically 10–12% for India)

Outputs **fair value per share** and **upside / downside %** vs current price.

**What it indicates:**
- **Upside > 20%** with conservative inputs → margin of safety exists.
- **Downside** → stock is priced for perfection; avoid or trim.

**How it helps you:**
- Forces you to put numbers on your conviction instead of relying on stories.
- Test sensitivity: change growth rate ±2% and discount ±1% — if intrinsic value swings >40%, the valuation is fragile.

**Caveat:** DCF is **garbage-in-garbage-out** — wrong growth assumption = wrong fair value. Cross-check with peer multiples (Tool #3).

---

## 3. Peer Comparison

**What it does:** Side-by-side **5-stock peer table** with P/E, P/B, ROE, ROCE, debt/equity, sales growth, OPM%, dividend yield.

**What it indicates:**
- Where the stock ranks within its industry on each metric.
- Outliers — a stock with sector-leading ROE but bottom-quartile P/E = potential mispricing.

**How it helps you:**
- Quickly spot whether you are paying a **deserved premium** (best ROE in the peer set) or **overpaying** (mediocre fundamentals, top valuation).
- Build the "why this one and not the others" thesis in 60 seconds.

---

## 4. Earnings Calendar

**What it shows:** Upcoming **quarterly result announcements** for your watchlist / top universe over the next 30 days.

**What it indicates:**
- Volatility windows — expect 5–15% price swings on result day.
- Concall date — read the transcript *before* the next quarter starts.

**How it helps you:**
- Plan around results: avoid initiating fresh positions 2 days before results (event risk).
- Set reminders to read concalls — long-term value is in management commentary, not the numbers themselves.

---

## 5. Shareholding Pattern

**What it shows:** Quarterly breakdown of **promoter / FII / DII / public** holdings, with **trend deltas** (Δ vs last quarter).

**What it indicates:**
- **Promoter buying** → strong insider conviction.
- **Promoter pledging > 30%** → red flag (Vedanta, Adani-style risk).
- **Rising FII + falling promoter** → ownership transition, often re-rating ahead.
- **Falling promoter + falling FII + rising public** → distribution; avoid.

**How it helps you:**
- One of the **strongest leading indicators** of stock returns in India.
- Always check the **last 4-quarter trend**, not a single snapshot.

---

## 6. Corporate Actions

**What it shows:** Recent + upcoming **dividends, bonus issues, splits, rights, buybacks, mergers**.

**What it indicates:**
- **Buyback at premium** → management thinks stock is cheap.
- **Bonus / split** → improves liquidity, not value, but often triggers retail buying.
- **Special dividend** → cash-rich, no growth use-case (mature business).
- **Rights issue** → cash crunch unless for growth capex; read the prospectus.

**How it helps you:**
- Adjust your cost basis correctly.
- Use buybacks as signals — companies rarely buy back near tops.

---

## 7. Annual Report Q&A — *RAG-powered*

**What it does:** Ask **natural-language questions** about a company's annual report / concall transcript, answered using **RAG retrieval** over the indexed corpus.

**Example questions:**
- *"What is the management's capex guidance for FY26?"*
- *"How has the gross margin trend changed over the last 3 years?"*
- *"What did the CFO say about working-capital cycles?"*

**What it indicates:**
- Direct quotes + citations from the original document.

**How it helps you:**
- Replaces hours of reading 200-page PDFs.
- Forces *specific* questions, which is how good analysts think.
- Always **click through to the source** before quoting in your notes.

---

## 8. Concall Sentiment

**What it does:** Scores the **tone** of the most recent earnings concall — bullish / neutral / cautious — by keyword + sentiment analysis on the transcript.

**What it indicates:**
- Mismatch between **bullish numbers + cautious tone** = warning. Management knows something the headline EPS doesn't show.
- Bullish numbers + bullish tone = confirmation.

**How it helps you:**
- Reads the body language of management without you sitting through 90 minutes of audio.
- Compare scores across quarters: deteriorating sentiment over 2 quarters is a strong sell signal.

---

## 9. Moat / Quality Score

**What it does:** A composite **0–100 score** built from:
- ROE / ROCE consistency over 5 years
- Debt levels (D/E, interest coverage)
- Free-cash-flow conversion (FCF / PAT)
- Operating margin stability
- Sales growth durability

**What it indicates:**
- **>75 = Wide Moat** — Asian Paints / HDFC Bank class.
- **50–75 = Decent Moat** — sector leader but with vulnerabilities.
- **<50 = No Moat** — commodity / cyclical / leveraged.

**How it helps you:**
- For long-term portfolios, **anchor** at least 60% of capital in Wide Moat names.
- Treat low-moat stocks as **trades**, not investments — exit on thesis breach.

---

## 10. Portfolio Health

**What it does:** Analyses your current holdings for:
- **Sector concentration** (>30% in one sector = risk)
- **Single-stock concentration** (>15% in one name)
- **Quality distribution** (% in Wide Moat vs No Moat)
- **Valuation aggregate** (weighted P/E, P/B vs NIFTY)
- **Drawdown contributors**

**What it indicates:**
- Hidden risks you may not notice trade-by-trade.

**How it helps you:**
- Quarterly rebalance check. If the report flags "60% in financials" — trim, even if every individual stock looks healthy.
- Surfaces "boil-the-frog" concentration that builds up unnoticed during sector rallies.

---

## 11. Insider Tracker

**What it shows:** Recent **promoter / director / SAST** buying and selling, with rolling 6-month tallies.

**What it indicates:**
- **Insider buying** → high-conviction bullish signal (insiders only buy for one reason).
- **Insider selling** → ambiguous (could be diversification, tax, ESOP exercise); only meaningful in clusters.
- **Cluster buying** (multiple insiders, same month) → very strong signal.

**How it helps you:**
- Tracks the "people who know the most" doing the actual transactions.
- Combine with shareholding (Tool #5) for full ownership picture.

---

## 12. SIP Simulator

**What it does:** Simulates a **monthly SIP** in any stock / index over your chosen period. Computes:
- Total invested vs final corpus
- XIRR (annualised return)
- Max drawdown along the way
- Comparison with NIFTY 50 SIP for the same period

**What it indicates:**
- Real-world return *including* the discipline tax (you bought through every dip and every peak).

**How it helps you:**
- Settles the "stock vs index SIP" question with data, not narrative.
- Useful for clients / family discussions — visual proof that disciplined SIP beats market timing.

---

## Recommended investing workflow

1. **Funnel-top** → Screener (Tool #1) with your style preset.
2. **Quality filter** → Moat / Quality Score (Tool #9). Drop everything < 50.
3. **Valuation sanity** → DCF (Tool #2) + Peer Comparison (Tool #3).
4. **Governance & ownership** → Shareholding (Tool #5) + Insider Tracker (Tool #11) + Corporate Actions (Tool #6).
5. **Qualitative deep-dive** → Annual Report Q&A (Tool #7) + Concall Sentiment (Tool #8).
6. **Event awareness** → Earnings Calendar (Tool #4).
7. **Portfolio hygiene (quarterly)** → Portfolio Health (Tool #10) + SIP Simulator (Tool #12) for benchmarking.

**Buffett-style discipline:** A stock you can't justify after Tools #1 → #9 is a stock you should not own. Trust the process, not the price chart.
