"""Broker OAuth + Holdings import — Fyers & Dhan integration.

Routes:
  GET  /broker/fyers/connect     → redirect user to Fyers OAuth login
  GET  /fyers/callback           → handle OAuth redirect, store user token
  GET  /broker/fyers/status      → check if user has connected Fyers
  POST /broker/fyers/sync        → fetch holdings and import into portfolio
  POST /broker/fyers/disconnect  → remove stored Fyers token

  POST /broker/dhan/connect      → save user-provided Dhan access token
  GET  /broker/dhan/status       → check if user has connected Dhan
  POST /broker/dhan/sync         → fetch holdings and import into portfolio
  POST /broker/dhan/disconnect   → remove stored Dhan token
"""
import hashlib
import uuid
import logging

import requests
from flask import (Blueprint, redirect, request, session, url_for,
                   jsonify, flash)

from application import config
from application.services.azure_table import stocks_table_client

log = logging.getLogger(__name__)

broker_api = Blueprint("broker_api", __name__)

_FYERS_AUTH_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"
_FYERS_TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
_FYERS_HOLDINGS_URL = "https://api-t1.fyers.in/api/v3/holdings"
_REDIRECT_PATH = "/fyers/callback"

def _broker_redirect():
    """Redirect to the portfolio maker page with the broker tab active."""
    return redirect(url_for("portfolioMaker") + "#broker")

_DHAN_HOLDINGS_URL = "https://api.dhan.co/v2/holdings"


def _login_required(f):
    from functools import wraps

    @wraps(f)
    def wrap(*args, **kwargs):
        if "email" not in session or "user_id" not in session:
            return redirect(url_for("logIn"))
        return f(*args, **kwargs)

    return wrap


# ── Token storage (in-memory, keyed by broker:user_id) ──────────────────
_user_tokens: dict[str, str] = {}


def _store_token(user_id: str, token: str, broker: str = "fyers"):
    _user_tokens[f"{broker}:{user_id}"] = token


def _get_token(user_id: str, broker: str = "fyers") -> str | None:
    return _user_tokens.get(f"{broker}:{user_id}")


def _delete_token(user_id: str, broker: str = "fyers"):
    _user_tokens.pop(f"{broker}:{user_id}", None)


# ── Routes ──────────────────────────────────────────────────────────────

@broker_api.route("/broker/fyers/connect")
@_login_required
def fyers_connect():
    """Redirect the user to the Fyers OAuth login page."""
    app_id = config.FYERS_APP_ID
    if not app_id:
        flash("Fyers integration is not configured (FYERS_APP_ID missing).", "danger")
        return _broker_redirect()

    redirect_uri = request.host_url.rstrip("/") + _REDIRECT_PATH
    # State carries user_id so the callback knows who we're authenticating.
    state = session["user_id"]

    auth_url = (
        f"{_FYERS_AUTH_URL}"
        f"?client_id={app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&state={state}"
    )
    return redirect(auth_url)


@broker_api.route(_REDIRECT_PATH)
def fyers_callback():
    """Handle the Fyers OAuth callback, exchange auth_code for access_token."""
    auth_code = request.args.get("auth_code", "").strip()
    state = request.args.get("state", "").strip()

    if not auth_code:
        flash("Fyers login failed — no auth code received.", "danger")
        return _broker_redirect()

    # Verify the state matches the logged-in user.
    if "user_id" not in session or state != session.get("user_id"):
        flash("Session mismatch. Please try connecting again.", "danger")
        return _broker_redirect()

    app_id = config.FYERS_APP_ID
    secret = config.FYERS_SECRET_KEY
    if not app_id or not secret:
        flash("Fyers not configured (missing APP_ID or SECRET_KEY).", "danger")
        return _broker_redirect()

    # Exchange auth_code → access_token.
    app_id_hash = hashlib.sha256(f"{app_id}:{secret}".encode()).hexdigest()
    payload = {
        "grant_type": "authorization_code",
        "appIdHash": app_id_hash,
        "code": auth_code,
    }
    try:
        r = requests.post(_FYERS_TOKEN_URL, json=payload, timeout=15)
        data = r.json()
    except Exception as e:
        log.warning("Fyers token exchange failed: %s", e)
        flash("Could not connect to Fyers. Please try again.", "danger")
        return _broker_redirect()

    if r.status_code != 200 or data.get("s") != "ok":
        msg = data.get("message") or data.get("data") or "Unknown error"
        log.warning("Fyers token exchange rejected: %s", data)
        flash(f"Fyers login failed: {msg}", "danger")
        return _broker_redirect()

    access_token = data.get("access_token")
    if not access_token:
        flash("Fyers returned no access token.", "danger")
        return _broker_redirect()

    _store_token(session["user_id"], access_token)
    flash("Fyers account connected successfully! You can now import your holdings.", "success")
    return _broker_redirect()


