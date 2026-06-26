"""
AI Compute Demand Radar — demand model.

Pure functions: take `assumptions` (dict) + optional `live` (dict from data_sources)
and return a full `snapshot` dict. No I/O, no network -> trivially unit-testable.

Methodology (three-lens triangulation, demand worked BACKWARD from end demand):

  Lens A  TOP-DOWN (primary, "from final demand backward")
          global DC capex  ->  x AI share  ->  x silicon-conversion band
          ->  total AI compute silicon $  ->  split by category mix  ->  units (/ASP)

  Lens B  BOTTOM-UP (hard floor, live)
          sum of vendor data-center silicon revenue (NVDA/AMD/AVGO/MRVL/INTC + internal ASIC)
          each anchor auto-scaled by live yfinance TTM revenue at refresh.

  Lens C  ANALYST TAM benchmarks (external triangulation overlay)
          Omdia / Mordor / Bloomberg Intelligence / bull case.

  Reconciliation: blended central = median(top-down mid, bottom-up, analyst mid).
  The gap between lenses is surfaced as a confidence signal, not hidden.
"""
from __future__ import annotations

from statistics import median


def _years(a):
    return [int(y) for y in a["_meta"]["horizon"]]


def _asp_for(asp_block, year, base_year):
    base = asp_block["base"]
    chg = asp_block.get("annual_change", 0.0)
    return base * ((1.0 + chg) ** (year - base_year))


# --------------------------------------------------------------------------- #
# Lens A — top-down from end demand
# --------------------------------------------------------------------------- #
def hyperscaler_capex_path(a, live=None):
    """Sum top-hyperscaler capex per year from sourced guidance.

    Capex is intentionally NOT overridden by live yfinance capex: yfinance gives
    a TRAILING twelve-month actual, whereas these are FORWARD guidance figures —
    mixing them would understate forward years and make the '2026E' KPI mislead.
    Capex updates manually in assumptions.json as new guidance lands; live data
    drives the bottom-up vendor lens instead. We still surface live TTM capex as
    context (not substituted into the path).

    Out-years (where by_company carries no guidance) fall back to the explicit
    `extrapolated_total` block and are flagged so the front-end can label them
    'extrapolated' rather than silently summing to 0."""
    hblock = a["hyperscaler_capex_usd_bn"]
    block = hblock["by_company"]
    extrap = hblock.get("extrapolated_total", {})
    tmap = hblock.get("yfinance_ticker_map", {})
    years = _years(a)
    out = {}
    extrapolated = {}  # year -> bool (True if the value came from extrapolated_total)
    for y in years:
        sy = str(y)
        cols = [by_year[sy] for by_year in block.values() if sy in by_year]
        if cols:
            out[sy] = round(sum(cols), 1)
            extrapolated[sy] = False
        elif sy in extrap and not str(sy).startswith("_"):
            out[sy] = round(float(extrap[sy]), 1)
            extrapolated[sy] = True
        else:
            out[sy] = None  # genuinely unknown -> N/A, never a silent 0
            extrapolated[sy] = False
    context = {}
    if live:
        for tick, name in tmap.items():
            lv = (live.get(tick) or {}).get("capex_ttm_usd_bn")
            if lv:
                context[name] = round(lv, 1)
    return out, context, extrapolated


def global_dc_capex_path(a):
    return {str(y): float(a["global_dc_capex_usd_bn"]["path"][str(y)]) for y in _years(a)}


def topdown(a):
    """Return per-year top-down AI compute silicon $ (low/mid/high band) + category split + units."""
    years = _years(a)
    base_year = a["_meta"]["base_year"]
    gdc = global_dc_capex_path(a)
    ai_share = a["ai_dc_capex_share"]["by_year"]
    band = a["silicon_conversion_band"]
    mix = a["category_mix"]["by_year"]
    asp = a["asp_usd"]

    rows = []
    for y in years:
        sy = str(y)
        ai_capex = gdc[sy] * float(ai_share[sy])
        total = {k: ai_capex * band[k] for k in ("low", "mid", "high")}
        m = mix[sy]
        cats = {}
        for cat in ("gpu", "asic", "cpu"):
            usd = total["mid"] * float(m[cat])
            asp_key = {"gpu": "gpu_accelerator", "asic": "asic_accelerator", "cpu": "server_cpu"}[cat]
            unit_asp = _asp_for(asp[asp_key], y, base_year)
            # usd is in $bn, asp in $ -> units in millions = usd_bn*1e9 / asp / 1e6
            units_m = (usd * 1e9) / unit_asp / 1e6
            cats[cat] = {"usd_bn": round(usd, 1), "units_m": round(units_m, 2), "asp_usd": round(unit_asp)}
        rows.append({
            "year": y,
            "ai_dc_capex_usd_bn": round(ai_capex, 1),
            "total_usd_bn": {k: round(v, 1) for k, v in total.items()},
            "categories": cats,
        })
    return rows


