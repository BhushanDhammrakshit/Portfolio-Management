"""Centralized email templates for all user segments.

Each template function returns an HTML string ready for ``email_service.send_email``.
The ``TEMPLATES`` catalogue maps a short key to metadata used by the admin UI
for preview and manual send.
"""
from __future__ import annotations

from application.config import APP_BASE_URL, APP_NAME

# ── Template catalogue ──────────────────────────────────────────────────

TEMPLATES: dict[str, dict] = {
    "welcome": {
        "key": "welcome",
        "name": "Welcome / Onboarding",
        "icon": "fa-hand-sparkles",
        "color": "#6366f1",
        "subject": f"Welcome to {APP_NAME} — let's get you started",
        "description": "Sent to new users right after signup & persona selection.",
        "target": "All new signups",
    },
    "feature_discovery": {
        "key": "feature_discovery",
        "name": "Feature Discovery",
        "icon": "fa-lightbulb",
        "color": "#0ea5e9",
        "subject": f"You're missing the best parts of {APP_NAME}",
        "description": "Nudges Pro/Elite users who haven't explored key tools yet.",
        "target": "Pro / Elite users (unused features)",
    },
    "winback": {
        "key": "winback",
        "name": "Win-back",
        "icon": "fa-heart-crack",
        "color": "#dc2626",
        "subject": "We saved your seat — come back with 20% off",
        "description": "Targets churned paid users 14+ days after expiry with a discount offer.",
        "target": "Churned paid users (14+ days expired)",
    },
    "re_engagement": {
        "key": "re_engagement",
        "name": "Re-engagement",
        "icon": "fa-bell",
        "color": "#f59e0b",
        "subject": f"The market moved — did you catch it? | {APP_NAME}",
        "description": "Reminds inactive users (7+ days) about what they're missing.",
        "target": "Any user inactive 7+ days",
    },
    "renewal_success": {
        "key": "renewal_success",
        "name": "Renewal Success",
        "icon": "fa-circle-check",
        "color": "#16a34a",
        "subject": f"You're all set — {APP_NAME} plan activated!",
        "description": "Confirmation email sent after successful payment/upgrade.",
        "target": "Users who just renewed / upgraded",
    },
    "weekly_swing": {
        "key": "weekly_swing",
        "name": "Weekly Digest — Swing Trader",
        "icon": "fa-chart-line",
        "color": "#7c3aed",
        "subject": f"Your weekly swing setups | {APP_NAME}",
        "description": "Weekly highlights for swing traders: breakouts, RS rankings, patterns.",
        "target": "Users with Swing Trader persona",
    },
    "weekly_intraday": {
        "key": "weekly_intraday",
        "name": "Weekly Digest — Intraday Trader",
        "icon": "fa-bolt",
        "color": "#ea580c",
        "subject": f"This week's intraday edge | {APP_NAME}",
        "description": "Weekly highlights for intraday traders: ORB picks, RVOL, gap forecasts.",
        "target": "Users with Intraday Trader persona",
    },
    "weekly_investor": {
        "key": "weekly_investor",
        "name": "Weekly Digest — Investor",
        "icon": "fa-building-columns",
        "color": "#0d9488",
        "subject": f"Your portfolio pulse this week | {APP_NAME}",
        "description": "Weekly highlights for long-term investors: fundamentals, MF, portfolio health.",
        "target": "Users with Investor persona",
    },
    "usage_summary": {
        "key": "usage_summary",
        "name": "Monthly Usage Summary",
        "icon": "fa-chart-pie",
        "color": "#6366f1",
        "subject": f"Your {APP_NAME} month in review",
        "description": "Monthly recap of AI tokens used, analyses run, tools accessed.",
        "target": "Pro / Elite users",
    },
    "usage_limit_warning": {
        "key": "usage_limit_warning",
        "name": "Usage Limit Warning",
        "icon": "fa-gauge-high",
        "color": "#dc2626",
        "subject": f"You've used 80% of your AI quota | {APP_NAME}",
        "description": "Alert when a user hits 80% of their monthly AI token quota.",
        "target": "Pro / Elite users nearing limits",
    },
    "broker_sync_failure": {
        "key": "broker_sync_failure",
        "name": "Broker Sync Failure",
        "icon": "fa-plug-circle-exclamation",
        "color": "#dc2626",
        "subject": f"Action needed — your broker connection expired | {APP_NAME}",
        "description": "Alert when a linked broker token (Fyers/Dhan/Upstox) expires.",
        "target": "Users with expired broker tokens",
    },
    "referral_reward": {
        "key": "referral_reward",
        "name": "Referral Reward",
        "icon": "fa-gift",
        "color": "#16a34a",
        "subject": f"Your referral bonus is here! | {APP_NAME}",
        "description": "Notification when a referred user signs up or converts to paid.",
        "target": "Users with active referrals",
    },
    "admin_new_signup": {
        "key": "admin_new_signup",
        "name": "Admin: New Signup Alert",
        "icon": "fa-user-plus",
        "color": "#16a34a",
        "subject": f"[FinanceCandle] New signup \u2014 {{name}}",
        "description": "Internal notification sent to the admin inbox whenever a new user registers.",
        "target": "Admin inbox (not sent to end users)",
        "admin_only": True,
    },
    "admin_feedback": {
        "key": "admin_feedback",
        "name": "Admin: Feedback Received",
        "icon": "fa-comment-dots",
        "color": "#6366f1",
        "subject": "[FinanceCandle Feedback] <category> \u2014 from <user>",
        "description": "Internal notification sent to the admin inbox whenever a user submits feedback.",
        "target": "Admin inbox (not sent to end users)",
        "admin_only": True,
    },
}


