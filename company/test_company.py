"""Company Deep-Dive — engine tests (no network, rules fallback only)."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from company import model  # noqa: E402

KB_DIR = Path(__file__).resolve().parent / "kb"


@pytest.fixture
def amazon_kb():
    with open(KB_DIR / "amazon.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def snap(amazon_kb, monkeypatch):
    # force rules engine (no API key) for deterministic tests
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return model.build_snapshot(amazon_kb, live=None, generated_at="2026-06-26 00:00 UTC", today="2026-06-26")


# ── shape ──
def test_snapshot_top_level_keys(snap):
    for k in ("slug", "headline", "pillars", "l3", "l4", "l5", "analysis_engine", "cross_links"):
        assert k in snap
    assert snap["slug"] == "amazon"
    assert snap["is_demo"] is True
    assert snap["source"] == "seed"


def test_engine_is_rules_without_key(snap):
    assert snap["analysis_engine"] == "rules"


# ── pillar A: pricing power ──
def test_pricing_score_in_range(snap):
    sc = snap["headline"]["compute_pricing_score"]
    assert 0 <= sc <= 100


def test_pricing_verdict_consistent_with_score(snap):
    sc = snap["headline"]["compute_pricing_score"]
    key = snap["headline"]["verdict_key"]
    if sc >= 60:
        assert key == "raising"
    elif sc <= 40:
        assert key == "eroding"
    else:
        assert key == "holding"


def test_amazon_is_raising(snap):
    # seed levers are strong → should read RAISING
    assert snap["headline"]["verdict_key"] == "raising"
    assert snap["headline"]["compute_pricing_score"] >= 60


def test_levers_sorted_desc_by_impact(snap):
    levers = snap["pillars"]["pricing"]["levers"]
    impacts = [lv["strength"] * lv["weight"] for lv in levers]
    assert impacts == sorted(impacts, reverse=True)
    assert len(levers) == 6


def test_top_lever_is_custom_silicon(snap):
    assert snap["pillars"]["pricing"]["top_lever_id"] == "custom_silicon_margin"


# ── pillar C: benefit ──
def test_benefit_headline_present(snap):
    h = snap["headline"]
    assert h["ai_benefit_usd_bn"] is not None
    assert h["ai_benefit_metric"] == "revenue_runrate"


def test_benefit_consensus_only_same_metric(snap):
    b = snap["pillars"]["benefit"]
    # headline metric is revenue_runrate; only one estimate shares it → consensus equals it
    same = [e for e in b["estimates"] if e["metric"] == b["headline_metric"]]
    assert b["consensus_usd_bn"] == round(sum(e["value_usd_bn"] for e in same) / len(same), 1)


# ── pillar D: silicon / TSMC ──
def test_tsmc_exposure_high(snap):
    assert snap["headline"]["tsmc_exposure_pct"] >= 90


def test_every_chain_link_is_tsmc(snap):
    chain = snap["pillars"]["silicon"]["chain"]
    assert all(c["fab"].upper().startswith("TSMC") for c in chain)
    assert snap["pillars"]["silicon"]["tsmc_fabbed_count"] == len(chain)


def test_chain_sorted_by_dependency(snap):
    rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    deps = [rank[c["dependency"]] for c in snap["pillars"]["silicon"]["chain"]]
    assert deps == sorted(deps, reverse=True)


# ── alerts (deterministic) ──
def test_alerts_nonempty_and_exposure_flag(snap):
    alerts = snap["l3"]["alerts"]
    assert alerts
    assert any(a["level"] == "exposure" for a in alerts)


# ── L4/L5 rules ──
def test_l4_pillars_bilingual(snap):
    l4 = snap["l4"]
    assert set(l4["pillars"].keys()) == {"pricing", "sources", "benefit", "silicon"}
    for v in l4["pillars"].values():
        assert v["en"] and v["zh"]
    assert l4["integrated_thesis"]["en"] and l4["integrated_thesis"]["zh"]
    assert l4["tsmc_implication"]["zh"]


def test_l5_scenarios_sum_100(snap):
    probs = [s["prob"] for s in snap["l5"]["scenarios"]]
    assert sum(probs) == 100
    assert len(probs) == 4


def test_l5_watch_has_freq(snap):
    assert all(w.get("freq") for w in snap["l5"]["watch"])


# ── live merge (synthetic, no network) ──
def test_live_proxy_merge_marks_live(amazon_kb):
    live = {"metrics": {"amzn_equity": {"value": 230.0, "chg_1w": 2.0, "chg_1m": 9.0, "live": True}},
            "news": [], "fetched_at": "2026-06-26 01:00 UTC"}
    snap = model.build_snapshot(amazon_kb, live=live, generated_at="x", today="2026-06-26")
    amzn = next(p for p in snap["l3"]["proxies"] if p["id"] == "amzn_equity")
    assert amzn["live"] is True and amzn["value"] == 230.0
    # one fetched, two fell back to seed → partial
    assert snap["source"] == "partial"


def test_proxies_not_in_pricing_score(amazon_kb):
    """Equity proxies must NOT move the pricing-power score (sentiment only)."""
    base = model.build_snapshot(amazon_kb, live=None, generated_at="x", today="t")
    hot = {"metrics": {pid: {"value": 1.0, "chg_1w": 50.0, "chg_1m": 99.0, "live": True}
                       for pid in ("amzn_equity", "nvda_equity", "tsm_equity")},
           "news": [], "fetched_at": "x"}
    moved = model.build_snapshot(amazon_kb, live=hot, generated_at="x", today="t")
    assert base["headline"]["compute_pricing_score"] == moved["headline"]["compute_pricing_score"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
