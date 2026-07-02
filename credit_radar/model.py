"""AI Credit & Financing Radar — L3 quant engine + snapshot assembler.

build_snapshot(kb, live, ...) merges live metrics over the KB seeds and computes:
  • hyperscaler funding gaps   — TTM capex − TTM operating cash flow, per company
                                 (capex canonical, aligned with /payback 2026-07-02;
                                 OCF live via yfinance with seed fallback)
  • credit backdrop            — IG / HY / CCC OAS levels + 6m trend (keyless FRED),
                                 CCC−IG differential and a "decompression" flag
  • financing-events ledger    — curated (all EST); soft-money share weighted by
                                 the funding-stack softness rank (1 hard … 5 softest)
  • lab burn multiples         — burn ÷ revenue run-rate (canonical /payback values)
  • composite credit-tightness — 0 (fully self-funded / easy) … 100 (debt &
                                 off-balance-sheet extreme); weights hard-coded in
                                 the KB and disclosed in the methodology panel.
Then calls analysis.analyze() for L4/L5 (Claude, or rules fallback).
"""
from __future__ import annotations

from . import analysis

VERDICTS = {
    "SELF-FUNDED": {"en": "SELF-FUNDED — capex still paid with hard money",
                    "zh": "SELF-FUNDED — capex 仍由硬錢支付"},
    "LEVERING":    {"en": "LEVERING — the marginal dollar is migrating to debt & off-balance-sheet",
                    "zh": "LEVERING — 邊際資金正轉向債與表外結構"},
    "STRESSED":    {"en": "STRESSED — soft layers repricing; funding is the story now",
                    "zh": "STRESSED — 軟錢層重新定價;融資本身就是行情"},
}


def _clamp01(x):
    return max(0.0, min(1.0, x))


def verdict_for(score, thresholds):
    if score < thresholds.get("self_funded_max", 35.0):
        return "SELF-FUNDED"
    if score < thresholds.get("levering_max", 60.0):
        return "LEVERING"
    return "STRESSED"


# --------------------------------------------------------------------------- #
# L3a — hyperscaler funding gaps
# --------------------------------------------------------------------------- #
def _merge_hyperscalers(kb, live):
    live_cos = (live or {}).get("companies", {})
    rows = []
    for h in kb.get("hyperscalers", []):
        lv = live_cos.get(h["id"]) or {}
        capex = lv.get("capex_ttm_usd_bn")
        ocf = lv.get("ocf_ttm_usd_bn")
        is_live = capex is not None and ocf is not None
        if not is_live:
            capex = h["seed"]["capex_ttm_usd_bn"]
            ocf = h["seed"]["ocf_ttm_usd_bn"]
        gap = round(capex - ocf, 1)
        ext_share = round(max(0.0, gap) / capex * 100, 1) if capex else None
        capex_ocf = round(capex / ocf * 100, 1) if ocf else None
        rows.append({
            "id": h["id"], "name": h["name"], "ticker": h["ticker"],
            "capex_ttm_usd_bn": round(capex, 1), "ocf_ttm_usd_bn": round(ocf, 1),
            "gap_usd_bn": gap, "external_share_pct": ext_share,
            "capex_ocf_pct": capex_ocf, "live": bool(is_live),
            "capex_tier": h.get("capex_tier", "T1"), "ocf_tier": h.get("ocf_tier", "T2"),
            "ocf_est": bool(h.get("ocf_est")), "as_of": (lv.get("as_of") or h["seed"].get("as_of", "")),
            "align_note": h.get("align_note", ""),
            "note_en": h.get("note_en", ""), "note_zh": h.get("note_zh", ""),
        })
    return rows


def _aggregate(rows, ledger):
    total_capex = sum(r["capex_ttm_usd_bn"] for r in rows)
    gap_total = sum(max(0.0, r["gap_usd_bn"]) for r in rows)
    gap_share = round(gap_total / total_capex * 100, 1) if total_capex else 0.0
    debt_bn = sum(e.get("size_drawn_usd_bn", 0.0) for e in ledger if e.get("counts_debt_issuance"))
    debt_share = round(debt_bn / total_capex * 100, 1) if total_capex else 0.0
    return {
        "capex_total_usd_bn": round(total_capex, 1),
        "gap_total_usd_bn": round(gap_total, 1),
        "gap_share_pct": gap_share,
        "debt_issuance_usd_bn": round(debt_bn, 1),
        "debt_issuance_share_pct": debt_share,
    }