def _wrapper(gradient: str, tagline: str, headline: str, body_html: str,
             cta_text: str | None = None, cta_url: str | None = None,
             footer_note: str = "") -> str:
    """Shared email shell — keeps every template visually consistent."""
    cta_block = ""
    if cta_text and cta_url:
        cta_block = f"""\
        <div style="text-align:center;margin:24px 0">
          <a href="{cta_url}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;text-decoration:none;font-weight:700;font-size:15px;padding:14px 36px;border-radius:10px;box-shadow:0 4px 14px rgba(99,102,241,.35)">{cta_text} &#8594;</a>
        </div>"""
    footer = footer_note or f"You received this as a member of {APP_NAME}"
    billing_url = f"{APP_BASE_URL}/billing"
    return f"""\
<!doctype html>
<html><body style="margin:0;padding:0;background:#f6f9fc;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937">
  <div style="max-width:600px;margin:0 auto;padding:24px">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden">
      <div style="background:linear-gradient(135deg,{gradient});padding:28px;color:#fff;text-align:center">
        <div style="font-size:12px;opacity:.85;letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px">{tagline}</div>
        <div style="font-size:22px;font-weight:800;line-height:1.3">{headline}</div>
      </div>
      <div style="padding:28px">
{body_html}
{cta_block}
      </div>
      <div style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:16px 28px;text-align:center">
        <p style="margin:0;font-size:11px;color:#d1d5db;line-height:1.5">
          &copy; {APP_NAME} &middot; {footer} &middot;
          <a href="{billing_url}" style="color:#6366f1;text-decoration:none">Manage subscription</a>
        </p>
      </div>
    </div>
  </div>
</body></html>"""


# ── Individual templates ────────────────────────────────────────────────

