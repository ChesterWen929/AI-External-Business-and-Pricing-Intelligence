"""Positioning & Sentiment Radar tests — pytest, fully offline (seed/engine only,
no network, no Claude).

Run:  cd macro-ai-monitor && python3 -m pytest positioning_radar/ -q
"""
import json
from pathlib import Path

import pytest

from positioning_radar import _compute, _kb, model

PKG = Path(__file__).resolve().parent
SNAPSHOT_FILE = PKG.parent / "data" / "positioning" / "snapshot.json"


@pytest.fixture(scope="module")
def kb():
    return _kb()


@pytest.fixture(scope="module")
def snap():
    # seed-based compute; force rules engine regardless of environment
    import os
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        return _compute()
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


# ── KB integrity ──────────────────────────────────────────────────────────────
def test_player_map_five_layers_unwind_ranks(kb):
    ranks = sorted(p["unwind_rank"] for p in kb["player_map"])
    assert ranks == [1, 2, 3, 4, 5]
    for p in kb["player_map"]:
        assert p["name_en"] and p["name_zh"]
        assert p["holds_en"] and p["holds_zh"]
        assert p["unwind_en"] and p["unwind_zh"], f"{p['id']} must declare its unwind behavior"
        assert p["watch_en"] and p["watch_zh"]


def test_kb_bilingual_parity(kb):
    for d in kb["indicators_dict"]:
        for base in ("name", "what", "why", "threshold"):
            assert d[f"{base}_en"] and d[f"{base}_zh"], f"{d['id']} {base}"
    for i in kb["indicators_l3"]:
        assert i["name_en"] and i["name_zh"] and i["unit_en"] and i["unit_zh"]
        assert i["note_en"] and i["note_zh"], f"{i['id']} needs source notes"
    for s in kb["scenarios_seed"]:
        assert s["name_en"] and s["name_zh"] and s["trigger_en"] and s["trigger_zh"]
    assert len(kb["blind_spots_en"]) == len(kb["blind_spots_zh"])


def test_l3_indicators_tiered_dated_and_banded(kb):
    for i in kb["indicators_l3"]:
        assert i["tier"] in ("T1", "T2", "T3"), i["id"]
        assert i["seed"].get("as_of"), f"{i['id']} seed needs as_of"
        assert i["seed"].get("value") is not None, i["id"]
        bands = i["bands"]
        assert len(bands) >= 3, i["id"]
        vals = [b[0] for b in bands]
        pcts = [b[1] for b in bands]
        assert vals == sorted(vals), f"{i['id']} band values must ascend"
        assert pcts == sorted(pcts), f"{i['id']} band percentiles must be non-decreasing"
        assert 0 <= pcts[0] and pcts[-1] <= 100, i["id"]


def test_proxies_and_estimates_flagged(kb):
    by = {i["id"]: i for i in kb["indicators_l3"]}
    for pid in ("vix_term", "qqq_spy_vol", "soxx_rel_vol"):
        assert by[pid]["proxy"] is True, f"{pid} must be flagged PROXY"
    # margin debt: official (T1) but its note must declare the 2-month lag
    md = by["margin_debt"]
    assert md["tier"] == "T1"
    assert "lag" in md["note_en"].lower() and "兩個月" in md["note_zh"]
    assert md["seed"]["as_of"] == "2026-04-30"  # honestly stale


def test_weights_sum_to_one_and_cover_indicators(kb):
    assert sum(kb["weights"].values()) == pytest.approx(1.0)
    ids = {i["id"] for i in kb["indicators_l3"]}
    assert set(kb["weights"]) == ids


# ── canonical / cross-card alignment ─────────────────────────────────────────
def test_credit_decompression_pairing_aligned(kb):
    cd = kb["cross_card"]["credit_decompression"]
    assert cd["value"] is True  # /credit 2026-07-02: decompression flag ON
    assert "/credit" in cd["align_note"] and "對齊" in cd["align_note"]
    assert "41.6" in cd["align_note"]  # cite the canonical composite, don't re-derive


def test_aibubble_context_aligned_not_scored(kb):
    ab = kb["cross_card"]["aibubble_market"]
    assert ab["value"] == 64.6  # canonical /aibubble 2026-07-02 market score
    assert "對齊" in ab["align_note"]
    assert "aibubble_market" not in kb["weights"]  # context only, never scored


# ── L3 engine math ────────────────────────────────────────────────────────────
def test_pct_from_bands_anchors_interpolation_and_clamps():
    bands = [[0, 10], [10, 50], [20, 90]]
    assert model.pct_from_bands(0, bands) == 10.0
    assert model.pct_from_bands(10, bands) == 50.0
    assert model.pct_from_bands(5, bands) == 30.0     # midpoint interpolation
    assert model.pct_from_bands(15, bands) == 70.0
    assert model.pct_from_bands(-99, bands) == 10.0   # clamp low
    assert model.pct_from_bands(99, bands) == 90.0    # clamp high
    assert model.pct_from_bands(None, bands) is None


