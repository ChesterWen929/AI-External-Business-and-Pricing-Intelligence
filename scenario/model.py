"""Scenario Radar — L3 pure-quant aggregator + snapshot assembler.

Unlike the other cards (which pull raw yfinance/FRED), this platform's L3 input is
the OTHER pyramid platforms' snapshots. `build_snapshot(kb, siblings, ...)`:

  1. _extract_drivers  — read each sibling snapshot, normalize 10 drivers to [-1,+1]
                         (+1 = risk-on / AI-favorable), with live/seed/unavailable tagging
  2. coverage          — live_count / 10 (honesty metric)
  3. baseline dist     — prior + coverage-shrunk softmax over driver evidence → probs
  4. renormalize_to_100 — integer probabilities that provably sum to 100
  5. attribution       — proxy decomposition of each scenario's deviation-from-prior
  6. divergence engine — cross-platform contradictions (the signature signal)
  7. drift             — probability change vs the prior snapshot
  8. sensitivity       — first-order, perturb the pre-softmax state vector
Then calls analysis.analyze() for L4/L5 (Claude, or rules fallback).

This module ONLY consumes the `siblings` dict it is given — it never imports the
sibling packages (those imports are lazy in __init__._gather_siblings), so importing
`scenario` never boots the whole app.
"""
from __future__ import annotations

import math

from . import analysis

# flows' KB scenarios_seed order (stable ids) — its l5.scenarios[] carry no id, so we
# map positionally by this order, then to our scenario ids.
FLOWS_SEED_ORDER = ["continuation", "rotation", "blowoff", "regime_break"]
FLOWS_TO_OURS = {
    "continuation": "soft_landing_broadening",
    "blowoff": "goldilocks_melt_up",
    "regime_break": "credit_liquidity_break",
    "rotation": "growth_scare_rotation",
}

REGIME_BY_SCENARIO = {
    "soft_landing_broadening": "risk_on",
    "goldilocks_melt_up": "melt_up",
    "late_cycle_topping": "topping",
    "growth_scare_rotation": "risk_off",
    "credit_liquidity_break": "risk_off",
    "ai_capex_air_pocket": "air_pocket",
}

# When late_cycle_topping is the base case but the SIGNATURE high-severity divergence
# has NOT fired (capital is not yet draining — flow_direction >= 0), we must not assert
# the "capital is quietly draining" thesis. Reframe as an EARLY/UNCONFIRMED topping risk
# (bubble hot, internals soft, but flows have not turned) and downgrade the regime tag
# from "topping" to "mixed" so the headline matches the engine's actual evidence.
TOPPING_UNCONFIRMED_NARRATIVE = {
    "en": ("Topping risk is building but NOT yet confirmed: the bubble runs hot and "
           "monetization/pricing internals are soft, yet capital has not turned — flows are "
           "still neutral-to-inbound. An early-cycle divergence, not the signature top. "
           "Watch flow_direction: a turn negative would complete the late-cycle pattern."),
    "zh": ("見頂風險在累積、但尚未確認:泡沫燒得熱、變現/議價等內裡偏弱,然而資金尚未轉向 — "
           "流向仍中性偏流入。屬早期分歧,非招牌見頂。緊盯 flow_direction:一旦轉負,"
           "後段見頂格局才算成形。"),
}


def _headline_regime_narrative(base_s, signature_active):
    """Resolve (regime_key, base_narrative) so the headline matches the EVIDENCE (H10).

    base_s is the winning scenario dict (has id + narrative). When the base case is
    late_cycle_topping we keep the "topping" regime + signature narrative ONLY if the
    high-severity divergence actually fired. If it did not (capital not draining), we
    downgrade to a "mixed" regime and an UNCONFIRMED-topping narrative — never asserting
    capital is leaving. For every other scenario the regime map applies directly, and the
    signature divergence (if separately present) still escalates the regime to topping.
    """
    sid = base_s["id"]
    if sid == "late_cycle_topping":
        if signature_active:
            return "topping", base_s["narrative"]
        return "mixed", dict(TOPPING_UNCONFIRMED_NARRATIVE)
    regime_key = REGIME_BY_SCENARIO.get(sid, "mixed")
    if signature_active:
        regime_key = "topping"
    return regime_key, base_s["narrative"]


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _tanh(x):
    try:
        return math.tanh(x)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# renormalize_to_100 — shared by rules + Claude paths
