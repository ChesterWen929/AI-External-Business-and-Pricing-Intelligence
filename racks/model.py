"""
AI Rack BOM × Supply-Chain Radar — model.

Pure functions: take the curated `kb` (knowledge_base.json) + optional `live`
(news + supplier stock context) -> a `snapshot` dict. No I/O, no network.

The KB is the curated, sourced core (specs + suppliers + evidence tiers).
The model adds: summary stats, HBM reconciliation checks, a supplier->systems
inverted index, staleness flags, comparison series for charts, and merges the
live news / stock layer when present.
"""
from __future__ import annotations

from datetime import date


def _parse_date(s):
    try:
        y, m, d = (int(x) for x in s.split("-")[:3])
        return date(y, m, d)
    except Exception:
        return None


def _staleness_days(last_verified, today):
    lv, td = _parse_date(last_verified), _parse_date(today)
    if not lv or not td:
        return None
    return (td - lv).days


def hbm_check(sys):
    """Reconcile accelerators × per-accel HBM vs stated total (TB). Returns
    (ok, implied_tb) or (None, None) when not applicable (LPDDR / unknown count)."""
    n = sys.get("accelerators", 0)
    per = sys.get("hbm_gb_per_accel", 0)
    total = sys.get("hbm_total_tb", 0)
    if not n or not per or not total or "LPDDR" in (sys.get("hbm_type") or ""):
        return None, None
    implied_raw = n * per / 1000.0
    ok = abs(implied_raw - total) / total <= 0.05 if total else False
    return ok, round(implied_raw, 2)


def supplier_index(systems):
    """Invert systems -> suppliers: which systems each supplier appears in."""
    idx = {}
    for s in systems:
        for cat, lst in (s.get("suppliers") or {}).items():
            for sup in lst:
                # take the leading vendor name token (before '/' or '(')
                name = sup.get("name", "").split("(")[0].strip()
                key = name
                e = idx.setdefault(key, {"name": name, "categories": set(), "systems": [], "count": 0})
                e["categories"].add(cat)
                e["systems"].append(s["id"])
                e["count"] += 1
    # serialize sets
    out = []
    for e in idx.values():
        out.append({"name": e["name"], "categories": sorted(e["categories"]),
                    "systems": e["systems"], "count": e["count"]})
    out.sort(key=lambda x: -x["count"])
    return out


_TIER_ORDER = {"T1": 1, "T2": 2, "T3": 3}


def collect_references(kb):
    """Aggregate EVERY source URL in the KB (system specs + supplier landscape)
    into one deduplicated, tiered bibliography with back-references. Proves that
    every claim is traceable to a citation."""
    items = {}

    def add(url, label, tier, cite):
        if not url:
            return
        e = items.setdefault(url, {"url": url, "label": label, "tier": tier, "cited_by": []})
        if cite and cite not in e["cited_by"]:
            e["cited_by"].append(cite)
        if _TIER_ORDER.get(tier, 9) < _TIER_ORDER.get(e["tier"], 9):
            e["tier"], e["label"] = tier, label

    n_sourced = 0
    for s in kb.get("systems", []):
        srcs = s.get("sources", [])
        if srcs:
            n_sourced += 1
        for src in srcs:
            add(src.get("url"), src.get("label"), src.get("tier"), s["name"])
    for cat, blk in kb.get("supplier_landscape", {}).items():
        if not isinstance(blk, dict):
            continue
        for r in blk.get("rows", []):
            if r.get("url"):
                add(r["url"], f"{r['name']} — {blk.get('label_en') or cat}", r.get("tier"), blk.get("label_en") or cat)

    lst = sorted(items.values(), key=lambda x: (_TIER_ORDER.get(x["tier"], 9), -len(x["cited_by"])))
    counts = {t: sum(1 for x in lst if x["tier"] == t) for t in ("T1", "T2", "T3")}
    total_systems = len(kb.get("systems", []))
    return {
        "items": lst, "counts": counts, "n_unique": len(lst),
        "n_systems_sourced": n_sourced, "total_systems": total_systems,
        "all_sourced": n_sourced == total_systems,
    }


