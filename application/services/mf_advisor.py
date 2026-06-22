"""AI advisory layer for mutual funds."""
from __future__ import annotations

import json
from typing import Optional

from application.services import ai_client


_SYSTEM_PROMPT = """You are a SEBI-aware Indian mutual fund advisor.
You analyse a user's MF portfolio (and optionally direct stock holdings)
and give crisp, actionable advice. Avoid generic disclaimers; instead point
out concrete issues (over-concentration in one AMC, duplicate large-cap
exposure, expensive expense ratios, sector skew, missing asset classes).

ALWAYS return strictly valid JSON in this shape:
{
  "summary": "<2-3 sentence verdict>",
  "risk_level": "low" | "moderate" | "high",
  "diversification_score": 0-10,
  "strengths": ["...", "..."],
  "issues": [
    {"severity": "low|medium|high", "title": "...", "detail": "..."}
  ],
  "suggestions": [
    {"action": "add|reduce|exit|hold|rebalance", "target": "<fund or category>", "rationale": "..."}
  ],
  "asset_mix_recommendation": {"equity_pct": 70, "debt_pct": 20, "gold_pct": 5, "intl_pct": 5}
}
No prose outside the JSON.
"""


def analyze_portfolio(summary: dict,
                      stock_overlap: Optional[dict] = None) -> tuple[Optional[dict], Optional[str]]:
    """Call the AI to analyse a portfolio summary dict (from mf_portfolio.portfolio_summary)."""
    if not ai_client.is_configured():
        return None, "AI is not configured. Set OPENAI_API_KEY and OPENAI_ENDPOINT."

    payload = {
        "totals": summary.get("totals", {}),
        "holdings": [
            {k: v for k, v in h.items()
             if k in ("scheme_name", "category", "fund_house",
                      "invested", "current_value", "pnl_pct", "cagr_pct")}
            for h in summary.get("holdings", [])
        ],
        "allocation_by_category": summary.get("allocation_by_category", []),
        "allocation_by_amc": summary.get("allocation_by_amc", []),
        "stock_overlap": (stock_overlap or {}).get("overlaps", [])[:10],
    }
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, default=str)},
    ]
    content, err = ai_client.chat(messages, temperature=0.3,
                                  max_tokens=1200, timeout=45)
    if err or not content:
        return None, err or "Empty AI response"
    # Strip code fences if model added them
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text), None
    except Exception as e:
        return {"summary": content, "_parse_error": str(e)}, None


def recommend_for_goal(goal: str, horizon_years: float, risk: str,
                       monthly_amount: float) -> tuple[Optional[dict], Optional[str]]:
    """AI suggests fund categories + sample schemes for a goal."""
    if not ai_client.is_configured():
        return None, "AI is not configured."

    sys = """You are an Indian MF advisor. Recommend an asset allocation
and 3–5 specific scheme categories (NOT scheme names, just categories like
'Large Cap Index Fund', 'Flexi Cap', 'Short Duration Debt') with a sample
allocation %, suitable for the user's goal.

Return strict JSON:
{
  "verdict": "<one-line>",
  "asset_mix": {"equity_pct": 60, "debt_pct": 30, "gold_pct": 5, "intl_pct": 5},
  "categories": [
    {"category": "...", "allocation_pct": 30, "rationale": "..."}
  ],
  "monthly_split": [{"category": "...", "amount": 5000}],
  "warnings": ["..."]
}
"""
    payload = {
        "goal": goal, "horizon_years": horizon_years,
        "risk_profile": risk, "monthly_amount": monthly_amount,
    }
    messages = [
        {"role": "system", "content": sys},
        {"role": "user", "content": json.dumps(payload)},
    ]
    content, err = ai_client.chat(messages, temperature=0.4, max_tokens=900)
    if err or not content:
        return None, err or "Empty AI response"
    text = content.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    try:
        return json.loads(text), None
    except Exception as e:
        return {"raw": content, "_parse_error": str(e)}, None
