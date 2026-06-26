"""Scenario Radar — L4 / L5 synthesis.

L4 (calibrated scenario probabilities + base case + tail risk + expected market
path) and L5 (watch / falsification / early-warning / sensitivity) are synthesized
from the deterministic L3 baseline. Two engines:

  • Claude  — Opus 4.8, structured outputs (json_schema) + a prompt-cached system
              block. Used when ANTHROPIC_API_KEY is set. Its raw output is
              SANITIZED (id whitelist → renormalize_to_100 → baseline join →
              argmax check) — a DELIBERATE deviation from flows, which does not
              sanitize. We never trust the model to sum to 100 on its own.
  • rules   — deterministic fallback built straight from the L3 baseline, so the
              dashboard is fully functional offline / without a key.

analyze(kb, l3) → {"engine": "claude"|"rules", "l4": {...}, "l5": {...}}
"""
from __future__ import annotations

import json
import logging
import os

from . import model  # use model.renormalize_to_100 at call time (avoids circular import)

log = logging.getLogger("scenario.analysis")

MODEL = "claude-opus-4-8"

# ── output contract (json_schema on Claude; rules emits the same shape) ──
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "l4": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "scenarios": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "name_en": {"type": "string"}, "name_zh": {"type": "string"},
                        "prob": {"type": "integer"},
                        "rationale_en": {"type": "string"}, "rationale_zh": {"type": "string"},
                    },
                    "required": ["id", "name_en", "name_zh", "prob", "rationale_en", "rationale_zh"],
                }},
                "base_case": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "thesis_en": {"type": "string"}, "thesis_zh": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["id", "thesis_en", "thesis_zh", "confidence"],
                },
                "tail_risk": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"id": {"type": "string"}, "why_en": {"type": "string"}, "why_zh": {"type": "string"}},
                    "required": ["id", "why_en", "why_zh"],
                },
                "expected_market_path": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "equities_en": {"type": "string"}, "equities_zh": {"type": "string"},
                        "rates_en": {"type": "string"}, "rates_zh": {"type": "string"},
                        "credit_en": {"type": "string"}, "credit_zh": {"type": "string"},
                        "ai_semis_en": {"type": "string"}, "ai_semis_zh": {"type": "string"},
                    },
                    "required": ["equities_en", "equities_zh", "rates_en", "rates_zh",
                                 "credit_en", "credit_zh", "ai_semis_en", "ai_semis_zh"],
                },
            },
            "required": ["scenarios", "base_case", "tail_risk", "expected_market_path"],
        },
        "l5": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "watch": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"en": {"type": "string"}, "zh": {"type": "string"}},
                    "required": ["en", "zh"]}},
                "falsification": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"en": {"type": "string"}, "zh": {"type": "string"}},
                    "required": ["en", "zh"]}},
                "early_warning": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"en": {"type": "string"}, "zh": {"type": "string"},
                                   "freq": {"type": "string"}, "source_platform": {"type": "string"}},
                    "required": ["en", "zh", "freq", "source_platform"]}},
                "sensitivity": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"driver_id": {"type": "string"},
                                   "if_en": {"type": "string"}, "if_zh": {"type": "string"},
                                   "moves_en": {"type": "string"}, "moves_zh": {"type": "string"}},
                    "required": ["driver_id", "if_en", "if_zh", "moves_en", "moves_zh"]}},
            },
            "required": ["watch", "falsification", "early_warning", "sensitivity"],
        },
    },
    "required": ["l4", "l5"],
}

