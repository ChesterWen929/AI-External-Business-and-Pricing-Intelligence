"""Live layer (L3) for Capital Flow Radar — best-effort; failures never break
the dashboard (the seed KB still renders a coherent demo).

Three keyless sources + a news radar:
  - yfinance        → price & % change for each market reservoir (gold, BTC, SPX,
                      RSP, QQQ, SOXX, ARKK, IWM, TLT, HYG, DXY, VIX).
  - FRED            → keyless CSV endpoint (fredgraph.csv?id=…) for liquidity /
                      rates / credit series (WALCL, RRP, TGA, WM2NS, DFII10,
                      DGS10, BAMLH0A0HYM2). No FRED_API_KEY needed.
  - defillama       → total stablecoin circulating supply (crypto dry powder).
  - Google News RSS → headlines per KB query (what's moving the flows).

Each indicator in the KB carries a `fetch` spec; fetch_indicator() dispatches on
`fetch.kind`. Output per id: {"value", "chg_1w", "chg_1m", "live": True}.
"""
from __future__ import annotations

import csv
import io
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; capital-flow-radar)"


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
def fetch_yfinance(ticker, is_level=False):
    import yfinance as yf

    tk = yf.Ticker(ticker)
    hist = tk.history(period="3mo", interval="1d")
    if hist is None or hist.empty:
        return None
    closes = [float(x) for x in hist["Close"].tolist() if x == x]  # drop NaN
    if not closes:
        return None
    value = round(closes[-1], 2)
    # ~5 trading days = 1w, ~21 = 1m
    wk = closes[-6] if len(closes) > 6 else closes[0]
    mo = closes[-22] if len(closes) > 22 else closes[0]
    if is_level:  # VIX, yields → report absolute point change, not %
        return {"value": value, "chg_1w": round(value - wk, 2), "chg_1m": round(value - mo, 2), "live": True}
    return {"value": value, "chg_1w": _pct(value, wk), "chg_1m": _pct(value, mo), "live": True}


# --------------------------------------------------------------------------- #
# FRED — keyed API (api.stlouisfed.org) when FRED_API_KEY is set, else keyless
# CSV (fredgraph.csv). Both yield (date, value) oldest→newest.
# --------------------------------------------------------------------------- #
def _fred_api_obs(series):
    import os

    key = os.environ.get("FRED_API_KEY")
    if not key:
        return None
    url = ("https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={urllib.parse.quote(series)}&api_key={urllib.parse.quote(key)}"
           "&file_type=json&sort_order=asc&observation_start=2024-01-01")
    import json

    data = json.loads(_get(url, timeout=25))
    obs = []
    for o in data.get("observations", []):
        v = (o.get("value") or "").strip()
        if v in (".", "", "NA"):
            continue
        d = _safe(lambda: float(v))
        if d is not None:
            obs.append((o["date"], d))
    return obs or None


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


def fetch_fred(series, scale=1.0, is_level=False):
    obs = _safe(lambda: _fred_api_obs(series))  # keyed path (None if no key / fails)
    if not obs:
        obs = _safe(lambda: _fred_csv_obs(series))  # keyless fallback
    if not obs:
        return None
    obs = [(d, v * scale) for d, v in obs]
    value = round(obs[-1][1], 2)
    wk = _nearest_back(obs, 7)
    mo = _nearest_back(obs, 31)
    if is_level:
        return {"value": value, "chg_1w": round(value - wk, 3), "chg_1m": round(value - mo, 3), "live": True}
    return {"value": value, "chg_1w": _pct(value, wk), "chg_1m": _pct(value, mo), "live": True}


def _nearest_back(obs, days):
    """Value ~`days` calendar days before the last observation (handles weekly/
    monthly series where exact lag rows don't exist)."""
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
# defillama — total stablecoin circulating supply ($B)
# --------------------------------------------------------------------------- #
def fetch_stablecoins():
    text = _get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=25)
    import json

    data = json.loads(text)
    if not isinstance(data, list) or not data:
        return None

    def total(point):
        cz = point.get("totalCirculatingUSD") or {}
        return sum(float(v) for v in cz.values())

    last = total(data[-1]) / 1e9
    wk = total(data[-8]) / 1e9 if len(data) > 8 else total(data[0]) / 1e9
    mo = total(data[-31]) / 1e9 if len(data) > 31 else total(data[0]) / 1e9
    return {"value": round(last, 1), "chg_1w": _pct(last, wk), "chg_1m": _pct(last, mo), "live": True}


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def fetch_indicator(ind):
    spec = ind.get("fetch", {})
    kind = spec.get("kind")
    is_level = bool(ind.get("is_level"))
    if kind == "yfinance":
        return _safe(lambda: fetch_yfinance(spec["ticker"], is_level=is_level))
    if kind == "fred":
        return _safe(lambda: fetch_fred(spec["series"], scale=spec.get("scale", 1.0), is_level=is_level))
    if kind == "defillama":
        return _safe(fetch_stablecoins)
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
    for ind in kb.get("indicators", []):
        row = fetch_indicator(ind)
        if row and row.get("value") is not None:
            metrics[ind["id"]] = row
            if log:
                log(f"  {ind['id']}: {row['value']} (1w {row['chg_1w']}, 1m {row['chg_1m']})")
        elif log:
            log(f"  {ind['id']}: FAILED (seed will be used)")
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
