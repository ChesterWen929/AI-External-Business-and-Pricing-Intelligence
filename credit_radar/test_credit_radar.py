"""AI Credit & Financing Radar tests — pytest, fully offline (seed/engine only,
no network, no Claude).

Run:  cd macro-ai-monitor && python3 -m pytest credit_radar/ -q
"""
import json
from pathlib import Path

import pytest

from credit_radar import _compute, _kb, model

PKG = Path(__file__).resolve().parent
SNAPSHOT_FILE = PKG.parent / "data" / "credit" / "snapshot.json"

# Canonical shared numbers (audit rule: cite, don't re-derive) — /payback live
# yfinance 2026-07-02 for capex; /payback KB v2 for the labs; FRED 2026-07-01 HY.
CANON_CAPEX = {"msft": 97.2, "googl": 109.9, "amzn": 151.0, "meta": 75.7, "orcl": 55.7}
CANON_HY_OAS = 2.66


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
def test_funding_stack_five_layers_ordered(kb):
    ranks = [l["rank"] for l in kb["funding_stack"]]
    assert ranks == [1, 2, 3, 4, 5]
    for l in kb["funding_stack"]:
        assert l["name_en"] and l["name_zh"] and l["desc_en"] and l["desc_zh"]
        assert l["hardness_en"] and l["hardness_zh"]


def test_kb_bilingual_parity(kb):
    for c in kb["contagion_channels"]:
        assert c["desc_en"] and c["desc_zh"]
    for d in kb["indicators"]:
        for base in ("name", "what", "why", "threshold"):
            assert d[f"{base}_en"] and d[f"{base}_zh"], f"{d['id']} {base}"
    for s in kb["scenarios_seed"]:
        assert s["name_en"] and s["name_zh"] and s["trigger_en"] and s["trigger_zh"]
    assert len(kb["blind_spots_en"]) == len(kb["blind_spots_zh"])
    for r in kb["tsmc_view"]:
        assert r["counterparty_en"] and r["counterparty_zh"] and r["channel_en"] and r["channel_zh"]
        assert r["wobble"] in ("high", "medium", "low")


def test_ledger_rows_tiered_and_flagged(kb):
    for e in kb["ledger"]:
        assert e["tier"] in ("T1", "T2", "T3"), e["id"]
        assert e.get("est") is True, f"{e['id']} must be flagged EST"
        assert e.get("as_of"), e["id"]
        assert e["size_committed_usd_bn"] > 0 and e["size_drawn_usd_bn"] > 0
        assert e["size_drawn_usd_bn"] <= e["size_committed_usd_bn"], e["id"]
        assert e["instrument"] in kb["instruments"], e["id"]
        assert e["cost_hint_en"] and e["cost_hint_zh"]
        assert e["name_en"] and e["name_zh"]


def test_instrument_softness_ranks_valid(kb):
    for iid, inst in kb["instruments"].items():
        assert 1 <= inst["softness"] <= 5, iid
        assert inst["name_en"] and inst["name_zh"]


def test_weights_sum_to_one(kb):
    assert sum(kb["weights"].values()) == pytest.approx(1.0)


# ── canonical alignment ───────────────────────────────────────────────────────
def test_capex_matches_payback_canonical(kb):
    for h in kb["hyperscalers"]:
        assert h["seed"]["capex_ttm_usd_bn"] == CANON_CAPEX[h["id"]]
        assert "payback" in h["align_note"], f"{h['id']} missing cross-reference note"


def test_labs_match_payback_canonical(kb):
    labs = {l["id"]: l["seed"] for l in kb["labs"]}
    assert labs["openai"]["revenue_runrate_usd_bn"]["value"] == 25
    assert labs["openai"]["funding_raised_usd_bn"]["value"] == 122
    assert labs["openai"]["valuation_usd_bn"]["value"] == 850
    assert labs["anthropic"]["revenue_runrate_usd_bn"]["value"] == 12
    assert labs["anthropic"]["funding_raised_usd_bn"]["value"] == 95
    assert labs["anthropic"]["valuation_usd_bn"]["value"] == 965
    for l in kb["labs"]:
        assert "payback" in l["align_note"]
        for field in l["seed"].values():
            assert field["tier"] in ("T1", "T2", "T3")
            assert field.get("est") is True
            assert field.get("as_of")