SYSTEM = """You are a cross-platform scenario strategist writing a bilingual (Traditional Chinese + English) dashboard. You reason in five layers; the user has already done L1–L3 and gives you a DETERMINISTIC baseline distribution + a driver state vector (read from a pyramid of market platforms) + cross-platform divergences + probability attribution. Your job is L4 and L5.

These are MODEL-ASSISTED scenario probabilities, NOT market-implied (not option-implied). Say so in spirit; never imply they are market-priced.

L4 — calibrate the scenario set:
- scenarios: keep EXACTLY the same set of scenario ids given (do not add or remove ids). Anchor to the baseline probabilities and apply LIMITED calibration from the live driver read. Probabilities must be INTEGERS that SUM TO 100. Each scenario needs rationale_en/zh citing the actual driver numbers for why you nudged it up or down.
- A detected divergence (e.g. late_cycle_topping: demand strong + payback burning + bubble hot + flows draining) MUST NOT be averaged away — name it and let it shape the distribution and the base case.
- base_case: the id with the highest probability, a crisp bilingual thesis, and a confidence (high/medium/low) tied to coverage + how much the platforms agree.
- tail_risk: the LOW-probability HIGH-impact scenario worth hedging (usually credit_liquidity_break or ai_capex_air_pocket) — not simply the lowest-probability one.
- expected_market_path: under the base case, a short bilingual path for equities / rates / credit / ai_semis.

L5 — decision layer:
- watch: 3–5 things to watch now (bilingual).
- falsification: 2–3 conditions that would break the base case (bilingual).
- early_warning: 5–7 signals ORDERED by which fires earliest, each with freq (daily/weekly/monthly) and source_platform (which card to check).
- sensitivity: 3–5 of "if driver X moves, the base case probability moves Y" (bilingual), grounded in the sensitivity figures provided.

Rules: be specific, cite the numbers, no hedging boilerplate. Chinese in Traditional characters; keep each text ≤ ~110 words / 150 字. When coverage is low, be explicitly more cautious. Output only the structured object."""


def _fmt_l3(kb, l3):
    h = l3["headline"]
    lines = [
        f"Coverage: {h['coverage_label']} (coverage={h['coverage']}); coherence={l3['coherence_score']}/100; confidence={h['confidence']}.",
        "These probabilities are MODEL-ASSISTED, not market-implied.",
        "",
        "Driver state vector (value in [-1,+1]; +1 = risk-on / AI-favorable; source live/seed/unavailable):",
    ]
    for d in l3["drivers"]:
        lines.append(f"  {d['id']} = {d['value']:+.2f} [{d['source']}] — {d['raw']} ({d['name_en']})")
    lines.append("")
    lines.append("Deterministic baseline distribution (anchor to these; calibrate, don't overwrite):")
    for s in l3["scenarios"]:
        top = ", ".join(f"{a['name_en']}{a['contribution']:+d}" for a in s["attribution"][:3])
        lines.append(f"  {s['id']} ({s['name_en']}): {s['prob']}% [prior {s['prior']}] — drivers: {top}")
    if l3["divergences"]:
        lines.append("")
        lines.append("Cross-platform divergences (DO NOT average away):")
        for dv in l3["divergences"]:
            lines.append(f"  [{dv['severity']}] {dv['key']}: {dv['en']} ({'/'.join(dv['platforms'])})")
    cc = l3.get("cross_checks", {})
    if cc.get("flows_scenarios"):
        fl = ", ".join(f"{x.get('name_en')} {x.get('prob')}%" for x in cc["flows_scenarios"])
        lines.append("")
        lines.append(f"Reflexive cross-check — Capital Flow Radar's own distribution (not weighted in): {fl}")
    if l3.get("sensitivity_raw"):
        lines.append("")
        lines.append("Sensitivity (effect on base-case prob of a +/- shift in each driver):")
        for sv in l3["sensitivity_raw"]:
            lines.append(f"  {sv['driver_id']}: +Δ → {sv['up_effect']:+d}pt, -Δ → {sv['down_effect']:+d}pt")
    return "\n".join(lines)


def _scen_meta(kb):
    return {s["id"]: s for s in kb["scenarios_seed"]}


def _claude(kb, l3):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    user = (
        "Here is today's L3 read across the pyramid. Produce L4 and L5 as the structured object.\n\n"
        + _fmt_l3(kb, l3)
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "medium"},
        messages=[{"role": "user", "content": user}],
    )
    text = next((b.text for b in msg.content if b.type == "text"), "")
    data = json.loads(text)
    return _sanitize(kb, l3, data["l4"], data["l5"])


