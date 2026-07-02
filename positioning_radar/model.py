"""Positioning & Sentiment Radar — L3 quant engine + snapshot assembler.

build_snapshot(kb, live, ...) merges live readings over the KB seeds and computes:
  • per-indicator 3y percentile  — TRUE percentile when the live layer provides
                                   one (COT computes it from fetched history);
                                   otherwise interpolated on the curated band
                                   anchors in the KB (disclosed as an estimate)
  • crowdedness contribution     — percentile, inverted for indicators where a
                                   LOW value means crowded (put/call, VIX term)
  • composite crowdedness score  — 0 (uncrowded) … 100 (one-sided), weights
                                   hard-coded in the KB and disclosed in the
                                   methodology panel
  • the nuance state             — crowded-and-rising vs crowded-and-cracking,
                                   pairing internal crack tells with the /credit
                                   card's decompression flag (對齊, not re-derived)
Then calls analysis.analyze() for L4/L5 (Claude, or rules fallback).
"""
from __future__ import annotations

from . import analysis

VERDICTS = {
    "UNCROWDED": {"en": "UNCROWDED — positioning has room; drawdowns should be shallow",
                  "zh": "UNCROWDED — 部位仍有空間;回檔應屬淺層"},
    "CROWDED":   {"en": "CROWDED — the marginal buyer is mostly in; drawdown violence is pre-loaded",
                  "zh": "CROWDED — 邊際買家大多已進場;回檔的猛烈度已預先裝填"},
    "ONE-SIDED": {"en": "ONE-SIDED — everyone is on the same side of the boat; the exit is narrow",
                  "zh": "ONE-SIDED — 所有人站在船的同一邊;出口很窄"},
}

STATES = {
    "not_crowded": {
        "en": "Not crowded", "zh": "未達擁擠",
        "desc_en": "Composite below the crowded threshold — positioning is not the story yet.",
        "desc_zh": "綜合分數未達擁擠門檻——部位還不是主線劇情。",
    },
    "crowded_rising": {
        "en": "Crowded & still rising", "zh": "擁擠且仍在加碼",
        "desc_en": "Crowding is still building (positioning trends up) and fewer than the required crack tells are on — momentum intact, but every new buyer moves the future seller queue up one notch.",
        "desc_zh": "擁擠仍在累積(部位趨勢向上),裂紋訊號未達門檻——動能還在,但每多一個新買家,未來的賣壓隊列就往前排一格。",
    },
    "crowded_stalling": {
        "en": "Crowded & stalling", "zh": "擁擠但停止加碼",
        "desc_en": "Crowded, no longer building, cracks not confirmed — the most ambiguous state; watch the crack tells daily.",
        "desc_zh": "擁擠、不再加碼、裂紋未確認——最曖昧的狀態;裂紋訊號要每天盯。",
    },
    "crowded_cracking": {
        "en": "Crowded & cracking", "zh": "擁擠且開始龜裂",
        "desc_en": "Crowded positioning WITH crack tells confirmed — the combination that turns an ordinary pullback into a forced-seller cascade.",
        "desc_zh": "擁擠部位加上裂紋訊號確認——這個組合會把普通回檔變成強制賣壓的連鎖。",
    },
}


def _clamp01(x):
    return max(0.0, min(1.0, x))


def verdict_for(score, thresholds):
    if score < thresholds.get("uncrowded_max", 40.0):
        return "UNCROWDED"
    if score < thresholds.get("crowded_max", 75.0):
        return "CROWDED"
    return "ONE-SIDED"


def pct_from_bands(value, bands):
    """Linear interpolation of a 3y-percentile estimate over curated band
    anchors [[value, pct], ...] sorted ascending by value. Clamped to the
    endpoint percentiles. Disclosed everywhere as an ESTIMATE."""
    if value is None or not bands:
        return None
    if value <= bands[0][0]:
        return round(float(bands[0][1]), 1)
    if value >= bands[-1][0]:
        return round(float(bands[-1][1]), 1)
    for (v0, p0), (v1, p1) in zip(bands, bands[1:]):
        if v0 <= value <= v1:
            if v1 == v0:
                return round(float(p1), 1)
            return round(p0 + (value - v0) / (v1 - v0) * (p1 - p0), 1)
    return None


