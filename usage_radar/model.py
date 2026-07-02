"""AI Usage & Token Economics Radar — L3 quant engine + snapshot assembler.

build_snapshot(kb, live, ...) merges live metrics over the KB seeds and computes:
  • token disclosure ledger    — dated public token-throughput disclosures per
                                 platform (curated, tier-tagged); annualized
                                 growth computed between disclosure points,
                                 WITHIN a platform only (scopes differ)
  • per-token price deflation  — flagship API list prices 2023→2026 (3:1 in:out
                                 blend), annualized change per family + mean;
                                 the OpenRouter market point can refresh live
  • monetization               — realized $/M tokens = revenue run-rate ÷
                                 annualized tokens (OpenAI $25B / Anthropic $12B
                                 aligned with /payback KB v2 2026-07-02), plus a
                                 serving-cost floor derived from /aibubble's
                                 vast.ai H100 rent
  • usage-vs-spend scissors    — token unit growth AND deflation-adjusted dollar
                                 growth vs capex +80.6% YoY (aligned /aibubble)
  • composite demand-reality   — 0 (spend far ahead of use) … 100 (usage real &
                                 compounding); weights hard-coded in the KB and
                                 disclosed in the methodology panel.
Then calls analysis.analyze() for L4/L5 (Claude, or rules fallback).
"""
from __future__ import annotations

from . import analysis

VERDICTS = {
    "REAL-AND-COMPOUNDING": {
        "en": "REAL-AND-COMPOUNDING — usage dollars and units both outrun spend",
        "zh": "REAL-AND-COMPOUNDING — 用量的美元與單位都跑贏支出;需求是真的且在複利"},
    "GROWING-BUT-UNPAID": {
        "en": "GROWING-BUT-UNPAID — units outrun spend, but deflation eats the dollars",
        "zh": "GROWING-BUT-UNPAID — 單位跑贏支出,但通縮吃掉美元;用量是真的,變現還沒跟上"},
    "SPEND-AHEAD-OF-USE": {
        "en": "SPEND-AHEAD-OF-USE — even unit growth trails capex; the final judge is flashing",
        "zh": "SPEND-AHEAD-OF-USE — 連單位增速都輸給 capex;泡沫的最終裁判亮燈"},
}


def _clamp01(x):
    return max(0.0, min(1.0, x))


def verdict_for(score, thresholds):
    if score < thresholds.get("spend_ahead_max", 40.0):
        return "SPEND-AHEAD-OF-USE"
    if score < thresholds.get("unpaid_max", 70.0):
        return "GROWING-BUT-UNPAID"
    return "REAL-AND-COMPOUNDING"


# --------------------------------------------------------------------------- #
# helpers — dated growth math
# --------------------------------------------------------------------------- #
def months_between(d1, d2):
    """Whole months between two 'YYYY-MM' strings (d2 later than d1)."""
    y1, m1 = int(d1[:4]), int(d1[5:7])
    y2, m2 = int(d2[:4]), int(d2[5:7])
    return (y2 - y1) * 12 + (m2 - m1)


def annualized_growth_pct(v1, v2, months):
    """Annualized % change between two dated values; None if not computable."""
    if not v1 or not v2 or months <= 0:
        return None
    return ((v2 / v1) ** (12.0 / months) - 1.0) * 100.0


def blended_price(point, ratio=3.0):
    """$/M tokens blended at ratio:1 input:output; direct 'blended' wins."""
    if point.get("blended_usd_per_m") is not None:
        return float(point["blended_usd_per_m"])
    return (ratio * float(point["in_usd_per_m"]) + float(point["out_usd_per_m"])) / (ratio + 1.0)


def _median(vals):
    vals = sorted(vals)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


# --------------------------------------------------------------------------- #
# L3a — token disclosure ledger
# --------------------------------------------------------------------------- #
def token_ledger(kb):
    rows = []
    for p in kb.get("token_platforms", []):
        pts = p["points"]
        first, prev, last = pts[0], pts[-2], pts[-1]
        g = annualized_growth_pct(prev["monthly_tokens_t"], last["monthly_tokens_t"],
                                  months_between(prev["date"], last["date"]))
        mult = round(last["monthly_tokens_t"] / first["monthly_tokens_t"], 1) if first["monthly_tokens_t"] else None
        rows.append({
            "id": p["id"], "name": p["name"], "class": p["class"],
            "latest_monthly_tokens_t": last["monthly_tokens_t"],
            "latest_date": last["date"], "latest_tier": last["tier"],
            "latest_est": bool(last.get("est")),
            "growth_yoy_pct": round(g, 1) if g is not None else None,
            "multiple_since_first": mult, "first_date": first["date"],
            "points": pts,
            "note_en": p.get("note_en", ""), "note_zh": p.get("note_zh", ""),
        })
    growths = [r["growth_yoy_pct"] for r in rows if r["growth_yoy_pct"] is not None]
    return {"platforms": rows,
            "growth_median_yoy_pct": round(_median(growths), 1) if growths else None,
            "note_en": kb.get("token_ledger_note_en", ""),
            "note_zh": kb.get("token_ledger_note_zh", "")}


