"""Tests for the chip-depreciation engine and public-company runway (Phase 1
extension of the AI Capex Payback Radar)."""
import json
import os
from datetime import date

import pytest

from payback import depreciation, model

KB_PATH = os.path.join(os.path.dirname(__file__), "knowledge_base.json")


@pytest.fixture(scope="module")
def kb():
    with open(KB_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def snap(kb):
    return model.build_snapshot(kb, live=None, generated_at="t", today=date.today().isoformat())


# --------------------------------------------------------------------------- #
# Shock formulas
# --------------------------------------------------------------------------- #
def test_impairment_is_base_times_pct():
    assert depreciation.shock_impairment(40.0, 0.25) == 10.0


def test_impairment_scales_with_pct():
    assert depreciation.shock_impairment(40.0, 0.5) > depreciation.shock_impairment(40.0, 0.25)


def test_life_reversal_six_to_four_is_half_more():
    # 6→4 means ×1.5, so a $40B compute-dep base gains $20B.
    assert depreciation.shock_life_reversal(40.0, 6, 4) == 20.0


def test_life_reversal_no_change_when_life_equal():
    assert depreciation.shock_life_reversal(40.0, 6, 6) == 0.0


def test_shorter_reversal_life_hurts_more():
    assert depreciation.shock_life_reversal(40.0, 6, 3) > depreciation.shock_life_reversal(40.0, 6, 4)


def test_stranded_positive_when_window_shorter_than_remaining():
    # retire faster than normal remaining life → incremental annual dep > 0
    hit = depreciation.shock_stranded(40.0, 0.30, remaining_life_years=3.5, replacement_window_years=1.5)
    assert hit > 0


def test_stranded_zero_when_window_equals_remaining():
    hit = depreciation.shock_stranded(40.0, 0.30, remaining_life_years=2.0, replacement_window_years=2.0)
    assert hit == 0.0


def test_stranded_guards_zero_inputs():
    assert depreciation.shock_stranded(40.0, 0.3, 0, 1.5) == 0.0


# --------------------------------------------------------------------------- #
# Per-company + aggregate
# --------------------------------------------------------------------------- #
def test_company_depreciation_shape(kb):
    amzn = next(c for c in kb["companies"] if c["id"] == "amzn")
    r = depreciation.company_depreciation(amzn)
    assert set(r["shocks"]) == {"impairment", "stranded", "life_reversal"}
    assert r["baseline"]["ai_dep_ttm_usd_bn"] > 0
    assert r["shocks"]["impairment"]["kind"] == "one_time"
    assert r["shocks"]["life_reversal"]["kind"] == "annual"


def test_impact_percentages_grounded_in_op_income(kb):
    amzn = next(c for c in kb["companies"] if c["id"] == "amzn")
    r = depreciation.company_depreciation(amzn)
    hit = r["shocks"]["life_reversal"]["hit_usd_bn"]
    op = r["baseline"]["op_income_ttm_usd_bn"]
    assert r["shocks"]["life_reversal"]["pct_of_op_income"] == round(hit / op * 100, 1)


def test_build_aggregate_sums(kb):
    out = depreciation.build(kb["companies"])
    rows = out["companies"]
    assert len(rows) == 4
    assert out["aggregate"]["total_impairment_one_time_usd_bn"] == round(
        sum(r["shocks"]["impairment"]["hit_usd_bn"] for r in rows), 1)


def test_amazon_is_most_exposed(kb):
    # huge D&A base ($70B) against a smaller op income ($85B) → highest % exposure
    out = depreciation.build(kb["companies"])
    assert out["aggregate"]["most_exposed_id"] == "amzn"


def test_build_skips_companies_without_financials():
    out = depreciation.build([{"id": "x", "kind": "public", "name_en": "X", "name_zh": "X"}])
    assert out["companies"] == []


# --------------------------------------------------------------------------- #
# Public runway
# --------------------------------------------------------------------------- #
def test_runway_self_funded_vs_tight():
    big = model._public_runway({"fcf_ttm_usd_bn": 30.0, "cash_usd_bn": 100.0}, capex=90.0, ai_capex=50.0)
    tight = model._public_runway({"fcf_ttm_usd_bn": 9.8, "cash_usd_bn": 143.0}, capex=151.0, ai_capex=83.0)
    assert big["status"] == "self_funded"
    assert tight["status"] == "tight"
    assert tight["capex_to_ocf_pct"] > big["capex_to_ocf_pct"]


def test_runway_external_adds_cash_buffer():
    ext = model._public_runway({"fcf_ttm_usd_bn": -20.0, "cash_usd_bn": 60.0}, capex=100.0, ai_capex=70.0)
    assert ext["status"] == "external"
    assert ext["cash_buffer_years"] == 3.0


def test_runway_none_when_missing_data():
    assert model._public_runway({}, capex=None, ai_capex=10.0) is None


# --------------------------------------------------------------------------- #
# Snapshot integration
# --------------------------------------------------------------------------- #
def test_snapshot_carries_depreciation(snap):
    assert "depreciation" in snap["l3"]
    assert snap["l3"]["depreciation"]["aggregate"]["total_combined_annual_usd_bn"] > 0


def test_snapshot_public_have_runway(snap):
    for p in snap["l3"]["companies"]:
        assert p.get("runway") and "status" in p["runway"]


def test_depreciation_alert_present(snap):
    txts = " ".join(a["en"] for a in snap["l3"]["alerts"])
    assert "useful-life reversal" in txts and "write-down" in txts
