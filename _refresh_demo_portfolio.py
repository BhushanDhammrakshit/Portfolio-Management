"""One-off maintenance: bring existing sample (IsDemo) portfolios up to the
current 10-stock DEFAULT_DEMO_STOCKS lineup, and fix purchase prices to the
target profit/loss multiplier so old demo portfolios aren't stuck all-loss.
Run manually: python _refresh_demo_portfolio.py
"""
import uuid
from collections import defaultdict

from azure.data.tables import UpdateMode

from application.services.azure_table import stocks_table_client
from application.services.demo_portfolio import DEFAULT_DEMO_STOCKS

_BY_SYMBOL = {s["Symbol"]: s for s in DEFAULT_DEMO_STOCKS}


def main():
    demo_rows = list(stocks_table_client.query_entities(
        query_filter="IsDemo eq true"))

    by_user = defaultdict(list)
    for row in demo_rows:
        by_user[row.get("UserId")].append(row)

    fixed = 0
    added = 0
    for user_id, rows in by_user.items():
        have_symbols = {r.get("Symbol") for r in rows}

        # Fix purchase price on existing rows to match the target multiplier.
        for row in rows:
            spec = _BY_SYMBOL.get(row.get("Symbol"))
            current_price = float(row.get("CurrentPrice") or 0)
            if not spec or current_price <= 0:
                continue
            row["PurchasePrice"] = round(current_price * spec["PurchaseMultiplier"], 2)
            stocks_table_client.update_entity(entity=row, mode=UpdateMode.MERGE)
            fixed += 1

        # Add any lineup symbols this user doesn't have yet.
        for spec in DEFAULT_DEMO_STOCKS:
            if spec["Symbol"] in have_symbols:
                continue
            current_price = spec["FallbackCurrentPrice"]
            try:
                from application.services import market_data
                quote = market_data.get_quote(spec["Symbol"]) or {}
                if quote.get("price"):
                    current_price = float(quote["price"])
            except Exception:
                pass
            entity = {
                "PartitionKey": "stock",
                "RowKey": str(uuid.uuid4()),
                "UserId": user_id,
                "StockName": spec["StockName"],
                "Quantity": spec["Quantity"],
                "PurchasePrice": round(current_price * spec["PurchaseMultiplier"], 2),
                "CurrentPrice": current_price,
                "Sector": spec["Sector"],
                "Symbol": spec["Symbol"],
                "IsDemo": True,
            }
            stocks_table_client.create_entity(entity=entity)
            added += 1

    print(f"done: {len(by_user)} users touched, {fixed} prices fixed, {added} new sample rows added")


if __name__ == "__main__":
    main()