# --------------------------------------------------------------------------- #
# L3 — merge live over seeds, percentile + crowd contribution per indicator
# --------------------------------------------------------------------------- #
def _merge_indicators(kb, live):
    live_ind = (live or {}).get("indicators", {})
    weights = kb.get("weights", {})
    rows = []
    for ind in kb.get("indicators_l3", []):
        lv = live_ind.get(ind["id"]) or {}
        is_live = lv.get("value") is not None
        value = lv["value"] if is_live else ind["seed"]["value"]
        chg = lv.get("chg") if is_live else ind["seed"].get("chg")
        as_of = lv.get("as_of") if is_live else ind["seed"].get("as_of", "")
        if is_live and lv.get("pct3y") is not None:
            pct, pct_source = round(float(lv["pct3y"]), 1), "live_3y"
        else:
            pct, pct_source = pct_from_bands(value, ind.get("bands", [])), "curated_band"
        crowd = None
        if pct is not None:
            crowd = round(100.0 - pct, 1) if ind.get("invert") else round(pct, 1)
        rows.append({
            "id": ind["id"], "name_en": ind["name_en"], "name_zh": ind["name_zh"],
            "unit_en": ind["unit_en"], "unit_zh": ind["unit_zh"],
            "value": value, "chg": chg, "chg_window": ind.get("chg_window", ""),
            "as_of": as_of or "", "live": bool(is_live),
            "tier": ind.get("tier", "T3"), "est": bool(ind.get("est")),
            "proxy": bool(ind.get("proxy")), "invert": bool(ind.get("invert")),
            "pct3y": pct, "pct_source": pct_source, "crowd": crowd,
            "weight": weights.get(ind["id"], 0.0),
            "level_usd_bn": (ind["seed"].get("level_usd_bn") if not is_live else lv.get("level_usd_bn")) or ind["seed"].get("level_usd_bn"),
            "note_en": ind.get("note_en", ""), "note_zh": ind.get("note_zh", ""),
        })
    return rows


# --------------------------------------------------------------------------- #
# Composite crowdedness score
# --------------------------------------------------------------------------- #
def compute_composite(kb, rows):
    weights = kb.get("weights", {})
    total_w = sum(weights.get(r["id"], 0.0) for r in rows if r["crowd"] is not None)
    score = 0.0
    if total_w:
        score = sum(weights.get(r["id"], 0.0) * r["crowd"]
                    for r in rows if r["crowd"] is not None) / total_w
    score = round(score, 1)
    verdict = verdict_for(score, kb.get("verdict_thresholds", {}))
    return {
        "score": score,
        "verdict": verdict,
        "verdict_en": VERDICTS[verdict]["en"],
        "verdict_zh": VERDICTS[verdict]["zh"],
    }


