"""AI Usage & Token Economics Radar tests — pytest, fully offline (seed/engine
only, no network, no Claude).

Run:  cd macro-ai-monitor && python3 -m pytest usage_radar/ -q
"""
import json
from pathlib import Path

import pytest

from usage_radar import _compute, _kb, _refresh_password, model

PKG = Path(__file__).resolve().parent
SNAPSHOT_FILE = PKG.parent / "data" / "usage" / "snapshot.json"

# Canonical shared numbers (audit rule: cite, don't re-derive) — /payback KB v2
# for lab run-rates; /aibubble 2026-07-02 for capex growth & the vast.ai rent
# behind the serving-cost floor.
CANON_OPENAI_RUNRATE = 25
CANON_ANTHROPIC_RUNRATE = 12
CANON_CAPEX_YOY = 80.6

VALID_TIERS = ("T1", "T2", "T3")
VERDICTS = ("SPEND-AHEAD-OF-USE", "GROWING-BUT-UNPAID", "REAL-AND-COMPOUNDING")


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
def test_usage_map_five_classes(kb):
    ranks = [u["rank"] for u in kb["usage_map"]]
    assert ranks == [1, 2, 3, 4, 5]
    for u in kb["usage_map"]:
        assert u["name_en"] and u["name_zh"] and u["desc_en"] and u["desc_zh"]
        assert u["econ_en"] and u["econ_zh"]          # economics of the class
        assert u["silicon_en"] and u["silicon_zh"]    # silicon-durability read


def test_kb_bilingual_parity(kb):
    for d in kb["indicators"]:
        for base in ("name", "what", "why", "threshold"):
            assert d[f"{base}_en"] and d[f"{base}_zh"], f"{d['id']} {base}"
    for s in kb["scenarios_seed"]:
        assert s["name_en"] and s["name_zh"] and s["trigger_en"] and s["trigger_zh"]
    assert len(kb["blind_spots_en"]) == len(kb["blind_spots_zh"])
    assert kb["positioning_en"] and kb["positioning_zh"]  # /aibubble·/compute framing


def test_token_ledger_points_tiered_and_dated(kb):
    for p in kb["token_platforms"]:
        assert len(p["points"]) >= 2, p["id"]
        dates = [pt["date"] for pt in p["points"]]
        assert dates == sorted(dates), f"{p['id']} points must be chronological"
        for pt in p["points"]:
            assert pt["tier"] in VALID_TIERS, p["id"]
            assert len(pt["date"]) == 7 and pt["date"][4] == "-", pt["date"]
            assert pt["monthly_tokens_t"] > 0
            assert pt["src_en"] and pt["src_zh"]
            if pt["tier"] == "T3":
                assert pt.get("est") is True, f"{p['id']} T3 point must be EST"


def test_price_curves_points_tiered_and_dated(kb):
    ratio = kb["blend_ratio_in_out"]
    for c in kb["price_curves"]:
        assert len(c["points"]) >= 2, c["id"]
        dates = [pt["date"] for pt in c["points"]]
        assert dates == sorted(dates), c["id"]
        for pt in c["points"]:
            assert pt["tier"] in VALID_TIERS, c["id"]
            assert model.blended_price(pt, ratio) > 0
            if pt["tier"] == "T3":
                assert pt.get("est") is True, f"{c['id']} T3 point must be EST"


def test_weights_sum_to_one(kb):
    assert sum(kb["weights"].values()) == pytest.approx(1.0)


# ── canonical alignment ───────────────────────────────────────────────────────
def test_lab_runrates_match_payback_canonical(kb):
    oa = kb["monetization"]["openai"]["points"][-1]
    an = kb["monetization"]["anthropic"]["points"][-1]
    assert oa["revenue_runrate_usd_bn"] == CANON_OPENAI_RUNRATE
    assert an["revenue_runrate_usd_bn"] == CANON_ANTHROPIC_RUNRATE
    assert "payback" in oa["align_note"] and "payback" in an["align_note"]
    for pt in (oa, an):
        assert pt["tier"] in VALID_TIERS and pt.get("est") is True and pt["date"]