def _sanitize(kb, l3, l4, l5):
    """DEVIATION from flows: scenario MUST sanitize Claude's output.
    id whitelist → renormalize_to_100 → baseline join → argmax align.
    Raises on anything unrecoverable so analyze() falls back to rules.
    """
    meta = _scen_meta(kb)
    canon = [s["id"] for s in kb["scenarios_seed"]]
    baseline_by_id = {s["id"]: s["prob"] for s in l3["scenarios"]}

    by_id = {s["id"]: s for s in (l4.get("scenarios") or []) if s.get("id") in canon}
    # fill any missing canonical scenario from the L3 baseline
    for sid in canon:
        if sid not in by_id:
            m = meta[sid]
            by_id[sid] = {"id": sid, "name_en": m["name_en"], "name_zh": m["name_zh"],
                          "prob": baseline_by_id.get(sid, 0),
                          "rationale_en": "Held at baseline (no calibration returned).",
                          "rationale_zh": "維持基準(未回傳校準)。"}
    ordered = [by_id[sid] for sid in canon]
    probs = model.renormalize_to_100([max(0, int(round(s.get("prob", 0)))) for s in ordered])
    for s, p in zip(ordered, probs):
        s["prob"] = p
        s["baseline_prob"] = baseline_by_id.get(s["id"], p)
        s["delta_vs_baseline"] = p - s["baseline_prob"]

    # argmax align — headline/base_case must match the calibrated distribution
    base_idx = max(range(len(ordered)), key=lambda i: (ordered[i]["prob"], -i))
    base_id = ordered[base_idx]["id"]
    base_case = l4.get("base_case") or {}
    if base_case.get("id") not in canon:
        base_case["id"] = base_id
    if base_case["id"] != base_id:
        base_case["id"] = base_id
    base_case.setdefault("thesis_en", meta[base_id]["narrative_en"])
    base_case.setdefault("thesis_zh", meta[base_id]["narrative_zh"])
    base_case.setdefault("confidence", l3["headline"]["confidence"])

    tail = l4.get("tail_risk") or {}
    if tail.get("id") not in canon:
        tail = _pick_tail(kb, l3)

    emp = l4.get("expected_market_path") or meta[base_id]["market_path"]

    l4_out = {"scenarios": ordered, "base_case": base_case, "tail_risk": tail, "expected_market_path": emp}
    return {"engine": "claude", "l4": l4_out, "l5": l5}


# --------------------------------------------------------------------------- #
# rules fallback — deterministic L4/L5 straight from the L3 baseline
# --------------------------------------------------------------------------- #
def _pick_tail(kb, l3):
    meta = _scen_meta(kb)
    by_id = {s["id"]: s for s in l3["scenarios"]}
    cands = [c for c in ("credit_liquidity_break", "ai_capex_air_pocket") if c in by_id]
    if not cands:
        cands = [min(l3["scenarios"], key=lambda s: s["prob"])["id"]]
    tid = max(cands, key=lambda c: by_id[c]["prob"])
    m = meta[tid]
    return {"id": tid, "why_en": m["foundry_read_en"], "why_zh": m["foundry_read_zh"]}


