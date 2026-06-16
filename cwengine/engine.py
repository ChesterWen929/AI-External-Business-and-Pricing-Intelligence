"""
CapEx-to-Wafer Demand Inference Engine — the pure model.

The conversion CapEx $ -> leading-edge logic-wafer demand is NOT one formula. It
is a directed chain of assumption NODES, each of which is versioned, dated and
regime-tagged DATA (see knowledge_base.json). This module is a pure, side-effect-
free interpreter of that graph: no I/O, no network, no globals -> trivially
unit-testable. The API/UI layer is a thin shell over these functions.

Transformation chain (each stage reads one or more assumption nodes):
  1  CapEx total            -> silicon-addressable $        (node: silicon_fraction)
  2  silicon $              -> accelerator $ by segment     (node: segment_mix)
  3  accelerator $          -> unit volume                  (node: segment_asp)
  4  units                  -> logic die area               (node: segment_die_mm2)
  5  die area               -> wafer starts                 (nodes: segment_yield,
                                                              wafer_usable_area_mm2,
                                                              segment_node [label])
  6  training/inference mix is the REGIME: stages 1-5 each resolve a regime-tagged
     version, so switching regime re-routes the whole chain through a different
     assumption set. Same CapEx, different regime -> different wafer demand.

Per-segment wafers are multiplicative:
    wafers = (capex * silicon_fraction * mix_share) / asp        # units
             * die_mm2                                            # -> die area
             / (wafer_usable_area * yield)                        # -> wafers
The confidence envelope pushes every banded node to its wafer-min / wafer-max end
simultaneously: an explicit outer envelope, NOT a probabilistic CI (we refuse the
false precision of a single number AND of a fake standard deviation).
"""
from __future__ import annotations

# Direction of each banded node's effect on wafer count:
#   +1  value-high  -> wafers-high (monotonically increasing)
#   -1  value-high  -> wafers-low  (monotonically decreasing)
WAFER_DIR = {
    "silicon_fraction": +1,
    "segment_die_mm2": +1,
    "segment_asp": -1,
    "segment_yield": -1,
    "wafer_usable_area_mm2": -1,
}

WAFER_CHAIN_NODES = ["silicon_fraction", "segment_mix", "segment_asp",
                     "segment_die_mm2", "segment_yield", "wafer_usable_area_mm2"]


# --------------------------------------------------------------------------- #
# Graph access — versioned, dated, regime-aware resolution
# --------------------------------------------------------------------------- #
def get_node(kb, node_id):
    for n in kb["nodes"]:
        if n["id"] == node_id:
            return n
    raise KeyError(f"unknown node {node_id}")


def segment_ids(kb):
    return [s["id"] for s in kb["segments"]]


def resolve_version(node, regime_id, as_of):
    """Pick the active version for (regime, date).

    A version applies if its regime is the requested regime or the universal
    "_all", and its valid_from is on/before `as_of`. Among applicable versions
    we take the most recent valid_from; ties break toward the regime-specific
    value (over "_all") and then the higher version number. If nothing is valid
    yet at `as_of`, fall back to the earliest version so the model never crashes.
    """
    applicable = [v for v in node["versions"]
                  if v["regime"] in (regime_id, "_all") and v["valid_from"] <= as_of]
    if not applicable:
        applicable = sorted(node["versions"], key=lambda v: v["valid_from"])[:1]
    applicable.sort(key=lambda v: (v["valid_from"], 0 if v["regime"] == "_all" else 1, v["version"]))
    return applicable[-1]


def node_value(kb, node_id, regime_id, as_of):
    """Return (value, meta) for a node under a regime as-of a date."""
    node = get_node(kb, node_id)
    v = resolve_version(node, regime_id, as_of)
    meta = {
        "node_id": node_id,
        "name_en": node["name_en"], "name_zh": node["name_zh"],
        "stage": node["stage"], "kind": node["kind"], "unit": node["unit"],
        "version": v["version"], "valid_from": v["valid_from"],
        "regime_source": v["regime"], "confidence": v["confidence"],
        "rationale_en": v.get("rationale_en", ""), "rationale_zh": v.get("rationale_zh", ""),
        "evidence_ids": v.get("evidence_ids", []),
    }
    return v["value"], meta


