"""AI Capex Payback Radar — Module: GPU / Chip Depreciation Engine.

The thesis the user wants modeled: capex hits cash now, but the EARNINGS hit
arrives later as depreciation — and three forces can pull that hit forward or make
it bigger than the straight-line schedule implies. This module turns each into a
quantified, switchable shock on top of the REAL reported D&A and PP&E (T1, seeded
from the Capital IQ .xls), and translates every shock into "% of operating income"
and "% of cloud operating profit" so a CEO sees the bite, not just a number.

Three shocks (all act on the recent-vintage accelerator book = at_risk_base):
  (a) IMPAIRMENT  — H100s bought at peak now carry book value above economic value.
                    One-time write-down = at_risk_base × impairment_pct.
  (b) STRANDED    — a newer chip's perf/watt beats H100 while POWER & LAND are
                    capped, so the only way to install it is to rip out
                    un-depreciated H100s. Their remaining book value is expensed
                    early → accelerated annual depreciation over the replacement
                    window vs. the normal remaining-life schedule.
  (c) LIFE-REVERSAL — the 2023-24 useful-life EXTENSIONS (4→5→6yr) that lifted
                    reported margins get reversed back toward 3-4yr → steady-state
                    depreciation on the compute base rises by old_life/new_life.

Nothing here invents AI revenue or AI operating income; it only re-times and
re-sizes depreciation, an expense we can ground in reported D&A and PP&E.

build(companies) → {"aggregate": {...}, "companies": [per-company dicts]}
"""
from __future__ import annotations


def _pct(part, whole):
    if part is None or not whole:
        return None
    return round(part / whole * 100, 1)


def _round(x, n=1):
    return round(x, n) if x is not None else None


# --------------------------------------------------------------------------- #
# Individual shocks (all dollars in $bn)
# --------------------------------------------------------------------------- #
def shock_impairment(at_risk_base, impairment_pct):
    """One-time write-down of the recent-vintage accelerator book."""
    return _round(at_risk_base * impairment_pct)


def shock_stranded(at_risk_base, stranded_fraction, remaining_life_years, replacement_window_years):
    """Incremental ANNUAL depreciation from ripping out un-depreciated chips early.

    The retired net book value would normally have depreciated over its remaining
    life; forced replacement compresses it into the (shorter) replacement window.
    Incremental annual hit = retired_nbv × (1/replacement − 1/remaining).
    """
    if not remaining_life_years or not replacement_window_years:
        return 0.0
    retired_nbv = at_risk_base * stranded_fraction
    accelerated = retired_nbv / replacement_window_years
    normal = retired_nbv / remaining_life_years
    return _round(max(0.0, accelerated - normal))


def shock_life_reversal(ai_dep_ttm, useful_life_years, reversal_life_years):
    """Incremental ANNUAL depreciation if the compute useful life is shortened.

    Straight-line dep scales inversely with life, so shortening 6→4 raises the
    compute slice of D&A by old/new (×1.5), and the delta lands on operating income.
    """
    if not reversal_life_years or not useful_life_years:
        return 0.0
    new_dep = ai_dep_ttm * (useful_life_years / reversal_life_years)
    return _round(max(0.0, new_dep - ai_dep_ttm))


