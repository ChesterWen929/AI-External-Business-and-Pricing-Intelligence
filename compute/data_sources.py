"""
Live data layer (yfinance). Best-effort: every fetch is wrapped so a network
failure NEVER breaks the dashboard — it just falls back to the manual anchors.

What we pull per ticker:
  - price, market_cap            (context)
  - ttm_revenue_usd_bn           (scales bottom-up vendor anchors)
  - capex_ttm_usd_bn             (overrides hyperscaler capex spine, current year)

yfinance does not expose business-segment revenue, so the model scales the
manually-anchored segment figures by the ratio of live-TTM to anchor-TTM total
revenue. A new earnings print -> yfinance TTM moves -> estimate auto-updates.
"""
from __future__ import annotations

import csv
import io
import json
import os
import urllib.request
from datetime import datetime

DEFAULT_TICKERS = ["MSFT", "GOOGL", "AMZN", "META", "ORCL",
                   "NVDA", "AMD", "AVGO", "MRVL", "INTC", "TSM"]

# SEC EDGAR requires a descriptive User-Agent with contact info.
EDGAR_UA = os.environ.get("EDGAR_UA", "AI-Compute-Demand-Radar research sleptbeauty@gmail.com")
EDGAR_REVENUE_CONCEPTS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
]
EDGAR_CAPEX_CONCEPTS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
]


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _ttm_from_quarterly(df, row_names):
    """Sum the latest 4 quarters for the first matching row name."""
    if df is None or getattr(df, "empty", True):
        return None
    for name in row_names:
        if name in df.index:
            vals = [v for v in list(df.loc[name].values)[:4] if v is not None]
            vals = [float(v) for v in vals if _is_num(v)]
            if vals:
                return sum(vals)
    return None


def _is_num(v):
    try:
        float(v)
        return v == v  # not NaN
    except (TypeError, ValueError):
        return False


def fetch_one(ticker):
    import yfinance as yf
    t = yf.Ticker(ticker)
    out = {"ticker": ticker}

    info = _safe(lambda: t.info, {}) or {}
    fast = _safe(lambda: t.fast_info, {}) or {}
    price = info.get("currentPrice") or _safe(lambda: fast.get("last_price"))
    mcap = info.get("marketCap") or _safe(lambda: fast.get("market_cap"))
    out["price"] = round(float(price), 2) if _is_num(price) else None
    out["market_cap_usd_bn"] = round(float(mcap) / 1e9, 1) if _is_num(mcap) else None

    # TTM revenue: prefer quarterly income statement sum-of-4, fall back to info
    q_inc = _safe(lambda: t.quarterly_income_stmt)
    ttm_rev = _ttm_from_quarterly(q_inc, ["Total Revenue", "TotalRevenue", "Operating Revenue"])
    if ttm_rev is None and _is_num(info.get("totalRevenue")):
        ttm_rev = float(info["totalRevenue"])
    out["ttm_revenue_usd_bn"] = round(ttm_rev / 1e9, 1) if _is_num(ttm_rev) else None

    # TTM capex (cash-flow capital expenditure, usually negative -> abs)
    q_cf = _safe(lambda: t.quarterly_cashflow)
    capex = _ttm_from_quarterly(q_cf, ["Capital Expenditure", "CapitalExpenditures", "Capital Expenditures"])
    out["capex_ttm_usd_bn"] = round(abs(capex) / 1e9, 1) if _is_num(capex) else None

    return out


def fetch_live(tickers=None, log=None):
    tickers = tickers or DEFAULT_TICKERS
    result = {}
    for tk in tickers:
        row = _safe(lambda: fetch_one(tk))
        if row:
            result[tk] = row
            if log:
                log(f"  {tk}: price={row.get('price')} rev_ttm={row.get('ttm_revenue_usd_bn')} "
                    f"capex_ttm={row.get('capex_ttm_usd_bn')}")
        else:
            if log:
                log(f"  {tk}: fetch failed -> anchors will be used")
    return result