# --------------------------------------------------------------------------- #
# Core chain
# --------------------------------------------------------------------------- #
def _band_end(node_id, which):
    """Map a wafer-direction request ('point'|'wmin'|'wmax') to a value end
    ('point'|'low'|'high') for a given node, honoring WAFER_DIR."""
    if which == "point":
        return "point"
    d = WAFER_DIR.get(node_id, +1)
    if which == "wmax":
        return "high" if d > 0 else "low"
    return "low" if d > 0 else "high"  # wmin


def _scalar(value, end):
    return value[end] if end in value else value["point"]


def _per_seg(value, seg, end):
    cell = value[seg]
    return cell[end] if end in cell else cell["point"]


def total_wafers(kb, regime_id, capex_usd_bn, as_of, override=None):
    """Total annual wafer starts. `override` maps node_id -> 'wmin'|'wmax' to push
    that node to a band extreme (used for the envelope and sensitivity); all other
    nodes use their point value."""
    override = override or {}
    segs = segment_ids(kb)
    sf, _ = node_value(kb, "silicon_fraction", regime_id, as_of)
    mix, _ = node_value(kb, "segment_mix", regime_id, as_of)
    asp, _ = node_value(kb, "segment_asp", regime_id, as_of)
    die, _ = node_value(kb, "segment_die_mm2", regime_id, as_of)
    yld, _ = node_value(kb, "segment_yield", regime_id, as_of)
    wua, _ = node_value(kb, "wafer_usable_area_mm2", regime_id, as_of)

    sf_v = _scalar(sf, _band_end("silicon_fraction", override.get("silicon_fraction", "point")))
    wua_v = _scalar(wua, _band_end("wafer_usable_area_mm2", override.get("wafer_usable_area_mm2", "point")))
    silicon = capex_usd_bn * sf_v

    total = 0.0
    for seg in segs:
        share = mix.get(seg, 0.0)
        if share <= 0:
            continue
        asp_v = _per_seg(asp, seg, _band_end("segment_asp", override.get("segment_asp", "point")))
        die_v = _per_seg(die, seg, _band_end("segment_die_mm2", override.get("segment_die_mm2", "point")))
        yld_v = _per_seg(yld, seg, _band_end("segment_yield", override.get("segment_yield", "point")))
        seg_dollars = silicon * share
        units = seg_dollars * 1e9 / asp_v
        die_area = units * die_v
        total += die_area / (wua_v * yld_v)
    return total


