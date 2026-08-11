"""Provider package — concrete market-data backends.

Each provider exposes the same callable surface (see ``market_data.py``):

    get_history(symbol, days, interval)         -> pandas.DataFrame
    download_history(symbols, start, end, interval) -> dict[symbol -> DataFrame]
    get_quote(symbol)                            -> dict | None
    get_info(symbol)                             -> dict | None
    search(query)                                -> list[dict]

Providers raise on unrecoverable transport errors; they return ``None`` /
``{}`` for "no data" so the dispatcher can fall back cleanly.
"""
