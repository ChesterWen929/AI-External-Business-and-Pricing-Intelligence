"""Live layer (L3) for Geopolitics & Second-Chain Radar — best-effort; failures
never break the dashboard (the seed KB still renders a coherent view).

Two keyless sources:
  - yfinance        → China-semis basket (0981.HK SMIC, 1347.HK Hua Hong,
                      002371.SZ NAURA, 688012.SS AMEC, 688256.SS Cambricon)
                      vs a western-tools contrast (ASML, LRCX, AMAT).
                      Price momentum only — a sentiment PROXY, weight 0 in the
                      completeness score.
  - Google News RSS → bilingual radar (export controls / Huawei Ascend / SMIC /
                      entity list / CXMT HBM / CoWoS 國產). Headlines get
                      keyword-rule classification in engine.classify_headline.

Each fetch is per-item best-effort: failure falls back to the KB seed and the
row loses its `live` flag. No API keys required.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; geo-second-chain-radar)"


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
# yfinance — price history → value + 1m % change (momentum proxy)
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
    mo = closes[-22] if len(closes) > 22 else closes[0]  # ~21 trading days = 1m
    return {"value": value, "chg_1m": _pct(value, mo), "live": True}


def fetch_market(kb, log=None):
    out = {}
    for t in kb.get("market_basket", []):
        row = _safe(lambda: fetch_yfinance(t["ticker"]))
        if row and row.get("value") is not None:
            out[t["id"]] = row
            if log:
                log(f"  {t['id']}: {row['value']} (1m {row['chg_1m']}%)")
        elif log:
            log(f"  {t['id']}: FAILED (seed will be used)")
    return out


# --------------------------------------------------------------------------- #
# News — Google News RSS, bilingual (en + zh-TW editions)
# --------------------------------------------------------------------------- #
def _fmt_date(rfc822):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(rfc822, fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
    return (rfc822 or "")[:16]


def fetch_news_query(query, lang="en", limit=3):
    q = urllib.parse.quote(query)
    if lang == "zh":
        url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    else:
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
        out.append({"title": title, "url": link, "source": src, "date": pub, "lang": lang})
        if len(out) >= limit:
            break
    return out


def fetch_news(queries, limit_per=3):
    seen, news = set(), []
    for spec in queries:
        q, lang = spec.get("q", ""), spec.get("lang", "en")
        for r in _safe(lambda: fetch_news_query(q, lang=lang, limit=limit_per)) or []:
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
    return {
        "market": fetch_market(kb, log=log),
        "news": fetch_news(kb.get("news_queries", [])),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


if __name__ == "__main__":
    import json
    import os

    kb = json.load(open(os.path.join(os.path.dirname(__file__), "knowledge_base.json"), encoding="utf-8"))
    print(json.dumps(fetch_bundle(kb, log=print), indent=2, ensure_ascii=False)[:3000])