def test_capex_growth_matches_aibubble_canonical(kb):
    cg = kb["capex_growth"]
    assert cg["yoy_pct"] == CANON_CAPEX_YOY
    assert "aibubble" in cg["align_note"]
    assert cg["as_of"] == "2026-07-02"


def test_cost_floor_derived_from_aibubble_vastai(kb):
    floor = kb["monetization"]["serving_cost_floor"]
    assert floor["tier"] == "T3" and floor["est"] is True and floor["as_of"]
    assert "aibubble" in floor["align_note"] and "2.16" in floor["align_note"]
    # $2.16/hr ÷ 3.6M tokens/hr (1,000 tok/s assumption) ≈ $0.60/M
    assert floor["usd_per_m_tokens"] == pytest.approx(2.16 / 3.6, abs=0.01)


# ── L3 engine math ────────────────────────────────────────────────────────────
def test_growth_and_blend_helpers():
    assert model.months_between("2025-06", "2026-06") == 12
    assert model.months_between("2025-10", "2026-06") == 8
    assert model.annualized_growth_pct(100, 200, 12) == pytest.approx(100.0)
    assert model.annualized_growth_pct(100, 200, 24) == pytest.approx(41.42, abs=0.01)
    assert model.annualized_growth_pct(0, 200, 12) is None
    # 3:1 in:out blend, direct blended value wins
    assert model.blended_price({"in_usd_per_m": 30, "out_usd_per_m": 60}) == pytest.approx(37.5)
    assert model.blended_price({"blended_usd_per_m": 0.9, "in_usd_per_m": 99, "out_usd_per_m": 99}) == 0.9


def test_token_growth_median_and_per_platform(snap):
    led = snap["l3"]["token_ledger"]
    rows = {r["id"]: r for r in led["platforms"]}
    # growth computed WITHIN platform between the last two disclosure points
    assert rows["google"]["growth_yoy_pct"] == pytest.approx(((1900/980)**(12/10)-1)*100, abs=0.1)
    assert rows["msft"]["growth_yoy_pct"] == pytest.approx((110/33.3-1)*100, abs=0.1)
    assert rows["openai_api"]["growth_yoy_pct"] == pytest.approx(((480/259)**(12/8)-1)*100, abs=0.1)
    assert rows["openrouter"]["growth_yoy_pct"] == pytest.approx((55/18-1)*100, abs=0.1)
    growths = sorted(r["growth_yoy_pct"] for r in led["platforms"])
    assert led["growth_median_yoy_pct"] == pytest.approx((growths[1]+growths[2])/2, abs=0.1)


def test_deflation_curves(snap):
    dfl = snap["l3"]["price_deflation"]
    cur = {c["id"]: c for c in dfl["curves"]}
    oa = cur["openai_flagship"]
    assert oa["first_blended"] == pytest.approx(37.5)          # GPT-4 3:1 blend
    assert oa["last_blended"] == pytest.approx(4.31, abs=0.01)  # GPT-5.2-class
    assert oa["annual_change_pct"] == pytest.approx(
        ((4.31/37.5)**(12/37)-1)*100, abs=0.15)
    assert cur["anthropic_flagship"]["first_blended"] == pytest.approx(30.0)
    assert cur["openrouter_market"]["annual_change_pct"] == pytest.approx((0.55/0.9-1)*100, abs=0.1)
    # all deflating, mean is the plain average
    chgs = [c["annual_change_pct"] for c in dfl["curves"]]
    assert all(c < 0 for c in chgs)
    assert dfl["mean_annual_change_pct"] == pytest.approx(sum(chgs)/len(chgs), abs=0.05)