def run_chain(kb, regime_id, capex_usd_bn, as_of):
    """Full traceable run: per-segment + per-stage breakdown, plus the envelope.

    Returns a dict with the headline wafer number (annual + wpm), the band,
    per-segment detail, the stage-by-stage waterfall (each stage carries the
    assumption-node provenance so the UI can click a number back to its source),
    and parallel HBM cross-check.
    """
    segs = segment_ids(kb)
    seg_meta = {s["id"]: s for s in kb["segments"]}

    sf, sf_m = node_value(kb, "silicon_fraction", regime_id, as_of)
    mix, mix_m = node_value(kb, "segment_mix", regime_id, as_of)
    asp, asp_m = node_value(kb, "segment_asp", regime_id, as_of)
    die, die_m = node_value(kb, "segment_die_mm2", regime_id, as_of)
    nodes, node_m = node_value(kb, "segment_node", regime_id, as_of)
    yld, yld_m = node_value(kb, "segment_yield", regime_id, as_of)
    wua, wua_m = node_value(kb, "wafer_usable_area_mm2", regime_id, as_of)
    hbm, hbm_m = node_value(kb, "segment_hbm_gb", regime_id, as_of)

    silicon = capex_usd_bn * sf["point"]

    seg_rows = []
    tot_units = tot_die_area = tot_wafers = tot_hbm_gb = 0.0
    for seg in segs:
        share = mix.get(seg, 0.0)
        seg_dollars = silicon * share
        asp_v = asp[seg]["point"]
        die_v = die[seg]["point"]
        yld_v = yld[seg]["point"]
        units = seg_dollars * 1e9 / asp_v if asp_v else 0.0
        die_area = units * die_v
        good_per_wafer = wua["point"] * yld_v
        wafers = die_area / good_per_wafer if good_per_wafer else 0.0
        hbm_gb_total = units * hbm[seg]["point"]
        tot_units += units
        tot_die_area += die_area
        tot_wafers += wafers
        tot_hbm_gb += hbm_gb_total
        seg_rows.append({
            "id": seg, "name_en": seg_meta[seg]["name_en"], "name_zh": seg_meta[seg]["name_zh"],
            "examples": seg_meta[seg]["examples"],
            "share": round(share, 4),
            "dollars_usd_bn": round(seg_dollars, 2),
            "asp_usd": round(asp_v),
            "units_m": round(units / 1e6, 3),
            "die_mm2": die_v,
            "node": nodes[seg],
            "yield": yld_v,
            "wafers_year": round(wafers),
            "wafers_share": 0.0,  # filled after total known
            "hbm_gb_per_unit": hbm[seg]["point"],
            "hbm_tonnes_note": None,
        })
    for r in seg_rows:
        r["wafers_share"] = round(r["wafers_year"] / tot_wafers, 4) if tot_wafers else 0.0

    # Two bands, both honest, neither a single false-precise number:
    #  • primary "confidence band" — per-node uncertainties combined in quadrature
    #    (treats the assumptions as independent; the decision-useful read).
    #  • outer envelope — every banded node pushed to its wafer-extreme together
    #    (fully correlated worst/best case; deliberately wide).
    rels = []
    for nid in WAFER_DIR:
        w_lo_i = total_wafers(kb, regime_id, capex_usd_bn, as_of, override={nid: "wmin"})
        w_hi_i = total_wafers(kb, regime_id, capex_usd_bn, as_of, override={nid: "wmax"})
        if tot_wafers:
            rels.append(abs(w_hi_i - w_lo_i) / (2 * tot_wafers))
    rel_q = (sum(r * r for r in rels)) ** 0.5
    w_low = max(0.0, tot_wafers * (1 - rel_q))
    w_high = tot_wafers * (1 + rel_q)
    env_low = total_wafers(kb, regime_id, capex_usd_bn, as_of,
                           override={n: "wmin" for n in WAFER_DIR})
    env_high = total_wafers(kb, regime_id, capex_usd_bn, as_of,
                            override={n: "wmax" for n in WAFER_DIR})

    # stage waterfall — each stage records its assumption provenance
    waterfall = [
        {"stage": 1, "node": "silicon_fraction",
         "name_en": "CapEx -> Silicon-addressable", "name_zh": "CapEx → 可矽化",
         "in_label_en": f"CapEx ${capex_usd_bn:,.0f}B", "in_label_zh": f"CapEx ${capex_usd_bn:,.0f}B",
         "factor_en": f"x {sf['point']:.2f} silicon fraction", "factor_zh": f"× {sf['point']:.2f} 可矽化比例",
         "out_value": round(silicon, 1), "out_unit": "usd_bn",
         "out_label_en": f"${silicon:,.1f}B accelerator silicon", "out_label_zh": f"${silicon:,.1f}B 加速器矽",
         "meta": sf_m},
        {"stage": 2, "node": "segment_mix",
         "name_en": "Silicon $ -> Accelerator $ by segment", "name_zh": "矽 $ → 各區段加速器 $",
         "in_label_en": f"${silicon:,.1f}B silicon", "in_label_zh": f"${silicon:,.1f}B 矽",
         "factor_en": "split 4 segments", "factor_zh": "拆分 4 區段",
         "out_value": round(silicon, 1), "out_unit": "usd_bn",
         "out_label_en": " / ".join(f"{seg_meta[s]['name_en']} {mix.get(s,0)*100:.0f}%" for s in segs),
         "out_label_zh": " / ".join(f"{seg_meta[s]['name_zh']} {mix.get(s,0)*100:.0f}%" for s in segs),
         "meta": mix_m},
        {"stage": 3, "node": "segment_asp",
         "name_en": "Accelerator $ -> Units", "name_zh": "加速器 $ → 出貨單位",
         "in_label_en": f"${silicon:,.1f}B across segments", "in_label_zh": f"${silicon:,.1f}B 分區段",
         "factor_en": "/ per-segment ASP", "factor_zh": "÷ 各區段 ASP",
         "out_value": round(tot_units / 1e6, 2), "out_unit": "units_m",
         "out_label_en": f"{tot_units/1e6:,.1f}M accelerators", "out_label_zh": f"{tot_units/1e6:,.1f}M 顆加速器",
         "meta": asp_m},
        {"stage": 4, "node": "segment_die_mm2",
         "name_en": "Units -> Logic die area", "name_zh": "出貨單位 → 邏輯裸晶面積",
         "in_label_en": f"{tot_units/1e6:,.1f}M units", "in_label_zh": f"{tot_units/1e6:,.1f}M 單位",
         "factor_en": "x logic die mm2", "factor_zh": "× 邏輯裸晶 mm²",
         "out_value": round(tot_die_area / 1e6, 1), "out_unit": "Mmm2",
         "out_label_en": f"{tot_die_area/1e6:,.0f}M mm2 logic die", "out_label_zh": f"{tot_die_area/1e6:,.0f}M mm² 邏輯裸晶",
         "meta": die_m},
        {"stage": 5, "node": "wafer_usable_area_mm2",
         "name_en": "Die area -> Wafer demand", "name_zh": "裸晶面積 → 晶圓需求",
         "in_label_en": f"{tot_die_area/1e6:,.0f}M mm2 die", "in_label_zh": f"{tot_die_area/1e6:,.0f}M mm² 裸晶",
         "factor_en": "/ usable area x yield", "factor_zh": "÷ 可用面積 × 良率",
         "out_value": round(tot_wafers), "out_unit": "wafers_year",
         "out_label_en": f"{tot_wafers:,.0f} wafers/yr", "out_label_zh": f"{tot_wafers:,.0f} 片/年",
         "meta": {**wua_m, "co_nodes": [yld_m, node_m]}},
    ]

    return {
        "regime": regime_id,
        "capex_usd_bn": capex_usd_bn,
        "silicon_usd_bn": round(silicon, 1),
        "wafers_year": round(tot_wafers),
        "wafers_year_low": round(w_low),
        "wafers_year_high": round(w_high),
        "wafers_env_low": round(env_low),
        "wafers_env_high": round(env_high),
        "wafers_per_month": round(tot_wafers / 12),
        "band_pct": round(rel_q * 100),
        "env_pct": round((env_high - env_low) / tot_wafers * 100) if tot_wafers else None,
        "total_units_m": round(tot_units / 1e6, 2),
        "total_die_area_Mmm2": round(tot_die_area / 1e6, 1),
        "hbm_total_PB": round(tot_hbm_gb / 1e6, 2),  # GB -> PB
        "segments": seg_rows,
        "waterfall": waterfall,
    }


