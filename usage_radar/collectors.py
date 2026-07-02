"""Live layer (L3) for AI Usage & Token Economics Radar — best-effort; failures
never break the dashboard (the curated KB still renders a coherent seed view).

Keyless sources, each per-item best-effort (a failed fetch keeps the seed and
clears the live flag):
  - OpenRouter public API → https://openrouter.ai/api/v1/models (keyless):
                            per-model pricing → live median blended $/M tokens
                            (refreshes the openrouter_market deflation point)
                            + model count; best-effort frontend rankings
                            endpoint for a top-model mix sample. Declared
                            limitation: developer-long-tail sample.
  - Google News RSS       → usage / token-economics news radar.

Token disclosure points and lab revenue run-rates have NO live keyless source
(they are earnings-call / event statements) and always keep their KB seeds.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; ai-usage-radar)"

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
OPENROUTER_RANKINGS = "https://openrouter.ai/api/frontend/models/find?order=top-weekly"


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
# OpenRouter — keyless public API
# --------------------------------------------------------------------------- #
def _blended_from_pricing(pricing, ratio=3.0):
    """OpenRouter prices are USD per token → convert to $/M and blend 3:1."""
    pin = float(pricing.get("prompt") or 0) * 1e6
    pout = float(pricing.get("completion") or 0) * 1e6
    if pin <= 0 or pout <= 0:
        return None
    return (ratio * pin + pout) / (ratio + 1.0)


def fetch_openrouter_models(ratio=3.0):
    """{"median_blended_usd_per_m", "model_count", "as_of", "live": True} or None."""
    data = json.loads(_get(OPENROUTER_MODELS, timeout=25))
    models = data.get("data") or []
    prices = []
    for m in models:
        b = _safe(lambda: _blended_from_pricing(m.get("pricing") or {}, ratio))
        if b:
            prices.append(b)
    if not prices:
        return None
    prices.sort()
    n = len(prices)
    median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2.0
    return {
        "median_blended_usd_per_m": round(median, 3),
        "model_count": len(models),
        "priced_models": len(prices),
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "live": True,
    }


def fetch_openrouter_top_models(limit=10):
    """Best-effort top-weekly model-mix sample (unofficial endpoint) — list of
    model names, or None. Purely cosmetic; failure is expected and harmless."""
    data = json.loads(_get(OPENROUTER_RANKINGS, timeout=20))
    items = (data.get("data") or {}).get("models") or data.get("models") or []
    names = []
    for m in items:
        name = m.get("name") or m.get("slug") or (m.get("model") or {}).get("name")
        if name:
            names.append(str(name))
        if len(names) >= limit:
            break
    return names or None


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
    ratio = kb.get("blend_ratio_in_out", 3.0)
    openrouter = _safe(lambda: fetch_openrouter_models(ratio))
    if openrouter:
        top = _safe(lambda: fetch_openrouter_top_models())
        if top:
            openrouter["top_models"] = top
        if log:
            log(f"  openrouter: median ${openrouter['median_blended_usd_per_m']}/M "
                f"across {openrouter['priced_models']} priced models")
    elif log:
        log("  openrouter: FAILED (seed will be used)")
    news = fetch_news(kb.get("news_queries", []))
    return {
        "openrouter": openrouter,
        "news": news,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


if __name__ == "__main__":
    import os

    kb = json.load(open(os.path.join(os.path.dirname(__file__), "knowledge_base.json")))
    print(json.dumps(fetch_bundle(kb, log=print), indent=2, ensure_ascii=False)[:3000])
