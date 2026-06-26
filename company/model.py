"""Company Deep-Dive (Company Lens) — L1/L2/L3 quant engine + snapshot assembler.

One company decomposed through four pillars, all framed for a TSMC CEO:
  A · pricing    — how the company raises its REALIZED price per unit of compute,
                   scored 0-100 from named pricing LEVERS (list price ≠ realized price)
  B · sources    — the data-source dictionary: where each lever can be observed
  C · benefit    — multi-method estimate of how much the company makes from AI
  D · silicon    — the chain from each accelerator → node → CoWoS → TSMC, and the
                   TSMC-exposure read that ties the benefit back to leading-edge supply

build_snapshot(kb, live, ...) merges live equity/GPU proxies (sentiment context
only — kept OUT of the core score) over the curated seed, computes per-pillar
reads + deterministic alerts, then analysis.analyze() writes L4/L5 (Claude, or a
rules fallback so the board is fully functional offline).
"""
from __future__ import annotations

from . import analysis

_VERDICT = {
    "raising": {"en": "RAISING — realized compute price expanding", "zh": "RAISING — 已實現算力售價擴張"},
    "holding": {"en": "HOLDING — realized price roughly flat",       "zh": "HOLDING — 已實現價格大致持平"},
    "eroding": {"en": "ERODING — realized price under pressure",     "zh": "ERODING — 已實現價格受壓"},
}


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _r(x, n=2):
    return None if x is None else round(x, n)


# --------------------------------------------------------------------------- #
# Live proxies — sentiment context only (NOT in the pricing-power score)
# --------------------------------------------------------------------------- #
def _merge_proxies(kb, live):
    live_metrics = (live or {}).get("metrics", {}) if live else {}
    out = []
    for p in kb.get("live_proxies", []):
        pid = p["id"]
        m = live_metrics.get(pid)
        if m:
            row = {"value": m.get("value"), "chg_1w": m.get("chg_1w"), "chg_1m": m.get("chg_1m"), "live": True}
        else:
            seed = p.get("seed", {})
            row = {"value": seed.get("value"), "chg_1w": seed.get("chg_1w"), "chg_1m": seed.get("chg_1m"), "live": False}
        out.append({
            "id": pid, "name_en": p["name_en"], "name_zh": p["name_zh"], "kind": p.get("kind", "equity"),
            **row,
        })
    return out


# --------------------------------------------------------------------------- #
# Pillar A — compute pricing power from the levers
# --------------------------------------------------------------------------- #
def _pricing(kb):
    levers = []
    num = wsum = 0.0
    for lv in kb.get("pricing_levers", []):
        s = float(lv.get("strength", 50))
        w = float(lv.get("weight", 1.0))
        num += w * s
        wsum += w
        levers.append({
            "id": lv["id"], "name_en": lv["name_en"], "name_zh": lv["name_zh"],
            "mechanism_en": lv["mechanism_en"], "mechanism_zh": lv["mechanism_zh"],
            "direction": lv.get("direction", "up"), "strength": s, "weight": w,
            "tier": lv.get("tier"), "is_estimate": bool(lv.get("is_estimate")),
            "observability_en": lv.get("observability_en", ""), "observability_zh": lv.get("observability_zh", ""),
            "source_en": lv.get("source_en", ""), "source_zh": lv.get("source_zh", ""),
            "source_url": lv.get("source_url", ""),
        })
    score = round(_clamp(num / wsum if wsum else 50.0, 0, 100), 1)
    key = "raising" if score >= 60 else "eroding" if score <= 40 else "holding"
    # strongest lever drives the headline
    top = max(levers, key=lambda x: x["strength"] * x["weight"]) if levers else None
    return {
        "score": score, "verdict_key": key,
        "verdict_en": _VERDICT[key]["en"], "verdict_zh": _VERDICT[key]["zh"],
        "levers": sorted(levers, key=lambda x: -(x["strength"] * x["weight"])),
        "top_lever_id": top["id"] if top else None,
    }


