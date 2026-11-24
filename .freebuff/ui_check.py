"""Headless-browser verification of the Phase 4 page layouts."""
import json
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

BASE = "http://127.0.0.1:5055"

opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--window-size=1440,1200")
opts.add_argument("--disable-gpu")
opts.add_argument("--no-sandbox")
opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})

driver = webdriver.Chrome(options=opts)
driver.set_window_size(1440, 1200)


def js(script, *args):
    return driver.execute_script(script, *args)


def visit(path, wait=4):
    driver.get(BASE + path)
    time.sleep(wait)
    return driver.title


def report(label, value):
    print(f"{label}: {value}")


results = {}

# Stock Screener
visit("/stock-screener", 6)
report("screener status", js("return document.querySelector('.screener-shell') ? 'ok' : 'missing'"))
report("screener panel width", js("return getComputedStyle(document.querySelector('.screener-panel')).width"))
report("screener accordions", js("return document.querySelectorAll('.screener-filters details.filter-group').length"))
report("screener groups", js("return Array.from(document.querySelectorAll('.screener-filters summary')).map(e=>e.textContent.trim()).join('|')"))
report("screener rows", js("return document.querySelectorAll('#screenerRows tr').length"))
report("screener banner", js("return document.querySelector('.info-banner') ? Math.round(document.querySelector('.info-banner').getBoundingClientRect().height) : 'missing'"))
report("screener cols", js("return document.querySelectorAll('#screenColumns input').length"))
results["screener"] = "ok"

# Intraday Scanner
visit("/intraday?mode=intraday", 3)
report("intraday cards", js("return document.querySelectorAll('#scanCardRow .scan-card').length"))
report("intraday card width", js("var c=document.querySelector('#scanCardRow .scan-card'); return c ? Math.round(c.getBoundingClientRect().width) : 'missing'"))
report("intraday info height", js("var b=document.querySelector('.info-banner'); return b ? Math.round(b.getBoundingClientRect().height) : 'missing'"))
report("intraday toggle", js("return document.querySelectorAll('#scanHistoryToggle [data-view]').length"))
report("intraday viewall", js("return document.querySelector('#scanViewAll') ? document.querySelector('#scanViewAll').textContent.trim() : 'missing'"))
report("intraday past hidden", js("return document.getElementById('pastScans') ? document.getElementById('pastScans').hidden : 'missing'"))
js("document.querySelector('#scanHistoryToggle [data-view=\\'past\\']').click()")
time.sleep(0.4)
report("past shown", js("return !document.getElementById('pastScans').hidden"))
report("empty hidden on past", js("return document.getElementById('emptyState').hidden"))
js("document.querySelector('#scanHistoryToggle [data-view=\\'recent\\']').click()")

# Intraday Tools / scans page
visit("/intraday-tools", 3)
report("tools cards", js("return document.querySelectorAll('#toolCardRow .scan-card').length"))
report("tools card width", js("var c=document.querySelector('#toolCardRow .scan-card'); return c ? Math.round(c.getBoundingClientRect().width) : 'missing'"))
report("tools toggle", js("return document.querySelectorAll('#toolHistoryToggle [data-view]').length"))
report("tools viewall", js("return document.querySelector('#toolViewAll') ? document.querySelector('#toolViewAll').textContent.trim() : 'missing'"))
js("document.querySelector('#toolHistoryToggle [data-view=\\'past\\']').click()")
time.sleep(0.4)
report("tools past shown", js("return !document.getElementById('toolPast').hidden"))
js("document.querySelector('#toolHistoryToggle [data-view=\\'recent\\']').click()")
time.sleep(0.4)
report("tools panels visible after recent", js("return Array.from(document.querySelectorAll('.tool-panel')).filter(p=>p.offsetParent!==null).length"))

# Volume shockers
visit("/volume", 5)
report("volume title", driver.title)
report("volume window chips", js("return document.querySelectorAll('#volWindow [data-window]').length"))
report("volume rows", js("return document.querySelectorAll('#volBody tr').length"))
report("volume headers", js("return Array.from(document.querySelectorAll('#volTable thead th')).map(e=>e.textContent.trim()).join('|')"))

# Market Pulse
visit("/market-pulse", 5)
report("pulse window chips", js("return document.querySelectorAll('#mpWindow [data-window]').length"))
report("pulse headers", js("return Array.from(document.querySelectorAll('.mp-movers-table thead th')).map(e=>e.textContent.trim()).join('|')"))
report("pulse banner height", js("var b=document.querySelector('.info-banner'); return b ? Math.round(b.getBoundingClientRect().height) : 'missing'"))

# Options analytics
visit("/option-chain", 8)
report("oc underlying", js("return document.getElementById('ocUnderlying') ? 'ok' : 'missing'"))
report("oc expiry chips", js("return document.querySelectorAll('#ocExpiryChips .ds-chip').length"))
report("oc tabs", js("return Array.from(document.querySelectorAll('.oc-tab-btn')).map(e=>e.textContent.trim()).join('|')"))
report("oc strike header centered", js("var th=document.querySelector('.oc-table thead tr:nth-child(2) th:nth-child(7)'); return th ? getComputedStyle(th).textAlign : 'missing'"))
report("oc sticky", js("var th=document.querySelector('.oc-table thead th'); return th ? getComputedStyle(th).position : 'missing'"))
report("oc oi bars", js("return document.querySelectorAll('.oc-table .oc-oi-track').length"))
report("oc itm rows", js("return document.querySelectorAll('.oc-table td.itm').length"))
report("oc rows", js("return document.querySelectorAll('#ocTbody tr').length"))
report("oc pcr panel", js("return document.getElementById('ocPcrValue') ? 'ok' : 'missing'"))
report("oc maxpain rows", js("return document.querySelectorAll('#ocMaxPainRows tr').length"))

# F&O gap forecast
visit("/fno-gap-forecast", 7)
report("gap headers", js("return Array.from(document.querySelectorAll('.gap-table thead th')).map(e=>e.textContent.trim()).join('|')"))
report("gap rows", js("return document.querySelectorAll('#fnoRows tr').length"))
report("gap methodology", js("return document.body.textContent.indexOf('Methodology') >= 0"))
report("gap disclaimer", js("return document.body.textContent.indexOf('not investment advice') >= 0 || document.body.textContent.indexOf('Not investment advice') >= 0"))
report("gap banner", js("var b=document.querySelector('.info-banner'); return b ? Math.round(b.getBoundingClientRect().height) : 'missing'"))

logs = driver.get_log("browser")
severe = [entry for entry in logs if entry["level"] == "SEVERE"]
print("\nSEVERE console errors:", len(severe))
for entry in severe[:12]:
    print(" -", entry["message"][:300])

driver.quit()
