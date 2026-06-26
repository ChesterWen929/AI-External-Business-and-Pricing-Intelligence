"""
Deliverable-Compute Bottleneck Radar — the pure model.

TSMC AI capacity is necessary but NOT sufficient. The compute that actually ships
is set by the WEAKEST link across the whole supply chain (Liebig's law of the
minimum / theory of constraints). This module converts every link to ONE common
unit — B200/GB200-class accelerator-equivalents per quarter (EA/qtr) — and takes
the minimum. It is pure, side-effect-free (no I/O, no network, no globals), so the
whole thing is trivially unit-testable. The API/UI layer is a thin shell over it.

Each link's current-quarter capacity is computed transparently from a `derivation`
chain (base × factor × factor / factor …), every factor being dated, sourced and
tier-graded DATA. The per-quarter `curve` drives the timeline view; a sanity test
asserts the derivation product matches the curve at the current quarter.

Outputs:
  • inference   — current-quarter capacity per link + the binding link (waterfall)
  • thesis      — the headline answer: is TSMC the bottleneck? by how much?
  • timeline    — binding link per quarter + bottleneck-migration segments
  • scenarios   — supply-side overrides → does the binding link move?
  • sensitivity — which link, varied across its band, moves deliverable the most
"""
from __future__ import annotations


# --------------------------------------------------------------------------- #
# Graph access
# --------------------------------------------------------------------------- #
def get_link(kb, link_id):
    for ln in kb["links"]:
        if ln["id"] == link_id:
            return ln
    raise KeyError(f"unknown link {link_id}")


def link_ids(kb):
    return [ln["id"] for ln in kb["links"]]


def quarter_index(kb, q):
    return kb["quarters"].index(q)


# --------------------------------------------------------------------------- #
# Derivation — fold a base×/factor chain into a banded capacity
# --------------------------------------------------------------------------- #
def fold_derivation(derivation):
    """Fold an ordered derivation into {point, low, high, steps}.

    The confidence band is an explicit OUTER envelope: we push every factor to
    its low end together (and its high end together), respecting that dividing by
    a larger number lowers the result. This is a deliberate min/max envelope, not
    a fake probabilistic CI — we refuse the false precision of a single number.
    """
    point = low = high = None
    steps = []
    for f in derivation:
        v = f["value"]
        op = f["op"]
        if op == "base":
            point, low, high = v["point"], v["low"], v["high"]
        elif op == "x":
            point *= v["point"]
            low *= v["low"]
            high *= v["high"]
        elif op == "/":
            point /= v["point"]
            low /= v["high"]   # divide by the high end -> low result
            high /= v["low"]
        else:
            raise ValueError(f"unknown derivation op {op!r}")
        steps.append({
            "op": op,
            "factor_en": f.get("factor_en"), "factor_zh": f.get("factor_zh"),
            "unit_en": f.get("unit_en"), "unit_zh": f.get("unit_zh"),
            "tier": f.get("tier"), "source_en": f.get("source_en"),
            "source_zh": f.get("source_zh"), "valid_from": f.get("valid_from"),
            "value": v,
            "running_point": round(point),
        })
    return {"point": round(point), "low": round(low), "high": round(high), "steps": steps}


def link_capacity(link):
    """Current-quarter capacity (EA/qtr) for one link, from its derivation."""
    folded = fold_derivation(link["derivation"])
    return folded


def curve_at(link, q_index):
    return link["curve"][q_index]


# --------------------------------------------------------------------------- #
# Bottleneck — the minimum across links
# --------------------------------------------------------------------------- #
def binding(capacities):
    """capacities: {link_id: point}. Returns (binding_id, deliverable_point)."""
    binding_id = min(capacities, key=capacities.get)
    return binding_id, capacities[binding_id]


def ranked_links(kb, capacities, deliverable):
    """Links sorted ascending by capacity, each with headroom over the binding link."""
    rows = []
    for lid in sorted(capacities, key=capacities.get):
        ln = get_link(kb, lid)
        cap = capacities[lid]
        rows.append({
            "id": lid,
            "name_en": ln["name_en"], "name_zh": ln["name_zh"],
            "category": ln["category"], "icon": ln.get("icon", ""),
            "owner_en": ln.get("owner_en"), "owner_zh": ln.get("owner_zh"),
            "capacity": cap,
            "is_binding": cap == deliverable,
            "headroom_pct": round((cap - deliverable) / deliverable * 100, 1) if deliverable else 0.0,
        })
    return rows