# --------------------------------------------------------------------------- #
# Per-company assembly
# --------------------------------------------------------------------------- #
def company_depreciation(c):
    """c carries `financials` (real T1) and `dep` (assumptions) blocks."""
    fin = c.get("financials", {})
    dep = c.get("dep", {})
    cloud = c.get("cloud", {})

    dep_ttm = float(fin.get("dep_ttm_usd_bn") or 0)
    op_income = float(fin.get("op_income_ttm_usd_bn") or 0)
    ppe_net = float(fin.get("ppe_net_usd_bn") or 0)
    cloud_op_profit = _round(
        float(cloud.get("rev_ttm_usd_bn") or 0) * float(cloud.get("op_margin_pct") or 0) / 100.0
    )

    compute_dep_share = float(dep.get("compute_dep_share", 0.6))
    at_risk_pct = float(dep.get("at_risk_pct_of_ppe", 0.10))
    useful_life = float(dep.get("useful_life_years", 6))
    reversal_life = float(dep.get("reversal_life_years", 4))
    impairment_pct = float(dep.get("impairment_pct", 0.25))
    stranded_fraction = float(dep.get("stranded_fraction", 0.30))
    remaining_life = float(dep.get("remaining_life_years", 3.5))
    replacement_window = float(dep.get("replacement_window_years", 1.5))

    ai_dep_ttm = _round(dep_ttm * compute_dep_share)
    at_risk_base = _round(ppe_net * at_risk_pct)

    imp = shock_impairment(at_risk_base, impairment_pct)
    strand = shock_stranded(at_risk_base, stranded_fraction, remaining_life, replacement_window)
    life = shock_life_reversal(ai_dep_ttm, useful_life, reversal_life)
    combined_annual = _round((strand or 0) + (life or 0))  # exclude one-time impairment

    def impact(hit):
        return {"hit_usd_bn": hit,
                "pct_of_op_income": _pct(hit, op_income),
                "pct_of_cloud_op_profit": _pct(hit, cloud_op_profit)}

    return {
        "id": c["id"], "name_en": c["name_en"], "name_zh": c["name_zh"],
        "baseline": {
            "dep_ttm_usd_bn": _round(dep_ttm),
            "ai_dep_ttm_usd_bn": ai_dep_ttm,
            "at_risk_base_usd_bn": at_risk_base,
            "op_income_ttm_usd_bn": _round(op_income),
            "cloud_op_profit_usd_bn": cloud_op_profit,
        },
        "assumptions": {
            "compute_dep_share": compute_dep_share,
            "at_risk_pct_of_ppe": at_risk_pct,
            "useful_life_years": useful_life,
            "reversal_life_years": reversal_life,
            "impairment_pct": impairment_pct,
            "stranded_fraction": stranded_fraction,
            "remaining_life_years": remaining_life,
            "replacement_window_years": replacement_window,
        },
        "shocks": {
            "impairment": {"kind": "one_time", **impact(imp)},
            "stranded": {"kind": "annual", **impact(strand)},
            "life_reversal": {"kind": "annual", **impact(life)},
        },
        "combined_annual": impact(combined_annual),
        "exposure_pct_op_income": _pct(combined_annual, op_income),
        "note_en": c.get("dep", {}).get("note_en", ""),
        "note_zh": c.get("dep", {}).get("note_zh", ""),
    }


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #
def build(companies):
    rows = [company_depreciation(c) for c in companies
            if c.get("kind") == "public" and c.get("financials")]
    if not rows:
        return {"aggregate": {}, "companies": []}

    tot_at_risk = _round(sum(r["baseline"]["at_risk_base_usd_bn"] or 0 for r in rows))
    tot_impair = _round(sum(r["shocks"]["impairment"]["hit_usd_bn"] or 0 for r in rows))
    tot_combined = _round(sum(r["combined_annual"]["hit_usd_bn"] or 0 for r in rows))
    tot_op_income = _round(sum(r["baseline"]["op_income_ttm_usd_bn"] or 0 for r in rows))
    most_exposed = max(rows, key=lambda r: r["exposure_pct_op_income"] or 0)

    return {
        "aggregate": {
            "total_at_risk_base_usd_bn": tot_at_risk,
            "total_impairment_one_time_usd_bn": tot_impair,
            "total_combined_annual_usd_bn": tot_combined,
            "combined_pct_of_op_income": _pct(tot_combined, tot_op_income),
            "most_exposed_id": most_exposed["id"],
            "most_exposed_pct_op_income": most_exposed["exposure_pct_op_income"],
        },
        "companies": rows,
    }