# --------------------------------------------------------------------------- #
# L3b — credit backdrop (FRED spreads)
# --------------------------------------------------------------------------- #
def _merge_spreads(kb, live):
    live_sp = (live or {}).get("spreads", {})
    out = {}
    for s in kb.get("fred_series", []):
        lv = live_sp.get(s["id"]) or {}
        if lv.get("value") is not None:
            out[s["id"]] = {"value": lv["value"], "chg_6m": lv.get("chg_6m"),
                            "as_of": lv.get("as_of", ""), "live": True,
                            "tier": s.get("tier", "T1"), "series": s["series"],
                            "align_note": s.get("align_note", "")}
        else:
            seed = s.get("seed", {})
            out[s["id"]] = {"value": seed.get("value"), "chg_6m": seed.get("chg_6m"),
                            "as_of": seed.get("as_of", ""), "live": False,
                            "tier": s.get("tier", "T1"), "series": s["series"],
                            "align_note": s.get("align_note", "")}
    ig, hy, ccc = out.get("ig_oas", {}), out.get("hy_oas", {}), out.get("ccc_oas", {})
    diff = None
    if ccc.get("value") is not None and ig.get("value") is not None:
        diff = round(ccc["value"] - ig["value"], 2)
    # decompression: the weakest credits widening while HY stays calm — the
    # bottom of the stack cracking before the public middle notices.
    decomp = bool((ccc.get("chg_6m") or 0) > 0.25 and (hy.get("chg_6m") or 0) < 0.10)
    out["ccc_minus_ig"] = diff
    out["decompression"] = decomp
    return out


# --------------------------------------------------------------------------- #
# L3c — ledger + soft-money share
# --------------------------------------------------------------------------- #
def _ledger_rows(kb):
    instruments = kb.get("instruments", {})
    rows = []
    for e in kb.get("ledger", []):
        inst = instruments.get(e["instrument"], {})
        rows.append({**e,
                     "instrument_en": inst.get("name_en", e["instrument"]),
                     "instrument_zh": inst.get("name_zh", e["instrument"]),
                     "softness": inst.get("softness", 3)})
    return rows


def soft_money_score(ledger_rows):
    """Drawn-size-weighted softness of the financing ledger, 0 (all hardest
    rank-1 money) … 100 (all softest rank-5). Softness rank r → (r−1)/4×100."""
    total = sum(r.get("size_drawn_usd_bn", 0.0) for r in ledger_rows)
    if not total:
        return 0.0, 0.0
    weighted = sum(r.get("size_drawn_usd_bn", 0.0) * (r["softness"] - 1) / 4.0 * 100.0
                   for r in ledger_rows)
    return round(weighted / total, 1), round(total, 1)


# --------------------------------------------------------------------------- #
# L3d — labs
# --------------------------------------------------------------------------- #
def _lab_rows(kb):
    rows = []
    for lab in kb.get("labs", []):
        seed = lab["seed"]
        rev = seed["revenue_runrate_usd_bn"]["value"]
        burn = seed["annual_burn_usd_bn"]["value"]
        mult = round(burn / rev, 2) if rev else None
        rows.append({
            "id": lab["id"], "name": lab["name"],
            "revenue_runrate_usd_bn": rev,
            "funding_raised_usd_bn": seed["funding_raised_usd_bn"]["value"],
            "valuation_usd_bn": seed["valuation_usd_bn"]["value"],
            "annual_burn_usd_bn": burn,
            "burn_multiple": mult,
            "tier": "T3", "est": True, "as_of": seed["revenue_runrate_usd_bn"].get("as_of", ""),
            "align_note": lab.get("align_note", ""),
            "note_en": lab.get("note_en", ""), "note_zh": lab.get("note_zh", ""),
        })
    return rows


# --------------------------------------------------------------------------- #
# Composite credit-tightness score
# --------------------------------------------------------------------------- #
def compute_subscores(agg, spreads, soft_score, labs):
    """Each subscore 0 (self-funded / easy) … 100 (debt & off-BS extreme).
    Mappings are hard-coded here and disclosed in the methodology panel."""
    # (a) funding gap: half aggregate cash-flow gap, half observed gross debt
    # issuance — Meta shows why: OCF covers capex yet bonds get issued anyway.
    gap_comp = min(100.0, agg["gap_share_pct"] * 2.5)          # 0% → 0, 40%+ → 100
    debt_comp = min(100.0, agg["debt_issuance_share_pct"] * 2.5)
    sub_gap = round(0.5 * gap_comp + 0.5 * debt_comp, 1)

    # (b) spreads: HY level (2.5% easy → 6% shut) + CCC−IG differential
    # (5pp normal → 10pp shut) + 6m widening trend (0 → +1.5pp saturates).
    hy = spreads.get("hy_oas", {})
    hy_comp = _clamp01(((hy.get("value") or 0.0) - 2.5) / 3.5) * 100.0
    diff = spreads.get("ccc_minus_ig")
    diff_comp = _clamp01(((diff or 0.0) - 5.0) / 5.0) * 100.0
    trend_comp = _clamp01(max(0.0, (hy.get("chg_6m") or 0.0)) / 1.5) * 100.0
    sub_spreads = round(0.4 * hy_comp + 0.3 * diff_comp + 0.3 * trend_comp, 1)

    # (c) soft-money share of the curated ledger (already 0–100)
    sub_soft = round(soft_score, 1)

    # (d) lab burn: mean burn÷revenue multiple, 0.5× → 0, 2.0× → 100
    mults = [l["burn_multiple"] for l in labs if l.get("burn_multiple") is not None]
    avg_mult = sum(mults) / len(mults) if mults else 0.0
    sub_lab = round(_clamp01((avg_mult - 0.5) / 1.5) * 100.0, 1)

    return {
        "funding_gap": sub_gap,
        "spreads": sub_spreads,
        "soft_money": sub_soft,
        "lab_burn": sub_lab,
        "detail": {"gap_comp": round(gap_comp, 1), "debt_comp": round(debt_comp, 1),
                   "hy_comp": round(hy_comp, 1), "diff_comp": round(diff_comp, 1),
                   "trend_comp": round(trend_comp, 1), "avg_burn_multiple": round(avg_mult, 2)},
    }