def welcome_html(name: str, persona: str = "") -> str:
    safe_name = (name or "there").split("@")[0][:60]
    persona_tip = ""
    if persona:
        persona_tip = f"""\
        <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;padding:14px 18px;margin:0 0 18px">
          <div style="font-size:13px;font-weight:700;color:#0369a1;margin-bottom:4px">&#127919; Your persona: {persona}</div>
          <div style="font-size:12px;color:#0c4a6e">We've tailored your dashboard to focus on what matters most to you.</div>
        </div>"""
    return _wrapper(
        gradient="#6366f1 0%,#4f46e5 100%",
        tagline="Welcome aboard",
        headline=f"Hey {safe_name}, let's build your edge",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Welcome to <strong>{APP_NAME}</strong>! You now have access to AI-powered stock analysis,
          real-time market tools, and a portfolio tracker built for Indian markets.
        </p>
{persona_tip}
        <p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6366f1;margin:0 0 12px">Get started in 3 steps</p>
        <table style="width:100%;border-collapse:collapse;margin:0 0 20px;font-size:13px">
          <tr><td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:700;color:#6366f1;width:36px">1</td><td style="padding:10px 14px;border:1px solid #e5e7eb">Add your holdings &mdash; manual entry, CSV, or broker sync</td></tr>
          <tr><td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:700;color:#6366f1">2</td><td style="padding:10px 14px;border:1px solid #e5e7eb">Run your first AI analysis on any stock</td></tr>
          <tr><td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:700;color:#6366f1">3</td><td style="padding:10px 14px;border:1px solid #e5e7eb">Explore the tools &mdash; heatmap, MF advisor, swing scanner</td></tr>
        </table>""",
        cta_text="Go to Dashboard",
        cta_url=f"{APP_BASE_URL}/home",
        footer_note=f"You received this because you just signed up for {APP_NAME}",
    )


def feature_discovery_html(name: str, plan_name: str = "Pro") -> str:
    safe_name = (name or "there").split("@")[0][:60]
    return _wrapper(
        gradient="#0ea5e9 0%,#0284c7 100%",
        tagline="Did you know?",
        headline=f"You're on {plan_name} — here's what you haven't tried yet",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          You're on the <strong>{plan_name}</strong> plan &mdash; but you haven't explored some of the most
          powerful tools available to you. Here's what other {plan_name} users love:
        </p>
        <ul style="font-size:14px;line-height:1.8;color:#374151;padding-left:20px;margin:0 0 20px">
          <li><strong>Swing Scanner</strong> &mdash; find breakout setups with RS rankings</li>
          <li><strong>ORB Scanner</strong> &mdash; real-time opening-range breakout picks</li>
          <li><strong>AI Chat</strong> &mdash; ask anything about any stock, get instant analysis</li>
          <li><strong>Fundamentals Lookup</strong> &mdash; deep-dive financial metrics</li>
          <li><strong>RVOL Heatmap</strong> &mdash; spot unusual volume in real time</li>
        </ul>
        <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;padding:14px 18px;margin:0 0 18px;text-align:center">
          <div style="font-size:14px;color:#0369a1;font-weight:600">&#128161; Pro tip: Start with the Swing Scanner &mdash; it runs in 10 seconds</div>
        </div>""",
        cta_text="Explore Tools",
        cta_url=f"{APP_BASE_URL}/home",
    )


def winback_html(name: str, plan_name: str = "Pro") -> str:
    safe_name = (name or "there").split("@")[0][:60]
    return _wrapper(
        gradient="#dc2626 0%,#b91c1c 100%",
        tagline="We miss you",
        headline="Come back with 20% off your next plan",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          It's been a while since your <strong>{plan_name}</strong> plan ended. Markets don't stop
          &mdash; and neither should your edge. We'd love to have you back.
        </p>
        <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:18px;margin:0 0 20px;text-align:center">
          <div style="font-size:20px;font-weight:800;color:#dc2626;margin-bottom:4px">COMEBACK20</div>
          <div style="font-size:13px;color:#991b1b">Use this code at checkout for <strong>20% off</strong> any plan</div>
          <div style="font-size:11px;color:#b91c1c;margin-top:6px">Valid for 7 days from this email</div>
        </div>
        <p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6366f1;margin:0 0 12px">What's new since you left</p>
        <ul style="font-size:14px;line-height:1.8;color:#374151;padding-left:20px;margin:0 0 20px">
          <li>Improved AI analysis with deeper fundamentals</li>
          <li>New F&amp;O gap forecast model with higher accuracy</li>
          <li>Options OI buildup visualizations</li>
          <li>Faster multi-broker sync</li>
        </ul>""",
        cta_text="Reactivate My Plan",
        cta_url=f"{APP_BASE_URL}/billing",
        footer_note=f"You received this because your {APP_NAME} subscription expired recently",
    )


def re_engagement_html(name: str, days_inactive: int = 7) -> str:
    safe_name = (name or "there").split("@")[0][:60]
    return _wrapper(
        gradient="#f59e0b 0%,#d97706 100%",
        tagline="Don't miss out",
        headline="Markets moved while you were away",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          It's been {days_inactive} days since your last visit. In that time, markets have made
          big moves. Here's what you might have missed:
        </p>
        <ul style="font-size:14px;line-height:1.8;color:#374151;padding-left:20px;margin:0 0 20px">
          <li>Nifty moved significantly &mdash; key sectors rotated</li>
          <li>New breakout setups appeared on the swing scanner</li>
          <li>Options OI data shows interesting buildup patterns</li>
          <li>Your portfolio may need a health check</li>
        </ul>
        <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:14px 18px;margin:0 0 18px;text-align:center">
          <div style="font-size:14px;color:#92400e;font-weight:600">&#9200; A quick 2-minute check-in can save you from a bad trade</div>
        </div>""",
        cta_text="Check My Portfolio",
        cta_url=f"{APP_BASE_URL}/home",
        footer_note=f"You received this because you haven't logged into {APP_NAME} recently",
    )


