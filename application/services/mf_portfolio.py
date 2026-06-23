"""User mutual-fund portfolio service.

Holdings live in Azure Table ``USER_MF_TABLE`` (env var). Entity schema:

    PartitionKey:   "mf"
    RowKey:         UUID
    UserId:         user.RowKey
    SchemeCode:     "120503"
    SchemeName:     "HDFC Top 100 Fund - Growth"
    Category:       "Large Cap"
    FundHouse:      "HDFC Mutual Fund"
    Units:          float
    NavAtPurchase:  float
    PurchaseDate:   "YYYY-MM-DD"
    SipMonthly:     float (0 if lumpsum)
    FolioNumber:    string
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode

from application.config import AZURE_TABLE_CONN_STR
from application.services import mf_data
from application.services.azure_table import _MissingClient  # type: ignore

import os

_USER_MF_TABLE = os.getenv("USER_MF_TABLE", "UserMutualFunds")


# ── Lazy table client (mirrors azure_table.py pattern) ───────────────────
def _build_client():
    if not AZURE_TABLE_CONN_STR:
        return _MissingClient()
    try:
        svc = TableServiceClient.from_connection_string(
            conn_str=AZURE_TABLE_CONN_STR)
        try:
            svc.create_table_if_not_exists(table_name=_USER_MF_TABLE)
        except Exception as e:
            print(f"[mf_portfolio] create table skipped: {e}")
        return svc.get_table_client(table_name=_USER_MF_TABLE)
    except Exception as e:
        print(f"[mf_portfolio] client init failed: {e}")
        return _MissingClient()


mf_table_client = _build_client()


def _v(x):
    return x.value if hasattr(x, "value") else x


def _entity_to_dict(e: dict) -> dict:
    return {
        "id": _v(e.get("RowKey")),
        "scheme_code": _v(e.get("SchemeCode")),
        "scheme_name": _v(e.get("SchemeName")),
        "category": _v(e.get("Category")) or "",
        "fund_house": _v(e.get("FundHouse")) or "",
        "units": float(_v(e.get("Units")) or 0),
        "nav_at_purchase": float(_v(e.get("NavAtPurchase")) or 0),
        "purchase_date": _v(e.get("PurchaseDate")) or "",
        "sip_monthly": float(_v(e.get("SipMonthly")) or 0),
        "folio_number": _v(e.get("FolioNumber")) or "",
    }


# ── CRUD ────────────────────────────────────────────────────────────────
def add_holding(user_id: str, scheme_code: str, units: float,
                nav_at_purchase: float, purchase_date: str,
                sip_monthly: float = 0.0,
                folio_number: str = "",
                scheme_name: Optional[str] = None) -> dict:
    if not user_id:
        raise ValueError("user_id required")
    if not scheme_code:
        raise ValueError("scheme_code required")
    if units <= 0:
        raise ValueError("units must be > 0")

    scheme = mf_data.get_scheme(scheme_code)
    meta = scheme.get("meta") or {}
    name = meta.get("scheme_name")
    category = meta.get("scheme_category", "") or ""
    fund_house = meta.get("fund_house", "") or ""

    # Fall back to the cached universe list if MFAPI scheme detail is unavailable.
    if not name:
        try:
            for s in mf_data.list_schemes():
                if str(s.get("schemeCode")) == str(scheme_code):
                    name = s.get("schemeName")
                    break
        except Exception:
            pass
    # Last resort: client-supplied name from the search dropdown.
    if not name:
        name = (scheme_name or "").strip() or None
    if not name:
        raise ValueError("scheme_not_found")

    entity = {
        "PartitionKey": "mf",
        "RowKey": str(uuid.uuid4()),
        "UserId": user_id,
        "SchemeCode": str(scheme_code),
        "SchemeName": name,
        "Category": category,
        "FundHouse": fund_house,
        "Units": float(units),
        "NavAtPurchase": float(nav_at_purchase or 0),
        "PurchaseDate": purchase_date or date.today().isoformat(),
        "SipMonthly": float(sip_monthly or 0),
        "FolioNumber": folio_number or "",
    }
    mf_table_client.create_entity(entity=entity)
    return _entity_to_dict(entity)


def update_holding(user_id: str, holding_id: str, **fields) -> Optional[dict]:
    try:
        ent = mf_table_client.get_entity(partition_key="mf", row_key=holding_id)
    except ResourceNotFoundError:
        return None
    if _v(ent.get("UserId")) != user_id:
        return None
    mapping = {
        "units": "Units", "nav_at_purchase": "NavAtPurchase",
        "purchase_date": "PurchaseDate", "sip_monthly": "SipMonthly",
        "folio_number": "FolioNumber",
    }
    for k, col in mapping.items():
        if k in fields and fields[k] is not None:
            ent[col] = fields[k]
    mf_table_client.update_entity(entity=ent, mode=UpdateMode.MERGE)
    return _entity_to_dict(ent)


def delete_holding(user_id: str, holding_id: str) -> bool:
    try:
        ent = mf_table_client.get_entity(partition_key="mf", row_key=holding_id)
    except ResourceNotFoundError:
        return False
    if _v(ent.get("UserId")) != user_id:
        return False
    mf_table_client.delete_entity(partition_key="mf", row_key=holding_id)
    return True


def list_holdings(user_id: str) -> list[dict]:
    if not user_id:
        return []
    try:
        items = list(mf_table_client.query_entities(
            query_filter=f"UserId eq '{user_id}'"))
    except ResourceNotFoundError:
        return []
    except Exception as e:
        print(f"[mf_portfolio] list_holdings: {e}")
        return []
    return [_entity_to_dict(e) for e in items]


# ── Portfolio valuation ──────────────────────────────────────────────────
def portfolio_summary(user_id: str) -> dict:
    """Compute live P&L + per-holding rows. Uses cached NAVs to keep cheap."""
    holdings = list_holdings(user_id)
    rows = []
    total_invested = 0.0
    total_current = 0.0
    by_category: dict[str, float] = {}
    by_fund_house: dict[str, float] = {}

    for h in holdings:
        try:
            nav = mf_data.latest_nav(h["scheme_code"]) or h["nav_at_purchase"]
        except Exception:
            nav = h["nav_at_purchase"]
        invested = h["units"] * h["nav_at_purchase"]
        current = h["units"] * nav
        pnl = current - invested
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0.0

        # Hold-period CAGR (approx)
        cagr_pct = None
        try:
            d0 = datetime.fromisoformat(h["purchase_date"]).date()
            years = max(0.01, (date.today() - d0).days / 365.25)
            if invested > 0 and years > 0:
                cagr_pct = round(((current / invested) ** (1 / years) - 1) * 100, 2)
        except Exception:
            pass

        rows.append({
            **h,
            "current_nav": round(nav, 4),
            "invested": round(invested, 2),
            "current_value": round(current, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "cagr_pct": cagr_pct,
        })
        total_invested += invested
        total_current += current
        cat = h["category"] or "Uncategorised"
        by_category[cat] = by_category.get(cat, 0) + current
        fh = h["fund_house"] or "Unknown"
        by_fund_house[fh] = by_fund_house.get(fh, 0) + current

    total_pnl = total_current - total_invested
    pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0
    return {
        "holdings": rows,
        "totals": {
            "invested": round(total_invested, 2),
            "current_value": round(total_current, 2),
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(pct, 2),
            "count": len(rows),
        },
        "allocation_by_category": [
            {"label": k, "value": round(v, 2)}
            for k, v in sorted(by_category.items(), key=lambda x: -x[1])
        ],
        "allocation_by_amc": [
            {"label": k, "value": round(v, 2)}
            for k, v in sorted(by_fund_house.items(), key=lambda x: -x[1])
        ],
    }


# ── Calculators ──────────────────────────────────────────────────────────
def sip_future_value(monthly: float, years: float, annual_return_pct: float,
                     step_up_pct: float = 0.0) -> dict:
    """Standard SIP FV with optional yearly step-up."""
    months = int(round(years * 12))
    r = (annual_return_pct / 100) / 12
    total_invested = 0.0
    fv = 0.0
    current_sip = float(monthly)
    for m in range(1, months + 1):
        total_invested += current_sip
        fv = (fv + current_sip) * (1 + r)
        if step_up_pct and m % 12 == 0:
            current_sip *= (1 + step_up_pct / 100)
    gains = fv - total_invested
    return {
        "future_value": round(fv, 2),
        "total_invested": round(total_invested, 2),
        "gains": round(gains, 2),
        "wealth_multiplier": round(fv / total_invested, 2) if total_invested else 0,
    }


def lumpsum_future_value(amount: float, years: float,
                         annual_return_pct: float) -> dict:
    r = annual_return_pct / 100
    fv = amount * ((1 + r) ** years)
    return {
        "future_value": round(fv, 2),
        "total_invested": round(amount, 2),
        "gains": round(fv - amount, 2),
        "wealth_multiplier": round(fv / amount, 2) if amount else 0,
    }


def goal_sip(target_amount: float, years: float,
             annual_return_pct: float) -> dict:
    """Monthly SIP needed to reach target."""
    months = int(round(years * 12))
    r = (annual_return_pct / 100) / 12
    if r <= 0:
        monthly = target_amount / max(1, months)
    else:
        # Standard annuity formula: FV = P * [((1+r)^n - 1) / r] * (1+r)
        monthly = target_amount / ((((1 + r) ** months - 1) / r) * (1 + r))
    return {
        "monthly_sip": round(monthly, 2),
        "target": round(target_amount, 2),
        "years": years,
        "assumed_return_pct": annual_return_pct,
    }


def stock_overlap_with_portfolio(user_id: str, stocks_table_client,
                                 holdings: Optional[list[dict]] = None,
                                 nav_cache: Optional[dict] = None) -> dict:
    """Find stocks held BOTH directly by the user AND inside their MFs.

    `stocks_table_client` is injected (the same Azure client used elsewhere)
    to avoid a circular import surface. `holdings` and `nav_cache` can be
    passed in to avoid re-fetching when the caller has them already.
    """
    if holdings is None:
        holdings = list_holdings(user_id)
    nav_cache = nav_cache or {}
    if not holdings:
        return {"overlaps": [], "fund_count": 0, "direct_count": 0}

    # Direct stock symbols (uppercased base ticker, strip .NS/.BO)
    try:
        items = list(stocks_table_client.query_entities(
            query_filter=f"UserId eq '{user_id}'"))
    except Exception:
        items = []
    direct: dict[str, float] = {}
    for s in items:
        sym = (_v(s.get("Symbol")) or _v(s.get("StockName")) or "").upper()
        if not sym:
            continue
        base = sym.split(".")[0].split(":")[-1].strip()
        qty = float(_v(s.get("Quantity")) or 0)
        cp = float(_v(s.get("CurrentPrice")) or 0)
        direct[base] = direct.get(base, 0) + qty * cp

    if not direct:
        return {"overlaps": [], "fund_count": len(holdings), "direct_count": 0}

    # MF holdings → aggregated indicative exposure (units × NAV × weight%)
    fund_exposure: dict[str, list[dict]] = {}
    for h in holdings:
        hd = mf_data.get_holdings(h["scheme_code"])
        if not hd:
            continue
        # Prefer pre-computed current_value to avoid re-fetching NAV
        if h.get("current_value"):
            fund_value = float(h["current_value"])
        else:
            nav = nav_cache.get(h["scheme_code"]) or mf_data.latest_nav(h["scheme_code"]) or h["nav_at_purchase"]
            fund_value = h["units"] * nav
        for row in hd.get("top", []):
            sym = row["symbol"]
            exposure = fund_value * (row.get("weight_pct", 0) / 100.0)
            fund_exposure.setdefault(sym, []).append({
                "fund_name": h["scheme_name"],
                "weight_pct": row.get("weight_pct", 0),
                "exposure_value": round(exposure, 2),
            })

    overlaps = []
    for sym, direct_val in direct.items():
        if sym in fund_exposure:
            indirect_total = sum(x["exposure_value"] for x in fund_exposure[sym])
            overlaps.append({
                "symbol": sym,
                "direct_value": round(direct_val, 2),
                "indirect_value_via_funds": round(indirect_total, 2),
                "total_exposure": round(direct_val + indirect_total, 2),
                "funds": fund_exposure[sym],
            })
    overlaps.sort(key=lambda x: x["total_exposure"], reverse=True)
    return {
        "overlaps": overlaps,
        "fund_count": len(holdings),
        "direct_count": len(direct),
    }
