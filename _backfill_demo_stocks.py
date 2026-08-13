"""One-off maintenance script: seed sample holdings for existing accounts
that still have zero stocks, instead of waiting for their next dashboard
visit. Run manually: python _backfill_demo_stocks.py
"""
from application.services.azure_table import user_table_client
from application.services import demo_portfolio


def main():
    checked = 0
    seeded = 0
    # list_entities (no filter) so legacy rows with PartitionKey='PartitionKey'
    # are covered too, not just the standard 'user' partition.
    for user in user_table_client.list_entities():
        checked += 1
        user_id = user.get("RowKey")
        if not user_id:
            continue
        if demo_portfolio.seed_demo_stocks_if_needed(user_id):
            seeded += 1
            print(f"seeded demo stocks for {user.get('Email', user_id)}")
    print(f"done: checked {checked} accounts, seeded {seeded}")


if __name__ == "__main__":
    main()