def renewal_success_html(name: str, plan_name: str = "Pro",
                         expires_on: str = "") -> str:
    safe_name = (name or "there").split("@")[0][:60]
    exp_line = ""
    if expires_on:
        exp_line = f'<div class="hc-row"><span style="font-size:13px;color:#374151"><strong>Valid until:</strong> {expires_on}</span></div>'
    return _wrapper(
        gradient="#16a34a 0%,#15803d 100%",
        tagline="Payment confirmed",
        headline=f"Your {plan_name} plan is now active!",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Your <strong>{plan_name}</strong> plan is confirmed and active. You now have full access
          to all {plan_name}-tier tools and features.
        </p>
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:18px;margin:0 0 20px;text-align:center">
          <div style="font-size:28px;margin-bottom:8px">&#9989;</div>
          <div style="font-size:16px;font-weight:800;color:#16a34a;margin-bottom:4px">{plan_name} Plan — Active</div>
          {exp_line}
        </div>
        <p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6366f1;margin:0 0 12px">What's included</p>
        <ul style="font-size:14px;line-height:1.8;color:#374151;padding-left:20px;margin:0 0 20px">
          <li>Unlimited AI stock analysis</li>
          <li>Swing &amp; intraday tools</li>
          <li>Multi-broker sync (Fyers, Dhan, Upstox)</li>
          <li>Unlimited portfolio holdings</li>
          <li>F&amp;O gap forecasts &amp; options analytics</li>
        </ul>""",
        cta_text="Start Trading Smarter",
        cta_url=f"{APP_BASE_URL}/home",
        footer_note=f"You received this because you upgraded your {APP_NAME} subscription",
    )


def weekly_swing_html(name: str) -> str:
    safe_name = (name or "Trader").split("@")[0][:60]
    return _wrapper(
        gradient="#7c3aed 0%,#6d28d9 100%",
        tagline="Weekly swing digest",
        headline="This week's top breakout setups",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Here's your weekly swing trading digest. These are the setups our scanner
          flagged this week — tightening ranges, relative strength leaders, and chart patterns.
        </p>
        <p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#7c3aed;margin:0 0 12px">Top setups this week</p>
        <table style="width:100%;border-collapse:collapse;margin:0 0 20px;font-size:13px">
          <tr style="background:#f8fafc">
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:700;color:#374151">Tool</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:700;color:#374151">What to look for</td>
          </tr>
          <tr>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:600;color:#7c3aed">Breakout Scanner</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Stocks breaking out of consolidation zones</td>
          </tr>
          <tr style="background:#f8fafc">
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:600;color:#7c3aed">RS Rankings</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Relative strength leaders vs Nifty</td>
          </tr>
          <tr>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:600;color:#7c3aed">Pattern Scanner</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Cup &amp; handle, ascending triangles, flags</td>
          </tr>
          <tr style="background:#f8fafc">
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:600;color:#7c3aed">52-Week High</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Stocks within 5% of their yearly high</td>
          </tr>
        </table>
        <div style="background:#faf5ff;border:1px solid #e9d5ff;border-radius:10px;padding:14px 18px;margin:0 0 18px;text-align:center">
          <div style="font-size:14px;color:#6b21a8;font-weight:600">&#128200; Log in to see live picks for each scanner</div>
        </div>""",
        cta_text="View Swing Setups",
        cta_url=f"{APP_BASE_URL}/home",
        footer_note=f"You received this as a swing trader on {APP_NAME}",
    )


