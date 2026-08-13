"""Seed a starter/sample portfolio for brand-new or empty accounts so the
dashboard isn't blank on first login, and let users clear it in one click.
"""
import uuid

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode

from application.services.azure_table import (stocks_table_client,
                                               user_table_client)
from application.services.event_tracker import track_event

# Shown to every new / empty-portfolio user as removable "sample" holdings.
# PurchaseMultiplier is applied to the current price to derive the buy price
# (e.g. 0.90 = bought 10% below today's price = profit; 1.15 = loss), so the
# sample portfolio always shows a realistic mix of gains and losses instead
# of drifting all-profit or all-loss as real market prices move over time.
# Spans multiple sectors and both small/large gains and losses.
DEFAULT_DEMO_STOCKS = [
    {"Symbol": "RELIANCE.NS", "StockName": "Reliance Industries",
     "Sector": "Energy", "Quantity": 10, "FallbackCurrentPrice": 2450.0,
     "PurchaseMultiplier": 0.90},   # moderate profit
    {"Symbol": "TCS.NS", "StockName": "Tata Consultancy Services",
     "Sector": "IT", "Quantity": 5, "FallbackCurrentPrice": 3550.0,
     "PurchaseMultiplier": 1.18},   # moderate loss
    {"Symbol": "HDFCBANK.NS", "StockName": "HDFC Bank",
     "Sector": "Financial Services", "Quantity": 15, "FallbackCurrentPrice": 1520.0,
     "PurchaseMultiplier": 0.93},   # small profit
    {"Symbol": "INFY.NS", "StockName": "Infosys",
     "Sector": "IT", "Quantity": 10, "FallbackCurrentPrice": 1480.0,
     "PurchaseMultiplier": 1.12},   # moderate loss
    {"Symbol": "ITC.NS", "StockName": "ITC Limited",
     "Sector": "FMCG", "Quantity": 25, "FallbackCurrentPrice": 410.0,
     "PurchaseMultiplier": 0.96},   # small profit
    {"Symbol": "ICICIBANK.NS", "StockName": "ICICI Bank",
     "Sector": "Financial Services", "Quantity": 12, "FallbackCurrentPrice": 1150.0,
     "PurchaseMultiplier": 1.08},   # small loss
    {"Symbol": "LT.NS", "StockName": "Larsen & Toubro",
     "Sector": "Infrastructure", "Quantity": 4, "FallbackCurrentPrice": 3600.0,
     "PurchaseMultiplier": 0.85},   # large profit
    {"Symbol": "SUNPHARMA.NS", "StockName": "Sun Pharmaceutical",
     "Sector": "Healthcare", "Quantity": 8, "FallbackCurrentPrice": 1650.0,
     "PurchaseMultiplier": 1.02},   # near-breakeven, slight loss
    {"Symbol": "TATAMOTORS.NS", "StockName": "Tata Motors",
     "Sector": "Automobile", "Quantity": 15, "FallbackCurrentPrice": 950.0,
     "PurchaseMultiplier": 0.80},   # large profit
    {"Symbol": "HINDALCO.NS", "StockName": "Hindalco Industries",
     "Sector": "Metals", "Quantity": 20, "FallbackCurrentPrice": 650.0,
     "PurchaseMultiplier": 1.20},   # large loss
]


def _get_user(user_id):
    # Some legacy rows were written with a literal "PartitionKey" value
    # instead of "user" (same quirk handled in route.py's delete fallback).
    for pk in ("user", "PartitionKey"):
        try:
            return user_table_client.get_entity(partition_key=pk, row_key=user_id)
        except ResourceNotFoundError:
            continue
        except Exception as e:
            print(f"[demo_portfolio] user lookup failed: {e}")
            return None
    return None


def _mark_seeded(user):
    user["DemoSeeded"] = True
    try:
        user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
    except Exception as e:
        print(f"[demo_portfolio] could not mark user seeded: {e}")


def seed_demo_stocks_if_needed(user_id):
    """Insert the sample holdings once for a user with an empty portfolio.

    Returns True if stocks were seeded, False otherwise (already seeded,
    already has holdings, or a lookup failed).
    """
    user = _get_user(user_id)
    if user is None or user.get("DemoSeeded"):
        return False

    try:
        existing = list(stocks_table_client.query_entities(
            query_filter=f"UserId eq '{user_id}'"))
    except Exception as e:
        print(f"[demo_portfolio] existing stocks query failed: {e}")
        return False

    if existing:
        # Never seed on top of holdings the user (or an import) already added.
        _mark_seeded(user)
        return False

    from application.services import market_data  # lazy: avoid import cost at startup

    for stock in DEFAULT_DEMO_STOCKS:
        current_price = stock["FallbackCurrentPrice"]
        try:
            quote = market_data.get_quote(stock["Symbol"]) or {}
            if quote.get("price"):
                current_price = float(quote["price"])
        except Exception:
            pass
        purchase_price = round(current_price * stock["PurchaseMultiplier"], 2)
        entity = {
            "PartitionKey": "stock",
            "RowKey": str(uuid.uuid4()),
            "UserId": user_id,
            "StockName": stock["StockName"],
            "Quantity": stock["Quantity"],
            "PurchasePrice": purchase_price,
            "CurrentPrice": current_price,
            "Sector": stock["Sector"],
            "Symbol": stock["Symbol"],
            "IsDemo": True,
        }
        try:
            stocks_table_client.create_entity(entity=entity)
        except Exception as e:
            print(f"[demo_portfolio] seed failed for {stock['Symbol']}: {e}")

    _mark_seeded(user)
    track_event(user_id, "demo_seeded", {"count": len(DEFAULT_DEMO_STOCKS)})
    return True


def remove_demo_stocks(user_id):
    """Delete every sample (IsDemo) stock row for a user. Returns count removed."""
    try:
        demo_rows = list(stocks_table_client.query_entities(
            query_filter=f"UserId eq '{user_id}' and IsDemo eq true"))
    except Exception as e:
        print(f"[demo_portfolio] remove query failed: {e}")
        return 0

    removed = 0
    for row in demo_rows:
        try:
            stocks_table_client.delete_entity(
                partition_key=row["PartitionKey"], row_key=row["RowKey"])
            removed += 1
        except Exception as e:
            print(f"[demo_portfolio] delete failed for {row.get('RowKey')}: {e}")

    if removed:
        track_event(user_id, "demo_removed", {"count": removed})
    return removed