@broker_api.route("/broker/fyers/status")
@_login_required
def fyers_status():
    """Check whether the current user has a stored Fyers token."""
    connected = _get_token(session["user_id"]) is not None
    return jsonify({"connected": connected})


@broker_api.route("/broker/fyers/sync", methods=["POST"])
@_login_required
def fyers_sync():
    """Fetch user's holdings from Fyers and import into portfolio."""
    token = _get_token(session["user_id"])
    if not token:
        return jsonify({"error": "Not connected to Fyers. Please connect first."}), 401

    app_id = config.FYERS_APP_ID

    # Fetch holdings from Fyers.
    headers = {
        "Authorization": f"{app_id}:{token}",
        "Accept": "application/json",
    }
    try:
        r = requests.get(_FYERS_HOLDINGS_URL, headers=headers, timeout=15)
        data = r.json()
    except Exception as e:
        log.warning("Fyers holdings fetch failed: %s", e)
        return jsonify({"error": f"Could not fetch holdings: {e}"}), 502

    if data.get("s") != "ok":
        msg = data.get("message") or str(data)
        # If token expired, remove it.
        if r.status_code == 401 or "token" in msg.lower() or "expired" in msg.lower():
            _delete_token(session["user_id"])
            return jsonify({"error": "Fyers token expired. Please re-connect."}), 401
        return jsonify({"error": f"Fyers error: {msg}"}), 400

    overall = data.get("overall") or {}
    holdings = data.get("holdings") or []
    if not holdings:
        return jsonify({"error": "No holdings found in your Fyers account."}), 404

    # Fetch existing symbols in user's portfolio to skip duplicates.
    existing_symbols = set()
    try:
        items = list(stocks_table_client.query_entities(
            query_filter=f"UserId eq '{session['user_id']}'"))
        for it in items:
            sym = (it.get("Symbol") or "").strip().upper()
            if sym:
                existing_symbols.add(sym)
    except Exception:
        pass

    imported = 0
    skipped = 0
    errors = []
    results = []

    for h in holdings:
        try:
            # Fyers holding object fields:
            # holdingType, symbol, quantity, avgCostPrice, ltp, currentValue, ...
            fy_symbol = h.get("symbol", "").strip()   # e.g. "NSE:RELIANCE-EQ"
            qty = int(h.get("quantity") or h.get("remainingQuantity") or 0)
            avg_price = float(h.get("costPrice") or h.get("avgCostPrice") or 0)
            ltp = float(h.get("ltp") or 0)

            if qty <= 0:
                skipped += 1
                continue

            # Convert Fyers symbol → Yahoo format.
            yahoo_sym = _fyers_to_yahoo(fy_symbol)
            display_name = _fyers_display_name(fy_symbol)

            if yahoo_sym.upper() in existing_symbols:
                skipped += 1
                results.append({
                    "name": display_name, "symbol": yahoo_sym,
                    "qty": qty, "status": "skipped", "reason": "Already in portfolio"
                })
                continue

            entity = {
                "PartitionKey": "stock",
                "RowKey": str(uuid.uuid4()),
                "UserId": session["user_id"],
                "StockName": display_name,
                "Quantity": qty,
                "PurchasePrice": avg_price,
                "CurrentPrice": ltp if ltp else avg_price,
                "Sector": "Other",
                "Symbol": yahoo_sym,
            }

            # Try to get sector from market_data.
            try:
                from application.services import market_data
                info = market_data.get_info(yahoo_sym) or {}
                if info.get("sector"):
                    entity["Sector"] = info["sector"]
                if info.get("name"):
                    entity["StockName"] = info["name"]
            except Exception:
                pass

            stocks_table_client.create_entity(entity=entity)
            existing_symbols.add(yahoo_sym.upper())
            imported += 1
            results.append({
                "name": display_name, "symbol": yahoo_sym,
                "qty": qty, "avg_price": avg_price, "ltp": ltp,
                "status": "imported"
            })

        except Exception as e:
            errors.append(f"{h.get('symbol', '?')}: {e}")

    return jsonify({
        "imported": imported,
        "skipped": skipped,
        "total": len(holdings),
        "errors": errors,
        "results": results,
    })