# --------------------------------------------------------------------------- #
# Scenario comparison — same CapEx, every regime
# --------------------------------------------------------------------------- #
def scenario_comparison(kb, capex_usd_bn, as_of):
    rows = []
    for r in kb["regimes"]:
        run = run_chain(kb, r["id"], capex_usd_bn, as_of)
        rows.append({
            "regime": r["id"], "name_en": r["name_en"], "name_zh": r["name_zh"],
            "era_en": r["era_en"], "era_zh": r["era_zh"], "color": r["color"],
            "inference_share": r["inference_share"],
            "wafers_year": run["wafers_year"],
            "wafers_year_low": run["wafers_year_low"],
            "wafers_year_high": run["wafers_year_high"],
            "wafers_per_month": run["wafers_per_month"],
            "total_units_m": run["total_units_m"],
            "asic_inf_share": round(sum(s["share"] for s in run["segments"]
                                        if s["id"] in ("asic_custom", "inference_chip")), 3),
        })
    w = [r["wafers_year"] for r in rows]
    return {
        "capex_usd_bn": capex_usd_bn,
        "rows": rows,
        "spread_wafers": max(w) - min(w) if w else 0,
        "spread_pct": round((max(w) - min(w)) / min(w) * 100) if w and min(w) else None,
    }