# --------------------------------------------------------------------------- #
# Pillar C — AI benefit, multi-method
# --------------------------------------------------------------------------- #
def _benefit(kb):
    ests = []
    for e in kb.get("benefit_estimates", []):
        ests.append({
            "id": e["id"], "method_en": e["method_en"], "method_zh": e["method_zh"],
            "value_usd_bn": float(e.get("value_usd_bn", 0.0)), "metric": e.get("metric"),
            "confidence": e.get("confidence"), "tier": e.get("tier"), "is_estimate": bool(e.get("is_estimate")),
            "basis_en": e.get("basis_en", ""), "basis_zh": e.get("basis_zh", ""),
        })
    head_id = kb.get("benefit_headline_id")
    headline = next((e for e in ests if e["id"] == head_id), ests[0] if ests else None)
    # consensus across estimates that share the headline metric (don't add op_income to revenue)
    if headline:
        same = [e["value_usd_bn"] for e in ests if e["metric"] == headline["metric"]]
        consensus = round(sum(same) / len(same), 1) if same else headline["value_usd_bn"]
    else:
        consensus = None
    return {
        "estimates": ests,
        "headline_id": head_id,
        "headline_usd_bn": headline["value_usd_bn"] if headline else None,
        "headline_metric": headline["metric"] if headline else None,
        "consensus_usd_bn": consensus,
    }


# --------------------------------------------------------------------------- #
# Pillar D — silicon / TSMC linkage
# --------------------------------------------------------------------------- #
_DEP_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _silicon(kb):
    chain = []
    for c in kb.get("silicon_chain", []):
        chain.append({
            "accelerator_en": c["accelerator_en"], "accelerator_zh": c["accelerator_zh"],
            "role_en": c.get("role_en", ""), "role_zh": c.get("role_zh", ""),
            "designer": c.get("designer", ""), "node": c.get("node", ""),
            "packaging": c.get("packaging", ""), "fab": c.get("fab", ""),
            "dependency": c.get("dependency", "high"), "tier": c.get("tier"),
            "is_estimate": bool(c.get("is_estimate")),
            "note_en": c.get("note_en", ""), "note_zh": c.get("note_zh", ""),
        })
    tsmc_fabbed = sum(1 for c in chain if (c["fab"] or "").upper().startswith("TSMC"))
    critical = sum(1 for c in chain if c["dependency"] == "critical")
    return {
        "chain": sorted(chain, key=lambda x: -_DEP_RANK.get(x["dependency"], 0)),
        "tsmc_exposure_pct": kb.get("tsmc_exposure_pct", 100),
        "tsmc_fabbed_count": tsmc_fabbed, "chain_count": len(chain),
        "critical_count": critical,
        "tsmc_read_en": kb.get("tsmc_read_en", ""), "tsmc_read_zh": kb.get("tsmc_read_zh", ""),
    }


