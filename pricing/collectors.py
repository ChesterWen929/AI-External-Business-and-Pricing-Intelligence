"""Live layer for Pricing Power Radar — best-effort; failures never break the
board (the curated KB still renders a coherent demo).

Only the `proxy` items carry a `fetch` spec; the curated `price` estimates have
none and keep their seed move. Two keyless sources + a news radar:
  - yfinance        → price & % change for stock/commodity proxies
                      (ASML, AMAT, HG=F copper, TSM, NVDA, AMD).
  - FRED            → keyless CSV (fredgraph.csv?id=…) for the semiconductor PPI.
  - Google News RSS → headlines per KB query (what's moving prices).

Output per id: {"value", "chg_1w", "chg_1m", "live": True}.
"""
from __future__ import annotations

import csv
import io
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; pricing-power-radar)"


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
    return round((now / then - 1) * 100, 2)


# --------------------------------------------------------------------------- #
# yfinance — price history → value + 1w / 1m % change
# --------------------------------------------------------------------------- #
def fetch_yfinance(ticker):
    import yfinance as yf

    tk = yf.Ticker(ticker)
    hist = tk.history(period="3mo", interval="1d")
    if hist is None or hist.empty:
        return None
    closes = [float(x) for x in hist["Close"].tolist() if x == x]  # drop NaN
    if not closes:
        return None
    value = round(closes[-1], 2)
    wk = closes[-6] if len(closes) > 6 else closes[0]
    mo = closes[-22] if len(closes) > 22 else closes[0]
    return {"value": value, "chg_1w": _pct(value, wk), "chg_1m": _pct(value, mo), "live": True}


# --------------------------------------------------------------------------- #
# FRED — keyless CSV (fredgraph.csv). Yields (date, value) oldest→newest.
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


def fetch_fred(series):
    obs = _safe(lambda: _fred_csv_obs(series))
    if not obs:
        return None
    value = round(obs[-1][1], 2)
    wk = _nearest_back(obs, 31)   # PPI is monthly; ~1m back for "1w" slot
    mo = _nearest_back(obs, 62)   # ~2m back for the "1m" slot
    return {"value": value, "chg_1w": _pct(value, wk), "chg_1m": _pct(value, mo), "live": True}


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


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def fetch_item(it):
    spec = it.get("fetch")
    if not spec:
        return None  # curated price estimate — no live source
    kind = spec.get("kind")
    if kind == "yfinance":
        return _safe(lambda: fetch_yfinance(spec["ticker"]))
    if kind == "fred":
        return _safe(lambda: fetch_fred(spec["series"]))
    return None


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
    for it in kb.get("items", []):
        row = fetch_item(it)
        if row and row.get("value") is not None:
            metrics[it["id"]] = row
            if log:
                log(f"  {it['id']}: {row['value']} (1w {row['chg_1w']}, 1m {row['chg_1m']})")
        elif log and it.get("fetch"):
            log(f"  {it['id']}: FAILED (seed will be used)")
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