# --------------------------------------------------------------------------- #
# Drift detection — the early-warning feature
# --------------------------------------------------------------------------- #
def _drift_series(node):
    """Chronological scalar series for a node's drift metric, across ALL versions
    (regime shifts included — the drift IS the regime shift)."""
    metric_segs = node.get("drift_metric_segments")
    pts = []
    for v in sorted(node["versions"], key=lambda x: x["valid_from"]):
        val = v["value"]
        if node["kind"] == "mix" and metric_segs:
            m = sum(val.get(s, 0.0) for s in metric_segs)
        elif node["kind"] == "per_segment_band" and metric_segs:
            m = val[metric_segs[0]]["point"]
        elif node["kind"] == "scalar":
            m = val["point"]
        else:
            continue
        pts.append({"valid_from": v["valid_from"], "version": v["version"],
                    "regime": v["regime"], "metric": round(m, 4)})
    return pts


def detect_drift(kb, as_of):
    """Flag assumptions that keep getting revised in one direction. A monotonic
    run of >= min_run versions ending at the latest revision = a possible
    structural regime change forming."""
    min_run = kb.get("drift_config", {}).get("min_run", 3)
    flags = []
    for node in kb["nodes"]:
        if "drift_metric_segments" not in node:
            continue
        series = [p for p in _drift_series(node) if p["valid_from"] <= as_of]
        if len(series) < min_run:
            continue
        # longest monotonic run ending at the last point
        diffs = [series[i]["metric"] - series[i - 1]["metric"] for i in range(1, len(series))]
        if not diffs:
            continue
        last_sign = 1 if diffs[-1] > 0 else (-1 if diffs[-1] < 0 else 0)
        if last_sign == 0:
            continue
        run = 1
        for d in reversed(diffs):
            s = 1 if d > 0 else (-1 if d < 0 else 0)
            if s == last_sign:
                run += 1
            else:
                break
        if run < min_run:
            continue
        first = series[len(series) - run]
        last = series[-1]
        total_chg = last["metric"] - first["metric"]
        pct = round(total_chg / first["metric"] * 100, 1) if first["metric"] else None
        flags.append({
            "node_id": node["id"], "name_en": node["name_en"], "name_zh": node["name_zh"],
            "metric_en": node.get("drift_metric_en", ""), "metric_zh": node.get("drift_metric_zh", ""),
            "direction": "up" if last_sign > 0 else "down",
            "run_len": run,
            "from_value": first["metric"], "to_value": last["metric"],
            "abs_change": round(total_chg, 4), "pct_change": pct,
            "from_date": first["valid_from"], "to_date": last["valid_from"],
            "series": series,
            "confidence": last.get("confidence", ""),
        })
    flags.sort(key=lambda f: (f["run_len"], abs(f["pct_change"] or 0)), reverse=True)
    return {"min_run": min_run, "flags": flags,
            "note_en": kb.get("drift_config", {}).get("note_en", ""),
            "note_zh": kb.get("drift_config", {}).get("note_zh", "")}