# --------------------------------------------------------------------------- #
def renormalize_to_100(vals):
    """Largest-remainder rounding to integers summing to exactly 100.

    Pre-clamps inputs to >= 0 (softmax is positive, but sensitivity perturbations
    and a disobedient Claude can emit 0/negative/sum!=100). All-zero input returns
    an as-even-as-possible deterministic split. Tie-break by index → deterministic.
    """
    v = [max(0.0, float(x)) for x in vals]
    n = len(v)
    if n == 0:
        return []
    tot = sum(v)
    if tot <= 0:
        base = 100 // n
        out = [base] * n
        for i in range(100 - base * n):
            out[i] += 1
        return out
    scaled = [x / tot * 100.0 for x in v]
    floor = [int(math.floor(x)) for x in scaled]
    rem = 100 - sum(floor)
    order = sorted(range(n), key=lambda i: (-(scaled[i] - floor[i]), i))
    for k in range(rem):
        floor[order[k]] += 1
    return floor


# --------------------------------------------------------------------------- #
# sibling field helpers
# --------------------------------------------------------------------------- #
def _ind(snap, iid):
    for i in (snap.get("indicators") or []):
        if i.get("id") == iid:
            return i.get("latest_value")
    return None


def _ind_chg(snap, iid, hz):
    for i in (snap.get("indicators") or []):
        if i.get("id") == iid:
            c = (i.get("changes") or {}).get(hz) or {}
            return c.get("pct")
    return None


def _is_live(platform, snap):
    """Per-platform live-vs-seed rule (7 siblings have no is_demo flag — pin each)."""
    if snap is None:
        return False
    try:
        if platform in ("flows", "payback", "pricing"):
            return snap.get("source") == "live" or snap.get("is_demo") is False
        if platform in ("compute", "racks", "cwengine"):
            return snap.get("live_present") is True
        if platform == "earnings":
            s = snap.get("source")
            return bool(s) and s != "seed"
        if platform == "econ":
            return bool(snap.get("date"))
        if platform == "aibubble":
            return (snap.get("scores") or {}).get("composite") is not None
        if platform == "rival":
            return len(snap.get("events") or []) > 0
        if platform == "bottleneck":
            return True
    except Exception:
        return False
    return False


