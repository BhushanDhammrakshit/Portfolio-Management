"""Azure Tables client wrappers with safe fallbacks."""
from azure.data.tables import TableServiceClient

from application.config import (AZURE_TABLE_CONN_STR, USER_INFO_TABLE,
                                USER_STOCKS_TABLE, USER_EVENTS_TABLE)


class _MissingClient:
    """Returned when Azure isn't configured. Every call raises a clear error."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise RuntimeError(
                "Azure Tables is not configured. Set AZURE_TABLE_CONN_STR, "
                "USER_INFO_TABLE, and USER_STOCKS_TABLE in your .env file."
            )
        return _raise


if AZURE_TABLE_CONN_STR and USER_INFO_TABLE and USER_STOCKS_TABLE:
    try:
        service = TableServiceClient.from_connection_string(
            conn_str=AZURE_TABLE_CONN_STR)
        try:
            service.create_table_if_not_exists(table_name=USER_INFO_TABLE)
            service.create_table_if_not_exists(table_name=USER_STOCKS_TABLE)
            if USER_EVENTS_TABLE:
                service.create_table_if_not_exists(table_name=USER_EVENTS_TABLE)
        except Exception as e:
            print(f"[azure-tables] table create skipped: {e}")
        user_table_client = service.get_table_client(table_name=USER_INFO_TABLE)
        stocks_table_client = service.get_table_client(table_name=USER_STOCKS_TABLE)
        events_table_client = (service.get_table_client(table_name=USER_EVENTS_TABLE)
                               if USER_EVENTS_TABLE else _MissingClient())
    except Exception as e:
        print(f"[azure-tables] init failed: {e}")
        user_table_client = _MissingClient()
        stocks_table_client = _MissingClient()
        events_table_client = _MissingClient()
else:
    print("[azure-tables] not configured (missing env vars). Using stub clients.")
    user_table_client = _MissingClient()
    stocks_table_client = _MissingClient()
    events_table_client = _MissingClient()


def get_user_by_credentials(email: str, password: str):
    filter_query = f"Email eq '{email}' and Password eq '{password}'"
    try:
        for user in user_table_client.query_entities(query_filter=filter_query):
            return user
    except Exception as e:
        print(f"[azure-tables] get_user_by_credentials: {e}")
    return None


def get_user_stocks_by_row_key(row_key: str):
    try:
        return list(stocks_table_client.query_entities(
            query_filter=f"UserId eq '{row_key}'"))
    except Exception as e:
        print(f"[azure-tables] get_user_stocks: {e}")
        return []