# --------------------------------------------------------------------------- #
# Sensitivity — which single assumption moves the answer most
# --------------------------------------------------------------------------- #
def sensitivity(kb, regime_id, capex_usd_bn, as_of):
    """For each banded node, swing it across its own band (others held at point)
    and measure the % move in total wafers. Ranks where the output uncertainty is
    concentrated -> where to spend research effort. Separately reports the regime
    swing, which is structurally larger than any single parameter."""
    central = total_wafers(kb, regime_id, capex_usd_bn, as_of)
    rows = []
    for node_id in WAFER_DIR:
        node = get_node(kb, node_id)
        w_lo = total_wafers(kb, regime_id, capex_usd_bn, as_of, override={node_id: "wmin"})
        w_hi = total_wafers(kb, regime_id, capex_usd_bn, as_of, override={node_id: "wmax"})
        swing = w_hi - w_lo
        rows.append({
            "node_id": node_id, "name_en": node["name_en"], "name_zh": node["name_zh"],
            "stage": node["stage"],
            "wafers_low": round(w_lo), "wafers_high": round(w_hi),
            "swing_wafers": round(swing),
            "swing_pct": round(swing / central * 100, 1) if central else None,
        })
    rows.sort(key=lambda r: r["swing_wafers"], reverse=True)

    sc = scenario_comparison(kb, capex_usd_bn, as_of)
    regime_swing = {
        "swing_wafers": sc["spread_wafers"], "swing_pct": sc["spread_pct"],
        "low_regime": min(sc["rows"], key=lambda r: r["wafers_year"])["regime"],
        "high_regime": max(sc["rows"], key=lambda r: r["wafers_year"])["regime"],
    }
    return {"central_wafers": round(central), "rows": rows, "regime_swing": regime_swing}


# --------------------------------------------------------------------------- #
# Evidence linkage (for traceability + the human-in-the-loop intake panel)
# --------------------------------------------------------------------------- #
def evidence_for_node(kb, node_id):
    return [e for e in kb.get("evidence", []) if node_id in e.get("affects", [])]


def evidence_index(kb):
    return {e["id"]: e for e in kb.get("evidence", [])}


# --------------------------------------------------------------------------- #
# Top-level snapshot assembly
# --------------------------------------------------------------------------- #
def default_capex(kb):
    inputs = kb.get("capex_inputs", [])
    for c in inputs:
        if c.get("id") == kb.get("default_capex_input"):
            return c
    for c in inputs:
        if c.get("default"):
            return c
    return inputs[0] if inputs else {"id": "none", "value_usd_bn": 0}


def build_snapshot(kb, generated_at=None, live_capex=None, evidence_proposal=None):
    """Assemble the full rendered snapshot from the assumption graph. Pure except
    for the timestamp/live context passed in by the caller."""
    as_of = kb["_meta"]["as_of"]
    regime_id = kb["active_regime"]
    cap = default_capex(kb)
    capex_bn = cap["value_usd_bn"]

    primary = run_chain(kb, regime_id, capex_bn, as_of)
    scenarios = scenario_comparison(kb, capex_bn, as_of)
    drift = detect_drift(kb, as_of)
    sens = sensitivity(kb, regime_id, capex_bn, as_of)

    ev_idx = evidence_index(kb)
    # attach resolved evidence objects to each waterfall stage for click-through
    for st in primary["waterfall"]:
        ids = st["meta"].get("evidence_ids", [])
        st["evidence"] = [ev_idx[i] for i in ids if i in ev_idx]

    active_regime = next((r for r in kb["regimes"] if r["id"] == regime_id), None)

    return {
        "generated_at": generated_at,
        "as_of": as_of,
        "is_demo": live_capex is None,
        "meta": kb["_meta"],
        "active_regime": regime_id,
        "active_regime_obj": active_regime,
        "regimes": kb["regimes"],
        "segments": kb["segments"],
        "stages": kb["stages"],
        "capex_input": cap,
        "capex_inputs": kb.get("capex_inputs", []),
        "live_capex_context": live_capex or {},
        "inference": primary,
        "scenarios": scenarios,
        "drift": drift,
        "sensitivity": sens,
        "evidence": kb.get("evidence", []),
        "sources": kb.get("sources", []),
        "evidence_proposal": evidence_proposal,
        "nodes_index": [{"id": n["id"], "stage": n["stage"], "name_en": n["name_en"],
                         "name_zh": n["name_zh"], "kind": n["kind"],
                         "n_versions": len(n["versions"])} for n in kb["nodes"]],
    }