def _normalize(did, snap, cfg):
    """Return (value in [-clamp,+clamp], raw_label) for driver `did` from `snap`."""
    c = cfg["value_clamp"]
    if did == "macro_cycle":
        cfnai = _ind(snap, "cfnai")
        curve = _ind(snap, "yield_curve_3m_10y")
        v = _tanh(cfnai if cfnai is not None else 0.0)
        if curve is not None:
            v += 0.3 * _tanh(curve)
        tn = snap.get("tsmc_negative_count") or 0
        if tn:
            v -= 0.1 * min(tn, 3)
        return _clamp(v, -c, c), f"CFNAI {cfnai}, 3m10y {curve}, tsmc- {tn}"
    if did == "inflation_rates":
        cpi = _ind_chg(snap, "cpi", "1y")
        v = -_tanh(((cpi if cpi is not None else 2.5) - 3.0) / 3.0)
        return _clamp(v, -c, c), f"CPI YoY {cpi:.1f}%" if cpi is not None else "CPI YoY n/a"
    if did == "bubble_heat":
        sc = snap.get("scores") or {}
        comp = sc.get("composite")
        zone = (sc.get("zone") or {}).get("key")
        v = -((comp if comp is not None else 50.0) - 50.0) / 50.0
        if zone == "alert":
            v -= 0.2
        return _clamp(v, -c, c), f"composite {comp}, zone {zone}"
    if did == "flow_direction":
        md = ((snap.get("l3") or {}).get("marginal_direction") or {}).get("score")
        return _clamp((md or 0) / 100.0, -c, c), f"marginal {md}"
    if did == "retail_inst":
        rvi = (snap.get("l3") or {}).get("retail_vs_inst") or {}
        if rvi.get("warning"):
            v = -0.4
        else:
            v = -_clamp((rvi.get("divergence") or 0) / 50.0, -1, 1)
        return _clamp(v, -c, c), f"divergence {rvi.get('divergence')}, warning {rvi.get('warning')}"
    if did == "payback_health":
        h = snap.get("headline") or {}
        cov = h.get("coverage")
        vk = h.get("verdict_key")
        base = {"monetizing": 0.6, "investing": 0.0, "burning": -0.6}.get(vk, 0.0)
        v = base + ((cov if cov is not None else 0.3) - 0.3) * 1.5
        return _clamp(v, -c, c), f"coverage {cov}, verdict {vk}"
    if did == "pricing_power":
        pp = snap.get("pricing_power") or {}
        sc = pp.get("score")
        vk = pp.get("verdict_key")
        v = ((sc if sc is not None else 50.0) - 50.0) / 50.0
        if vk == "squeezed":
            v -= 0.15
        elif vk == "defensible":
            v += 0.15
        return _clamp(v, -c, c), f"score {sc}, verdict {vk}"
    if did == "compute_demand":
        h = snap.get("headline") or {}
        v0 = h.get("grand_total_end_year_usd_bn")
        base = cfg["compute_baseline_bn"]
        v = _tanh((v0 / base - 1) * cfg["compute_gain_k"]) if (v0 and base) else 0.0
        return _clamp(v, -c, c), f"grand_total {v0} vs {base}"
    if did == "wafer_demand":
        v0 = (snap.get("inference") or {}).get("wafers_per_month")
        base = cfg["wafer_baseline_wpm"]
        v = _tanh((v0 / base - 1) * cfg["wafer_gain_k"]) if (v0 and base) else 0.0
        return _clamp(v, -c, c), f"wpm {v0} vs {base}"
    if did == "rival_pressure":
        n = len(snap.get("events") or [])
        v = -_clamp((n - 12) / 24.0, -0.6, 0.6)
        return _clamp(v, -c, c), f"{n} events"
    if did == "catalyst_density":
        n = snap.get("event_count") or 0
        return _clamp(n / 20.0, 0, 1), f"{n} upcoming earnings"
    if did == "supply_structure":
        n = (snap.get("summary") or {}).get("n_systems") or 0
        return 0.0, f"{n} systems"
    return 0.0, ""


def _extract_one(driver, siblings, cfg, is_context):
    did = driver["id"]
    platform = driver["source_platform"]
    seed = driver.get("seed", {})
    base = {
        "id": did, "name_en": driver["name_en"], "name_zh": driver["name_zh"],
        "source_platform": platform, "weight": driver.get("weight", 1.0),
        "is_context": is_context, "note_en": driver.get("note_en", ""), "note_zh": driver.get("note_zh", ""),
    }
    if siblings is None:  # pure seed / cold path — never read siblings
        v = seed.get("value", 0.0)
        return {**base, "value": v, "raw": "seed", "available": seed.get("available", True),
                "source": "seed", "label_en": f"{v:+.2f} (seed)", "label_zh": f"{v:+.2f} (種子)"}
    snap = siblings.get(platform)
    if snap is None:  # missing platform / import failed / loader returned None
        v = seed.get("value", 0.0)
        return {**base, "value": v, "raw": "unavailable", "available": False,
                "source": "unavailable", "label_en": "unavailable", "label_zh": "無資料"}
    try:
        v, raw = _normalize(did, snap, cfg)
        src = "live" if _is_live(platform, snap) else "seed"
        return {**base, "value": v, "raw": raw, "available": True, "source": src,
                "label_en": f"{v:+.2f} ({src})", "label_zh": f"{v:+.2f} ({'即時' if src == 'live' else '種子'})"}
    except Exception:
        v = seed.get("value", 0.0)
        return {**base, "value": v, "raw": "extract_error", "available": False,
                "source": "unavailable", "label_en": "unavailable", "label_zh": "無資料"}