def test_invert_logic_for_low_is_crowded(snap):
    by = {r["id"]: r for r in snap["l3"]["indicators"]}
    pc = by["put_call"]
    assert pc["invert"] is True
    assert pc["crowd"] == pytest.approx(100.0 - pc["pct3y"], abs=0.05)
    cot = by["cot_nq"]
    assert cot["invert"] is False
    assert cot["crowd"] == pytest.approx(cot["pct3y"], abs=0.05)


def test_seed_percentiles_from_curated_bands(snap):
    by = {r["id"]: r for r in snap["l3"]["indicators"]}
    # NAAIM 92.5 on [[90,88],[105,99]] → 88 + 2.5/15*11 = 89.8
    assert by["naaim"]["pct3y"] == pytest.approx(89.8, abs=0.05)
    # margin debt +18% YoY on [[10,55],[20,80]] → 75.0
    assert by["margin_debt"]["pct3y"] == pytest.approx(75.0, abs=0.05)
    # AAII +8.4pp on [[0,35],[10,60]] → 56.0 (the lukewarm-retail tell)
    assert by["aaii"]["pct3y"] == pytest.approx(56.0, abs=0.05)
    for r in snap["l3"]["indicators"]:
        assert r["pct_source"] == "curated_band"  # seed view: no live percentiles


def test_composite_is_weighted_crowd_mean(snap, kb):
    rows = snap["l3"]["indicators"]
    w = kb["weights"]
    expect = sum(w[r["id"]] * r["crowd"] for r in rows)  # weights sum to 1
    assert snap["composite"]["score"] == pytest.approx(expect, abs=0.06)
    assert 0 <= snap["composite"]["score"] <= 100


def test_seed_verdict_crowded_not_one_sided(snap):
    comp = snap["composite"]
    assert comp["verdict"] == "CROWDED"  # seed world: high-end crowded, not yet one-sided
    assert 40 <= comp["score"] < 75
    assert comp["verdict_en"] and comp["verdict_zh"]


def test_verdict_thresholds():
    th = {"uncrowded_max": 40.0, "crowded_max": 75.0}
    assert model.verdict_for(0, th) == "UNCROWDED"
    assert model.verdict_for(39.9, th) == "UNCROWDED"
    assert model.verdict_for(40.0, th) == "CROWDED"
    assert model.verdict_for(74.9, th) == "CROWDED"
    assert model.verdict_for(75.0, th) == "ONE-SIDED"
    assert model.verdict_for(100, th) == "ONE-SIDED"


# ── nuance: crowded-and-rising vs crowded-and-cracking ───────────────────────
def test_seed_nuance_crowded_rising_one_crack(snap):
    nu = snap["nuance"]
    assert nu["state"] == "crowded_rising"
    assert nu["cracks_on"] == 1  # only the /credit decompression tell is on
    tells = {t["id"]: t for t in nu["crack_tells"]}
    assert tells["credit_decompression"]["on"] is True
    assert "對齊 /credit" in tells["credit_decompression"]["align_note"]
    assert tells["vix_inversion"]["on"] is False   # 0.913 < 0.95
    assert tells["put_call_bid"]["on"] is False    # 0.58 < 0.70
    assert nu["rising"] is True and nu["rising_count"] == 3  # COT/NAAIM/margin all building


def test_nuance_flips_to_cracking_with_two_tells(kb):
    live = {"indicators": {
        "vix_term": {"value": 0.98, "chg": 0.06, "as_of": "2026-07-02", "live": True},
    }}
    snap = model.build_snapshot(kb, live=live, generated_at="t", today="2026-07-02")
    nu = snap["nuance"]
    assert nu["cracks_on"] == 2  # credit decompression + VIX ≥ 0.95
    assert nu["state"] == "crowded_cracking"


def test_nuance_not_crowded_below_threshold(kb):
    # push every indicator to its uncrowded extreme via the live channel
    lows = {"cot_nq": -35, "aaii": -35, "naaim": 5, "margin_debt": -25,
            "put_call": 1.2, "vix_term": 1.25, "qqq_spy_vol": 0.3, "soxx_rel_vol": 0.5}
    live = {"indicators": {k: {"value": v, "chg": -1.0, "as_of": "2026-07-02", "live": True}
                           for k, v in lows.items()}}
    snap = model.build_snapshot(kb, live=live, generated_at="t", today="2026-07-02")
    assert snap["composite"]["verdict"] == "UNCROWDED"
    assert snap["nuance"]["state"] == "not_crowded"


