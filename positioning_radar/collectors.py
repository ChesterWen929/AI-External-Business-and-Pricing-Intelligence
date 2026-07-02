"""Live layer (L3) for Positioning & Sentiment Radar — best-effort; failures
never break the dashboard (the curated KB still renders a coherent seed view).

Keyless sources, each per-item best-effort (a failed fetch keeps the seed and
clears the live flag):
  - CFTC Socrata      → legacy COT futures-only (dataset 6dca-aqww), NASDAQ-100
                        e-mini (contract code 209742): net non-commercial level,
                        4-week change, and the TRUE 3y percentile computed from
                        the fetched weekly history.
  - naaim.org         → NAAIM Exposure Index (weekly), best-effort HTML regex.
  - aaii.com          → AAII bull-bear spread (weekly), best-effort HTML regex.
  - cboe.com          → equity put/call daily statistic, best-effort HTML regex.
  - yfinance          → the RELIABLY-live proxies: ^VIX / ^VIX3M term-structure
                        ratio, QQQ/SPY 20d dollar-volume ratio, SOXX relative
                        volume (3m vs 1y).
  - Google News RSS   → positioning/sentiment news radar.

FINRA margin debt has NO live fetch by design — it publishes with a ~2-month
lag and is maintained as a curated seed (the KB row says so honestly).
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; positioning-radar)"


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
# CFTC COT — legacy futures-only via keyless Socrata API
# --------------------------------------------------------------------------- #
COT_DATASET = "6dca-aqww"          # legacy COT, futures only
COT_NQ_CODE = "209742"             # NASDAQ-100 e-mini (CME)


def fetch_cot_nq():
    """{"value": net k contracts, "chg": 4w change, "as_of", "pct3y", "live": True}
    or None. pct3y is the TRUE percentile of the latest net vs ~3y of weekly
    history fetched from the same endpoint."""
    params = urllib.parse.urlencode({
        "cftc_contract_market_code": COT_NQ_CODE,
        "$select": "report_date_as_yyyy_mm_dd,noncomm_positions_long_all,noncomm_positions_short_all",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "160",
    })
    url = f"https://publicreporting.cftc.gov/resource/{COT_DATASET}.json?{params}"
    rows = json.loads(_get(url, timeout=25))
    hist = []  # newest first: (date, net contracts)
    for r in rows:
        d = str(r.get("report_date_as_yyyy_mm_dd", ""))[:10]
        lo = _safe(lambda: float(r["noncomm_positions_long_all"]))
        sh = _safe(lambda: float(r["noncomm_positions_short_all"]))
        if d and lo is not None and sh is not None:
            hist.append((d, lo - sh))
    if len(hist) < 8:
        return None
    latest = hist[0][1]
    chg = latest - hist[4][1] if len(hist) > 4 else None
    nets = [n for _, n in hist]
    pct = sum(1 for n in nets if n <= latest) / len(nets) * 100.0
    return {
        "value": round(latest / 1000.0, 1),
        "chg": round(chg / 1000.0, 1) if chg is not None else None,
        "as_of": hist[0][0],
        "pct3y": round(pct, 1),
        "live": True,
    }


# --------------------------------------------------------------------------- #
# NAAIM / AAII / CBOE — best-effort HTML regex (seed fallback is the norm)
# --------------------------------------------------------------------------- #
def fetch_naaim():
    html = _get("https://naaim.org/programs/naaim-exposure-index/", timeout=20)
    m = re.search(r"Exposure\s+Index\s+(?:number\s+)?is[:\s]*([0-9]+(?:\.[0-9]+)?)", html, re.I)
    if not m:
        return None
    return {"value": round(float(m.group(1)), 1), "chg": None,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "live": True}


def fetch_aaii():
    html = _get("https://www.aaii.com/sentimentsurvey", timeout=20)
    bull = re.search(r"Bullish[^0-9]{0,80}([0-9]+(?:\.[0-9]+)?)\s*%", html, re.I | re.S)
    bear = re.search(r"Bearish[^0-9]{0,80}([0-9]+(?:\.[0-9]+)?)\s*%", html, re.I | re.S)
    if not (bull and bear):
        return None
    spread = round(float(bull.group(1)) - float(bear.group(1)), 1)
    return {"value": spread, "chg": None,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "live": True}


def fetch_put_call():
    html = _get("https://www.cboe.com/us/options/market_statistics/daily/", timeout=20)
    m = re.search(r"EQUITY\s+PUT/CALL\s+RATIO[^0-9]{0,120}([0-9]+(?:\.[0-9]+)?)", html, re.I | re.S)
    if not m:
        return None
    v = float(m.group(1))
    if not (0.2 <= v <= 2.0):  # sanity — the page layout shifts sometimes
        return None
    return {"value": round(v, 2), "chg": None,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "live": True}


# --------------------------------------------------------------------------- #
# yfinance proxies — the reliably-live layer
# --------------------------------------------------------------------------- #
def _closes(ticker, period):
    import yfinance as yf

    h = yf.Ticker(ticker).history(period=period)
    if h is None or getattr(h, "empty", True):
        return None
    return h


def fetch_vix_term():
    """VIX ÷ VIX3M spot ratio, 4w change, dated to the latest common session."""
    v = _closes("^VIX", "3mo")
    v3 = _closes("^VIX3M", "3mo")
    if v is None or v3 is None:
        return None
    ratio = (v["Close"] / v3["Close"]).dropna()
    if len(ratio) < 2:
        return None
    now = float(ratio.iloc[-1])
    back = float(ratio.iloc[-21]) if len(ratio) > 21 else float(ratio.iloc[0])
    return {"value": round(now, 3), "chg": round(now - back, 3),
            "as_of": str(ratio.index[-1].date()), "live": True}


def fetch_qqq_spy_vol():
    """20d dollar-volume ratio QQQ ÷ SPY, plus change vs 20 sessions earlier."""
    q = _closes("QQQ", "6mo")
    s = _closes("SPY", "6mo")
    if q is None or s is None:
        return None
    dq = (q["Close"] * q["Volume"]).dropna()
    ds = (s["Close"] * s["Volume"]).dropna()
    if len(dq) < 45 or len(ds) < 45:
        return None

    def ratio_at(offset):
        a = dq.iloc[len(dq) - 20 - offset: len(dq) - offset].mean()
        b = ds.iloc[len(ds) - 20 - offset: len(ds) - offset].mean()
        return float(a) / float(b) if b else None

    now, back = ratio_at(0), ratio_at(20)
    if now is None:
        return None
    return {"value": round(now, 2), "chg": round(now - back, 2) if back else None,
            "as_of": str(dq.index[-1].date()), "live": True}


def fetch_soxx_rel_vol():
    """SOXX mean volume, last ~3 months vs full 1y — attention on semis."""
    h = _closes("SOXX", "1y")
    if h is None:
        return None
    vol = h["Volume"].dropna()
    if len(vol) < 130:
        return None
    base = float(vol.mean())
    if not base:
        return None
    now = float(vol.iloc[-63:].mean()) / base
    back = float(vol.iloc[-83:-20].mean()) / base
    return {"value": round(now, 2), "chg": round(now - back, 2),
            "as_of": str(vol.index[-1].date()), "live": True}


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
FETCHERS = {
    "cot_nq": fetch_cot_nq,
    "naaim": fetch_naaim,
    "aaii": fetch_aaii,
    "put_call": fetch_put_call,
    "vix_term": fetch_vix_term,
    "qqq_spy_vol": fetch_qqq_spy_vol,
    "soxx_rel_vol": fetch_soxx_rel_vol,
    # margin_debt: intentionally absent — FINRA publishes with a ~2-month lag;
    # the curated seed (with its honest as_of) is the source of record.
}


def fetch_bundle(kb, log=None):
    indicators = {}
    for ind_id, fn in FETCHERS.items():
        row = _safe(fn)
        if row:
            indicators[ind_id] = row
            if log:
                log(f"  {ind_id}: {row['value']} (as of {row['as_of']})")
        elif log:
            log(f"  {ind_id}: FAILED (seed will be used)")
    news = fetch_news(kb.get("news_queries", []))
    return {
        "indicators": indicators,
        "news": news,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


if __name__ == "__main__":
    import os

    kb = json.load(open(os.path.join(os.path.dirname(__file__), "knowledge_base.json")))
    print(json.dumps(fetch_bundle(kb, log=print), indent=2, ensure_ascii=False)[:3000])