# --------------------------------------------------------------------------- #
# L3b — per-token price deflation curves
# --------------------------------------------------------------------------- #
def deflation_curves(kb, live=None):
    ratio = kb.get("blend_ratio_in_out", 3.0)
    or_live = (live or {}).get("openrouter") or {}
    curves = []
    for c in kb.get("price_curves", []):
        pts = []
        is_live = False
        for i, p in enumerate(c["points"]):
            q = dict(p)
            if (c["id"] == "openrouter_market" and i == len(c["points"]) - 1
                    and or_live.get("median_blended_usd_per_m") is not None):
                q["blended_usd_per_m"] = round(or_live["median_blended_usd_per_m"], 2)
                q["live"] = True
                q["date"] = (or_live.get("as_of") or q["date"])[:7]
                is_live = True
            q["blended"] = round(blended_price(q, ratio), 2)
            pts.append(q)
        chg = annualized_growth_pct(pts[0]["blended"], pts[-1]["blended"],
                                    months_between(pts[0]["date"], pts[-1]["date"]))
        curves.append({
            "id": c["id"], "family_en": c["family_en"], "family_zh": c["family_zh"],
            "points": pts, "live": is_live,
            "first_blended": pts[0]["blended"], "last_blended": pts[-1]["blended"],
            "annual_change_pct": round(chg, 1) if chg is not None else None,
            "note_en": c.get("note_en", ""), "note_zh": c.get("note_zh", ""),
        })
    chgs = [c["annual_change_pct"] for c in curves if c["annual_change_pct"] is not None]
    mean_chg = round(sum(chgs) / len(chgs), 1) if chgs else None
    return {"curves": curves, "mean_annual_change_pct": mean_chg,
            "blend_ratio_in_out": ratio,
            "caveat_en": kb.get("price_caveat_en", ""), "caveat_zh": kb.get("price_caveat_zh", "")}


# --------------------------------------------------------------------------- #
# L3c — monetization: realized $/M tokens vs serving-cost floor
# --------------------------------------------------------------------------- #
def _realized_usd_per_m(revenue_bn, monthly_tokens_t):
    """$/M tokens = revenue_bn×1e9 ÷ (monthly_T×1e6 M-tokens ×12)."""
    if not monthly_tokens_t:
        return None
    return revenue_bn * 1000.0 / (monthly_tokens_t * 12.0)


def monetization(kb):
    mon = kb.get("monetization", {})
    labs = []
    for lab_id in ("openai", "anthropic"):
        lab = mon.get(lab_id)
        if not lab:
            continue
        pts = []
        for p in lab["points"]:
            q = dict(p)
            q["realized_usd_per_m"] = round(_realized_usd_per_m(
                p["revenue_runrate_usd_bn"], p["monthly_tokens_t"]), 2)
            pts.append(q)
        trend = None
        if len(pts) >= 2:
            trend = annualized_growth_pct(pts[-2]["realized_usd_per_m"], pts[-1]["realized_usd_per_m"],
                                          months_between(pts[-2]["date"], pts[-1]["date"]))
        labs.append({
            "id": lab_id, "name": lab["name"], "points": pts,
            "realized_usd_per_m": pts[-1]["realized_usd_per_m"],
            "realized_trend_pct_yr": round(trend, 1) if trend is not None else None,
            "note_en": lab.get("note_en", ""), "note_zh": lab.get("note_zh", ""),
        })
    floor = dict(mon.get("serving_cost_floor", {}))
    lead = labs[0] if labs else None
    multiple = None
    if lead and floor.get("usd_per_m_tokens"):
        multiple = round(lead["realized_usd_per_m"] / floor["usd_per_m_tokens"], 1)
    return {"labs": labs, "serving_cost_floor": floor,
            "realized_over_floor_x": multiple}


# --------------------------------------------------------------------------- #
# L3e — usage-vs-spend scissors
# --------------------------------------------------------------------------- #
def scissors(kb, token_growth_pct, mean_price_change_pct):
    cg = kb.get("capex_growth", {})
    capex = cg.get("yoy_pct")
    unit_ratio = dollar_growth = dollar_ratio = None
    if token_growth_pct is not None and capex:
        unit_ratio = round(token_growth_pct / capex, 2)
    if token_growth_pct is not None and mean_price_change_pct is not None:
        dollar_growth = round(((1 + token_growth_pct / 100.0)
                               * (1 + mean_price_change_pct / 100.0) - 1) * 100.0, 1)
        if capex:
            dollar_ratio = round(dollar_growth / capex, 2)
    return {
        "token_growth_yoy_pct": token_growth_pct,
        "capex_growth_yoy_pct": capex,
        "capex_tier": cg.get("tier", "T2"), "capex_est": bool(cg.get("est")),
        "capex_as_of": cg.get("as_of", ""), "capex_align_note": cg.get("align_note", ""),
        "unit_ratio": unit_ratio,
        "mean_price_change_pct_yr": mean_price_change_pct,
        "dollar_growth_yoy_pct": dollar_growth,
        "dollar_ratio": dollar_ratio,
    }