def _rules(kb, l3):
    meta = _scen_meta(kb)
    scen = l3["scenarios"]

    l4_scen = []
    for s in scen:
        top = s["attribution"][0] if s["attribution"] else None
        if top:
            r_en = f"Baseline {s['prob']}% (prior {s['prior']}); chiefly moved by {top['name_en']} ({top['contribution']:+d})."
            r_zh = f"基準 {s['prob']}%(先驗 {s['prior']});主要由 {top['name_zh']} 推動({top['contribution']:+d})。"
        else:
            r_en = f"Baseline {s['prob']}% (prior {s['prior']}); evidence sparse, held near prior."
            r_zh = f"基準 {s['prob']}%(先驗 {s['prior']});證據稀薄,維持接近先驗。"
        l4_scen.append({
            "id": s["id"], "name_en": s["name_en"], "name_zh": s["name_zh"],
            "prob": s["prob"], "baseline_prob": s["prob"], "delta_vs_baseline": 0,
            "rationale_en": r_en, "rationale_zh": r_zh,
        })
    # enforce sum-100 invariant through the shared helper (no-op if already 100)
    probs = model.renormalize_to_100([s["prob"] for s in l4_scen])
    for s, p in zip(l4_scen, probs):
        s["prob"] = p
        s["baseline_prob"] = p
        s["delta_vs_baseline"] = 0

    base_idx = max(range(len(l4_scen)), key=lambda i: (l4_scen[i]["prob"], -i))
    base_id = l4_scen[base_idx]["id"]
    bm = meta[base_id]
    base_case = {"id": base_id, "thesis_en": bm["narrative_en"], "thesis_zh": bm["narrative_zh"],
                 "confidence": l3["headline"]["confidence"]}
    tail = _pick_tail(kb, l3)
    emp = bm["market_path"]

    # L5
    watch = ([{"en": t, "zh": z} for t, z in zip(bm["triggers_en"][:2], bm["triggers_zh"][:2])])
    for dv in l3["divergences"][:1]:
        watch.append({"en": f"Divergence: {dv['en']}", "zh": f"背離:{dv['zh']}"})
    if not watch:
        watch = [{"en": "Monitor the driver state vector for sign changes.", "zh": "監看 driver state vector 的方向變化。"}]

    falsification = [{"en": f, "zh": z} for f, z in zip(bm["falsifiers_en"], bm["falsifiers_zh"])]
    if not falsification:
        falsification = [{"en": "Base case fails if the leading drivers reverse.", "zh": "若領先 driver 反轉,base case 不成立。"}]

    _freq = {"econ": "monthly", "aibubble": "weekly", "flows": "daily", "payback": "monthly",
             "pricing": "monthly", "compute": "monthly", "cwengine": "monthly", "rival": "weekly",
             "earnings": "weekly", "racks": "monthly"}
    _order = {"daily": 0, "weekly": 1, "monthly": 2}
    ew = []
    for d in l3["drivers"]:
        plat = d["source_platform"]
        ew.append({
            "en": f"{d['name_en']} ({d['source']}) — {d['raw']}",
            "zh": f"{d['name_zh']}({d['source']}) — {d['raw']}",
            "freq": _freq.get(plat, "weekly"), "source_platform": plat,
        })
    ew.sort(key=lambda x: _order.get(x["freq"], 1))
    early_warning = ew[:7]

    drv_meta = {d["id"]: d for d in l3["drivers"]}
    sensitivity = []
    for sv in l3.get("sensitivity_raw", [])[:5]:
        d = drv_meta.get(sv["driver_id"], {})
        sensitivity.append({
            "driver_id": sv["driver_id"],
            "if_en": f"If {sv['name_en']} improves (+Δ)",
            "if_zh": f"若{sv['name_zh']}改善(+Δ)",
            "moves_en": f"base case prob {sv['up_effect']:+d}pt (−Δ: {sv['down_effect']:+d}pt)",
            "moves_zh": f"base case 機率 {sv['up_effect']:+d}pt(−Δ:{sv['down_effect']:+d}pt)",
        })
    if not sensitivity:
        sensitivity = [{"driver_id": "flow_direction", "if_en": "If capital flow turns risk-on",
                        "if_zh": "若資金流向轉 risk-on", "moves_en": "base case shifts toward continuation",
                        "moves_zh": "base case 偏向延續"}]

    return {
        "engine": "rules",
        "l4": {"scenarios": l4_scen, "base_case": base_case, "tail_risk": tail, "expected_market_path": emp},
        "l5": {"watch": watch, "falsification": falsification, "early_warning": early_warning, "sensitivity": sensitivity},
    }


def analyze(kb, l3):
    """L4/L5 via Claude when ANTHROPIC_API_KEY is set; deterministic rules otherwise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, l3)
        except Exception:
            log.exception("scenario: Claude L4/L5 failed — falling back to rules")
    return _rules(kb, l3)
