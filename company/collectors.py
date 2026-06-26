"""Live layer for Company Deep-Dive — best-effort; failures never break the
board (the curated KB still renders a coherent demo).

The live proxies are SENTIMENT CONTEXT only — they are deliberately kept out of
the core pricing-power score (which is curated-lever based). Two keyless sources:
  - yfinance        → price & % change for AMZN / NVDA / TSM proxies.
  - Google News RSS → headlines per KB query (what's moving compute pricing).

Output per id: {"value", "chg_1w", "chg_1m", "live": True}.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; company-lens-radar)"


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _pct(now, then):
    if now is None or then in (None, 0):
        return None
    return round((now / then - 1) * 100, 2)


# --------------------------------------------------------------------------- #
# yfinance — price history → value + 1w / 1m % change (calendar-anchored)
# --------------------------------------------------------------------------- #
def fetch_yfinance(ticker):
    import yfinance as yf

    tk = yf.Ticker(ticker)
    hist = tk.history(period="3mo", interval="1d")
    if hist is None or hist.empty:
        return None
    series = []
    for ts, px in zip(hist.index, hist["Close"].tolist()):
        if px != px:  # drop NaN
            continue
        d = _safe(lambda: ts.date())
        if d is not None:
            series.append((d, float(px)))
    if not series:
        return None
    value = round(series[-1][1], 2)
    last_d = series[-1][0]
    wk = _close_on_or_before(series, last_d.toordinal() - 7)
    mo = _close_on_or_before(series, last_d.toordinal() - 30)
    return {"value": value, "chg_1w": _pct(value, wk), "chg_1m": _pct(value, mo), "live": True}


def _close_on_or_before(series, target_ord):
    best = series[0][1]
    for d, px in series:
        if d.toordinal() <= target_ord:
            best = px
        else:
            break
    return best


def fetch_item(p):
    spec = p.get("fetch")
    if not spec:
        return None
    if spec.get("kind") == "yfinance":
        return _safe(lambda: fetch_yfinance(spec["ticker"]))
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
# Bundle
# --------------------------------------------------------------------------- #
def fetch_bundle(kb, log=None):
    metrics = {}
    for p in kb.get("live_proxies", []):
        row = fetch_item(p)
        if row and row.get("value") is not None:
            metrics[p["id"]] = row
            if log:
                log(f"  {p['id']}: {row['value']} (1w {row['chg_1w']}, 1m {row['chg_1m']})")
        elif log and p.get("fetch"):
            log(f"  {p['id']}: FAILED (seed will be used)")
    news = fetch_news(kb.get("news_queries", []))
    return {
        "metrics": metrics,
        "news": news,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
