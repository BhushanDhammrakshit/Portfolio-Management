"""Cross-cutting tools: Watchlist, Alerts, Strategy Builder, AI Idea-of-the-Day.

All state is persisted via the shared cache module (Redis when configured,
in-process dict fallback). State is keyed by user email.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import uuid
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from application.services import cache as shared_cache, market_data

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _safe_float(v, default=0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _display_name(symbol: str) -> str:
    return symbol.replace(".NS", "").replace(".BO", "").replace("^", "")


def _canon(sym: str) -> str:
    sym = (sym or "").strip().upper()
    if not sym:
        return ""
    if "." not in sym and not sym.startswith("^"):
        sym += ".NS"
    return sym


# ═══════════════════════════════════════════════════════════════════════
# Watchlist
# ═══════════════════════════════════════════════════════════════════════

def _watchlist_key(email: str) -> str:
    return f"watchlist:{email.lower()}"


VALID_STYLES = {"intraday", "swing", "investing"}


def get_watchlist(email: str) -> List[Dict[str, Any]]:
    items = shared_cache.jget(_watchlist_key(email)) or []
    return items if isinstance(items, list) else []


def _save_watchlist(email: str, items: List[Dict[str, Any]]):
    # Long TTL (30d) — refresh on every save for persistence
    shared_cache.jset(_watchlist_key(email), items, ttl=60 * 60 * 24 * 30)


def add_to_watchlist(email: str, symbol: str, style: str, note: str = "") -> Dict[str, Any]:
    sym = _canon(symbol)
    if not sym:
        return {"error": "symbol required"}
    if style not in VALID_STYLES:
        return {"error": f"style must be one of {sorted(VALID_STYLES)}"}
    items = get_watchlist(email)
    # Dedupe per (symbol, style)
    items = [i for i in items if not (i.get("symbol") == sym and i.get("style") == style)]
    items.append({
        "id": str(uuid.uuid4())[:8],
        "symbol": sym, "name": _display_name(sym),
        "style": style, "note": note[:200],
        "added_at": _dt.datetime.now(_IST).isoformat(timespec="seconds"),
    })
    _save_watchlist(email, items)
    return {"ok": True, "watchlist": items}


def remove_from_watchlist(email: str, item_id: str) -> Dict[str, Any]:
    items = get_watchlist(email)
    items = [i for i in items if i.get("id") != item_id]
    _save_watchlist(email, items)
    return {"ok": True, "watchlist": items}


def watchlist_quotes(email: str) -> Dict[str, Any]:
    items = get_watchlist(email)
    if not items:
        return {"items": [], "by_style": {"intraday": [], "swing": [], "investing": []}}
    enriched = []
    for it in items:
        sym = it["symbol"]
        try:
            df = market_data.get_history(sym, days=10, interval="1d")
            if df is not None and not df.empty:
                ltp = _safe_float(df["Close"].iloc[-1])
                prev = _safe_float(df["Close"].iloc[-2]) if len(df) > 1 else ltp
                chg_pct = (ltp - prev) / prev * 100 if prev > 0 else 0
                enriched.append({**it, "ltp": round(ltp, 2),
                                 "change_pct": round(chg_pct, 2)})
                continue
        except Exception:
            pass
        enriched.append({**it, "ltp": None, "change_pct": None})
    by_style: Dict[str, List[Dict[str, Any]]] = {"intraday": [], "swing": [], "investing": []}
    for e in enriched:
        by_style.setdefault(e["style"], []).append(e)
    return {"items": enriched, "by_style": by_style, "count": len(enriched)}


# ═══════════════════════════════════════════════════════════════════════
# Alert Center
# ═══════════════════════════════════════════════════════════════════════

def _alerts_key(email: str) -> str:
    return f"alerts:{email.lower()}"


VALID_ALERT_TYPES = {"price_above", "price_below", "pct_change_above", "pct_change_below",
                     "volume_spike", "rsi_above", "rsi_below"}


def get_alerts(email: str) -> List[Dict[str, Any]]:
    items = shared_cache.jget(_alerts_key(email)) or []
    return items if isinstance(items, list) else []


def _save_alerts(email: str, items: List[Dict[str, Any]]):
    shared_cache.jset(_alerts_key(email), items, ttl=60 * 60 * 24 * 30)


def add_alert(email: str, symbol: str, alert_type: str, threshold: float,
              note: str = "") -> Dict[str, Any]:
    sym = _canon(symbol)
    if not sym:
        return {"error": "symbol required"}
    if alert_type not in VALID_ALERT_TYPES:
        return {"error": f"alert_type must be one of {sorted(VALID_ALERT_TYPES)}"}
    items = get_alerts(email)
    items.append({
        "id": str(uuid.uuid4())[:8],
        "symbol": sym, "name": _display_name(sym),
        "alert_type": alert_type,
        "threshold": float(threshold),
        "note": note[:200],
        "status": "armed",
        "triggered_at": None,
        "triggered_value": None,
        "created_at": _dt.datetime.now(_IST).isoformat(timespec="seconds"),
    })
    _save_alerts(email, items)
    return {"ok": True, "alerts": items}


def remove_alert(email: str, item_id: str) -> Dict[str, Any]:
    items = get_alerts(email)
    items = [i for i in items if i.get("id") != item_id]
    _save_alerts(email, items)
    return {"ok": True, "alerts": items}


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    up = delta.clip(lower=0); dn = -delta.clip(upper=0)
    rs = up.ewm(alpha=1/n, adjust=False).mean() / dn.ewm(alpha=1/n, adjust=False).mean().replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _evaluate_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    sym = alert["symbol"]
    typ = alert["alert_type"]
    thr = alert["threshold"]
    try:
        df = market_data.get_history(sym, days=40, interval="1d")
        if df is None or df.empty:
            return {**alert, "current_value": None, "would_trigger": False, "fetch_error": True}
        close = df["Close"].astype(float)
        ltp = _safe_float(close.iloc[-1])
        prev = _safe_float(close.iloc[-2]) if len(close) > 1 else ltp
        chg_pct = (ltp - prev) / prev * 100 if prev > 0 else 0
        current = None
        trigger = False
        if typ == "price_above":
            current = ltp; trigger = ltp >= thr
        elif typ == "price_below":
            current = ltp; trigger = ltp <= thr
        elif typ == "pct_change_above":
            current = chg_pct; trigger = chg_pct >= thr
        elif typ == "pct_change_below":
            current = chg_pct; trigger = chg_pct <= thr
        elif typ == "volume_spike":
            vol = df["Volume"].astype(float)
            today = _safe_float(vol.iloc[-1])
            base = vol.iloc[-21:-1].mean() if len(vol) > 21 else vol.mean()
            ratio = today / base if base > 0 else 1
            current = round(ratio, 2); trigger = ratio >= thr
        elif typ == "rsi_above":
            rsi = _safe_float(_rsi(close).iloc[-1]); current = round(rsi, 1); trigger = rsi >= thr
        elif typ == "rsi_below":
            rsi = _safe_float(_rsi(close).iloc[-1]); current = round(rsi, 1); trigger = rsi <= thr
        return {**alert, "current_value": current, "would_trigger": bool(trigger), "ltp": round(ltp, 2)}
    except Exception as e:
        return {**alert, "current_value": None, "would_trigger": False, "error": str(e)[:80]}


def evaluate_alerts(email: str) -> Dict[str, Any]:
    items = get_alerts(email)
    if not items:
        return {"alerts": [], "active_triggers": 0}
    out = []
    triggered_count = 0
    for a in items:
        ev = _evaluate_alert(a)
        if ev.get("would_trigger") and a.get("status") == "armed":
            ev["status"] = "triggered"
            ev["triggered_at"] = _dt.datetime.now(_IST).isoformat(timespec="seconds")
            ev["triggered_value"] = ev.get("current_value")
            triggered_count += 1
        out.append(ev)
    # Persist updated statuses
    _save_alerts(email, out)
    return {"alerts": out, "active_triggers": triggered_count,
            "armed": sum(1 for a in out if a.get("status") == "armed"),
            "triggered": sum(1 for a in out if a.get("status") == "triggered"),
            "scan_time": _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")}


def acknowledge_alert(email: str, item_id: str) -> Dict[str, Any]:
    """Reset a triggered alert back to 'armed'."""
    items = get_alerts(email)
    for it in items:
        if it.get("id") == item_id:
            it["status"] = "armed"
            it["triggered_at"] = None
            it["triggered_value"] = None
    _save_alerts(email, items)
    return {"ok": True, "alerts": items}


# ═══════════════════════════════════════════════════════════════════════
# Strategy Builder (no-code rule composer)
# ═══════════════════════════════════════════════════════════════════════
# A rule = {"metric": "rsi"|"vol_ratio"|"price_vs_ema20"|"price_vs_ema50"|
#                     "price_vs_vwap"|"day_change_pct"|"distance_52wh_pct",
#           "op": ">"|"<"|">="|"<="|"==",
#           "value": float}

VALID_METRICS = {
    "rsi", "vol_ratio", "price_vs_ema20", "price_vs_ema50",
    "price_vs_vwap", "day_change_pct", "distance_52wh_pct",
}
VALID_OPS = {">", "<", ">=", "<=", "=="}


def _metric_values(symbol: str) -> Optional[Dict[str, float]]:
    """Compute all metric values for one symbol."""
    try:
        df = market_data.get_history(symbol, days=260, interval="1d")
        if df is None or df.empty or len(df) < 50:
            return None
        close = df["Close"].astype(float); vol = df["Volume"].astype(float)
        ltp = _safe_float(close.iloc[-1]); prev = _safe_float(close.iloc[-2])
        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        rsi_val = _safe_float(_rsi(close).iloc[-1])
        today_v = _safe_float(vol.iloc[-1])
        base_v = vol.iloc[-21:-1].mean() if len(vol) > 21 else vol.mean()
        vol_ratio = today_v / base_v if base_v > 0 else 1.0
        day_chg = (ltp - prev) / prev * 100 if prev > 0 else 0
        hh52 = _safe_float(df["High"].astype(float).max())
        dist_52wh = (hh52 - ltp) / hh52 * 100 if hh52 > 0 else 100
        # VWAP from 5m today
        vwap_pct = 0.0
        try:
            intra = market_data.get_history(symbol, days=2, interval="5m")
            if intra is not None and not intra.empty:
                today = intra.index.max().date() if hasattr(intra.index, "date") else None
                tdf = intra[intra.index.date == today] if today else intra.tail(75)
                if not tdf.empty:
                    tp = (tdf["High"] + tdf["Low"] + tdf["Close"]) / 3
                    vw = (tp * tdf["Volume"]).cumsum() / tdf["Volume"].cumsum().replace(0, np.nan)
                    last_vw = _safe_float(vw.iloc[-1])
                    last_close = _safe_float(tdf.iloc[-1]["Close"])
                    if last_vw > 0:
                        vwap_pct = (last_close - last_vw) / last_vw * 100
        except Exception:
            vwap_pct = 0.0
        return {
            "ltp": ltp,
            "rsi": rsi_val,
            "vol_ratio": vol_ratio,
            "price_vs_ema20": (ltp - e20) / e20 * 100 if e20 > 0 else 0,
            "price_vs_ema50": (ltp - e50) / e50 * 100 if e50 > 0 else 0,
            "price_vs_vwap": vwap_pct,
            "day_change_pct": day_chg,
            "distance_52wh_pct": dist_52wh,
        }
    except Exception:
        return None


_OP_FN = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: abs(a - b) < 1e-6,
}


def run_strategy(rules: List[Dict[str, Any]],
                 universe_limit: int = 80) -> Dict[str, Any]:
    """Evaluate rules (AND-joined) across the universe."""
    # Validate
    norm_rules = []
    for r in rules or []:
        m = r.get("metric"); op = r.get("op"); v = r.get("value")
        if m not in VALID_METRICS or op not in VALID_OPS or v is None:
            continue
        try:
            norm_rules.append({"metric": m, "op": op, "value": float(v)})
        except Exception:
            continue
    if not norm_rules:
        return {"error": "no valid rules",
                "valid_metrics": sorted(VALID_METRICS),
                "valid_ops": sorted(VALID_OPS)}
    from application.services.swing_scanner import UNIVERSE as _U
    universe = list(_U)[:universe_limit]
    from concurrent.futures import ThreadPoolExecutor, as_completed
    matches = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        future_map = {ex.submit(_metric_values, s): s for s in universe}
        for fut in as_completed(future_map):
            sym = future_map[fut]
            vals = fut.result()
            if not vals:
                continue
            ok = True
            for r in norm_rules:
                fn = _OP_FN[r["op"]]
                if not fn(vals[r["metric"]], r["value"]):
                    ok = False; break
            if ok:
                matches.append({
                    "symbol": sym, "name": _display_name(sym),
                    **{k: round(v, 2) for k, v in vals.items()},
                })
    matches.sort(key=lambda r: r.get("day_change_pct", 0), reverse=True)
    return {
        "rules": norm_rules,
        "matches": matches,
        "match_count": len(matches),
        "screened": len(universe),
        "scan_time": _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST"),
    }


# ═══════════════════════════════════════════════════════════════════════
# AI Idea of the Day (per style)
# ═══════════════════════════════════════════════════════════════════════

def idea_of_the_day(style: str = "swing", force: bool = False) -> Dict[str, Any]:
    style = style if style in VALID_STYLES else "swing"
    key = f"idea_of_day:{style}:{_dt.datetime.now(_IST).date().isoformat()}"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    # Build context: best candidate from each style's primary scan
    candidate = None; context_lines: List[str] = []
    try:
        if style == "intraday":
            from application.services import intraday_tools
            data = intraday_tools.momentum_burst(lookback_min=30)
            stocks = (data or {}).get("stocks") or []
            stocks = [s for s in stocks if s.get("score", 0) > 0][:5]
            if stocks:
                candidate = stocks[0]
                context_lines.append("Top intraday momentum bursts (30m):")
                for s in stocks[:5]:
                    context_lines.append(f"- {s['name']}: +{s.get('move_pct',0)}% | "
                                         f"vol×{s.get('vol_ratio',0)} | score {s.get('score',0)}")
        elif style == "swing":
            from application.services import swing_tools
            data = swing_tools.mtf_alignment()
            bulls = (data or {}).get("bulls") or []
            if bulls:
                candidate = bulls[0]
                context_lines.append("Multi-timeframe bullish stocks (daily + weekly EMA stack):")
                for s in bulls[:5]:
                    context_lines.append(f"- {s['name']}: ₹{s['ltp']} | "
                                         f"+{s.get('distance_200dma_pct',0)}% above 200DMA")
        else:  # investing
            from application.services import investing_tools
            data = investing_tools.screener("quality")
            stocks = (data or {}).get("stocks") or []
            if stocks:
                candidate = stocks[0]
                context_lines.append("Top quality-screen stocks (ROE>15, ROCE>15, D/E<0.5):")
                for s in stocks[:5]:
                    name = s.get("name") or s.get("symbol")
                    context_lines.append(f"- {name}: ROE {s.get('roe','?')} | "
                                         f"ROCE {s.get('roce','?')} | PE {s.get('pe','?')}")
    except Exception as e:
        log.warning("idea_of_the_day candidate fetch failed: %s", e)

    if not candidate:
        return {"style": style, "error": "no candidate found",
                "note": "Underlying scans returned no data — try refreshing later."}

    # Generate AI commentary if configured
    ai_text = None; ai_error = None
    try:
        from application.services import ai_client
        if ai_client.is_configured():
            sys_prompt = (
                "You are a concise, factual Indian-equities analyst. Given a candidate "
                "stock and short context, produce a 4-line idea-of-the-day blurb. Format:\n"
                "Line 1: Stock name + one-sentence thesis.\n"
                "Line 2: Why now (catalyst / setup).\n"
                "Line 3: Entry / risk note.\n"
                "Line 4: Important caveat. No financial advice."
            )
            user_prompt = (
                f"Style: {style}\n"
                f"Top candidate: {candidate}\n\n"
                f"Context:\n" + "\n".join(context_lines) +
                "\n\nWrite the 4-line idea."
            )
            text, err = ai_client.chat(
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": user_prompt}],
                temperature=0.5, max_tokens=400,
            )
            ai_text = text; ai_error = err
    except Exception as e:
        ai_error = str(e)

    payload = {
        "style": style,
        "date": _dt.datetime.now(_IST).date().isoformat(),
        "candidate": candidate,
        "context_lines": context_lines,
        "ai_blurb": ai_text,
        "ai_error": ai_error,
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=60 * 60 * 12)  # 12h
    except Exception:
        pass
    return payload
