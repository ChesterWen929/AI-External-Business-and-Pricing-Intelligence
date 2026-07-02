"""Live layer (L3) for AI Credit & Financing Radar — best-effort; failures never
break the dashboard (the curated KB still renders a coherent seed view).

Keyless sources, each per-item best-effort (a failed fetch keeps the seed and
clears the live flag):
  - yfinance          → quarterly cash-flow statement per hyperscaler:
                        "Capital Expenditure" and "Operating Cash Flow" rows →
                        TTM (last 4 quarters), in $B.
  - FRED              → keyless CSV endpoint (fredgraph.csv?id=…) for the three
                        credit spreads (BAMLC0A0CM IG, BAMLH0A0HYM2 HY,
                        BAMLH0A3HYC CCC) → level + 6-month change. No key needed.
  - Google News RSS   → financing-events radar (what's moving the ledger).

The curated financing ledger and lab numbers have NO live source (private terms
are not public) and always keep their KB seeds.
"""
from __future__ import annotations

import csv
import io
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; ai-credit-radar)"
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


# --------------------------------------------------------------------------- #
# yfinance — quarterly cash-flow → TTM capex + TTM operating cash flow ($B)
# --------------------------------------------------------------------------- #
def _row_values(df, names):
    """Newest→oldest floats for the first index label matching any of `names`
    (case-insensitive substring)."""
    if df is None or getattr(df, "empty", True):
        return None
    target = None
    for want in names:
        wl = want.lower()
        for label in df.index:
            if wl in str(label).lower():
                target = label
                break
        if target is not None:
            break
    if target is None:
        return None
    vals = [float(v) for v in df.loc[target].tolist() if v == v]  # drop NaN
    return vals or None


def _ttm(vals):
    """Sum of the last 4 quarters (newest-first list); None if <4 quarters."""
    if not vals or len(vals) < 4:
        return None
    return sum(vals[:4])


def fetch_company(ticker):
    """{"capex_ttm_usd_bn", "ocf_ttm_usd_bn", "as_of", "live": True} or None."""
    import yfinance as yf

    tk = yf.Ticker(ticker)
    cf = tk.quarterly_cashflow
    capex_vals = _row_values(cf, ["capital expenditure"])
    ocf_vals = _row_values(cf, ["operating cash flow", "cash flow from continuing operating"])
    capex = _ttm(capex_vals)
    ocf = _ttm(ocf_vals)
    if capex is None or ocf is None:
        return None
    as_of = ""
    try:
        as_of = str(list(cf.columns)[0].date())
    except Exception:
        pass
    return {
        "capex_ttm_usd_bn": round(abs(capex) / _BN, 1),  # yfinance capex is negative
        "ocf_ttm_usd_bn": round(ocf / _BN, 1),
        "as_of": as_of,
        "live": True,
    }


# --------------------------------------------------------------------------- #
# FRED — keyless CSV → spread level + 6-month change
# --------------------------------------------------------------------------- #
def _fred_csv_obs(series):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series)}"
    text = _get(url, timeout=25)
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return None
    obs = []  # rows[0] is the header (observation_date,<SERIES>)
    for r in rows[1:]:
        if len(r) < 2:
            continue
        v = r[1].strip()
        if v in (".", "", "NA"):
            continue
        d = _safe(lambda: float(v))
        if d is not None:
            obs.append((r[0], d))
    return obs or None


def _nearest_back(obs, days):
    from datetime import date

    last_d = _safe(lambda: date.fromisoformat(obs[-1][0]))
    if last_d is None:
        return obs[max(0, len(obs) - 2)][1]
    target = last_d.toordinal() - days
    best = obs[0]
    for d_str, v in obs:
        d = _safe(lambda: date.fromisoformat(d_str))
        if d is None:
            continue
        if d.toordinal() <= target:
            best = (d_str, v)
        else:
            break
    return best[1]


def fetch_spread(series):
    """{"value", "chg_6m", "as_of", "live": True} in percentage points, or None."""
    obs = _safe(lambda: _fred_csv_obs(series))
    if not obs:
        return None
    value = round(obs[-1][1], 2)
    six_m = _nearest_back(obs, 182)
    return {"value": value, "chg_6m": round(value - six_m, 2), "as_of": obs[-1][0], "live": True}


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
    companies = {}
    for h in kb.get("hyperscalers", []):
        row = _safe(lambda: fetch_company(h["ticker"]))
        if row:
            companies[h["id"]] = row
            if log:
                log(f"  {h['id']}: capex {row['capex_ttm_usd_bn']} / ocf {row['ocf_ttm_usd_bn']} $B TTM")
        elif log:
            log(f"  {h['id']}: FAILED (seed will be used)")
    spreads = {}
    for s in kb.get("fred_series", []):
        row = fetch_spread(s["series"])
        if row:
            spreads[s["id"]] = row
            if log:
                log(f"  {s['id']}: {row['value']}% (6m {row['chg_6m']})")
        elif log:
            log(f"  {s['id']}: FAILED (seed will be used)")
    news = fetch_news(kb.get("news_queries", []))
    return {
        "companies": companies,
        "spreads": spreads,
        "news": news,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


if __name__ == "__main__":
    import json
    import os

    kb = json.load(open(os.path.join(os.path.dirname(__file__), "knowledge_base.json")))
    print(json.dumps(fetch_bundle(kb, log=print), indent=2, ensure_ascii=False)[:3000])