def test_hy_oas_seed_is_canonical(kb):
    hy = next(s for s in kb["fred_series"] if s["id"] == "hy_oas")
    assert hy["seed"]["value"] == CANON_HY_OAS
    assert hy["seed"]["as_of"] == "2026-07-01"
    assert hy["series"] == "BAMLH0A0HYM2"


# ── L3 engine math ────────────────────────────────────────────────────────────
def test_funding_gap_math(snap):
    rows = {r["id"]: r for r in snap["l3"]["hyperscalers"]}
    orcl = rows["orcl"]
    assert orcl["gap_usd_bn"] == pytest.approx(55.7 - 24.0, abs=0.05)
    assert orcl["external_share_pct"] == pytest.approx(31.7 / 55.7 * 100, abs=0.1)
    msft = rows["msft"]
    assert msft["gap_usd_bn"] < 0
    assert msft["external_share_pct"] == 0.0
    amzn = rows["amzn"]
    assert amzn["capex_ocf_pct"] == pytest.approx(91.0, abs=0.1)  # 對齊 /payback runway


def test_aggregate_gap_share(snap):
    agg = snap["l3"]["aggregate"]
    assert agg["capex_total_usd_bn"] == pytest.approx(489.5, abs=0.1)
    assert agg["gap_share_pct"] == pytest.approx(31.7 / 489.5 * 100, abs=0.1)
    assert agg["debt_issuance_usd_bn"] == pytest.approx(55 + 38 + 27, abs=0.1)


def test_soft_money_score_bounds_and_value(snap):
    sm = snap["l3"]["soft_money"]
    assert 0 <= sm["score"] <= 100
    # drawn-weighted softness of the seed ledger
    ledger = snap["l3"]["ledger"]
    total = sum(e["size_drawn_usd_bn"] for e in ledger)
    expect = sum(e["size_drawn_usd_bn"] * (e["softness"] - 1) / 4 * 100 for e in ledger) / total
    assert sm["score"] == pytest.approx(expect, abs=0.1)


def test_spread_merge_and_decompression(snap):
    sp = snap["l3"]["spreads"]
    assert sp["hy_oas"]["value"] == CANON_HY_OAS
    assert sp["hy_oas"]["live"] is False  # seed view
    assert sp["ccc_minus_ig"] == pytest.approx(7.60 - 0.82, abs=0.01)
    assert sp["decompression"] is True  # CCC +0.35 while HY −0.19


def test_lab_burn_multiples(snap):
    labs = {l["id"]: l for l in snap["l3"]["labs"]}
    assert labs["openai"]["burn_multiple"] == pytest.approx(35.0 / 25, abs=0.01)
    assert labs["anthropic"]["burn_multiple"] == pytest.approx(18.0 / 12, abs=0.01)


# ── composite score ───────────────────────────────────────────────────────────
def test_composite_in_bounds_and_verdict(snap):
    comp = snap["composite"]
    assert 0 <= comp["score"] <= 100
    assert comp["verdict"] in ("SELF-FUNDED", "LEVERING", "STRESSED")
    assert comp["verdict"] == "LEVERING"  # seed world: structure softening, spreads calm
    assert 35 <= comp["score"] < 60
    for s in comp["subscores"]:
        assert 0 <= s["score"] <= 100
        assert s["name_en"] and s["name_zh"]


def test_verdict_thresholds():
    th = {"self_funded_max": 35.0, "levering_max": 60.0}
    assert model.verdict_for(0, th) == "SELF-FUNDED"
    assert model.verdict_for(34.9, th) == "SELF-FUNDED"
    assert model.verdict_for(35.0, th) == "LEVERING"
    assert model.verdict_for(59.9, th) == "LEVERING"
    assert model.verdict_for(60.0, th) == "STRESSED"
    assert model.verdict_for(100, th) == "STRESSED"


def test_spread_subscore_monotonic_in_widening(kb):
    """A widening HY trend must push the spreads subscore up."""
    agg = {"gap_share_pct": 0.0, "debt_issuance_share_pct": 0.0}
    calm = {"hy_oas": {"value": 2.66, "chg_6m": -0.19}, "ccc_minus_ig": 6.78}
    wide = {"hy_oas": {"value": 4.50, "chg_6m": 1.20}, "ccc_minus_ig": 9.0}
    s_calm = model.compute_subscores(agg, calm, 0.0, [])["spreads"]
    s_wide = model.compute_subscores(agg, wide, 0.0, [])["spreads"]
    assert s_wide > s_calm