# --------------------------------------------------------------------------- #
# Nuance — crowded-and-rising vs crowded-and-cracking (pairs with /credit)
# --------------------------------------------------------------------------- #
def compute_nuance(kb, rows, score, thresholds):
    by = {r["id"]: r for r in rows}
    rules = kb.get("nuance_rules", {})
    cd = kb.get("cross_card", {}).get("credit_decompression", {})
    vix = by.get("vix_term", {})
    pc = by.get("put_call", {})
    vix_thr = rules.get("vix_ratio_crack", 0.95)
    pc_thr = rules.get("put_call_crack", 0.70)

    vix_on = vix.get("value") is not None and vix["value"] >= vix_thr
    pc_on = (pc.get("value") is not None and pc["value"] >= pc_thr
             and (pc.get("chg") or 0) > 0)
    tells = [
        {"id": "credit_decompression", "on": bool(cd.get("value")),
         "label_en": "Credit decompression (/credit)", "label_zh": "信用解壓縮(/credit)",
         "detail_en": f"CCC−IG decompression flag {'ON' if cd.get('value') else 'OFF'} on the /credit card — the softest funding layer repricing while positioning is full.",
         "detail_zh": f"/credit 卡的 CCC−IG 解壓縮旗標 {'ON' if cd.get('value') else 'OFF'}——最軟的融資層在部位滿載時開始重新定價。",
         "align_note": cd.get("align_note", "")},
        {"id": "vix_inversion", "on": vix_on,
         "label_en": f"VIX term ≥ {vix_thr}", "label_zh": f"VIX 期限結構 ≥ {vix_thr}",
         "detail_en": f"VIX÷VIX3M at {vix.get('value')} vs the {vix_thr} trigger line for vol-target de-leveraging.",
         "detail_zh": f"VIX÷VIX3M 目前 {vix.get('value')},vol-target 去槓桿的引線在 {vix_thr}。",
         "align_note": ""},
        {"id": "put_call_bid", "on": pc_on,
         "label_en": f"Put/call ≥ {pc_thr} & rising", "label_zh": f"Put/call ≥ {pc_thr} 且上升",
         "detail_en": f"Equity put/call 10d at {pc.get('value')} ({'+' if (pc.get('chg') or 0) > 0 else ''}{pc.get('chg')}/4w) — a hedging bid returning through {pc_thr} while indices hold is the crack tell.",
         "detail_zh": f"股票 put/call 10 日 {pc.get('value')}(4 週 {'+' if (pc.get('chg') or 0) > 0 else ''}{pc.get('chg')})——指數未跌而比值升破 {pc_thr},就是裂紋訊號。",
         "align_note": ""},
    ]
    cracks = sum(1 for t in tells if t["on"])

    rising_ids = rules.get("rising_ids", [])
    rising_count = sum(1 for i in rising_ids if (by.get(i, {}).get("chg") or 0) > 0)
    rising = rising_count >= rules.get("rising_min", 2)

    if score < thresholds.get("uncrowded_max", 40.0):
        state = "not_crowded"
    elif cracks >= rules.get("cracks_min", 2):
        state = "crowded_cracking"
    elif rising:
        state = "crowded_rising"
    else:
        state = "crowded_stalling"

    return {
        "state": state,
        "state_en": STATES[state]["en"], "state_zh": STATES[state]["zh"],
        "desc_en": STATES[state]["desc_en"], "desc_zh": STATES[state]["desc_zh"],
        "crack_tells": tells,
        "cracks_on": cracks, "cracks_min": rules.get("cracks_min", 2),
        "rising": rising, "rising_count": rising_count, "rising_total": len(rising_ids),
    }


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
def build_snapshot(kb, live=None, generated_at="", today=""):
    rows = _merge_indicators(kb, live)
    composite = compute_composite(kb, rows)
    nuance = compute_nuance(kb, rows, composite["score"], kb.get("verdict_thresholds", {}))

    l3 = {
        "indicators": rows,
        "live_count": sum(1 for r in rows if r.get("live")),
    }

    analysis_out = analysis.analyze(kb, l3, composite, nuance)

    return {
        "generated_at": generated_at,
        "as_of": today or kb.get("as_of_curated", ""),
        "source": "live" if live else "seed",
        "is_demo": live is None,
        "title_en": kb.get("title_en", "Positioning & Sentiment Radar"),
        "title_zh": kb.get("title_zh", "部位與情緒雷達"),
        "method_en": kb.get("method_en", ""),
        "method_zh": kb.get("method_zh", ""),
        "tier_legend": kb.get("tier_legend", {}),
        "l3": l3,
        "composite": composite,
        "nuance": nuance,
        "l4": analysis_out["l4"],
        "l5": analysis_out["l5"],
        "analysis_engine": analysis_out["engine"],
        "player_map": kb.get("player_map", []),
        "indicator_dictionary": kb.get("indicators_dict", []),
        "cross_card": kb.get("cross_card", {}),
        "weights": kb.get("weights", {}),
        "verdict_thresholds": kb.get("verdict_thresholds", {}),
        "blind_spots_en": kb.get("blind_spots_en", []),
        "blind_spots_zh": kb.get("blind_spots_zh", []),
        "news": (live or {}).get("news", []) if live else [],
        "fetched_at": (live or {}).get("fetched_at") if live else None,
    }