# ── live-merge fallback behavior ─────────────────────────────────────────────
def test_live_merge_wins_and_seed_fills_gaps(kb):
    live = {"indicators": {"cot_nq": {"value": 51.0, "chg": 9.0, "as_of": "2026-06-30",
                                      "pct3y": 93.0, "live": True}}}
    snap = model.build_snapshot(kb, live=live, generated_at="t", today="2026-07-02")
    by = {r["id"]: r for r in snap["l3"]["indicators"]}
    assert by["cot_nq"]["live"] is True and by["cot_nq"]["value"] == 51.0
    assert by["cot_nq"]["pct3y"] == 93.0 and by["cot_nq"]["pct_source"] == "live_3y"
    assert by["naaim"]["live"] is False           # seed fallback keeps rendering
    assert by["naaim"]["value"] == 92.5
    assert by["margin_debt"]["as_of"] == "2026-04-30"
    assert snap["source"] == "live" and snap["is_demo"] is False
    assert snap["l3"]["live_count"] == 1


# ── L4/L5 rules fallback ─────────────────────────────────────────────────────
def test_rules_engine_contract(snap):
    assert snap["analysis_engine"] == "rules"  # no API key in CI
    l4, l5 = snap["l4"], snap["l5"]
    for k in ("positioning_read", "unwind_queue", "tsmc_view", "thesis"):
        assert k in l4
    assert l4["positioning_read"]["summary_en"] and l4["positioning_read"]["summary_zh"]
    assert l4["tsmc_view"]["summary_en"] and l4["tsmc_view"]["summary_zh"]
    assert l4["thesis"]["confidence"] in ("high", "medium", "low")
    assert 3 <= len(l4["thesis"]["leading_signals"]) <= 6
    for sig in l4["thesis"]["leading_signals"]:
        assert sig["en"] and sig["zh"]
    assert len(l5["falsification"]) >= 2
    assert len(l5["early_warning"]) >= 5
    for e in l5["early_warning"]:
        assert e["freq"]


def test_unwind_queue_ordered_leverage_first_passive_last(snap):
    q = snap["l4"]["unwind_queue"]
    assert [x["rank"] for x in q] == [1, 2, 3, 4, 5]
    assert "Leveraged" in q[0]["player_en"]
    assert "Passive" in q[-1]["player_en"]
    for x in q:
        assert x["trigger_en"] and x["trigger_zh"]


def test_scenario_probs_sum_to_100(snap):
    probs = [sc["prob"] for sc in snap["l5"]["scenarios"]]
    assert sum(probs) == 100
    for sc in snap["l5"]["scenarios"]:
        assert sc["name_en"] and sc["name_zh"] and sc["trigger_en"] and sc["trigger_zh"]


# ── snapshot shape + committed seed ──────────────────────────────────────────
def test_snapshot_shape(snap):
    for k in ("l3", "l4", "l5", "composite", "nuance", "player_map",
              "indicator_dictionary", "cross_card", "analysis_engine", "as_of",
              "title_en", "title_zh", "blind_spots_en", "blind_spots_zh",
              "weights", "verdict_thresholds", "tier_legend"):
        assert k in snap, k
    assert snap["source"] == "seed"
    assert snap["is_demo"] is True
    assert snap["title_zh"] == "部位與情緒雷達"
    assert len(snap["player_map"]) == 5
    assert len(snap["l3"]["indicators"]) == 8


def test_committed_seed_snapshot_matches_engine(snap):
    # Platform practice: the committed snapshot may be pure seed or a live
    # Claude-refreshed one (flows/pricing/payback commit live too). Seed ->
    # strict determinism vs fresh compute; live -> structural bounds only.
    assert SNAPSHOT_FILE.exists(), "committed seed snapshot missing"
    with open(SNAPSHOT_FILE, encoding="utf-8") as f:
        seed = json.load(f)
    assert seed["analysis_engine"] in ("rules", "claude")
    if seed.get("source") == "seed":
        assert seed["composite"]["score"] == snap["composite"]["score"]
        assert seed["composite"]["verdict"] == snap["composite"]["verdict"]
        assert seed["nuance"]["state"] == snap["nuance"]["state"]
    else:
        assert 0 <= seed["composite"]["score"] <= 100
        assert seed["composite"]["verdict"] in ("UNCROWDED", "CROWDED", "ONE-SIDED")
        assert seed["nuance"]["state"] in ("not_crowded", "crowded_rising",
                                           "crowded_stalling", "crowded_cracking")
    # every indicator row still carries tier + as_of + a percentile source
    for r in seed["l3"]["indicators"]:
        assert r["tier"] in ("T1", "T2", "T3")
        assert r["as_of"] and r["pct_source"] in ("live_3y", "curated_band")