def _extract_drivers(kb, siblings, cfg):
    drivers = [_extract_one(d, siblings, cfg, False) for d in kb["drivers"]]
    context = [_extract_one(d, siblings, cfg, True) for d in kb.get("context_drivers", [])]
    state = {d["id"]: d["value"] for d in drivers}
    return drivers, context, state


# --------------------------------------------------------------------------- #
# distribution — the deterministic prior + coverage-shrunk softmax (reused for
# the baseline AND every sensitivity perturbation)
# --------------------------------------------------------------------------- #
def _distribution(scen, state, shrink, gain, avail, wmap, unavail_w):
    logits, ev_terms = [], []
    for s in scen:
        aff = s["affinity"]
        terms, ev = [], 0.0
        for did, st in state.items():
            a = aff.get(did, 0.0)
            w = wmap.get(did, 1.0)
            mult = 1.0 if avail.get(did, True) else unavail_w
            term = a * st * w * mult
            ev += term
            terms.append((did, term))
        logits.append(math.log(s["prior"]) + shrink * gain * ev)
        ev_terms.append(terms)
    m = max(logits)
    exps = [math.exp(l - m) for l in logits]
    z = sum(exps) or 1.0
    floats = [e / z for e in exps]
    return floats, ev_terms


def build_snapshot(kb, siblings=None, generated_at="", today="", prior_probs=None):
    cfg = kb["config"]
    scen = kb["scenarios_seed"]
    drivers, context, state = _extract_drivers(kb, siblings, cfg)

    # ── coverage (honesty core) — scoring drivers only ──
    scoring = [d for d in drivers]
    scoring_count = len(scoring)
    live_count = sum(1 for d in scoring if d["source"] == "live")
    seed_count = sum(1 for d in scoring if d["source"] == "seed")
    unavailable = [d["id"] for d in scoring if d["source"] == "unavailable"]
    coverage = round(live_count / scoring_count, 3) if scoring_count else 0.0

    wmap = {d["id"]: d.get("weight", 1.0) for d in kb["drivers"]}
    avail = {d["id"]: (d["source"] != "unavailable") for d in scoring}
    shrink = coverage ** cfg["coverage_power"]
    gain = cfg["evidence_gain"]

    floats, ev_terms = _distribution(scen, state, shrink, gain, avail, wmap, cfg["unavailable_evidence_weight"])
    int_probs = renormalize_to_100([p * 100 for p in floats])
    base_idx = max(range(len(int_probs)), key=lambda i: (int_probs[i], -i))

    # ── probability attribution (proxy: distribute deviation-from-prior across drivers) ──
    prior_norm = renormalize_to_100([s["prior"] for s in scen])
    driver_by_id = {d["id"]: d for d in scoring}
    prior_probs = prior_probs or {}

    scen_out = []
    for si, s in enumerate(scen):
        terms = ev_terms[si]
        # proxy attribution = first-order softmax derivative of THIS scenario's prob
        # wrt each driver's evidence: p*(1-p) * shrink * gain * term  (signed, in points).
        p = floats[si]
        deriv = p * (1 - p)
        ranked = sorted(terms, key=lambda kt: -abs(kt[1]))
        attribution = []
        for did, term in ranked[:4]:
            if abs(term) < 1e-9:
                continue
            d = driver_by_id.get(did, {})
            pts = int(round(100 * deriv * shrink * gain * term))
            up = term > 0
            attribution.append({
                "driver_id": did, "name_en": d.get("name_en", did), "name_zh": d.get("name_zh", did),
                "source": d.get("source", "seed"), "contribution": pts, "direction": "up" if up else "down",
                "reason_en": f"{d.get('name_en', did)} {'supports' if up else 'weighs against'} this scenario ({d.get('raw', '')}).",
                "reason_zh": f"{d.get('name_zh', did)}{'支撐' if up else '不利於'}此情境({d.get('raw', '')})。",
            })
        pprob = prior_probs.get(s["id"])
        scen_out.append({
            "id": s["id"], "name_en": s["name_en"], "name_zh": s["name_zh"], "horizon": s["horizon"],
            "prob": int_probs[si], "prior": s["prior"],
            "prior_prob": pprob if pprob is not None else prior_norm[si],
            "drift": (int_probs[si] - pprob) if pprob is not None else None,
            "attribution": attribution,
            "narrative": {"en": s["narrative_en"], "zh": s["narrative_zh"]},
            "market_path": s["market_path"],
            "triggers": {"en": s["triggers_en"], "zh": s["triggers_zh"]},
            "falsifiers": {"en": s["falsifiers_en"], "zh": s["falsifiers_zh"]},
            "foundry_read": {"en": s["foundry_read_en"], "zh": s["foundry_read_zh"]},
        })

    # ── divergence / coherence engine (signature signal) ──
    st = state
    divergences = []

    def push(key, sev, en, zh, plats):
        divergences.append({"key": key, "severity": sev, "en": en, "zh": zh, "platforms": plats})

    if st.get("compute_demand", 0) >= 0 and st.get("payback_health", 0) < 0 \
            and st.get("bubble_heat", 0) < -0.2 and st.get("flow_direction", 0) < 0:
        push("late_cycle_topping", "high",
             "Classic late-cycle top: AI demand still strong while payback burns, the bubble runs hot, and capital is draining from risk.",
             "典型後段見頂:AI 需求仍強,但回本在燒、泡沫熱、資金正撤離風險。",
             ["compute", "payback", "aibubble", "flows"])
    if st.get("compute_demand", 0) > 0 and st.get("payback_health", 0) < 0:
        push("demand_vs_payback_gap", "medium",
             "Demand is rising but payback is deteriorating — spend is outrunning monetization.",
             "需求上升但回本惡化 — 投入跑在變現前面。", ["compute", "payback"])
    if st.get("compute_demand", 0) > 0 and st.get("pricing_power", 0) < -0.3:
        push("demand_vs_pricing", "medium",
             "Volume is up while pricing power is squeezed — growth without margin.",
             "量增但議價能力受擠壓 — 有量無利。", ["compute", "pricing"])
    if st.get("macro_cycle", 0) < 0 and st.get("flow_direction", 0) > 0.3:
        push("macro_vs_market", "medium",
             "The economy is weakening but capital keeps flowing in on liquidity — a fragile prop.",
             "經濟轉弱、資金卻靠流動性續流入 — 脆弱的支撐。", ["econ", "flows"])
    if st.get("retail_inst", 0) <= -0.39:
        push("retail_euphoria", "low",
             "Retail appetite runs hot while institutions de-risk — a late-stage tell.",
             "散戶情緒偏熱、機構降風險 — 晚期訊號。", ["flows"])

    # coherence: how much the available drivers agree on direction
    sig = [(1 if v > 0.05 else -1 if v < -0.05 else 0) for did, v in st.items() if avail.get(did, True)]
    sig = [s for s in sig if s != 0]
    coherence_score = int(round(100 * abs(sum(sig) / len(sig)))) if sig else 50

    # ── reflexive flows reconcile (cross-check, NOT weighted in) ──
    flows_snap = (siblings or {}).get("flows") if siblings else None
    flows_scenarios = []
    flows_note_en = "flows' own distribution — same-source cross-check, not weighted in."
    flows_note_zh = "flows 自身分布 — 同源對賬,未計入加權。"
    if flows_snap:
        fl = ((flows_snap.get("l5") or {}).get("scenarios")) or []
        for i, item in enumerate(fl[:len(FLOWS_SEED_ORDER)]):
            stable = FLOWS_SEED_ORDER[i]
            mapped = FLOWS_TO_OURS.get(stable)
            flows_scenarios.append({
                "mapped_id": mapped, "name_en": item.get("name_en", stable),
                "name_zh": item.get("name_zh", stable), "prob": item.get("prob"),
            })
        # reflexive split: our base case vs flows' top scenario
        if flows_scenarios:
            ftop = max(flows_scenarios, key=lambda x: (x.get("prob") or 0))
            if ftop.get("mapped_id") and ftop["mapped_id"] != scen_out[base_idx]["id"]:
                push("reflexive_split", "low",
                     f"Our base case ({scen_out[base_idx]['name_en']}) differs from the Capital Flow Radar's own top read ({ftop['name_en']}).",
                     f"本平台 base case({scen_out[base_idx]['name_zh']})與資金流向雷達自身首選({ftop['name_zh']})不一致。",
                     ["flows"])
    else:
        flows_note_en += " (flows unavailable)"
        flows_note_zh += "(flows 無資料)"

    # ── confidence (M18) ──
    # Confidence reflects HOW MUCH EVIDENCE backs the *precise probabilities* — i.e.
    # coverage (how many drivers are live). It is deliberately DECOUPLED from coherence:
    # a strong cross-platform divergence drives coherence DOWN, but a divergence is a
    # high-VALUE signal, not a reason to distrust the read. Folding low coherence into
    # confidence used to punish exactly the signal this card exists to surface. So
    # divergence strength is reported on its OWN axis (signal_strength) and coherence is
    # only allowed to NUDGE confidence at the high end (where high coherence corroborates).
    if coverage >= 0.7:
        confidence = "high" if coherence_score >= 60 else "medium"
    elif coverage >= 0.5:
        confidence = "medium"
    else:
        confidence = "low"
    # signal strength = how loud the divergence engine is (separate from confidence).
    _sev_w = {"high": 3, "medium": 2, "low": 1}
    _sig = sum(_sev_w.get(d["severity"], 0) for d in divergences)
    signal_strength = "strong" if _sig >= 3 else "moderate" if _sig >= 1 else "quiet"

    # ── sensitivity (first-order, perturb the PRE-softmax state vector) ──
    base_prob = int_probs[base_idx]
    delta = cfg["sensitivity_delta"]

    def _base_prob_with(state2):
        fl2, _ = _distribution(scen, state2, shrink, gain, avail, wmap, cfg["unavailable_evidence_weight"])
        ip2 = renormalize_to_100([p * 100 for p in fl2])
        return ip2[base_idx]

    sens = []
    for d in scoring:
        did = d["id"]
        up_state = dict(state); up_state[did] = _clamp(state[did] + delta, -1, 1)
        dn_state = dict(state); dn_state[did] = _clamp(state[did] - delta, -1, 1)
        up_eff = _base_prob_with(up_state) - base_prob
        dn_eff = _base_prob_with(dn_state) - base_prob
        sens.append({
            "driver_id": did, "name_en": d["name_en"], "name_zh": d["name_zh"],
            "up_effect": up_eff, "down_effect": dn_eff,
            "mag": max(abs(up_eff), abs(dn_eff)),
        })
    sens.sort(key=lambda x: -x["mag"])
    sensitivity_raw = sens[:5]

    # ── headline ──
    # The signature high-severity late_cycle_topping divergence fires ONLY when capital is
    # actually draining (flow_direction < 0). Track it so the headline narrative/regime
    # never assert "capital is draining" when the engine did not detect it (H10).
    signature_active = any(
        d["key"] == "late_cycle_topping" and d["severity"] == "high" for d in divergences)
    base_s = scen_out[base_idx]
    regime_key, base_narrative = _headline_regime_narrative(base_s, signature_active)
    headline = {
        "base_id": base_s["id"], "base_prob": base_s["prob"],
        "base_label": {"en": base_s["name_en"], "zh": base_s["name_zh"]},
        "base_narrative": base_narrative,
        "regime_key": regime_key,
        "signature_active": signature_active,
        "signal_strength": signal_strength,
        "divergence_count": len(divergences), "coherence_score": coherence_score,
        "coverage": coverage, "coverage_label": f"{live_count}/{scoring_count} live",
        "confidence": confidence,
    }

    l3 = {
        "drivers": drivers, "context_drivers": context, "state_vector": state,
        "coverage": {"live_count": live_count, "seed_count": seed_count,
                     "unavailable": unavailable, "coverage": coverage,
                     "scoring_count": scoring_count},
        "coherence_score": coherence_score,
        "scenarios": scen_out,
        "divergences": divergences,
        "cross_checks": {"flows_scenarios": flows_scenarios,
                         "note_en": flows_note_en, "note_zh": flows_note_zh},
        "sensitivity_raw": sensitivity_raw,
        "headline": headline,
        "engine_inputs": {"shrink": shrink, "gain": gain, "base_idx": base_idx},
    }

    # ── L4 / L5 synthesis (Claude or rules) ──
    analysis_out = analysis.analyze(kb, l3)

    # reconcile headline base_* with the FINAL l4 distribution (rules == L3 baseline;
    # Claude may have recalibrated → argmax can shift). headline must match l4.
    final_scen = analysis_out["l4"]["scenarios"]
    if final_scen:
        fb_idx = max(range(len(final_scen)), key=lambda i: (final_scen[i]["prob"], -i))
        fb = final_scen[fb_idx]
        sd = {s["id"]: s for s in scen}
        m = sd.get(fb["id"], {})
        headline["base_id"] = fb["id"]
        headline["base_prob"] = fb["prob"]
        headline["base_label"] = {"en": m.get("name_en", fb["id"]), "zh": m.get("name_zh", fb["id"])}
        # re-resolve regime + narrative against the FINAL base case + the actual signature
        # state, so the headline never claims a topping/draining thesis the engine didn't see.
        fb_meta = {"id": fb["id"],
                   "narrative": {"en": m.get("narrative_en", ""), "zh": m.get("narrative_zh", "")}}
        rk, narr = _headline_regime_narrative(fb_meta, signature_active)
        headline["regime_key"] = rk
        headline["base_narrative"] = narr

    is_demo = siblings is None
    source = "live" if live_count > 0 else "seed"

    return {
        "generated_at": generated_at,
        "as_of": today or kb.get("as_of_curated", ""),
        "source": source,
        "is_demo": is_demo,
        "title_en": kb.get("title_en", "Scenario Radar"),
        "title_zh": kb.get("title_zh", "情境雷達"),
        "method_en": kb.get("method_en", ""),
        "method_zh": kb.get("method_zh", ""),
        "disclaimer_en": kb.get("disclaimer_en", ""),
        "disclaimer_zh": kb.get("disclaimer_zh", ""),
        "headline": headline,
        "l3": l3,
        "l4": analysis_out["l4"],
        "l5": analysis_out["l5"],
        "analysis_engine": analysis_out["engine"],
        "prior_scenarios": [
            {"id": s["id"], "prob": (prior_probs.get(s["id"], prior_norm[i]) if prior_probs else prior_norm[i])}
            for i, s in enumerate(scen)
        ],
        "blind_spots_en": kb.get("blind_spots_en", []),
        "blind_spots_zh": kb.get("blind_spots_zh", []),
        "news": [],
        "fetched_at": None,
    }