# --------------------------------------------------------------------------- #
# composite demand-reality score
# --------------------------------------------------------------------------- #
def compute_subscores(unit_ratio, realized_trend_pct, dollar_ratio, dollar_growth_pct):
    """Each subscore 0 (spend ahead of use) … 100 (usage real & compounding).
    Mappings are hard-coded here and disclosed in the methodology panel."""
    # (a) unit scissors: token unit growth ÷ capex growth. 0.5× → 0, 2.5× → 100.
    sub_usage = round(_clamp01(((unit_ratio or 0.0) - 0.5) / 2.0) * 100.0, 1)
    # (b) monetization: realized $/M trend/yr. −30%/yr → 0, +20%/yr → 100.
    sub_monet = round(_clamp01(((realized_trend_pct or 0.0) + 30.0) / 50.0) * 100.0, 1)
    # (c) dollar scissors: deflation-adjusted token-dollar growth ÷ capex growth.
    #     0.4× → 0, 1.3× → 100 — the strictest lens, weighted heaviest with (a).
    sub_dollar = round(_clamp01(((dollar_ratio or 0.0) - 0.4) / 0.9) * 100.0, 1)
    # (d) elasticity health: does volume growth more than offset deflation?
    #     dollar-growth factor 0.7× → 0, 1.0× (flat revenue) ≈ 27, 1.8× → 100.
    factor = 1.0 + (dollar_growth_pct or 0.0) / 100.0
    sub_elastic = round(_clamp01((factor - 0.7) / 1.1) * 100.0, 1)
    return {"usage_vs_spend": sub_usage, "monetization": sub_monet,
            "dollar_scissors": sub_dollar, "elasticity": sub_elastic,
            "detail": {"unit_ratio": unit_ratio, "realized_trend_pct": realized_trend_pct,
                       "dollar_ratio": dollar_ratio, "dollar_growth_factor": round(factor, 3)}}


_SUB_NAMES = {
    "usage_vs_spend":  {"en": "Token units vs capex growth",        "zh": "token 單位 vs capex 增速"},
    "monetization":    {"en": "Realized $/M-tokens trend",          "zh": "已實現 $/M tokens 趨勢"},
    "dollar_scissors": {"en": "Token dollars vs capex growth",      "zh": "token 美元 vs capex 增速"},
    "elasticity":      {"en": "Price-elasticity health",            "zh": "價格彈性健康度"},
}


def compute_composite(kb, subs):
    weights = kb.get("weights", {})
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
            for k in ("usage_vs_spend", "monetization", "dollar_scissors", "elasticity")
        ],
        "detail": subs["detail"],
    }


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
def build_snapshot(kb, live=None, generated_at="", today=""):
    ledger = token_ledger(kb)
    deflation = deflation_curves(kb, live)
    monet = monetization(kb)
    lead = next((l for l in monet["labs"] if l["realized_trend_pct_yr"] is not None), None)
    sci = scissors(kb, ledger["growth_median_yoy_pct"], deflation["mean_annual_change_pct"])
    subs = compute_subscores(sci["unit_ratio"],
                             lead["realized_trend_pct_yr"] if lead else None,
                             sci["dollar_ratio"], sci["dollar_growth_yoy_pct"])
    composite = compute_composite(kb, subs)

    l3 = {
        "token_ledger": ledger,
        "price_deflation": deflation,
        "monetization": monet,
        "scissors": sci,
        "openrouter_live": (live or {}).get("openrouter") or None,
    }

    analysis_out = analysis.analyze(kb, l3, composite)

    return {
        "generated_at": generated_at,
        "as_of": today or kb.get("as_of_curated", ""),
        "source": "live" if live else "seed",
        "is_demo": live is None,
        "title_en": kb.get("title_en", "AI Usage & Token Economics Radar"),
        "title_zh": kb.get("title_zh", "AI 用量與 Token 經濟雷達"),
        "method_en": kb.get("method_en", ""),
        "method_zh": kb.get("method_zh", ""),
        "positioning_en": kb.get("positioning_en", ""),
        "positioning_zh": kb.get("positioning_zh", ""),
        "tier_legend": kb.get("tier_legend", {}),
        "l3": l3,
        "composite": composite,
        "l4": analysis_out["l4"],
        "l5": analysis_out["l5"],
        "analysis_engine": analysis_out["engine"],
        "usage_map": kb.get("usage_map", []),
        "indicator_dictionary": kb.get("indicators", []),
        "weights": kb.get("weights", {}),
        "verdict_thresholds": kb.get("verdict_thresholds", {}),
        "blind_spots_en": kb.get("blind_spots_en", []),
        "blind_spots_zh": kb.get("blind_spots_zh", []),
        "news": (live or {}).get("news", []) if live else [],
        "fetched_at": (live or {}).get("fetched_at") if live else None,
    }