# --------------------------------------------------------------------------- #
# Deterministic alerts (data, not prose)
# --------------------------------------------------------------------------- #
def _alerts(pricing, benefit, silicon, proxies):
    out = []
    by_id = {p["id"]: p for p in proxies}

    if pricing["score"] >= 65:
        out.append({"level": "strong",
                    "en": f"Realized-price engine running hot: pricing-power score {pricing['score']}/100 — the custom-silicon + up-stack levers are doing the work, not list-price hikes.",
                    "zh": f"已實現價格引擎火熱：定價權分數 {pricing['score']}/100 — 推動的是自研晶片＋服務棧上移槓桿，而非抬牌價。"})

    nv = by_id.get("nvda_equity", {})
    if nv.get("chg_1m") is not None and nv["chg_1m"] < -8:
        out.append({"level": "watch",
                    "en": f"GPU-scarcity proxy cooling: NVDA {nv['chg_1m']}%/m — watch the accelerator-scarcity premium (capacity-block pricing) for follow-through.",
                    "zh": f"GPU 稀缺 proxy 降溫：NVDA {nv['chg_1m']}%/月 — 留意加速器稀缺溢價（capacity-block 定價）是否跟跌。"})

    if silicon["tsmc_exposure_pct"] >= 90 and silicon["critical_count"] >= 1:
        out.append({"level": "exposure",
                    "en": f"Concentration: ~{silicon['tsmc_exposure_pct']}% of the AI compute behind this benefit is TSMC-fabbed, {silicon['critical_count']} link(s) critical — the CoWoS bottleneck TSMC controls is the binding constraint on growth.",
                    "zh": f"集中度：撐起此利益的 AI 算力約 {silicon['tsmc_exposure_pct']}% 由台積電製造，{silicon['critical_count']} 條鏈關鍵 — 台積電掌握的 CoWoS 瓶頸即成長綁定約束。"})

    if benefit.get("headline_usd_bn"):
        out.append({"level": "watch",
                    "en": f"AI benefit (headline est.) ≈ ${benefit['headline_usd_bn']}B {benefit['headline_metric']} — an ESTIMATE; AWS discloses no AI-only line.",
                    "zh": f"AI 利益（頭條估計）≈ ${benefit['headline_usd_bn']}B {benefit['headline_metric']} — 為估計值；AWS 無純 AI 揭露項。"})

    if not out:
        out.append({"level": "watch", "en": "No sharp signal this read.", "zh": "本次判讀無明顯訊號。"})
    return out


# --------------------------------------------------------------------------- #
# Snapshot assembler
# --------------------------------------------------------------------------- #
def build_snapshot(kb, live=None, generated_at="", today=""):
    proxies = _merge_proxies(kb, live)
    pricing = _pricing(kb)
    benefit = _benefit(kb)
    silicon = _silicon(kb)
    alerts = _alerts(pricing, benefit, silicon, proxies)

    l3 = {
        "proxies": proxies,
        "alerts": alerts,
    }

    pillars = {
        "pricing": pricing,
        "sources": {"items": kb.get("data_sources", [])},
        "benefit": benefit,
        "silicon": silicon,
    }

    analysis_out = analysis.analyze(kb, pillars, l3)

    # freshness
    fetchable = [p for p in kb.get("live_proxies", []) if p.get("fetch")]
    fetched_ok = [p for p in proxies if p["live"]]
    stale = [p["id"] for p in kb.get("live_proxies", []) if p.get("fetch")
             and not next((q for q in proxies if q["id"] == p["id"] and q["live"]), None)]
    if not live:
        source = "seed"
    elif fetched_ok and stale:
        source = "partial"
    elif fetched_ok:
        source = "live"
    else:
        source = "seed"

    return {
        "slug": kb.get("slug", "company"),
        "generated_at": generated_at,
        "as_of": today or kb.get("as_of_curated", ""),
        "source": source,
        "stale_fetch_ids": stale if live else [],
        "is_demo": live is None,
        "company": kb.get("company", {}),
        "thesis_en": kb.get("thesis_en", ""), "thesis_zh": kb.get("thesis_zh", ""),
        "headline": {
            "compute_pricing_score": pricing["score"],
            "verdict_key": pricing["verdict_key"],
            "verdict_en": pricing["verdict_en"], "verdict_zh": pricing["verdict_zh"],
            "ai_benefit_usd_bn": benefit.get("headline_usd_bn"),
            "ai_benefit_metric": benefit.get("headline_metric"),
            "tsmc_exposure_pct": silicon.get("tsmc_exposure_pct"),
        },
        "pillars": pillars,
        "l3": l3,
        "l4": analysis_out["l4"],
        "l5": analysis_out["l5"],
        "analysis_engine": analysis_out["engine"],
        "cross_links": kb.get("cross_links", []),
        "blind_spots_en": kb.get("blind_spots_en", []),
        "blind_spots_zh": kb.get("blind_spots_zh", []),
        "news": (live or {}).get("news", []) if live else [],
        "fetched_at": (live or {}).get("fetched_at") if live else None,
    }