_SUB_NAMES = {
    "funding_gap": {"en": "Funding gap & gross debt issuance", "zh": "資金缺口與發債"},
    "spreads":     {"en": "Credit-spread backdrop (CCC−IG)",    "zh": "信用利差背景(CCC−IG)"},
    "soft_money":  {"en": "GPU-collateral / vendor / SPV share", "zh": "GPU 抵押·供應商·SPV 占比"},
    "lab_burn":    {"en": "Lab burn vs runway",                  "zh": "實驗室燒錢與跑道"},
}


def compute_composite(kb, agg, spreads, soft_score, labs):
    weights = kb.get("weights", {})
    subs = compute_subscores(agg, spreads, soft_score, labs)
    score = round(sum(weights[k] * subs[k] for k in weights), 1)
    verdict = verdict_for(score, kb.get("verdict_thresholds", {}))
    return {
        "score": score,
        "verdict": verdict,
        "verdict_en": VERDICTS[verdict]["en"],
        "verdict_zh": VERDICTS[verdict]["zh"],
        "subscores": [
            {"id": k, "name_en": _SUB_NAMES[k]["en"], "name_zh": _SUB_NAMES[k]["zh"],
             "score": subs[k], "weight": weights[k]}
            for k in ("funding_gap", "spreads", "soft_money", "lab_burn")
        ],
        "detail": subs["detail"],
    }


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
def build_snapshot(kb, live=None, generated_at="", today=""):
    hyperscalers = _merge_hyperscalers(kb, live)
    ledger = _ledger_rows(kb)
    agg = _aggregate(hyperscalers, ledger)
    spreads = _merge_spreads(kb, live)
    soft_score, soft_total = soft_money_score(ledger)
    labs = _lab_rows(kb)
    composite = compute_composite(kb, agg, spreads, soft_score, labs)

    l3 = {
        "hyperscalers": hyperscalers,
        "aggregate": agg,
        "spreads": spreads,
        "ledger": ledger,
        "soft_money": {"score": soft_score, "total_drawn_usd_bn": soft_total},
        "labs": labs,
    }

    analysis_out = analysis.analyze(kb, l3, composite)

    return {
        "generated_at": generated_at,
        "as_of": today or kb.get("as_of_curated", ""),
        "source": "live" if live else "seed",
        "is_demo": live is None,
        "title_en": kb.get("title_en", "AI Credit & Financing Radar"),
        "title_zh": kb.get("title_zh", "AI 信用與融資雷達"),
        "method_en": kb.get("method_en", ""),
        "method_zh": kb.get("method_zh", ""),
        "tier_legend": kb.get("tier_legend", {}),
        "l3": l3,
        "composite": composite,
        "l4": analysis_out["l4"],
        "l5": analysis_out["l5"],
        "analysis_engine": analysis_out["engine"],
        "funding_stack": kb.get("funding_stack", []),
        "contagion_channels": kb.get("contagion_channels", []),
        "indicator_dictionary": kb.get("indicators", []),
        "ledger_note_en": kb.get("ledger_note_en", ""),
        "ledger_note_zh": kb.get("ledger_note_zh", ""),
        "tsmc_view": {"intro_en": kb.get("tsmc_view_intro_en", ""),
                      "intro_zh": kb.get("tsmc_view_intro_zh", ""),
                      "rows": kb.get("tsmc_view", [])},
        "weights": kb.get("weights", {}),
        "verdict_thresholds": kb.get("verdict_thresholds", {}),
        "blind_spots_en": kb.get("blind_spots_en", []),
        "blind_spots_zh": kb.get("blind_spots_zh", []),
        "news": (live or {}).get("news", []) if live else [],
        "fetched_at": (live or {}).get("fetched_at") if live else None,
    }