# --------------------------------------------------------------------------- #
# SEC EDGAR — official total revenue + capex from companyfacts XBRL
# --------------------------------------------------------------------------- #
def _http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _edgar_periods(facts, concepts):
    """Collect annual (~365d) and quarterly (~90d) USD values across candidate
    concepts, keyed by end date. companyfacts holds AGGREGATE facts only (no
    business-segment dimension), so this is TOTAL company revenue/capex."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    annuals, quarters = {}, {}
    for concept in concepts:
        for e in gaap.get(concept, {}).get("units", {}).get("USD", []):
            start, end, val = e.get("start"), e.get("end"), e.get("val")
            if not (start and end and val is not None):
                continue
            try:
                days = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
            except ValueError:
                continue
            if 350 <= days <= 380:
                annuals[end] = float(val)
            elif 80 <= days <= 100:
                quarters[end] = float(val)
    return annuals, quarters


def fetch_edgar_one(cik10):
    """Official SEC figures for the provenance panel: latest ANNUAL revenue
    (from 10-K) + latest reported QUARTER, with filing end dates. Used as an
    official cross-check, NOT as the model's scaling driver."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
    facts = json.loads(_http_get(url, headers={"User-Agent": EDGAR_UA}, timeout=30))
    ann, qtr = _edgar_periods(facts, EDGAR_REVENUE_CONCEPTS)
    cap_ann, _ = _edgar_periods(facts, EDGAR_CAPEX_CONCEPTS)
    a_end = max(ann) if ann else None
    q_end = max(qtr) if qtr else None
    ca_end = max(cap_ann) if cap_ann else None
    return {
        "annual_revenue_usd_bn": round(ann[a_end] / 1e9, 1) if a_end else None,
        "annual_fy_end": a_end,
        "latest_quarter_revenue_usd_bn": round(qtr[q_end] / 1e9, 1) if q_end else None,
        "quarter_end": q_end,
        "annual_capex_usd_bn": round(abs(cap_ann[ca_end]) / 1e9, 1) if ca_end else None,
        "source": "SEC EDGAR companyfacts",
    }


def fetch_edgar(cik_map, log=None):
    out = {}
    for tk, cik in cik_map.items():
        row = _safe(lambda: fetch_edgar_one(cik))
        if row and (row.get("annual_revenue_usd_bn") or row.get("latest_quarter_revenue_usd_bn")):
            out[tk] = row
            if log:
                log(f"  EDGAR {tk}: FY rev={row['annual_revenue_usd_bn']} ({row['annual_fy_end']}) "
                    f"Q rev={row['latest_quarter_revenue_usd_bn']} ({row['quarter_end']})")
        elif log:
            log(f"  EDGAR {tk}: no usable facts")
    return out


# --------------------------------------------------------------------------- #
# FRED — macro demand-environment context (no key needed via fredgraph CSV)
# --------------------------------------------------------------------------- #
def fetch_fred_series(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    text = _http_get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; compute-radar)"}, timeout=40)
    rows = list(csv.reader(io.StringIO(text)))
    pts = []
    for r in rows[1:]:
        if len(r) < 2 or r[1] in ("", "."):
            continue
        try:
            pts.append((r[0], float(r[1])))
        except ValueError:
            continue
    if not pts:
        return None
    last_date, last_val = pts[-1]
    prior = pts[-13] if len(pts) >= 13 else pts[0]
    yoy = round((last_val / prior[1] - 1) * 100, 1) if prior[1] else None
    return {
        "latest": round(last_val, 1), "date": last_date, "yoy_pct": yoy,
        "history": [{"d": d, "v": round(v, 1)} for d, v in pts[-24:]],
    }


def fetch_fred(series, log=None):
    out = {}
    for s in series:
        row = None
        for _ in range(2):  # FRED cold connects can be slow; one retry
            row = _safe(lambda: fetch_fred_series(s["id"]))
            if row:
                break
        if row:
            row["label_zh"], row["label_en"], row["kind"] = s.get("label_zh"), s.get("label_en"), s.get("kind")
            out[s["id"]] = row
            if log:
                log(f"  FRED {s['id']}: {row['latest']} ({row['date']}) YoY {row['yoy_pct']}%")
        elif log:
            log(f"  FRED {s['id']}: fetch failed")
    return out


# --------------------------------------------------------------------------- #
# Bundle: yfinance + EDGAR (preferred for rev/capex) + FRED
# --------------------------------------------------------------------------- #
def fetch_bundle(assumptions, log=None):
    """Returns {'tickers': {...}, 'edgar': {...}, 'fred': {...}}.

    tickers (yfinance) drive the model's bottom-up live scaling — clean, current
    TTM. EDGAR provides OFFICIAL SEC-filed revenue (annual + latest quarter) as a
    provenance/cross-check panel. companyfacts is aggregate-only (no segment), so
    EDGAR is shown as provenance, not substituted into the scaling math. FRED adds
    macro demand-environment context."""
    tickers = fetch_live(log=log)
    cik_map = assumptions.get("edgar_cik_map", {}).get("map", {})
    edgar = fetch_edgar(cik_map, log=log) if cik_map else {}
    fred = fetch_fred(assumptions.get("fred_macro_series", {}).get("series", []), log=log)
    return {"tickers": tickers, "edgar": edgar, "fred": fred}


if __name__ == "__main__":
    import json as _j
    a = _j.load(open(os.path.join(os.path.dirname(__file__), "assumptions.json")))
    print(_j.dumps(fetch_bundle(a, log=print), indent=2)[:2000])