# --------------------------------------------------------------------------- #
# Inference — current-quarter snapshot
# --------------------------------------------------------------------------- #
def build_inference(kb):
    caps, bands = {}, {}
    for ln in kb["links"]:
        folded = link_capacity(ln)
        caps[ln["id"]] = folded["point"]
        bands[ln["id"]] = folded
    binding_id, deliverable = binding(caps)
    ln_b = get_link(kb, binding_id)
    band_b = bands[binding_id]
    return {
        "capacities": caps,
        "bands": bands,
        "binding_link": binding_id,
        "binding_name_en": ln_b["name_en"], "binding_name_zh": ln_b["name_zh"],
        "deliverable_ea_qtr": deliverable,
        "deliverable_ea_year": deliverable * 4,
        "deliverable_low_qtr": band_b["low"],
        "deliverable_high_qtr": band_b["high"],
        "ranked": ranked_links(kb, caps, deliverable),
    }


# --------------------------------------------------------------------------- #
# Thesis — the headline answer to the user's question
# --------------------------------------------------------------------------- #
TSMC_LINKS = ("tsmc_wafer", "tsmc_cowos")


def build_thesis(kb, inference):
    caps = inference["capacities"]
    deliverable = inference["deliverable_ea_qtr"]
    binding_id = inference["binding_link"]
    tsmc_min = min(caps[l] for l in TSMC_LINKS)
    tsmc_binding_id = min(TSMC_LINKS, key=lambda l: caps[l])
    tsmc_is_bottleneck = binding_id in TSMC_LINKS
    # headroom of TSMC's tightest link OVER the system binding constraint
    headroom_pct = round((tsmc_min - deliverable) / deliverable * 100, 1) if deliverable else 0.0
    ln_b = get_link(kb, binding_id)
    ln_tb = get_link(kb, tsmc_binding_id)
    if tsmc_is_bottleneck:
        verdict_en = (f"TSMC IS the binding constraint this quarter ({ln_b['name_en']}). "
                      f"Deliverable compute is capped at TSMC, not by downstream components.")
        verdict_zh = (f"本季台積電就是綁定約束（{ln_b['name_zh']}）。"
                      f"可交付算力被台積電本身、而非下游零件所限制。")
    else:
        verdict_en = (f"TSMC capacity is sufficient — its tightest link ({ln_tb['name_en']}) "
                      f"sits ~{headroom_pct:.0f}% ABOVE the system bottleneck, which is "
                      f"{ln_b['name_en']}. Adding TSMC capacity alone would not raise deliverable compute.")
        verdict_zh = (f"台積電產能充足——其最緊環節（{ln_tb['name_zh']}）較系統瓶頸高約 "
                      f"{headroom_pct:.0f}%，真正的瓶頸是 {ln_b['name_zh']}。"
                      f"單獨增加台積電產能並不會提高可交付算力。")
    return {
        "binding_link": binding_id,
        "binding_name_en": ln_b["name_en"], "binding_name_zh": ln_b["name_zh"],
        "binding_category": ln_b["category"],
        "deliverable_ea_qtr": deliverable,
        "deliverable_ea_year": deliverable * 4,
        "tsmc_is_bottleneck": tsmc_is_bottleneck,
        "tsmc_binding_link": tsmc_binding_id,
        "tsmc_min_ea_qtr": tsmc_min,
        "tsmc_headroom_pct": headroom_pct,
        "verdict_en": verdict_en,
        "verdict_zh": verdict_zh,
    }