def test_lab_subscore_monotonic_in_burn():
    agg = {"gap_share_pct": 0.0, "debt_issuance_share_pct": 0.0}
    sp = {"hy_oas": {"value": 2.5, "chg_6m": 0.0}, "ccc_minus_ig": 5.0}
    low = [{"burn_multiple": 0.6}]
    high = [{"burn_multiple": 1.9}]
    assert (model.compute_subscores(agg, sp, 0.0, high)["lab_burn"]
            > model.compute_subscores(agg, sp, 0.0, low)["lab_burn"])


# ── live-merge fallback behavior ─────────────────────────────────────────────
def test_live_merge_wins_and_seed_fills_gaps(kb):
    live = {"companies": {"msft": {"capex_ttm_usd_bn": 100.0, "ocf_ttm_usd_bn": 150.0,
                                   "as_of": "2026-06-30", "live": True}},
            "spreads": {"hy_oas": {"value": 3.10, "chg_6m": 0.44, "as_of": "2026-07-02", "live": True}}}
    snap = model.build_snapshot(kb, live=live, generated_at="t", today="2026-07-02")
    rows = {r["id"]: r for r in snap["l3"]["hyperscalers"]}
    assert rows["msft"]["live"] is True and rows["msft"]["capex_ttm_usd_bn"] == 100.0
    assert rows["orcl"]["live"] is False  # seed fallback keeps rendering
    assert rows["orcl"]["capex_ttm_usd_bn"] == CANON_CAPEX["orcl"]
    sp = snap["l3"]["spreads"]
    assert sp["hy_oas"]["live"] is True and sp["hy_oas"]["value"] == 3.10
    assert sp["ig_oas"]["live"] is False
    assert snap["source"] == "live" and snap["is_demo"] is False


# ── L4/L5 rules fallback ─────────────────────────────────────────────────────
def test_rules_engine_contract(snap):
    assert snap["analysis_engine"] == "rules"  # no API key in CI
    l4, l5 = snap["l4"], snap["l5"]
    for k in ("structure_read", "tsmc_view", "thesis"):
        assert k in l4
    for part in ("structure_read", "tsmc_view"):
        assert l4[part]["summary_en"] and l4[part]["summary_zh"]
    assert l4["thesis"]["confidence"] in ("high", "medium", "low")
    assert 3 <= len(l4["thesis"]["leading_signals"]) <= 6
    for sig in l4["thesis"]["leading_signals"]:
        assert sig["en"] and sig["zh"]
    assert len(l5["falsification"]) >= 2
    assert len(l5["early_warning"]) >= 5
    for e in l5["early_warning"]:
        assert e["freq"]


def test_scenario_probs_sum_to_100(snap):
    probs = [sc["prob"] for sc in snap["l5"]["scenarios"]]
    assert sum(probs) == 100
    for sc in snap["l5"]["scenarios"]:
        assert sc["name_en"] and sc["name_zh"] and sc["trigger_en"] and sc["trigger_zh"]


# ── snapshot shape + committed seed ──────────────────────────────────────────
def test_snapshot_shape(snap):
    for k in ("l3", "l4", "l5", "composite", "funding_stack", "contagion_channels",
              "indicator_dictionary", "tsmc_view", "analysis_engine", "as_of",
              "title_en", "title_zh", "blind_spots_en", "blind_spots_zh",
              "weights", "tier_legend"):
        assert k in snap, k
    assert snap["source"] == "seed"
    assert snap["is_demo"] is True
    assert snap["tsmc_view"]["intro_en"] and snap["tsmc_view"]["intro_zh"]
    assert len(snap["tsmc_view"]["rows"]) >= 5


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
    else:
        assert 0 <= seed["composite"]["score"] <= 100
        assert seed["composite"]["verdict"] in ("SELF-FUNDED", "LEVERING", "STRESSED")
    # every seed hyperscaler row still carries tier + as_of + align note
    for r in seed["l3"]["hyperscalers"]:
        assert r["capex_tier"] and r["ocf_tier"] and r["as_of"] and r["align_note"]