# --------------------------------------------------------------------------- #
# Lens B — bottom-up from vendor revenue (live-scaled)
# --------------------------------------------------------------------------- #
def bottomup(a, live=None):
    lines = a["bottomup_vendor_anchors_usd_bn"]["lines"]
    out_lines = []
    cat_tot = {"gpu": 0.0, "asic": 0.0, "cpu": 0.0}
    for name, ln in lines.items():
        val = float(ln["value"])
        scaled = val
        live_used = False
        live_source = None
        if live and ln.get("ticker") and ln.get("anchor_ttm"):
            entry = live.get(ln["ticker"]) or {}
            lv = entry.get("ttm_revenue_usd_bn")
            if lv and ln["anchor_ttm"]:
                scaled = val * (lv / float(ln["anchor_ttm"]))
                live_used = True
                live_source = entry.get("source", "yfinance")
        scaled = round(scaled, 1)
        cat_tot[ln["category"]] += scaled
        out_lines.append({
            "name": name, "ticker": ln.get("ticker"), "category": ln["category"],
            "anchor_usd_bn": round(val, 1), "estimate_usd_bn": scaled,
            "live_scaled": live_used, "live_source": live_source,
            "confidence": ln.get("confidence", "medium"),
            "caliber": ln.get("caliber"),
        })
    total = round(sum(cat_tot.values()), 1)
    blk = a["bottomup_vendor_anchors_usd_bn"]
    return {
        "lines": out_lines,
        "by_category_usd_bn": {k: round(v, 1) for k, v in cat_tot.items()},
        "total_usd_bn": total,
        "caliber_note_zh": blk.get("caliber_note_zh"),
        "caliber_note_en": blk.get("caliber_note_en"),
    }


# --------------------------------------------------------------------------- #
# Lens C — analyst benchmarks
# --------------------------------------------------------------------------- #
def analyst_band(a, year):
    vals = []
    for b in a["analyst_tam_benchmarks_usd_bn"]["benchmarks"]:
        v = b.get(f"y{year}")
        if v is not None:
            vals.append(float(v))
    if not vals:
        return None
    return {"min": round(min(vals), 1), "max": round(max(vals), 1), "mid": round(median(vals), 1), "n": len(vals)}


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def scenario_paths(a):
    """Per-scenario total AI compute silicon $ path. Base = top-down mid;
    bull/bear scale the 2030 endpoint via capex_multiplier and use a different
    conversion tier, interpolating the multiplier linearly from the base year."""
    years = _years(a)
    base_year = a["_meta"]["base_year"]
    gdc = global_dc_capex_path(a)
    ai_share = a["ai_dc_capex_share"]["by_year"]
    band = a["silicon_conversion_band"]
    scen = a["scenarios"]
    out = {}
    for key in ("base", "bull", "bear"):
        s = scen[key]
        conv = band[s["conversion_use"]]
        m2030 = float(s["capex_multiplier_2030"])
        path = {}
        span = max(years) - base_year
        for y in years:
            sy = str(y)
            # linear ramp of the multiplier from 1.0 at base_year to m2030 at 2030
            frac = 0.0 if span == 0 else max(0, (y - base_year)) / span
            mult = 1.0 + (m2030 - 1.0) * frac
            val = gdc[sy] * float(ai_share[sy]) * conv * mult
            path[sy] = round(val, 1)
        out[key] = {
            "label_zh": s["label_zh"], "label_en": s["label_en"],
            "conversion_use": s["conversion_use"], "path": path,
        }
    return out


def cagr(start, end, years):
    if start <= 0 or years <= 0:
        return None
    return round(((end / start) ** (1.0 / years) - 1.0) * 100, 1)


# --------------------------------------------------------------------------- #
# Power cross-check
# --------------------------------------------------------------------------- #
def power_cross_check(a):
    p = a["power_cross_check"]
    out = []
    for y in _years(a):
        sy = str(y)
        twh = p["dc_twh"].get(sy)
        share = p["ai_share_of_dc_power"].get(sy)
        if twh is None or share is None:
            continue
        out.append({"year": y, "dc_twh": twh, "ai_share": share, "ai_twh": round(twh * share, 1)})
    return {"rows": out, "source": p.get("_source"), "url": p.get("url")}


