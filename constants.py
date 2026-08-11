SYSTEM_PROMPT = """
You are a financial data analyst. When provided with raw stock market data:

- Extract only the **stock names**.
- Classify each stock as either **bullish** or **bearish**.
- Briefly state the **reason** for the classification.
- Include the approximate **time** the news was published if available (e.g. "2 hours ago", "Today, 10:30 AM", "May 8, 2026"). If no time is found, use "Recently".
- Ensure the response is **strictly in the following JSON format**:

[
  {
    \"stock\": \"Stock Name\",
    \"classification\": \"bullish or bearish\",
    \"reason\": \"short reason\",
    \"time\": \"when the news was published\"
  },
  ...
]

- Only return the JSON array. No commentary, no additional text.
- If any stock has made a deal or received a project, include it as a stock with \"bullish\" classification and mention that in the reason.
- Ignore stocks mentioned under 'stocks to buy today'.
"""


# ---------------------------------------------------------------------------
# User personas
# ---------------------------------------------------------------------------
# A persona tailors the sidebar so users only see tools relevant to how they
# trade/invest. Each persona maps to a set of sidebar group keys. The keys
# match the ``data-group`` attributes used by the sidebar groups in
# ``layout.html``. ``workspace``, ``markets``, ``ai`` and ``account`` are
# shared across every persona; only the discipline-specific group differs.

PERSONAS = {
    "trader": {
        "id": "trader",
        "label": "Day Trader",
        "short": "Trader",
        "icon": "fa-bolt",
        "tagline": "Fast intraday momentum",
        "desc": "Live scanners, volume alerts and market pulse built for "
                "quick same-day decisions.",
        "groups": ["workspace", "intraday", "markets", "ai", "account"],
    },
    "swing": {
        "id": "swing",
        "label": "Swing Trader",
        "short": "Swing",
        "icon": "fa-rocket",
        "tagline": "Multi-day positional moves",
        "desc": "Swing setups, options analytics and trend tools for trades "
                "that play out over days to weeks.",
        "groups": ["workspace", "swing", "markets", "ai", "account"],
    },
    "investor": {
        "id": "investor",
        "label": "Long-term Investor",
        "short": "Investor",
        "icon": "fa-seedling",
        "tagline": "Wealth building & fundamentals",
        "desc": "Fundamentals, mutual funds and portfolio analytics for "
                "building wealth over the long run.",
        "groups": ["workspace", "investing", "markets", "ai", "account"],
    },
}

# Groups that are always visible regardless of persona.
PERSONA_COMMON_GROUPS = ["workspace", "markets", "ai", "account"]

DEFAULT_PERSONA = "swing"


def get_persona(persona_id):
    """Return the persona dict for ``persona_id`` or ``None`` if unknown."""
    if not persona_id:
        return None
    return PERSONAS.get(str(persona_id).strip().lower())


def persona_groups(persona_id):
    """Return the list of visible sidebar group keys for a persona.

    Falls back to every group when the persona is unknown/unset so the
    sidebar never renders empty.
    """
    p = get_persona(persona_id)
    if not p:
        return ["workspace", "intraday", "swing", "investing",
                "markets", "ai", "account"]
    return p["groups"]
