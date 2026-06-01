"""Probe v3."""
import json, requests

H = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.tickertape.in",
    "Referer": "https://www.tickertape.in/",
}


def hit(url, label):
    print(f"\n=== {label}\n  {url}")
    try:
        r = requests.get(url, headers=H, timeout=15)
        print("  status:", r.status_code)
        if r.status_code == 200:
            try:
                j = r.json()
                print(json.dumps(j, default=str)[:1500])
            except Exception:
                print("  not json:", r.text[:200])
        else:
            print("  body:", r.text[:200])
    except Exception as e:
        print("  ERR:", e)


import re

def fetch_html(url):
    print(f"\n=== HTML {url}")
    r = requests.get(url, headers={**H, "Accept": "text/html"}, timeout=15)
    print("  status:", r.status_code, "len:", len(r.text))
    if r.status_code != 200:
        return
    # Find script tags with "holding" or "portfolio"
    found_api = set()
    for m in re.finditer(r'api\.tickertape\.in/[a-zA-Z0-9_\-/?&=]+', r.text):
        found_api.add(m.group(0))
    print("  api refs:", list(found_api)[:30])
    # Look for embedded JSON with holdings
    for m in re.finditer(r'"holdings?"\s*:\s*\[(.{0,800}?)\]', r.text, re.S):
        print("  embedded holdings snippet:", m.group(0)[:300])
        break

fetch_html("https://www.tickertape.in/mutualfunds/parag-parikh-flexi-cap-fund-direct-growth-PPFCG/portfolio")
fetch_html("https://www.tickertape.in/mutualfunds/parag-parikh-flexi-cap-fund-direct-growth-PPFCG/holdings")