def weekly_intraday_html(name: str) -> str:
    safe_name = (name or "Trader").split("@")[0][:60]
    return _wrapper(
        gradient="#ea580c 0%,#c2410c 100%",
        tagline="Weekly intraday digest",
        headline="Your intraday edge this week",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          This week's intraday tools digest — covering ORB scanner picks, unusual volume spikes,
          and gap forecast performance.
        </p>
        <p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#ea580c;margin:0 0 12px">This week's highlights</p>
        <table style="width:100%;border-collapse:collapse;margin:0 0 20px;font-size:13px">
          <tr style="background:#f8fafc">
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:700;color:#374151">Tool</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:700;color:#374151">What it told you</td>
          </tr>
          <tr>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:600;color:#ea580c">ORB Scanner</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Opening range breakout picks for F&amp;O stocks</td>
          </tr>
          <tr style="background:#f8fafc">
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:600;color:#ea580c">RVOL Heatmap</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Stocks with 2x+ relative volume surge</td>
          </tr>
          <tr>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:600;color:#ea580c">Gap Forecast</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Predicted next-day Nifty gap direction</td>
          </tr>
        </table>
        <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:14px 18px;margin:0 0 18px;text-align:center">
          <div style="font-size:14px;color:#9a3412;font-weight:600">&#9889; ORB Scanner runs live at 9:20 AM — don't miss tomorrow's picks</div>
        </div>""",
        cta_text="Open Intraday Tools",
        cta_url=f"{APP_BASE_URL}/home",
        footer_note=f"You received this as an intraday trader on {APP_NAME}",
    )


def weekly_investor_html(name: str) -> str:
    safe_name = (name or "Investor").split("@")[0][:60]
    return _wrapper(
        gradient="#0d9488 0%,#0f766e 100%",
        tagline="Weekly portfolio pulse",
        headline="How your portfolio is doing this week",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Here's your weekly investing pulse — a quick overview of what matters for
          your long-term portfolio this week.
        </p>
        <p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#0d9488;margin:0 0 12px">Check these this week</p>
        <ul style="font-size:14px;line-height:1.8;color:#374151;padding-left:20px;margin:0 0 20px">
          <li><strong>Portfolio Health</strong> &mdash; review sector allocation &amp; concentration risk</li>
          <li><strong>Fundamentals Lookup</strong> &mdash; check valuations on your top holdings</li>
          <li><strong>MF Advisor</strong> &mdash; see if any mutual fund rebalancing is due</li>
          <li><strong>Sector Heatmap</strong> &mdash; which sectors are leading &amp; lagging</li>
          <li><strong>Tender Calendar</strong> &mdash; upcoming corporate actions</li>
        </ul>
        <div style="background:#f0fdfa;border:1px solid #99f6e4;border-radius:10px;padding:14px 18px;margin:0 0 18px;text-align:center">
          <div style="font-size:14px;color:#115e59;font-weight:600">&#128202; Long-term winners check their portfolio weekly &mdash; take 5 minutes today</div>
        </div>""",
        cta_text="View My Portfolio",
        cta_url=f"{APP_BASE_URL}/home",
        footer_note=f"You received this as an investor on {APP_NAME}",
    )