# --------------------------------------------------------------------------- #
# End-demand decompositions (inference/training, buyer type, edge AI)
# --------------------------------------------------------------------------- #
def workload_breakdown(a, td):
    """Split accelerator (GPU+ASIC) $ into training vs inference per year."""
    ws = a["workload_split"]["by_year"]
    rows = []
    for r in td:
        sy = str(r["year"])
        accel = r["categories"]["gpu"]["usd_bn"] + r["categories"]["asic"]["usd_bn"]
        s = ws.get(sy, {"training": 0.5, "inference": 0.5})
        rows.append({
            "year": r["year"],
            "accelerator_usd_bn": round(accel, 1),
            "training_usd_bn": round(accel * s["training"], 1),
            "inference_usd_bn": round(accel * s["inference"], 1),
            "inference_share": s["inference"],
        })
    return {"rows": rows, "source": a["workload_split"].get("_source")}


def buyer_breakdown(a, td):
    """Split total AI data-center silicon $ by buyer type per year."""
    bs = a["buyer_split"]["by_year"]
    keys = ["hyperscaler", "enterprise", "sovereign", "neocloud"]
    rows = []
    for r in td:
        sy = str(r["year"])
        total = r["total_usd_bn"]["mid"]
        s = bs.get(sy, {})
        rows.append({
            "year": r["year"],
            "total_usd_bn": round(total, 1),
            **{k: round(total * float(s.get(k, 0)), 1) for k in keys},
            "shares": {k: float(s.get(k, 0)) for k in keys},
        })
    return {"rows": rows, "keys": keys, "source": a["buyer_split"].get("_source")}


def edge_and_total(a, td):
    """Edge AI silicon path + grand total (data-center + edge) per year."""
    edge = a["edge_ai_silicon_usd_bn"]["path"]
    rows = []
    for r in td:
        sy = str(r["year"])
        dc = r["total_usd_bn"]["mid"]
        e = float(edge.get(sy, 0))
        rows.append({
            "year": r["year"],
            "datacenter_usd_bn": round(dc, 1),
            "edge_usd_bn": round(e, 1),
            "grand_total_usd_bn": round(dc + e, 1),
        })
    return {"rows": rows, "source": a["edge_ai_silicon_usd_bn"].get("_source"),
            "url": a["edge_ai_silicon_usd_bn"].get("url")}


# --------------------------------------------------------------------------- #
# EDGAR capex sanity (M6): companyfacts' generic PP&E-purchase concept is an
# unreliable proxy for asset-light fabless designers — it captures office/lab
# PP&E, not their economic "AI investment", and routinely prints absurdly low
# vs revenue (e.g. NVDA/AVGO/MRVL). Rather than show a misleading official-looking
# number, we BLANK the capex cell when capex/revenue is implausibly low and flag
# why. Capital-intensive operators (hyperscalers, IDM foundries) are unaffected.
# --------------------------------------------------------------------------- #
def _sanitize_edgar_capex(edgar, min_capex_to_revenue=0.06):
    if not edgar:
        return edgar or {}
    out = {}
    for tk, row in edgar.items():
        row = dict(row)
        cap = row.get("annual_capex_usd_bn")
        rev = row.get("annual_revenue_usd_bn")
        if cap is not None and rev and rev > 0 and (cap / rev) < min_capex_to_revenue:
            # implausibly low PP&E-purchase proxy -> suppress rather than mislead
            row["annual_capex_usd_bn"] = None
            row["capex_suppressed"] = True
            row["capex_suppressed_reason"] = (
                "companyfacts PP&E-purchase concept understates capex for "
                "asset-light fabless designers; suppressed to avoid a misleading "
                "official-looking figure."
            )
        out[tk] = row
    return out


