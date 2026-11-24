"""Render the option chain with synthetic rows to verify chain-specific UI."""
import json
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--window-size=1440,1200")
opts.add_argument("--disable-gpu")
opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})

driver = webdriver.Chrome(options=opts)
driver.get("http://127.0.0.1:5055/option-chain")
time.sleep(5)

driver.execute_script("document.getElementById('ocContent').hidden = false;")

payload = {
    "as_of": "21 Oct 2026, 10:30 AM IST",
    "spot": 25000,
    "expiry": "23 Oct 2026",
    "expiries": ["2026-10-23", "2026-10-30"],
    "totals": {"ce_oi": 1200000, "pe_oi": 1500000, "ce_oi_chg": 1000, "pe_oi_chg": -500},
    "pcr": 1.25,
    "pcr_prev": 1.18,
    "series": [],
    "rows": [],
    "indicator": {"label": "NEUTRAL", "summary": "test", "shift_score": 0, "reasons": [], "buildup": {}},
}

strikes = [24800, 24900, 25000, 25100, 25200]
for i, strike in enumerate(strikes):
    payload["rows"].append({
        "strike": strike,
        "ce": {"oi": 100000 + i * 20000, "oi_chg": 1200 - i * 100, "iv": 12.5 + i,
               "vol": 50000, "ltp": 120 - i * 10, "buildup": "Short Build-up", "buildup_bias": -1},
        "pe": {"oi": 180000 - i * 20000, "oi_chg": -600 + i * 100, "iv": 13.5 + i,
               "vol": 40000, "ltp": 90 + i * 10, "buildup": "Long Build-up", "buildup_bias": 1},
    })

driver.execute_script("render(arguments[0]);", payload)
time.sleep(1)

driver.execute_script("document.querySelector('.oc-tab-btn[data-tab-target=\"maxpain\"]').click();")
time.sleep(0.5)
print("maxpain rows after click:", driver.execute_script("return document.querySelectorAll('#ocMaxPainRows tr').length"))
driver.execute_script("document.querySelector('.oc-tab-btn[data-tab-target=\"pcr\"]').click();")
time.sleep(0.5)
print("pcr value after click:", driver.execute_script("return document.getElementById('ocPcrValue').textContent"))
driver.execute_script("document.querySelector('.oc-tab-btn[data-tab-target=\"chain\"]').click();")
time.sleep(0.3)

checks = {
    "rows": "return document.querySelectorAll('#ocTbody tr').length",
    "oi bars": "return document.querySelectorAll('.oc-table .oc-oi-track').length",
    "itm cells": "return document.querySelectorAll('.oc-table td.itm').length",
    "atm rows": "return document.querySelectorAll('#ocTbody tr.is-atm').length",
    "expiry chips": "return document.querySelectorAll('#ocExpiryChips .ds-chip').length",
    "ce oi text": "return document.querySelector('#ocTbody tr td:nth-child(2)').textContent.trim()",
    "strike cell": "return document.querySelector('#ocTbody tr td:nth-child(7)').textContent.trim()",
    "maxpain rows": "return document.querySelectorAll('#ocMaxPainRows tr').length",
    "pcr value": "return document.getElementById('ocPcrValue').textContent",
    "chart count": "return document.querySelectorAll('.oc-panel canvas').length",
}
for label, script in checks.items():
    print(label + ":", driver.execute_script(script))

# Tab navigation should swap panels
driver.execute_script("document.querySelector('.oc-tab-btn[data-tab-target=\"pcr\"]').click();")
time.sleep(0.5)
print("pcr panel visible:", driver.execute_script(
    "return document.querySelector('.oc-panel[data-tab=\"pcr\"]').offsetParent !== null"))
driver.execute_script("document.querySelector('.oc-tab-btn[data-tab-target=\"maxpain\"]').click();")
time.sleep(0.5)
print("maxpain panel visible:", driver.execute_script(
    "return document.querySelector('.oc-panel[data-tab=\"maxpain\"]').offsetParent !== null"))

logs = [e for e in driver.get_log("browser") if e["level"] == "SEVERE"]
print("severe:", len(logs))
for entry in logs[:6]:
    print(" -", entry["message"][:220])
driver.quit()