def usage_summary_html(name: str, plan_name: str = "Pro",
                       ai_used: int = 0, ai_limit: int = 100,
                       tokens_used: int = 0, tokens_limit: int = 100000) -> str:
    safe_name = (name or "there").split("@")[0][:60]
    ai_pct = min(100, round(ai_used / max(ai_limit, 1) * 100))
    tok_pct = min(100, round(tokens_used / max(tokens_limit, 1) * 100))
    tok_k = f"{tokens_used // 1000}K" if tokens_used >= 1000 else str(tokens_used)
    tok_lim_k = f"{tokens_limit // 1000}K" if tokens_limit >= 1000 else str(tokens_limit)
    return _wrapper(
        gradient="#6366f1 0%,#4f46e5 100%",
        tagline="Monthly recap",
        headline=f"Your {APP_NAME} month in numbers",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Here's how you used {APP_NAME} this month on your <strong>{plan_name}</strong> plan:
        </p>
        <table style="width:100%;border-collapse:collapse;margin:0 0 20px;font-size:13px">
          <tr style="background:#f8fafc">
            <td style="padding:12px 14px;border:1px solid #e5e7eb;font-weight:700;color:#374151">Metric</td>
            <td style="padding:12px 14px;border:1px solid #e5e7eb;font-weight:700;color:#374151;text-align:center">Used</td>
            <td style="padding:12px 14px;border:1px solid #e5e7eb;font-weight:700;color:#374151;text-align:center">Limit</td>
            <td style="padding:12px 14px;border:1px solid #e5e7eb;font-weight:700;color:#374151;text-align:center">Usage</td>
          </tr>
          <tr>
            <td style="padding:12px 14px;border:1px solid #e5e7eb">AI Analyses</td>
            <td style="padding:12px 14px;border:1px solid #e5e7eb;text-align:center;font-weight:700">{ai_used}</td>
            <td style="padding:12px 14px;border:1px solid #e5e7eb;text-align:center;color:#64748b">{ai_limit}</td>
            <td style="padding:12px 14px;border:1px solid #e5e7eb;text-align:center;font-weight:700;color:{'#dc2626' if ai_pct > 80 else '#16a34a'}">{ai_pct}%</td>
          </tr>
          <tr style="background:#f8fafc">
            <td style="padding:12px 14px;border:1px solid #e5e7eb">AI Tokens</td>
            <td style="padding:12px 14px;border:1px solid #e5e7eb;text-align:center;font-weight:700">{tok_k}</td>
            <td style="padding:12px 14px;border:1px solid #e5e7eb;text-align:center;color:#64748b">{tok_lim_k}</td>
            <td style="padding:12px 14px;border:1px solid #e5e7eb;text-align:center;font-weight:700;color:{'#dc2626' if tok_pct > 80 else '#16a34a'}">{tok_pct}%</td>
          </tr>
        </table>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Counters reset at the start of each calendar month. Keep using {APP_NAME} to
          get the most out of your subscription!
        </p>""",
        cta_text="View Dashboard",
        cta_url=f"{APP_BASE_URL}/home",
        footer_note=f"You received this as a {plan_name} member of {APP_NAME}",
    )


def usage_limit_warning_html(name: str, plan_name: str = "Pro",
                              usage_pct: int = 80) -> str:
    safe_name = (name or "there").split("@")[0][:60]
    return _wrapper(
        gradient="#dc2626 0%,#b91c1c 100%",
        tagline="Usage alert",
        headline=f"You've used {usage_pct}% of your monthly AI quota",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          You're approaching the monthly AI analysis limit on your <strong>{plan_name}</strong> plan.
          Once you hit the cap, AI-powered tools will be paused until next month.
        </p>
        <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:18px;margin:0 0 20px;text-align:center">
          <div style="font-size:24px;font-weight:800;color:#dc2626;margin-bottom:4px">{usage_pct}%</div>
          <div style="font-size:13px;color:#991b1b">of your monthly AI quota used</div>
          <div style="background:#e5e7eb;border-radius:6px;height:8px;margin:12px 0 0;overflow:hidden">
            <div style="background:linear-gradient(90deg,#f59e0b,#dc2626);height:100%;width:{usage_pct}%;border-radius:6px"></div>
          </div>
        </div>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          <strong>Options:</strong> Upgrade to Elite for 5x the quota, or wait for the
          monthly reset. Your other tools (heatmap, swing scanner, etc.) are unaffected.
        </p>""",
        cta_text="Upgrade for More",
        cta_url=f"{APP_BASE_URL}/billing",
        footer_note=f"You received this because your AI usage is nearing the limit on {APP_NAME}",
    )