# --------------------------------------------------------------------------- #
# Top-level assembly
# --------------------------------------------------------------------------- #
def build_snapshot(a, live=None, generated_at=None, macro=None, edgar=None):
    years = _years(a)
    base_year = a["_meta"]["base_year"]
    end_year = max(years)

    td = topdown(a)
    bu = bottomup(a, live)
    scen = scenario_paths(a)
    hcap, hcapex_ctx, hcap_extrap = hyperscaler_capex_path(a, live)
    gdc = global_dc_capex_path(a)

    td_by_year = {r["year"]: r for r in td}
    cur_year = a["_meta"]["current_year"]

    # ---- 2025 anchor: top-down + analyst agree tightly -> high-confidence base
    by = base_year
    td_mid = td_by_year[by]["total_usd_bn"]["mid"]
    an = analyst_band(a, by)
    parts = [td_mid] + ([an["mid"]] if an else [])
    blended = round(median(parts), 1)
    lo = min([td_by_year[by]["total_usd_bn"]["low"]] + ([an["min"]] if an else []))
    hi = max([td_by_year[by]["total_usd_bn"]["high"]] + ([an["max"]] if an else []))
    spread_pct = round((hi - lo) / blended * 100, 0) if blended else None

    # ---- current run-rate cross-check: bottom-up (annualized ~current year) vs
    #      top-down of the SAME year (apples-to-apples, not vs 2025)
    bu_total = bu["total_usd_bn"]
    td_cur_mid = td_by_year[cur_year]["total_usd_bn"]["mid"]
    runrate_gap_pct = round((bu_total - td_cur_mid) / td_cur_mid * 100, 0) if td_cur_mid else None

    base_path = scen["base"]["path"]
    forecast_cagr = cagr(base_path[str(by)], base_path[str(end_year)], end_year - by)

    # end-demand decompositions (computed once, reused in headline + body)
    workload_b = workload_breakdown(a, td)
    buyer_b = buyer_breakdown(a, td)
    edge_t = edge_and_total(a, td)
    edge_by_year = {r["year"]: r for r in edge_t["rows"]}
    wl_by_year = {r["year"]: r for r in workload_b["rows"]}
    by_buy_year = {r["year"]: r for r in buyer_b["rows"]}

    # category forecast table (top-down mid driven)
    cat_table = []
    for cat in ("gpu", "asic", "cpu"):
        row = {"category": cat, "by_year": {}}
        for r in td:
            row["by_year"][str(r["year"])] = r["categories"][cat]
        c0 = td_by_year[by]["categories"][cat]["usd_bn"]
        c1 = td_by_year[end_year]["categories"][cat]["usd_bn"]
        row["cagr_pct"] = cagr(c0, c1, end_year - by)
        cat_table.append(row)

    return {
        "generated_at": generated_at,
        "as_of": a["_meta"]["as_of"],
        "base_year": base_year,
        "end_year": end_year,
        "years": years,
        "live_present": bool(live),
        "live_capex_ttm_context": hcapex_ctx,
        "headline": {
            "base_year": by,
            "central_usd_bn": blended,
            "band_usd_bn": {"low": round(lo, 1), "high": round(hi, 1)},
            "spread_pct": spread_pct,
            "current_runrate_year": cur_year,
            "current_runrate_bottomup_usd_bn": bu_total,
            "current_runrate_topdown_mid_usd_bn": td_cur_mid,
            "current_runrate_gap_pct": runrate_gap_pct,
            f"forecast_{end_year}_base_usd_bn": base_path[str(end_year)],
            f"forecast_{end_year}_bull_usd_bn": scen["bull"]["path"][str(end_year)],
            f"forecast_{end_year}_bear_usd_bn": scen["bear"]["path"][str(end_year)],
            "forecast_cagr_pct": forecast_cagr,
            "hyperscaler_capex_base_year_usd_bn": hcap[str(by)],
            "hyperscaler_capex_current_year_usd_bn": hcap[str(cur_year)],
            "edge_base_year_usd_bn": edge_by_year[by]["edge_usd_bn"],
            "grand_total_base_year_usd_bn": edge_by_year[by]["grand_total_usd_bn"],
            "grand_total_end_year_usd_bn": round(base_path[str(end_year)] + edge_by_year[end_year]["edge_usd_bn"], 1),
            "inference_share_end_year": wl_by_year[end_year]["inference_share"],
            "sovereign_end_year_usd_bn": by_buy_year[end_year]["sovereign"],
        },
        "topdown": td,
        "bottomup": bu,
        "analyst": {"benchmarks": a["analyst_tam_benchmarks_usd_bn"]["benchmarks"],
                    "base_year_band": an},
        "scenarios": scen,
        "reconciliation": {
            "base_year": by,
            "topdown_mid_usd_bn": td_mid,
            "topdown_band_usd_bn": td_by_year[by]["total_usd_bn"],
            "analyst_mid_usd_bn": an["mid"] if an else None,
            "analyst_n": an["n"] if an else 0,
            "calibration_caveat": bool(a["silicon_conversion_band"].get("calibration_caveat")),
            "blended_central_usd_bn": blended,
            "current_runrate": {
                "year": cur_year,
                "bottomup_usd_bn": bu_total,
                "topdown_mid_usd_bn": td_cur_mid,
                "gap_pct": runrate_gap_pct,
            },
        },
        "category_forecast": cat_table,
        "workload": workload_b,
        "buyer": buyer_b,
        "edge_total": edge_t,
        "hyperscaler_capex_usd_bn": hcap,
        "hyperscaler_capex_extrapolated": hcap_extrap,
        "global_dc_capex_usd_bn": gdc,
        "power": power_cross_check(a),
        "supply": a.get("supply_anchors", {}),
        "macro": macro or {},
        "edgar_official": _sanitize_edgar_capex(edgar or {}),
        "sources": _collect_sources(a),
    }


def _collect_sources(a):
    src = []
    def add(key, block):
        if isinstance(block, dict) and "_source" in block:
            src.append({
                "key": key,
                "source": block.get("_source"),
                "confidence": block.get("_confidence", ""),
                "as_of": block.get("as_of", a["_meta"]["as_of"]),
                "url": block.get("url"),
            })
    for k, v in a.items():
        if k == "_meta":
            continue
        add(k, v)
    return src
