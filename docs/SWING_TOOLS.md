# Swing Tools — User Guide

The **Swing Tools** hub is built for positional traders holding **3 days to 8 weeks**. Signals are computed from **daily and weekly** candles, so you can scan once at end-of-day (3:35 PM IST onward) and act on the next morning's opening.

> **Access:** Sidebar → *Swing → Swing Tools*
> **URL:** `/swing-tools`
> **Best time to run scans:** After 3:35 PM IST (closing prices locked) or before 9:00 AM next day.

---

## 1. Breakouts

**What it measures:** Stocks closing **above their 50-day high** (or pivot resistance) with above-average volume.

**What it indicates:**
- A confirmed end-of-day breakout — the stock has cleared overhead supply.
- High volume = institutional accumulation, not retail noise.

**How it helps you:**
- Gives you a "ready to buy tomorrow morning" list.
- Use the **breakout level as your stop-loss** — if the stock closes back below, the setup has failed.

---

## 2. Relative Strength (RS)

**What it measures:** A stock's **3-month return vs NIFTY** (Mansfield-style RS).

**What it indicates:**
- **RS > 0** → outperforming the market.
- **RS > 20** → very strong leader (top decile typically).
- **RS declining** → loss of leadership; consider exit.

**How it helps you:**
- The market's best swing trades come from **the strongest stocks**. Filter your universe to only RS > 10 before applying any technical setup.
- Combine with sector leadership: a top-RS stock *inside* the top-RS sector = best of breed.

---

## 3. Chart Patterns

**What it shows:** Auto-detected classic patterns — **cup-and-handle, flag, ascending triangle, double bottom**, etc.

**What they indicate:**
- Each pattern has a **measured target** (e.g., flag pole height projected from breakout).
- Pattern quality is scored by tightness, duration, and volume contraction during the base.

**How it helps you:**
- Removes the subjective "I think I see a triangle" — the algorithm validates it.
- Use the printed **target** and **invalidation level** as your trade plan.

---
## How to use it properly 
## 4. Sector Leaders

**What it shows:** Top 3 stocks (by RS + price strength) **per sector**.

**What it indicates:**
- The captain of each sector. When the sector rotates up, these move first and farthest.

**How it helps you:**
- Pick your swing names from this list, not from a generic momentum scan.
- If the leader is breaking down, the whole sector is likely cooling — exit early.

---

## 5. Options-Confirmed Setups

**What it shows:** Bullish technical setups that are **also confirmed** by options OI behavior (call-side OI build-up + favourable PCR).

**What it indicates:**
- Smart money (options writers, FIIs) is also positioned *for* the move.
- Highest-conviction long setups — derivatives and cash are aligned.

**How it helps you:**
- Reduces "good chart, bad outcome" failures by adding a second confirmation lens.
- These are typically your **higher-allocation** swing trades.

---

## 6. FII / DII Cash-Market Flows

**What it shows:** Net buying / selling by **Foreign Institutional Investors** and **Domestic Institutional Investors** in the cash segment (₹ Cr).

**What it indicates:**
- **FII +ve & DII +ve** → very bullish (rare; market melts up).
- **FII +ve & DII −ve** → bullish but cautious.
- **FII −ve & DII +ve** → mixed (DII absorbing FII selling — common in corrections).
- **FII −ve & DII −ve** → very bearish (sell-off, raise cash).

**How it helps you:**
- Macro context for sizing. In "Very Bullish" regimes, push allocation up to plan max; in "Very Bearish", trim/hedge.
- Persistent FII selling (5+ days) historically precedes corrections.

---

## 7. MTF Alignment — *Multi-Time-Frame Trend*

**What it measures:** Stocks where the **daily EMA stack** (20 > 50 > 200) **and** the **weekly EMA stack** (20 > 50) are both pointing the same way.

**What it indicates:**
- **Strong Bull** → all 5 EMAs aligned upward + price above all = uptrend on every timeframe a swing trader cares about.
- **Strong Bear** → all aligned downward = falling-knife zone, avoid longs.

**How it helps you:**
- Highest-quality trend filter. **Never short a Strong Bull, never buy a Strong Bear.**
- Use the **% distance from 200DMA** column: 0–15% = healthy uptrend; 30%+ = stretched, wait for pullback.

**Verdict scoring:** Each EMA above price/in-order adds +20; reverse subtracts 20. Score in [+100, −100].

---

## 8. Near 52-Week High Proximity

**What it measures:** Stocks within **5%** (configurable) of their **52-week high**, with **base tightness** and **volume-ratio** filters to flag *constructive* bases.

**What it indicates:**
- Stocks near 52WH = leadership. New highs have no overhead supply.
- **Constructive base** = tight price range (low volatility) + volume contraction → classic Minervini / O'Neil base.

**How it helps you:**
- Pre-breakout buy list. Set alerts for these to break the 52WH.
- The **Base** column ("Constructive" vs "Loose") tells you whether the base is well-formed (buy on breakout) or sloppy (skip).

**Tunable:** Adjust **Max proximity %** — 3% for tight breakouts, 10% for wider radar.

---

## 9. Pocket Pivot Scanner

**What it measures:** The **Chris Kacher / Mark Minervini** pocket-pivot setup:
1. Stock is in a **volume dry-up** base (recent volume contracting).
2. Today's volume on an **up-day** exceeds the **largest down-day volume of the past 10 days**.

**What it indicates:**
- Institutional buying *inside* the base — they're not waiting for the breakout.
- Early entry signal, **before** a regular breakout fires.

**How it helps you:**
- Catch the move 2–5% **earlier** than a Breakouts-tool entry, often with a tighter stop.
- **Extension %** column — if the stock is already extended (5%+ above its EMA21), the pocket pivot is *late*; pass.

---

## 10. Backtest Sandbox

**What it does:** Runs your **EMA-cross + RSI threshold + volume confirmation** strategy over **N years** on any symbol and reports:
- Total trades, win rate, avg win/loss
- Profit factor (gross profit / gross loss)
- Strategy total return vs Buy & Hold
- **Outperformance %** — the real question: did the strategy beat just holding the stock?

**What it indicates:**
- **Profit factor > 1.5** with **win rate > 45%** = robust setup.
- **Outperformance > 0** = the strategy added alpha vs holding.
- If buy-and-hold wins → the strategy was overtrading.

**How it helps you:**
- Validate any rule-set on a real ticker before committing capital.
- Tune **EMA Fast / EMA Slow / RSI ≥ / Vol ×** sliders to find what historically worked on *that* stock.

**Default rules:** EMA20 crosses above EMA50, RSI ≥ 55, volume ≥ 1.2× 20-day average → enter long. Exit on opposite cross.

---

## Recommended swing workflow

1. **Macro check** → FII/DII (Tool #6). If Very Bearish, halve your sizing.
2. **Trend filter** → MTF Alignment (Tool #7). Only consider Strong Bulls for new longs.
3. **Leadership** → Relative Strength (Tool #2) + Sector Leaders (Tool #4).
4. **Setup discovery** → Breakouts / Patterns / 52WH Proximity / Pocket Pivot (Tools #1, #3, #8, #9).
5. **Confirmation** → Options-Confirmed (Tool #5) for high-conviction adds.
6. **Validation** → Backtest Sandbox (Tool #10) — confirm the setup historically works on *this* stock.
7. **Execution** — buy at next-day open, place stop just below pattern invalidation.

**Position-sizing rule of thumb:** Risk **1%** of capital per trade. Position size = `(1% × Capital) / (Entry − Stop)`.