def test_monetization_realized_per_m(snap):
    mon = snap["l3"]["monetization"]
    labs = {l["id"]: l for l in mon["labs"]}
    # $/M = revenue_bn × 1000 ÷ (monthly_T × 12)
    assert labs["openai"]["realized_usd_per_m"] == pytest.approx(25*1000/(480*12), abs=0.01)
    assert labs["anthropic"]["realized_usd_per_m"] == pytest.approx(12*1000/(130*12), abs=0.01)
    assert labs["anthropic"]["realized_trend_pct_yr"] is None  # single point → no trend
    assert labs["openai"]["realized_trend_pct_yr"] == pytest.approx(
        ((4.34/4.18)**(12/8)-1)*100, abs=0.3)
    assert mon["realized_over_floor_x"] == pytest.approx(4.34/0.60, abs=0.1)


def test_scissors_read(snap):
    sci = snap["l3"]["scissors"]
    assert sci["capex_growth_yoy_pct"] == CANON_CAPEX_YOY
    assert "aibubble" in sci["capex_align_note"]
    g, d = sci["token_growth_yoy_pct"], sci["mean_price_change_pct_yr"]
    assert sci["unit_ratio"] == pytest.approx(g/CANON_CAPEX_YOY, abs=0.01)
    expected_dollar = ((1+g/100)*(1+d/100)-1)*100
    assert sci["dollar_growth_yoy_pct"] == pytest.approx(expected_dollar, abs=0.1)
    assert sci["dollar_ratio"] == pytest.approx(expected_dollar/CANON_CAPEX_YOY, abs=0.01)
    # the seed world's signature: units outrun capex, dollars trail it
    assert sci["unit_ratio"] > 1.5
    assert sci["dollar_ratio"] < 1.0


# ── composite score ───────────────────────────────────────────────────────────
def test_composite_in_bounds_and_verdict(snap):
    comp = snap["composite"]
    assert 0 <= comp["score"] <= 100
    assert comp["verdict"] in VERDICTS
    # seed world: usage real in units, deflation eats the dollars
    assert comp["verdict"] == "GROWING-BUT-UNPAID"
    assert 40 <= comp["score"] < 70
    for s in comp["subscores"]:
        assert 0 <= s["score"] <= 100
        assert s["name_en"] and s["name_zh"]


def test_verdict_thresholds():
    th = {"spend_ahead_max": 40.0, "unpaid_max": 70.0}
    assert model.verdict_for(0, th) == "SPEND-AHEAD-OF-USE"
    assert model.verdict_for(39.9, th) == "SPEND-AHEAD-OF-USE"
    assert model.verdict_for(40.0, th) == "GROWING-BUT-UNPAID"
    assert model.verdict_for(69.9, th) == "GROWING-BUT-UNPAID"
    assert model.verdict_for(70.0, th) == "REAL-AND-COMPOUNDING"
    assert model.verdict_for(100, th) == "REAL-AND-COMPOUNDING"


def test_usage_subscore_monotonic_in_unit_ratio():
    lo = model.compute_subscores(0.8, 0.0, 0.5, 10.0)["usage_vs_spend"]
    hi = model.compute_subscores(2.2, 0.0, 0.5, 10.0)["usage_vs_spend"]
    assert hi > lo


def test_dollar_subscore_monotonic_in_dollar_ratio():
    lo = model.compute_subscores(2.0, 0.0, 0.5, 10.0)["dollar_scissors"]
    hi = model.compute_subscores(2.0, 0.0, 1.2, 10.0)["dollar_scissors"]
    assert hi > lo
    # elasticity health rises with dollar-growth factor
    weak = model.compute_subscores(2.0, 0.0, 0.5, -10.0)["elasticity"]
    strong = model.compute_subscores(2.0, 0.0, 0.5, 60.0)["elasticity"]
    assert strong > weak


