"""
Live layer for the refresh button. Best-effort: failures never break the
dashboard — the curated KB still renders.

Two live sources (the spec KB itself is curated, not auto-fetched):
  - fetch_news(): Google News RSS for each KB news query -> recent headlines
    on new racks / chips / supplier design wins ("what's new since last update").
  - fetch_stocks(): yfinance price/%chg/market-cap for the public suppliers
    named in the KB -> supplier market context.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; ai-rack-bom-radar)"


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
        # Google News titles are "Headline - Source"
        src = ""
        m = re.search(r" - ([^-]+)$", title)
        if m:
            src = m.group(1).strip()
            title = title[: m.start()].strip()
        out.append({"title": title, "url": link, "source": src, "date": pub, "query": query})
        if len(out) >= limit:
            break
    return out


def fetch_news(queries, limit_per=3, log=None):
    seen, news = set(), []
    for q in queries:
        rows = _safe(lambda: fetch_news_query(q, limit_per)) or []
        for r in rows:
            key = r["title"][:80]
            if key in seen or not r["title"]:
                continue
            seen.add(key)
            news.append(r)
        if log:
            log(f"  news '{q}': {len(rows)} items")
    news.sort(key=lambda r: r["date"], reverse=True)
    return news


# --------------------------------------------------------------------------- #
# Stocks — supplier market context (yfinance)
# --------------------------------------------------------------------------- #
def _is_ticker(t):
    return bool(t) and t not in ("—", "-", "") and not t.startswith("(")


def collect_tickers(kb):
    ticks = set()
    for blk in kb.get("supplier_landscape", {}).values():
        if not isinstance(blk, dict):
            continue
        for r in blk.get("rows", []):
            t = r.get("ticker")
            if _is_ticker(t):
                ticks.add(t)
    return sorted(ticks)


def fetch_stocks(tickers, log=None):
    import yfinance as yf
    out = {}
    for t in tickers:
        def _one():
            tk = yf.Ticker(t)
            fast = _safe(lambda: tk.fast_info, {}) or {}
            info = _safe(lambda: tk.info, {}) or {}
            price = info.get("currentPrice") or fast.get("last_price")
            prev = info.get("previousClose") or fast.get("previous_close")
            mcap = info.get("marketCap") or fast.get("market_cap")
            chg = None
            if price and prev:
                chg = round((price / prev - 1) * 100, 2)
            return {
                "price": round(float(price), 2) if price else None,
                "change_pct": chg,
                "market_cap_bn": round(float(mcap) / 1e9, 1) if mcap else None,
            }
        row = _safe(_one)
        if row and row.get("price"):
            out[t] = row
            if log:
                log(f"  stock {t}: {row['price']} ({row['change_pct']}%)")
    return out


# --------------------------------------------------------------------------- #
# Bundle
# --------------------------------------------------------------------------- #
def fetch_bundle(kb, log=None):
    if log:
        log("fetching news (Google News RSS) ...")
    news = fetch_news(kb.get("news_queries", []), log=log)
    if log:
        log("fetching supplier stock context (yfinance) ...")
    stocks = fetch_stocks(collect_tickers(kb), log=log)
    return {"news": news, "stocks": stocks,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


if __name__ == "__main__":
    import json
    import os
    kb = json.load(open(os.path.join(os.path.dirname(__file__), "knowledge_base.json")))
    print(json.dumps(fetch_bundle(kb, log=print), indent=2, ensure_ascii=False)[:2000])
