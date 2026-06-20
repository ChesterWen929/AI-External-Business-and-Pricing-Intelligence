"""Live layer for AI Capex Payback Radar — best-effort; failures never break the
board (the curated KB still renders a coherent demo).

Per public company we pull, keyless, from yfinance:
  - quarterly cash-flow  → "Capital Expenditure" row → TTM capex (last 4 q) and
                           the prior TTM (q5–q8) for a YoY growth read.
  - quarterly income     → "Total Revenue" row → TTM revenue + prior TTM.
  - daily price history  → latest stock + 1-month % change.
The curated layer (AI-share %, cloud segment revenue, AI-only band, private labs)
has NO live source — yfinance does not expose business segments — and keeps its
KB seed. A Google-News RSS radar surfaces what's moving the story.

Output per company id: {capex_ttm_usd_bn, capex_ttm_prev_usd_bn,
revenue_ttm_usd_bn, revenue_ttm_prev_usd_bn, stock, stock_chg_1m, as_of_q, live}.
All dollar figures are in $bn.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; ai-capex-payback-radar)"
_BN = 1_000_000_000.0


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _pct(now, then):
    if now is None or then in (None, 0):
        return None
    return round((now / then - 1) * 100, 1)


# --------------------------------------------------------------------------- #
# yfinance statement parsing
# --------------------------------------------------------------------------- #
def _row_values(df, names):
    """Return a DataFrame row (newest→oldest list of floats) for the first index
    label that matches any of `names` (case-insensitive substring)."""
    if df is None or getattr(df, "empty", True):
        return None
    idx = list(df.index)
    target = None
    for want in names:
        wl = want.lower()
        for label in idx:
            if wl in str(label).lower():
                target = label
                break
        if target is not None:
            break
    if target is None:
        return None
    # columns are dates newest-first in yfinance quarterly statements
    series = df.loc[target]
    vals = [float(v) for v in series.tolist() if v == v]  # drop NaN
    return vals or None


def _ttm_pair(vals):
    """(last-4-quarter sum, prior-4-quarter sum) from a newest-first list."""
    if not vals:
        return None, None
    last4 = vals[:4]
    prev4 = vals[4:8]
    ttm = sum(last4) if len(last4) == 4 else (sum(last4) * 4 / len(last4) if last4 else None)
    prev = sum(prev4) if len(prev4) == 4 else None
    return ttm, prev


def _yoy(vals):
    """YoY % growth from a newest-first quarterly list. Prefers TTM-vs-prior-TTM
    (needs 8 quarters); falls back to latest-quarter-vs-year-ago (needs 5) since
    yfinance usually returns only ~5 quarters. None if not enough data."""
    if not vals:
        return None
    ttm, prev = _ttm_pair(vals)
    if ttm and prev:
        return _pct(ttm, prev)
    if len(vals) > 4:
        return _pct(vals[0], vals[4])
    return None


def fetch_company(ticker):
    import yfinance as yf

    tk = yf.Ticker(ticker)

    qcf = _safe(lambda: tk.quarterly_cashflow)
    capex_vals = _row_values(qcf, ["Capital Expenditure", "Capital Expenditures"])
    capex_vals = [abs(v) for v in capex_vals] if capex_vals else None  # reported negative
    capex_ttm, capex_prev = _ttm_pair(capex_vals)
    capex_yoy = _yoy(capex_vals)

    qis = _safe(lambda: tk.quarterly_income_stmt)
    if qis is None or getattr(qis, "empty", True):
        qis = _safe(lambda: tk.quarterly_financials)
    rev_vals = _row_values(qis, ["Total Revenue", "TotalRevenue"])
    rev_ttm, rev_prev = _ttm_pair(rev_vals)
    rev_yoy = _yoy(rev_vals)

    if capex_ttm is None and rev_ttm is None:
        return None

    # latest reported quarter label
    as_of_q = None
    if qcf is not None and not getattr(qcf, "empty", True):
        cols = list(qcf.columns)
        if cols:
            d = _safe(lambda: cols[0].strftime("%Y-%m-%d"))
            as_of_q = d or str(cols[0])[:10]

    # stock price + 1m change
    stock = stock_chg = None
    hist = _safe(lambda: tk.history(period="2mo", interval="1d"))
    if hist is not None and not getattr(hist, "empty", True):
        closes = [float(x) for x in hist["Close"].tolist() if x == x]
        if closes:
            stock = round(closes[-1], 2)
            mo = closes[-22] if len(closes) > 22 else closes[0]
            stock_chg = _pct(stock, mo)

    return {
        "capex_ttm_usd_bn": round(capex_ttm / _BN, 1) if capex_ttm else None,
        "capex_ttm_prev_usd_bn": round(capex_prev / _BN, 1) if capex_prev else None,
        "revenue_ttm_usd_bn": round(rev_ttm / _BN, 1) if rev_ttm else None,
        "revenue_ttm_prev_usd_bn": round(rev_prev / _BN, 1) if rev_prev else None,
        "capex_yoy": capex_yoy, "rev_yoy": rev_yoy,
        "stock": stock, "stock_chg_1m": stock_chg,
        "as_of_q": as_of_q,
        "live": True,
    }


# --------------------------------------------------------------------------- #
# News — Google News RSS
# --------------------------------------------------------------------------- #
def _fmt_date(rfc822):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(rfc822, fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
    return (rfc822 or "")[:16]


def fetch_news_query(query, limit=3):
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    xml = _get(url, timeout=15)
    root = ET.fromstring(xml)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = _fmt_date(item.findtext("pubDate"))
        src = ""
        m = re.search(r" - ([^-]+)$", title)
        if m:
            src = m.group(1).strip()
            title = title[: m.start()].strip()
        out.append({"title": title, "url": link, "source": src, "date": pub})
        if len(out) >= limit:
            break
    return out


def fetch_news(queries, limit_per=3):
    seen, news = set(), []
    for q in queries:
        for r in _safe(lambda: fetch_news_query(q, limit_per)) or []:
            key = r["title"][:80]
            if key in seen or not r["title"]:
                continue
            seen.add(key)
            news.append(r)
    news.sort(key=lambda r: r["date"], reverse=True)
    return news


# --------------------------------------------------------------------------- #
# Bundle — everything the refresh button needs
# --------------------------------------------------------------------------- #
def fetch_bundle(kb, log=None):
    metrics = {}
    for c in kb.get("companies", []):
        if c.get("kind") != "public":
            continue
        spec = c.get("fetch") or {}
        ticker = spec.get("ticker")
        if not ticker:
            continue
        row = _safe(lambda: fetch_company(ticker))
        if row and (row.get("capex_ttm_usd_bn") or row.get("revenue_ttm_usd_bn")):
            metrics[c["id"]] = row
            if log:
                log(f"  {c['id']} ({ticker}): capex {row.get('capex_ttm_usd_bn')}B rev {row.get('revenue_ttm_usd_bn')}B")
        elif log:
            log(f"  {c['id']} ({ticker}): FAILED (seed will be used)")
    news = fetch_news(kb.get("news_queries", []))
    return {
        "metrics": metrics,
        "news": news,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


if __name__ == "__main__":
    import json
    import os

    kb = json.load(open(os.path.join(os.path.dirname(__file__), "knowledge_base.json")))
    print(json.dumps(fetch_bundle(kb, log=print), indent=2, ensure_ascii=False)[:3000])