# ── live-merge fallback behavior ─────────────────────────────────────────────
def test_live_merge_wins_and_seed_fills_gaps(kb):
    live = {"openrouter": {"median_blended_usd_per_m": 0.42, "model_count": 300,
                           "priced_models": 250, "as_of": "2026-07-02", "live": True}}
    snap = model.build_snapshot(kb, live=live, generated_at="t", today="2026-07-02")
    cur = {c["id"]: c for c in snap["l3"]["price_deflation"]["curves"]}
    orm = cur["openrouter_market"]
    assert orm["live"] is True
    assert orm["points"][-1]["blended"] == pytest.approx(0.42)
    # live point re-dates to the fetch as_of (2026-07): 13 months from 2025-06
    assert orm["points"][-1]["date"] == "2026-07"
    assert orm["annual_change_pct"] == pytest.approx(((0.42/0.9)**(12/13)-1)*100, abs=0.1)
    # everything else keeps its seed and stays non-live
    assert cur["openai_flagship"]["live"] is False
    assert cur["openai_flagship"]["last_blended"] == pytest.approx(4.31, abs=0.01)
    assert snap["source"] == "live" and snap["is_demo"] is False
    # token ledger has no live source — seeds always
    rows = {r["id"]: r for r in snap["l3"]["token_ledger"]["platforms"]}
    assert rows["google"]["latest_monthly_tokens_t"] == 1900


# ── L4/L5 rules fallback ─────────────────────────────────────────────────────
def test_rules_engine_contract(snap):
    assert snap["analysis_engine"] == "rules"  # no API key in CI
    l4, l5 = snap["l4"], snap["l5"]
    for k in ("usage_read", "silicon_view", "thesis"):
        assert k in l4
    for part in ("usage_read", "silicon_view"):
        assert l4[part]["summary_en"] and l4[part]["summary_zh"]
    assert l4["thesis"]["confidence"] in ("high", "medium", "low")
    assert 3 <= len(l4["thesis"]["leading_signals"]) <= 6
    for sig in l4["thesis"]["leading_signals"]:
        assert sig["en"] and sig["zh"]
    assert len(l5["falsification"]) >= 2
    # the bubble-confirmation line must be present verbatim in spirit
    assert any("兩季" in f["zh"] or "2 consecutive quarters" in f["en"]
               for f in l5["falsification"])
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
    for k in ("l3", "l4", "l5", "composite", "usage_map", "indicator_dictionary",
              "analysis_engine", "as_of", "title_en", "title_zh",
              "positioning_en", "positioning_zh", "blind_spots_en",
              "blind_spots_zh", "weights", "tier_legend"):
        assert k in snap, k
    assert snap["source"] == "seed"
    assert snap["is_demo"] is True
    for k in ("token_ledger", "price_deflation", "monetization", "scissors"):
        assert k in snap["l3"], k


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
        assert (seed["l3"]["token_ledger"]["growth_median_yoy_pct"]
                == snap["l3"]["token_ledger"]["growth_median_yoy_pct"])
    else:
        assert 0 <= seed["composite"]["score"] <= 100
        assert seed["composite"]["verdict"] in VERDICTS
    # every ledger point still carries tier + date; canonical capex still cited
    for r in seed["l3"]["token_ledger"]["platforms"]:
        for pt in r["points"]:
            assert pt["tier"] in VALID_TIERS and pt["date"]
    assert seed["l3"]["scissors"]["capex_growth_yoy_pct"] == CANON_CAPEX_YOY


# ── Flask surface ──────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def client():
    from flask import Flask
    from usage_radar import usage_bp

    app = Flask("usage_test", template_folder=str(PKG.parent / "templates"))
    app.register_blueprint(usage_bp)
    return app.test_client()


def test_dashboard_renders(client):
    r = client.get("/usage/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "AI 用量與 Token 經濟雷達" in html
    assert "數據做法與假設" in html


def test_api_snapshot_and_refresh_gate(client, monkeypatch):
    r = client.get("/usage/api/snapshot")
    assert r.status_code == 200
    data = r.get_json()
    assert "composite" in data and "scissors" in data["l3"]
    # wrong password → 403, no refresh attempted
    r = client.post("/usage/api/refresh", json={"password": "nope"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "wrong_password"
    # default + env-override password
    monkeypatch.delenv("USAGE_REFRESH_PASSWORD", raising=False)
    assert _refresh_password() == "usage2026"
    monkeypatch.setenv("USAGE_REFRESH_PASSWORD", "custom")
    assert _refresh_password() == "custom"