# --------------------------------------------------------------------------- #
# Timeline — binding link per quarter + migration segments
# --------------------------------------------------------------------------- #
def build_timeline(kb):
    quarters = kb["quarters"]
    rows = []
    for i, q in enumerate(quarters):
        caps = {ln["id"]: curve_at(ln, i) for ln in kb["links"]}
        b_id, deliverable = binding(caps)
        rows.append({"q": q, "caps": caps, "binding": b_id, "deliverable": deliverable})
    # compress consecutive same-binding quarters into migration segments
    segments = []
    for r in rows:
        if segments and segments[-1]["binding"] == r["binding"]:
            segments[-1]["to"] = r["q"]
        else:
            ln = get_link(kb, r["binding"])
            segments.append({
                "binding": r["binding"],
                "name_en": ln["name_en"], "name_zh": ln["name_zh"],
                "category": ln["category"], "icon": ln.get("icon", ""),
                "from": r["q"], "to": r["q"],
            })
    return {"rows": rows, "segments": segments}


# --------------------------------------------------------------------------- #
# Scenarios — supply-side overrides; does the binding link move?
# --------------------------------------------------------------------------- #
def apply_scenario(base_caps, overrides):
    return {lid: round(cap * overrides.get(lid, 1.0)) for lid, cap in base_caps.items()}


def build_scenarios(kb, inference):
    base_caps = inference["capacities"]
    base_binding = inference["binding_link"]
    base_deliverable = inference["deliverable_ea_qtr"]
    rows = []
    for sc in kb["scenarios"]:
        caps = apply_scenario(base_caps, sc.get("overrides", {}))
        b_id, deliverable = binding(caps)
        ln = get_link(kb, b_id)
        rows.append({
            "id": sc["id"],
            "name_en": sc["name_en"], "name_zh": sc["name_zh"],
            "desc_en": sc["desc_en"], "desc_zh": sc["desc_zh"],
            "overrides": sc.get("overrides", {}),
            "binding": b_id,
            "binding_name_en": ln["name_en"], "binding_name_zh": ln["name_zh"],
            "binding_category": ln["category"],
            "deliverable_ea_qtr": deliverable,
            "delta_pct": round((deliverable - base_deliverable) / base_deliverable * 100, 1) if base_deliverable else 0.0,
            "moved": b_id != base_binding,
        })
    return {"base_binding": base_binding, "rows": rows}


# --------------------------------------------------------------------------- #
# Sensitivity — vary each link across its band; rank by swing in deliverable
# --------------------------------------------------------------------------- #
def build_sensitivity(kb, inference):
    base_caps = inference["capacities"]
    bands = inference["bands"]
    base_deliverable = inference["deliverable_ea_qtr"]
    rows = []
    for ln in kb["links"]:
        lid = ln["id"]
        caps_low = dict(base_caps); caps_low[lid] = bands[lid]["low"]
        caps_high = dict(base_caps); caps_high[lid] = bands[lid]["high"]
        _, d_low = binding(caps_low)
        _, d_high = binding(caps_high)
        swing = d_high - d_low
        rows.append({
            "id": lid,
            "name_en": ln["name_en"], "name_zh": ln["name_zh"],
            "category": ln["category"], "icon": ln.get("icon", ""),
            "deliverable_low": d_low,
            "deliverable_high": d_high,
            "swing": swing,
            "swing_pct": round(swing / base_deliverable * 100, 1) if base_deliverable else 0.0,
        })
    rows.sort(key=lambda r: r["swing"], reverse=True)
    return {"rows": rows, "base_deliverable": base_deliverable}


# --------------------------------------------------------------------------- #
# Top-level assembly
# --------------------------------------------------------------------------- #
def build_snapshot(kb, generated_at):
    inference = build_inference(kb)
    return {
        "is_demo": True,
        "as_of": kb["_meta"]["as_of"],
        "current_quarter": kb["_meta"]["current_quarter"],
        "generated_at": generated_at,
        "meta": kb["_meta"],
        "reference_accelerator": kb["reference_accelerator"],
        "links_meta": [
            {k: ln.get(k) for k in ("id", "category", "icon", "name_en", "name_zh",
                                    "owner_en", "owner_zh", "desc_en", "desc_zh",
                                    "native_unit_en", "native_unit_zh", "derivation")}
            for ln in kb["links"]
        ],
        "inference": inference,
        "thesis": build_thesis(kb, inference),
        "timeline": build_timeline(kb),
        "scenarios": build_scenarios(kb, inference),
        "sensitivity": build_sensitivity(kb, inference),
        "evidence": kb.get("evidence", []),
        "quarters": kb["quarters"],
    }