@broker_api.route("/broker/fyers/disconnect", methods=["POST"])
@_login_required
def fyers_disconnect():
    """Remove the user's stored Fyers token."""
    _delete_token(session["user_id"], "fyers")
    return jsonify({"ok": True})


# ── Dhan Routes ─────────────────────────────────────────────────────────

@broker_api.route("/broker/dhan/connect", methods=["POST"])
@_login_required
def dhan_connect():
    """Save a user-provided Dhan access token."""
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "Please provide your Dhan access token."}), 400

    # Quick validation — try a lightweight API call.
    headers = {
        "access-token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        r = requests.get(_DHAN_HOLDINGS_URL, headers=headers, timeout=10)
        if r.status_code == 401:
            return jsonify({"error": "Invalid or expired token. Please generate a new one from Dhan."}), 401
    except Exception:
        pass  # Network error — still save, user can retry sync

    _store_token(session["user_id"], token, "dhan")
    return jsonify({"ok": True})


@broker_api.route("/broker/dhan/status")
@_login_required
def dhan_status():
    """Check whether the current user has a stored Dhan token."""
    connected = _get_token(session["user_id"], "dhan") is not None
    return jsonify({"connected": connected})


@broker_api.route("/broker/dhan/sync", methods=["POST"])
@_login_required
def dhan_sync():
    """Fetch user's holdings from Dhan and import into portfolio."""
    token = _get_token(session["user_id"], "dhan")
    if not token:
        return jsonify({"error": "Not connected to Dhan. Please connect first."}), 401

    headers = {
        "access-token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        r = requests.get(_DHAN_HOLDINGS_URL, headers=headers, timeout=15)
        if r.status_code == 401:
            _delete_token(session["user_id"], "dhan")
            return jsonify({"error": "Dhan token expired. Please re-connect with a new token."}), 401
        holdings = r.json()
    except Exception as e:
        log.warning("Dhan holdings fetch failed: %s", e)
        return jsonify({"error": f"Could not fetch holdings: {e}"}), 502

    if isinstance(holdings, dict) and holdings.get("errorCode"):
        msg = holdings.get("errorMessage") or str(holdings)
        _delete_token(session["user_id"], "dhan")
        return jsonify({"error": f"Dhan error: {msg}"}), 400

    if not isinstance(holdings, list) or not holdings:
        return jsonify({"error": "No holdings found in your Dhan account."}), 404

    # Fetch existing symbols in user's portfolio to skip duplicates.
    existing_symbols = set()
    try:
        items = list(stocks_table_client.query_entities(
            query_filter=f"UserId eq '{session['user_id']}'"))
        for it in items:
            sym = (it.get("Symbol") or "").strip().upper()
            if sym:
                existing_symbols.add(sym)
    except Exception:
        pass

    imported = 0
    skipped = 0
    errors = []
    results = []

    for h in holdings:
        try:
            trading_sym = (h.get("tradingSymbol") or "").strip()
            exchange = (h.get("exchange") or "NSE").strip().upper()
            qty = int(h.get("totalQty") or h.get("availableQty") or 0)
            avg_price = float(h.get("avgCostPrice") or 0)

            if qty <= 0:
                skipped += 1
                continue

            yahoo_sym = _dhan_to_yahoo(trading_sym, exchange)
            display_name = trading_sym

            if yahoo_sym.upper() in existing_symbols:
                skipped += 1
                results.append({
                    "name": display_name, "symbol": yahoo_sym,
                    "qty": qty, "status": "skipped", "reason": "Already in portfolio"
                })
                continue

            entity = {
                "PartitionKey": "stock",
                "RowKey": str(uuid.uuid4()),
                "UserId": session["user_id"],
                "StockName": display_name,
                "Quantity": qty,
                "PurchasePrice": avg_price,
                "CurrentPrice": avg_price,
                "Sector": "Other",
                "Symbol": yahoo_sym,
            }

            # Enrich with sector/name from market_data.
            try:
                from application.services import market_data
                info = market_data.get_info(yahoo_sym) or {}
                if info.get("sector"):
                    entity["Sector"] = info["sector"]
                if info.get("name"):
                    entity["StockName"] = info["name"]
                if info.get("price"):
                    entity["CurrentPrice"] = float(info["price"])
            except Exception:
                pass

            stocks_table_client.create_entity(entity=entity)
            existing_symbols.add(yahoo_sym.upper())
            imported += 1
            results.append({
                "name": entity["StockName"], "symbol": yahoo_sym,
                "qty": qty, "avg_price": avg_price,
                "status": "imported"
            })

        except Exception as e:
            errors.append(f"{h.get('tradingSymbol', '?')}: {e}")

    return jsonify({
        "imported": imported,
        "skipped": skipped,
        "total": len(holdings),
        "errors": errors,
        "results": results,
    })


@broker_api.route("/broker/dhan/disconnect", methods=["POST"])
@_login_required
def dhan_disconnect():
    """Remove the user's stored Dhan token."""
    _delete_token(session["user_id"], "dhan")
    return jsonify({"ok": True})


# ── Upstox market-data token (app-level, admin only) ────────────────────
# Unlike the Fyers/Dhan flows above (which import a *user's* holdings),
# these routes authorise the single Upstox account the app uses as a
# market-data provider. Admin-gated so a random logged-in user can't
# overwrite the shared market-data token with their own account.

def _is_market_data_admin(email: str) -> bool:
    import os
    allow = {e.strip().lower()
             for e in (os.getenv("ADMIN_EMAILS") or "").split(",") if e.strip()}
    if not email:
        return False
    if not allow:
        return True  # no allow-list configured → any logged-in user
    return email.lower() in allow


@broker_api.route("/broker/upstox/connect")
@_login_required
def upstox_connect():
    """Redirect to the Upstox OAuth consent page for the market-data account."""
    if not _is_market_data_admin(session.get("email", "")):
        flash("Admin access required to connect the Upstox market-data account.",
              "danger")
        return _broker_redirect()
    if not (config.UPSTOX_API_KEY and config.UPSTOX_REDIRECT_URI):
        flash("Upstox is not configured (UPSTOX_API_KEY / UPSTOX_REDIRECT_URI).",
              "danger")
        return _broker_redirect()
    from application.services.providers import upstox_auth
    return redirect(upstox_auth.authorization_url(state="market-data"))


@broker_api.route("/callback/upstox")
def upstox_callback():
    """Handle the Upstox OAuth redirect and store the market-data token."""
    code = request.args.get("code", "").strip()
    if not code:
        flash("Upstox login failed — no authorization code received.", "danger")
        return _broker_redirect()
    if not _is_market_data_admin(session.get("email", "")):
        flash("Admin access required to complete the Upstox connection.", "danger")
        return _broker_redirect()
    from application.services.providers import upstox_auth
    token = upstox_auth.exchange_code(code)
    if not token:
        flash("Upstox token exchange failed. Please try again.", "danger")
        return _broker_redirect()
    flash("Upstox market-data account connected successfully.", "success")
    return _broker_redirect()


@broker_api.route("/broker/upstox/status")
@_login_required
def upstox_status():
    """Report whether the app currently holds a usable Upstox token."""
    return jsonify({"connected": bool(config.upstox_access_token())})


# ── Helpers ─────────────────────────────────────────────────────────────

def _fyers_to_yahoo(fy_symbol: str) -> str:
    """Convert ``NSE:RELIANCE-EQ`` → ``RELIANCE.NS``, etc."""
    if not fy_symbol:
        return fy_symbol
    s = fy_symbol.strip().upper()
    if ":" in s:
        exch, rest = s.split(":", 1)
    else:
        return s

    # Strip suffix like -EQ, -BE, -A, etc.
    base = rest.split("-", 1)[0] if "-" in rest else rest

    if exch == "NSE":
        return f"{base}.NS"
    elif exch == "BSE":
        return f"{base}.BO"
    return f"{base}.NS"


def _fyers_display_name(fy_symbol: str) -> str:
    """Extract a readable name from ``NSE:RELIANCE-EQ``."""
    if ":" in fy_symbol:
        _, rest = fy_symbol.split(":", 1)
    else:
        rest = fy_symbol
    return rest.split("-", 1)[0].strip()


def _dhan_to_yahoo(trading_symbol: str, exchange: str = "NSE") -> str:
    """Convert Dhan tradingSymbol + exchange → Yahoo symbol.

    Dhan returns e.g. ``RELIANCE`` with ``exchange=NSE``.
    """
    sym = trading_symbol.strip().upper()
    exch = exchange.strip().upper()
    if exch in ("BSE", "BSE_EQ"):
        return f"{sym}.BO"
    return f"{sym}.NS"
