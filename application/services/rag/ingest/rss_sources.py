"""Free Indian financial-news RSS feeds.

Each feed is broad (markets/business). The fetcher filters items to a
target symbol's search terms — so we only need a handful of high-quality
feeds, not per-stock URLs (most aren't available for free anyway).
"""

RSS_FEEDS = [
    # Mint
    {"name": "Mint Markets",
     "url": "https://www.livemint.com/rss/markets",
     "source": "Mint"},
    {"name": "Mint Companies",
     "url": "https://www.livemint.com/rss/companies",
     "source": "Mint"},

    # Moneycontrol
    {"name": "Moneycontrol Business",
     "url": "https://www.moneycontrol.com/rss/business.xml",
     "source": "Moneycontrol"},
    {"name": "Moneycontrol Markets",
     "url": "https://www.moneycontrol.com/rss/marketreports.xml",
     "source": "Moneycontrol"},
    {"name": "Moneycontrol Results",
     "url": "https://www.moneycontrol.com/rss/results.xml",
     "source": "Moneycontrol"},

    # Business Standard
    {"name": "Business Standard Markets",
     "url": "https://www.business-standard.com/rss/markets-106.rss",
     "source": "Business Standard"},
    {"name": "Business Standard Companies",
     "url": "https://www.business-standard.com/rss/companies-101.rss",
     "source": "Business Standard"},

    # Economic Times
    {"name": "ET Markets",
     "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
     "source": "Economic Times"},
    {"name": "ET Stocks",
     "url": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
     "source": "Economic Times"},
]