def build_snapshot(kb, live=None, generated_at=None, today=None):
    today = today or kb["_meta"]["as_of"]
    systems_in = kb.get("systems", [])

    systems = []
    for s in systems_in:
        ok, implied = hbm_check(s)
        row = dict(s)
        row["hbm_check_ok"] = ok
        row["hbm_implied_tb"] = implied
        row["staleness_days"] = _staleness_days(s.get("last_verified"), today)
        # count supplier categories / evidence tiers for this system
        tiers = []
        for lst in (s.get("suppliers") or {}).values():
            tiers += [x.get("tier") for x in lst]
        tiers += [x.get("tier") for x in (s.get("sources") or [])]
        row["tier_mix"] = {t: tiers.count(t) for t in ("T1", "T2", "T3") if tiers.count(t)}
        systems.append(row)

    # summary
    by_vendor, by_cat, by_status = {}, {}, {}
    for s in systems:
        by_vendor[s["vendor"]] = by_vendor.get(s["vendor"], 0) + 1
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1

    shipping = [s for s in systems if s["status"] == "shipping"]
    max_hbm = max(systems, key=lambda s: s.get("hbm_total_tb") or 0)
    max_acc = max(systems, key=lambda s: s.get("accelerators") or 0)

    # comparison series (rack/pod-scale only, where a per-unit count is meaningful)
    comp = []
    for s in systems:
        if s["unit"] in ("rack", "pod", "UltraServer") and (s.get("accelerators") or 0) > 0:
            comp.append({
                "id": s["id"], "name": s["name"], "vendor": s["vendor"], "status": s["status"],
                "accelerators": s["accelerators"], "hbm_total_tb": s.get("hbm_total_tb") or 0,
                "power_kw": s.get("power_kw") or 0, "hbm_type": s.get("hbm_type"),
            })
    comp.sort(key=lambda x: -(x["hbm_total_tb"] or 0))

    sidx = supplier_index(systems)

    # merge live supplier stock context into landscape rows
    landscape = {}
    stocks = (live or {}).get("stocks", {})
    for cat, blk in kb.get("supplier_landscape", {}).items():
        if cat == "_note" or not isinstance(blk, dict):
            continue
        rows = []
        for r in blk.get("rows", []):
            rr = dict(r)
            st = stocks.get(r.get("ticker"))
            if st:
                rr["live"] = st
            rows.append(rr)
        landscape[cat] = {"label_zh": blk.get("label_zh"), "label_en": blk.get("label_en"), "rows": rows}

    stale_threshold = 120
    stale = [{"id": s["id"], "name": s["name"], "days": s["staleness_days"]}
             for s in systems if (s["staleness_days"] or 0) > stale_threshold]

    return {
        "generated_at": generated_at,
        "as_of": kb["_meta"]["as_of"],
        "today": today,
        "live_present": bool(live),
        "tier_legend": kb["_meta"].get("tier_legend", {}),
        "category_legend": kb["_meta"].get("category_legend", {}),
        "summary": {
            "n_systems": len(systems),
            "by_vendor": by_vendor, "by_category": by_cat, "by_status": by_status,
            "n_vendors": len(by_vendor),
            "max_hbm": {"name": max_hbm["name"], "tb": max_hbm.get("hbm_total_tb")},
            "max_accelerators": {"name": max_acc["name"], "n": max_acc.get("accelerators")},
            "n_shipping": len(shipping),
            "hbm_check_pass": sum(1 for s in systems if s["hbm_check_ok"]),
            "hbm_check_total": sum(1 for s in systems if s["hbm_check_ok"] is not None),
        },
        "systems": systems,
        "comparison": comp,
        "supplier_index": sidx,
        "supplier_landscape": landscape,
        "staleness": {"threshold_days": stale_threshold, "stale": stale},
        "news": (live or {}).get("news", []),
        "news_queries": kb.get("news_queries", []),
        "references": collect_references(kb),
    }
