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


# ── NVIDIA (second company; disclosed-benefit + KB-driven copy) ──
@pytest.fixture
def nvidia_kb():
    with open(KB_DIR / "nvidia.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def nv_snap(nvidia_kb, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return model.build_snapshot(nvidia_kb, live=None, generated_at="2026-06-27 00:00 UTC", today="2026-06-27")


def test_nvidia_builds_and_is_raising(nv_snap):
    assert nv_snap["slug"] == "nvidia"
    assert nv_snap["headline"]["verdict_key"] == "raising"
    assert nv_snap["headline"]["compute_pricing_score"] >= 60


def test_nvidia_top_lever_is_generational_asp(nv_snap):
    assert nv_snap["pillars"]["pricing"]["top_lever_id"] == "generational_asp"
    assert len(nv_snap["pillars"]["pricing"]["levers"]) == 6


def test_nvidia_benefit_is_disclosed(nv_snap):
    b = nv_snap["pillars"]["benefit"]
    # headline DC revenue is a disclosed (annualized) figure, not an estimate
    assert b["headline_is_estimate"] is False
    assert nv_snap["headline"]["ai_benefit_metric"] == "revenue_runrate"


def test_nvidia_full_tsmc_exposure(nv_snap):
    sil = nv_snap["pillars"]["silicon"]
    assert nv_snap["headline"]["tsmc_exposure_pct"] == 100
    assert all(c["fab"].upper().startswith("TSMC") for c in sil["chain"])
    assert sil["chain_count"] == 6


def test_nvidia_scenarios_sum_100(nv_snap):
    probs = [s["prob"] for s in nv_snap["l5"]["scenarios"]]
    assert sum(probs) == 100 and len(probs) == 4


def test_nvidia_benefit_alert_has_no_amazon_bleed(nv_snap):
    """The disclosed-benefit company must not inherit Amazon's 'AWS discloses no AI-only line'."""
    ben_alerts = [a for a in nv_snap["l3"]["alerts"] if "AI benefit" in a["en"]]
    assert ben_alerts, "expected an AI-benefit alert"
    assert all("AWS discloses no AI-only line" not in a["en"] for a in ben_alerts)
    assert any("reports Data Center revenue" in a["en"] for a in ben_alerts)


def test_nvidia_pricing_engine_note_drives_strong_alert(nv_snap):
    strong = [a for a in nv_snap["l3"]["alerts"] if a["level"] == "strong"]
    assert strong and "price-maker" in strong[0]["en"]


def test_amazon_defaults_preserved_for_disclosure_note(snap):
    """Amazon omits the KB note fields → engine falls back to the AWS-specific default."""
    ben_alerts = [a for a in snap["l3"]["alerts"] if "AI benefit" in a["en"]]
    assert ben_alerts and "AWS discloses no AI-only line" in ben_alerts[0]["en"]
    assert snap["pillars"]["benefit"]["headline_is_estimate"] is True


# ── Broadcom (third company; custom-ASIC / networking / VMware) ──
@pytest.fixture
def broadcom_kb():
    with open(KB_DIR / "broadcom.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def bc_snap(broadcom_kb, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return model.build_snapshot(broadcom_kb, live=None, generated_at="2026-06-28 00:00 UTC", today="2026-06-28")


def test_broadcom_builds_and_is_raising(bc_snap):
    assert bc_snap["slug"] == "broadcom"
    assert bc_snap["headline"]["verdict_key"] == "raising"
    assert bc_snap["headline"]["compute_pricing_score"] >= 60


def test_broadcom_top_lever_is_custom_asic(bc_snap):
    assert bc_snap["pillars"]["pricing"]["top_lever_id"] == "custom_asic_value_capture"
    assert len(bc_snap["pillars"]["pricing"]["levers"]) == 6


def test_broadcom_benefit_is_disclosed(bc_snap):
    assert bc_snap["pillars"]["benefit"]["headline_is_estimate"] is False
    assert bc_snap["headline"]["ai_benefit_metric"] == "revenue_runrate"


def test_broadcom_full_tsmc_exposure(bc_snap):
    sil = bc_snap["pillars"]["silicon"]
    assert bc_snap["headline"]["tsmc_exposure_pct"] == 100
    assert all(c["fab"].upper().startswith("TSMC") for c in sil["chain"])
    assert sil["chain_count"] == 6


def test_broadcom_scenarios_sum_100(bc_snap):
    probs = [s["prob"] for s in bc_snap["l5"]["scenarios"]]
    assert sum(probs) == 100 and len(probs) == 4


def test_broadcom_no_amazon_or_nvidia_bleed(bc_snap):
    """The de-Amazonified engine must not leak Amazon/NVIDIA-specific copy into Broadcom's read."""
    import json as _json
    blob = _json.dumps(bc_snap, ensure_ascii=False)
    assert "Trainium" not in blob
    assert "AWS discloses no AI-only line" not in blob
    ben_alerts = [a for a in bc_snap["l3"]["alerts"] if "AI benefit" in a["en"]]
    assert ben_alerts and any("guides AI semiconductor revenue" in a["en"] for a in ben_alerts)


def test_broadcom_pricing_engine_note_drives_strong_alert(bc_snap):
    strong = [a for a in bc_snap["l3"]["alerts"] if a["level"] == "strong"]
    assert strong and "shift away from merchant GPUs" in strong[0]["en"]


# ── Cerebras (fourth company; wafer-scale inference, newly public, HOLDING) ──
@pytest.fixture
def cerebras_kb():
    with open(KB_DIR / "cerebras.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def cb_snap(cerebras_kb, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return model.build_snapshot(cerebras_kb, live=None, generated_at="2026-06-29 00:00 UTC", today="2026-06-29")


def test_cerebras_builds_and_is_holding(cb_snap):
    # Honest read: demand booms but realized unit economics are under pressure → HOLDING,
    # deliberately distinct from the three RAISING price-makers (AMZN/NVDA/AVGO).
    assert cb_snap["slug"] == "cerebras"
    assert cb_snap["headline"]["verdict_key"] == "holding"
    sc = cb_snap["headline"]["compute_pricing_score"]
    assert 41 <= sc <= 59


def test_cerebras_top_lever_is_backlog_lockin(cb_snap):
    assert cb_snap["pillars"]["pricing"]["top_lever_id"] == "backlog_commitment_lockin"
    assert len(cb_snap["pillars"]["pricing"]["levers"]) == 6


def test_cerebras_benefit_is_disclosed_revenue(cb_snap):
    b = cb_snap["pillars"]["benefit"]
    # total revenue ≈ AI benefit for a pure-play; disclosed/guided, not an estimate
    assert b["headline_is_estimate"] is False
    assert cb_snap["headline"]["ai_benefit_metric"] == "revenue_runrate"
    # backlog ($24.6B) carries a DIFFERENT metric → must not be averaged into the run-rate consensus
    assert b["consensus_usd_bn"] == round(cb_snap["headline"]["ai_benefit_usd_bn"], 1)


def test_cerebras_full_tsmc_exposure(cb_snap):
    sil = cb_snap["pillars"]["silicon"]
    assert cb_snap["headline"]["tsmc_exposure_pct"] == 100
    assert all(c["fab"].upper().startswith("TSMC") for c in sil["chain"])
    assert sil["chain_count"] == 6


def test_cerebras_exposure_alert_fires(cb_snap):
    alerts = cb_snap["l3"]["alerts"]
    assert any(a["level"] == "exposure" for a in alerts)


def test_cerebras_scenarios_sum_100(cb_snap):
    probs = [s["prob"] for s in cb_snap["l5"]["scenarios"]]
    assert sum(probs) == 100 and len(probs) == 4


def test_cerebras_no_amazon_or_nvidia_bleed(cb_snap):
    """The KB-driven engine must not leak Amazon/NVIDIA-specific copy into Cerebras's read."""
    import json as _json
    blob = _json.dumps(cb_snap, ensure_ascii=False)
    assert "AWS discloses no AI-only line" not in blob
    assert "Trainium/Inferentia/Graviton" not in blob
    ben_alerts = [a for a in cb_snap["l3"]["alerts"] if "AI benefit" in a["en"]]
    assert ben_alerts and any("pure-play" in a["en"] for a in ben_alerts)


# ── AMD (fifth company; #2 AI-GPU challenger, inference value-play, RAISING) ──
@pytest.fixture
def amd_kb():
    with open(KB_DIR / "amd.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def amd_snap(amd_kb, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return model.build_snapshot(amd_kb, live=None, generated_at="2026-06-30 00:00 UTC", today="2026-06-30")


def test_amd_builds_and_is_raising(amd_snap):
    # Credible #2 — RAISING, but ranked below NVIDIA (75) and Broadcom (74), above Cerebras (holding).
    assert amd_snap["slug"] == "amd"
    assert amd_snap["headline"]["verdict_key"] == "raising"
    assert 60 <= amd_snap["headline"]["compute_pricing_score"] <= 73


def test_amd_top_lever_is_generational_asp(amd_snap):
    assert amd_snap["pillars"]["pricing"]["top_lever_id"] == "generational_asp"
    assert len(amd_snap["pillars"]["pricing"]["levers"]) == 6


def test_amd_benefit_instinct_is_estimate(amd_snap):
    b = amd_snap["pillars"]["benefit"]
    # AMD does NOT break out Instinct → headline is an ESTIMATE (unlike NVIDIA/Broadcom disclosed)
    assert b["headline_is_estimate"] is True
    assert amd_snap["headline"]["ai_benefit_metric"] == "revenue_runrate"
    # the disclosed DC segment ($23B) carries a DIFFERENT metric → not averaged into the Instinct consensus
    assert b["consensus_usd_bn"] == round(amd_snap["headline"]["ai_benefit_usd_bn"], 1)


def test_amd_full_tsmc_exposure(amd_snap):
    sil = amd_snap["pillars"]["silicon"]
    assert amd_snap["headline"]["tsmc_exposure_pct"] == 100
    assert all(c["fab"].upper().startswith("TSMC") for c in sil["chain"])
    assert sil["chain_count"] == 6


def test_amd_scenarios_sum_100(amd_snap):
    probs = [s["prob"] for s in amd_snap["l5"]["scenarios"]]
    assert sum(probs) == 100 and len(probs) == 4


def test_amd_no_amazon_or_nvidia_bleed(amd_snap):
    """KB-driven engine must not leak Amazon/NVIDIA-specific copy into AMD's read."""
    import json as _json
    blob = _json.dumps(amd_snap, ensure_ascii=False)
    assert "AWS discloses no AI-only line" not in blob
    assert "Trainium/Inferentia/Graviton" not in blob
    ben_alerts = [a for a in amd_snap["l3"]["alerts"] if "AI benefit" in a["en"]]
    assert ben_alerts and any("Instinct" in a["en"] for a in ben_alerts)


def test_amd_pricing_engine_note_drives_strong_alert(amd_snap):
    strong = [a for a in amd_snap["l3"]["alerts"] if a["level"] == "strong"]
    assert strong and "value alternative to NVIDIA" in strong[0]["en"]


# ── Apple (sixth company; TSMC volume anchor, on-device/edge-AI, HOLDING, CoWoS-light) ──
@pytest.fixture
def apple_kb():
    with open(KB_DIR / "apple.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def apple_snap(apple_kb, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return model.build_snapshot(apple_kb, live=None, generated_at="2026-06-30 00:00 UTC", today="2026-06-30")


def test_apple_builds_and_is_holding(apple_snap):
    # On-device AI monetized only indirectly → honest HOLDING, below AMD (68) / near Cerebras (58).
    assert apple_snap["slug"] == "apple"
    assert apple_snap["headline"]["verdict_key"] == "holding"
    assert 50 <= apple_snap["headline"]["compute_pricing_score"] < 60


def test_apple_top_lever_is_in_house_silicon(apple_snap):
    assert apple_snap["pillars"]["pricing"]["top_lever_id"] == "in_house_silicon"
    assert len(apple_snap["pillars"]["pricing"]["levers"]) == 6


def test_apple_benefit_is_estimate(apple_snap):
    b = apple_snap["pillars"]["benefit"]
    # Apple discloses NO AI line → headline is an ESTIMATE
    assert b["headline_is_estimate"] is True
    assert apple_snap["headline"]["ai_benefit_metric"] == "revenue_runrate"
    # the disclosed Services run-rate ($100B) carries a DIFFERENT metric → not averaged in
    assert b["consensus_usd_bn"] == round(apple_snap["headline"]["ai_benefit_usd_bn"], 1)


def test_apple_full_tsmc_exposure(apple_snap):
    sil = apple_snap["pillars"]["silicon"]
    assert apple_snap["headline"]["tsmc_exposure_pct"] == 100
    assert all(c["fab"].upper().startswith("TSMC") for c in sil["chain"])
    assert sil["chain_count"] == 6


def test_apple_scenarios_sum_100(apple_snap):
    probs = [s["prob"] for s in apple_snap["l5"]["scenarios"]]
    assert sum(probs) == 100 and len(probs) == 4


def test_apple_no_amazon_or_nvidia_bleed(apple_snap):
    """KB-driven engine must not leak Amazon/NVIDIA-specific copy into Apple's read."""
    import json as _json
    blob = _json.dumps(apple_snap, ensure_ascii=False)
    assert "AWS discloses no AI-only line" not in blob
    assert "Trainium/Inferentia/Graviton" not in blob
    ben_alerts = [a for a in apple_snap["l3"]["alerts"] if "AI benefit" in a["en"]]
    assert ben_alerts and any("estimate" in a["en"].lower() for a in ben_alerts)


def test_apple_is_cowos_light(apple_snap):
    """Apple's signature: ~100% TSMC leading-edge logic, yet uniquely CoWoS/HBM-light (on-device SoC)."""
    read = apple_snap["pillars"]["silicon"]["tsmc_read_en"].lower()
    assert "cowos" in read and "on-device" in read
    # the signature: ~100% TSMC leading-edge logic but explicitly CoWoS/HBM-LIGHT (drawn from the KB summary)
    summ = (apple_snap["pillars"]["silicon"].get("tsmc_read_en") or "").lower()
    assert "outside the cowos bottleneck" in summ or "not apple's binding constraint" in summ
    # the flagship SoC links lean on unified memory, not a GPU-style HBM stack
    info_links = [c for c in apple_snap["pillars"]["silicon"]["chain"] if "unified memory" in (c["packaging"] or "").lower()]
    assert len(info_links) >= 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
