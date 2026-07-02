"""Live layer for the Cycle Analogue Clock — best-effort; failures never break
the dashboard (KB seeds render a coherent view and keep their as-of dates).

Three sources, all optional:
  - Sibling snapshots (read-only local files, no network):
      data/aibubble/snapshot.json → hyperscaler capex YoY, H100 spot ÷ breakeven,
                                    circularity ledger ÷ TTM capex, HY OAS bps
      data/payback/snapshot.json  → capex TTM total + AI coverage (context)
      data/flows/snapshot.json    → HY OAS level (fallback for FRED)
  - FRED keyless CSV (fredgraph.csv?id=BAMLH0A0HYM2) → HY OAS level + 1-yr slope.
  - Google News RSS → analogy-debate headlines.

Output shape consumed by model._merge_today():
  {"pair_values": {pair_id: {"value", "change_1y"?, "via", "as_of"}},
   "context": {...}, "news": [...], "fetched_at": "..."}
Any item that fails is simply absent → the pair falls back to its seed and loses
its live flag.
"""
from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

UA = "Mozilla/5.0 (compatible; cycle-analogue-clock)"

PKG = Path(__file__).resolve().parent
DATA = PKG.parent / "data"


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
# Sibling snapshots — read-only, local, best-effort per item
# --------------------------------------------------------------------------- #
def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def sibling_values():
    """Pull today's inputs out of sibling cards' committed snapshots."""
    pair_values, context = {}, {}

    bubble = _read_json(DATA / "aibubble" / "snapshot.json")
    payback = _read_json(DATA / "payback" / "snapshot.json")
    flows = _read_json(DATA / "flows" / "snapshot.json")

    ttm_capex = None
    if bubble:
        asof = str(bubble.get("generated_at", ""))[:10]
        agg = (bubble.get("capex") or {}).get("agg") or {}
        yoy = agg.get("yoy")
        ttm_capex = agg.get("ttm_capex_total_b")
        if isinstance(yoy, (int, float)):
            pair_values["capex"] = {"value": round(float(yoy), 1), "via": "sibling:/aibubble", "as_of": asof}
        gs = ((bubble.get("frontier") or {}).get("gpu_spot") or {})
        h100 = (gs.get("gpus") or {}).get("h100") or {}
        band = gs.get("breakeven_band") or []
        if isinstance(h100.get("median"), (int, float)) and len(band) == 2 and band[0] + band[1] > 0:
            ratio = float(h100["median"]) / ((band[0] + band[1]) / 2.0)
            pair_values["gpu_rent"] = {"value": round(ratio, 2), "via": "sibling:/aibubble",
                                       "as_of": str(gs.get("asof", ""))[:10] or asof}
        circ = ((bubble.get("frontier") or {}).get("circularity") or {})
        ledger = circ.get("seed_total_b")
        if isinstance(ledger, (int, float)) and isinstance(ttm_capex, (int, float)) and ttm_capex > 0:
            pair_values["vendor_fin"] = {"value": round(float(ledger) / float(ttm_capex) * 100.0, 1),
                                         "via": "sibling:/aibubble", "as_of": asof}
            context["circularity_ledger_b"] = ledger
        hy = bubble.get("hy_oas") or {}
        if isinstance(hy.get("bps"), (int, float)):
            context["hy_oas_bubble_pct"] = round(hy["bps"] / 100.0, 2)
        context["ttm_capex_total_b"] = ttm_capex

    if payback:
        agg = (payback.get("l3") or {}).get("aggregate") or {}
        head = payback.get("headline") or {}
        context["payback_ai_coverage"] = head.get("coverage")
        context["payback_total_capex_ttm"] = agg.get("total_capex_ttm")

    if flows:
        for res in (flows.get("l3") or {}).get("reservoirs", []):
            for ind in res.get("indicators", []):
                if ind.get("id") == "hy_oas" and isinstance(ind.get("value"), (int, float)):
                    pair_values.setdefault(
                        "hy_oas",
                        {"value": round(float(ind["value"]), 2), "via": "sibling:/flows",
                         "as_of": str(flows.get("as_of", ""))[:10]})

    return pair_values, context


# --------------------------------------------------------------------------- #
# FRED — keyless CSV; HY OAS level + ~1-yr slope (preferred over sibling read)
# --------------------------------------------------------------------------- #
def fetch_fred_hy_oas(series="BAMLH0A0HYM2"):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series)}"
    text = _get(url, timeout=25)
    rows = list(csv.reader(io.StringIO(text)))
    obs = []
    for r in rows[1:]:
        if len(r) < 2 or r[1].strip() in (".", "", "NA"):
            continue
        v = _safe(lambda: float(r[1]))
        if v is not None:
            obs.append((r[0], v))
    if not obs:
        return None
    last_d, last_v = obs[-1]
    from datetime import date
    target = date.fromisoformat(last_d).toordinal() - 365
    back = obs[0][1]
    for d_str, v in obs:
        d = _safe(lambda: date.fromisoformat(d_str))
        if d and d.toordinal() <= target:
            back = v
        elif d and d.toordinal() > target:
            break
    return {"value": round(last_v, 2), "change_1y": round(last_v - back, 2),
            "via": "fred:BAMLH0A0HYM2", "as_of": last_d}


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
    pair_values, context = _safe(sibling_values, default=({}, {})) or ({}, {})
    fred = _safe(fetch_fred_hy_oas)
    if fred:
        pair_values["hy_oas"] = fred  # FRED direct beats the sibling read
    if log:
        for pid, row in pair_values.items():
            log(f"  {pid}: {row['value']} via {row['via']}")
    news = fetch_news(kb.get("news_queries", []))
    return {
        "pair_values": pair_values,
        "context": context,
        "news": news,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


if __name__ == "__main__":
    kb = json.load(open(PKG / "knowledge_base.json"))
    print(json.dumps(fetch_bundle(kb, log=print), indent=2, ensure_ascii=False)[:3000])