def broker_sync_failure_html(name: str, broker_name: str = "Fyers") -> str:
    safe_name = (name or "there").split("@")[0][:60]
    return _wrapper(
        gradient="#dc2626 0%,#991b1b 100%",
        tagline="Action required",
        headline=f"Your {broker_name} connection has expired",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Your <strong>{broker_name}</strong> broker token has expired. This means {APP_NAME}
          can no longer sync your live holdings and positions from {broker_name}.
        </p>
        <div style="background:#fef2f2;border-left:4px solid #dc2626;border-radius:8px;padding:14px 18px;margin:0 0 20px">
          <div style="font-size:14px;font-weight:700;color:#991b1b;margin:0 0 2px">&#9888; {broker_name} sync is paused</div>
          <div style="font-size:13px;color:#7f1d1d">Re-authenticate to resume auto-sync of holdings &amp; P&amp;L.</div>
        </div>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          This takes less than 30 seconds — just log in and re-link your {broker_name} account.
          Your historical data is safe.
        </p>""",
        cta_text=f"Re-connect {broker_name}",
        cta_url=f"{APP_BASE_URL}/home",
        footer_note=f"You received this because your broker token expired on {APP_NAME}",
    )


def referral_reward_html(name: str, referred_name: str = "a friend",
                          reward: str = "7 days of Pro") -> str:
    safe_name = (name or "there").split("@")[0][:60]
    return _wrapper(
        gradient="#16a34a 0%,#15803d 100%",
        tagline="Referral bonus",
        headline="Your referral paid off!",
        body_html=f"""\
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Great news! <strong>{referred_name}</strong> just joined {APP_NAME} using your referral link.
          As a thank-you, here's your reward:
        </p>
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:18px;margin:0 0 20px;text-align:center">
          <div style="font-size:28px;margin-bottom:8px">&#127881;</div>
          <div style="font-size:18px;font-weight:800;color:#16a34a;margin-bottom:4px">{reward}</div>
          <div style="font-size:13px;color:#166534">has been added to your account</div>
        </div>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Keep sharing your referral link to earn more rewards. Every friend who joins
          earns you extra Pro days!
        </p>""",
        cta_text="Share More & Earn",
        cta_url=f"{APP_BASE_URL}/home",
        footer_note=f"You received this because your referral signed up for {APP_NAME}",
    )


# ── Admin-only notification templates (internal inbox, not end users) ───

def admin_new_signup_html(name: str, email: str, phone: str = "", gender: str = "",
                          location: str = "", ref_code: str = "",
                          sent_at: str = "") -> str:
    """Notify the admin inbox that a new user just registered."""
    from html import escape as _esc

    dash = "\u2014"
    safe_name = _esc(name or "")
    safe_email = _esc(email or "")
    safe_phone = _esc(phone or "") or dash
    safe_gender = _esc(gender or "") or dash
    safe_location = _esc(location or "") or dash
    safe_ref = _esc(ref_code or "organic")
    first_name = safe_name.split()[0] if safe_name else "them"

    return f"""\
<!doctype html>
<html><body style="margin:0;padding:0;background:#f6f9fc;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937">
  <div style="max-width:560px;margin:0 auto;padding:24px">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#16a34a 0%,#22c55e 100%);padding:22px 28px;color:#fff">
        <div style="font-size:13px;opacity:.85;letter-spacing:.08em;text-transform:uppercase">New Signup</div>
        <div style="font-size:22px;font-weight:800;margin-top:4px">{APP_NAME}</div>
      </div>
      <div style="padding:26px 28px">
        <span style="display:inline-block;padding:4px 12px;border-radius:999px;background:#16a34a1a;color:#16a34a;font-weight:700;font-size:12px">&#127881; New User Registered</span>
        <table style="border-collapse:collapse;font-size:14px;margin:16px 0 4px;width:100%">
          <tr><td style="padding:4px 12px 4px 0;font-weight:700;color:#6b7280;white-space:nowrap">Name</td><td>{safe_name}</td></tr>
          <tr><td style="padding:4px 12px 4px 0;font-weight:700;color:#6b7280;white-space:nowrap">Email</td><td>{safe_email}</td></tr>
          <tr><td style="padding:4px 12px 4px 0;font-weight:700;color:#6b7280;white-space:nowrap">Phone</td><td>{safe_phone}</td></tr>
          <tr><td style="padding:4px 12px 4px 0;font-weight:700;color:#6b7280;white-space:nowrap">Gender</td><td>{safe_gender}</td></tr>
          <tr><td style="padding:4px 12px 4px 0;font-weight:700;color:#6b7280;white-space:nowrap">Location</td><td>{safe_location}</td></tr>
          <tr><td style="padding:4px 12px 4px 0;font-weight:700;color:#6b7280;white-space:nowrap">Referral</td><td>{safe_ref}</td></tr>
          <tr><td style="padding:4px 12px 4px 0;font-weight:700;color:#6b7280;white-space:nowrap">Sent</td><td>{sent_at}</td></tr>
        </table>
        <div style="text-align:center;margin:22px 0 6px">
          <a href="mailto:{safe_email}?subject=Welcome%20to%20{APP_NAME}"
             style="display:inline-block;padding:10px 22px;background:#16a34a;color:#fff;text-decoration:none;border-radius:8px;font-weight:700;font-size:14px">
            Say hi to {first_name}
          </a>
        </div>
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0 14px">
        <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.55">
          Automated notification from the {APP_NAME} signup flow.
        </p>
      </div>
    </div>
  </div>
</body></html>"""


def admin_feedback_html(name: str, email: str, category: str, message: str,
                        sent_at: str = "") -> str:
    """Notify the admin inbox that a user submitted feedback."""
    from html import escape as _esc

    cat_colors = {
        "bug": "#dc2626", "feature": "#0ea5e9", "general": "#6366f1",
        "praise": "#16a34a", "other": "#6b7280",
    }
    cat_color = cat_colors.get((category or "").lower(), "#6366f1")

    safe_name = _esc(name or "")
    safe_email = _esc(email or "")
    safe_category = _esc(category or "general")
    safe_message = _esc(message or "")
    first_name = safe_name.split()[0] if safe_name and safe_name != "Unknown" else "user"

    return f"""\
<!doctype html>
<html><body style="margin:0;padding:0;background:#f6f9fc;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937">
  <div style="max-width:560px;margin:0 auto;padding:24px">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#1e40af 0%,#2563eb 100%);padding:22px 28px;color:#fff">
        <div style="font-size:13px;opacity:.85;letter-spacing:.08em;text-transform:uppercase">New Feedback</div>
        <div style="font-size:22px;font-weight:800;margin-top:4px">{APP_NAME}</div>
      </div>
      <div style="padding:26px 28px">
        <span style="display:inline-block;padding:4px 12px;border-radius:999px;background:{cat_color}1a;color:{cat_color};font-weight:700;font-size:12px;text-transform:capitalize">{safe_category}</span>
        <table style="border-collapse:collapse;font-size:14px;margin:16px 0 4px;width:100%">
          <tr><td style="padding:4px 12px 4px 0;font-weight:700;color:#6b7280;white-space:nowrap">From</td><td>{safe_name} &lt;{safe_email}&gt;</td></tr>
          <tr><td style="padding:4px 12px 4px 0;font-weight:700;color:#6b7280;white-space:nowrap">Sent</td><td>{sent_at}</td></tr>
        </table>
        <div style="margin:18px 0;padding:16px 18px;background:#f3f4f6;border-left:4px solid {cat_color};border-radius:8px;white-space:pre-wrap;font-size:14px;line-height:1.6;color:#111827">{safe_message}</div>
        <div style="text-align:center;margin:22px 0 6px">
          <a href="mailto:{safe_email}?subject=Re:%20Your%20feedback%20on%20{APP_NAME}"
             style="display:inline-block;padding:10px 22px;background:#2563eb;color:#fff;text-decoration:none;border-radius:8px;font-weight:700;font-size:14px">
            Reply to {first_name}
          </a>
        </div>
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0 14px">
        <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.55">
          Automated notification from the {APP_NAME} feedback widget.
        </p>
      </div>
    </div>
  </div>
</body></html>"""


# ── Preview helper ──────────────────────────────────────────────────────

def preview_html(template_key: str) -> str | None:
    """Return a sample HTML preview for a template key, or None if unknown."""
    builders = {
        "welcome": lambda: welcome_html("Rahul", "Swing Trader"),
        "feature_discovery": lambda: feature_discovery_html("Priya", "Pro"),
        "winback": lambda: winback_html("Arjun", "Elite"),
        "re_engagement": lambda: re_engagement_html("Sneha", 10),
        "renewal_success": lambda: renewal_success_html("Rahul", "Pro", "15 Sep 2026"),
        "weekly_swing": lambda: weekly_swing_html("Arjun"),
        "weekly_intraday": lambda: weekly_intraday_html("Priya"),
        "weekly_investor": lambda: weekly_investor_html("Sneha"),
        "usage_summary": lambda: usage_summary_html("Rahul", "Pro", 72, 100, 68000, 100000),
        "usage_limit_warning": lambda: usage_limit_warning_html("Priya", "Pro", 82),
        "broker_sync_failure": lambda: broker_sync_failure_html("Arjun", "Fyers"),
        "referral_reward": lambda: referral_reward_html("Sneha", "Rahul Sharma", "7 days of Pro"),
        "admin_new_signup": lambda: admin_new_signup_html(
            "Raghavendra", "drraghavendrabhat80@gmail.com", "9844333775",
            "Male", "Bangalore", "organic", "09 Aug 2026, 08:33 AM IST"),
        "admin_feedback": lambda: admin_feedback_html(
            "Priya Sharma", "priya@example.com", "feature",
            "It would be great to get a dark mode option for the swing scanner page!",
            "09 Aug 2026, 08:33 AM IST"),
    }
    fn = builders.get(template_key)
    return fn() if fn else None
