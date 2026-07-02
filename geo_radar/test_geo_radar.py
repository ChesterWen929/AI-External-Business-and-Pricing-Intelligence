"""Geopolitics & Second-Chain Radar tests — pytest, fully offline.

Seed/engine only: no network, no Claude (ANTHROPIC_API_KEY is force-removed
around every compute so the rules engine always runs).

Run:  cd macro-ai-monitor && python3 -m pytest geo_radar/ -q
"""
import json
import re
from pathlib import Path

import pytest

from geo_radar import _compute, _kb, _refresh_password, engine, analysis

PKG = Path(__file__).resolve().parent
SEED_SNAPSHOT = PKG.parent / "data" / "geo" / "snapshot.json"
TODAY = "2026-07-02"  # frozen so recency-weighted assertions stay deterministic

KB = _kb()
VALID_TIERS = ("T1", "T2", "T3")
DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(scope="module")
def snap():
    import os
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        return engine.build_snapshot(KB, live=None, generated_at="test", today=TODAY)
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


# ── KB integrity ───────────────────────────────────────────────────────────
def test_kb_links_well_formed():
    assert len(KB["links"]) == 7
    ids = set()
    for ln in KB["links"]:
        for k in ("id", "name_en", "name_zh", "completeness", "years_behind",
                  "slope", "evidence_en", "evidence_zh"):
            assert k in ln, f"{ln.get('id')} missing {k}"
        assert ln["id"] not in ids
        ids.add(ln["id"])
        comp = ln["completeness"]
        assert 0 <= comp["pct"] <= 100
        assert comp["tier"] in VALID_TIERS
        assert DATE_RE.match(comp["as_of"]), f"{ln['id']} bad as_of"
        assert ln["slope"] in ("catching_up", "stalled", "slipping")


def test_kb_every_link_completeness_is_flagged_estimate():
    # honesty rule: no China-capability % may pose as measured truth
    for ln in KB["links"]:
        assert ln["completeness"].get("est") is True, f"{ln['id']} not flagged EST"


def test_kb_bilingual_parity():
    for ln in KB["links"]:
        for base in ("name", "evidence", "west_anchor"):
            assert ln.get(f"{base}_en") and ln.get(f"{base}_zh"), f"link {ln['id']} {base}"
    for r in KB["regime"]:
        for base in ("name", "status", "source"):
            assert r.get(f"{base}_en") and r.get(f"{base}_zh"), f"regime {r['id']} {base}"
    for m in KB["moves"]:
        assert m.get("name_en") and m.get("name_zh")
    for s in KB["scenarios_seed"]:
        assert s.get("name_en") and s.get("name_zh") and s.get("trigger_en") and s.get("trigger_zh")
    for w in KB["watchlist_seed"]:
        assert w.get("en") and w.get("zh") and w.get("freq")
    assert len(KB["blind_spots_en"]) == len(KB["blind_spots_zh"]) >= 4


def test_kb_regime_rows_tiered_and_dated():
    assert len(KB["regime"]) >= 6
    for r in KB["regime"]:
        assert r["tier"] in VALID_TIERS
        assert DATE_RE.match(r["as_of"])
        assert r["direction"] in ("tighten", "ease", "mixed", "gap")


def test_kb_moves_valid():
    for m in KB["moves"]:
        assert m["direction"] in ("tighten", "ease")
        assert 0 < m["weight"] <= 1.0
        assert DATE_RE.match(m["date"])
        assert m["tier"] in VALID_TIERS


def test_kb_market_basket_groups():
    groups = [t["group"] for t in KB["market_basket"]]
    assert groups.count("china") == 5
    assert groups.count("west") == 3
    for t in KB["market_basket"]:
        assert t["seed"].get("value") is not None
        assert t["seed"].get("est") is True  # seed prices are estimates too
        assert DATE_RE.match(t["seed"]["as_of"])


# ── engine math ────────────────────────────────────────────────────────────
def test_equipment_link_min_of_subcomponents():
    equip = next(ln for ln in KB["links"] if ln["id"] == "equipment")
    sub_min = min(s["pct"] for s in equip["sub_components"])
    assert engine.link_pct(equip) == sub_min == 18
    # KB-declared pct must agree with the sub-component MIN (guards KB edits)
    assert equip["completeness"]["pct"] == sub_min


def test_composite_formula_reproducible(snap):
    comp = snap["l2"]["composite"]
    pcts = [r["pct"] for r in snap["l2"]["links"]]
    expected = round(0.7 * min(pcts) + 0.3 * (sum(pcts) / len(pcts)), 1)
    assert comp["score"] == expected == 25.4
    assert comp["binding_id"] == "equipment"
    assert comp["verdict"] == "PARTIAL"
    binding_rows = [r for r in snap["l2"]["links"] if r["is_binding"]]
    assert len(binding_rows) == 1 and binding_rows[0]["id"] == "equipment"


def test_composite_verdict_bands():
    def rows(pcts):
        return [{"id": f"l{i}", "pct": p} for i, p in enumerate(pcts)]
    assert engine.composite_completeness(rows([90, 92, 95]))["verdict"] == "NEAR_PARITY"
    assert engine.composite_completeness(rows([50, 60, 70]))["verdict"] == "CLOSING"
    assert engine.composite_completeness(rows([25, 30, 40]))["verdict"] == "PARTIAL"
    assert engine.composite_completeness(rows([5, 10, 15]))["verdict"] == "DEPENDENT"
    for pcts in ([0, 0, 0], [100, 100, 100]):
        s = engine.composite_completeness(rows(pcts))["score"]
        assert 0 <= s <= 100


def test_control_direction_seed_score():
    d = engine.control_direction(KB["moves"], TODAY)
    assert d["score"] == -0.73
    assert d["verdict"] == "EASING"
    # moves older than the 18-month horizon must contribute exactly 0
    old = [c for c in d["contributions"] if c["months_ago"] >= 18]
    assert old and all(c["contribution"] == 0 for c in old)


def test_control_direction_synthetic_branches():
    tighten = [{"id": "x", "date": "2026-06", "name_en": "x", "name_zh": "x",
                "direction": "tighten", "weight": 1.0, "tier": "T1"}]
    d = engine.control_direction(tighten, TODAY)
    assert d["verdict"] == "TIGHTENING" and d["score"] > 0.5
    ease = [dict(tighten[0], direction="ease")]
    assert engine.control_direction(ease, TODAY)["verdict"] == "EASING"
    assert engine.control_direction([], TODAY)["verdict"] == "STABLE"


def test_classify_headline_rules():
    assert engine.classify_headline("US bans new chip exports to China") == "escalation"
    assert engine.classify_headline("BIS adds 20 firms to entity list") == "escalation"
    assert engine.classify_headline("美國擴大晶片出口管制") == "escalation"
    assert engine.classify_headline("Washington eases chip rules after truce") == "de_escalation"
    assert engine.classify_headline("中美休戰 稀土管制暫停") == "de_escalation"
    assert engine.classify_headline("SMIC reports quarterly results") == "neutral"
    assert engine.classify_headline("") == "neutral"


def test_market_proxy_has_weight_zero_in_composite(snap):
    # even an absurd live market read must not move the completeness score
    live = {"market": {t["id"]: {"value": 1.0, "chg_1m": 99.0, "live": True}
                       for t in KB["market_basket"]}}
    snap_live = engine.build_snapshot(KB, live=live, generated_at="t", today=TODAY)
    assert snap_live["l2"]["composite"]["score"] == snap["l2"]["composite"]["score"]
    assert snap_live["l1"]["direction"]["score"] == snap["l1"]["direction"]["score"]


def test_market_basket_math(snap):
    mk = snap["l3"]["market"]
    china = [t["seed"]["chg_1m"] for t in KB["market_basket"] if t["group"] == "china"]
    west = [t["seed"]["chg_1m"] for t in KB["market_basket"] if t["group"] == "west"]
    assert mk["china_avg_chg_1m"] == round(sum(china) / len(china), 2)
    assert mk["west_avg_chg_1m"] == round(sum(west) / len(west), 2)
    assert mk["spread"] == round(mk["china_avg_chg_1m"] - mk["west_avg_chg_1m"], 2)
    assert "PROXY" in mk["proxy_note_en"]


def test_live_merge_fallback():
    live = {"market": {"smic": {"value": 99.0, "chg_1m": 12.0, "live": True}}}
    mk = engine.build_market(KB, live)
    rows = {r["id"]: r for r in mk["rows"]}
    assert rows["smic"]["live"] is True and rows["smic"]["value"] == 99.0
    assert rows["asml"]["live"] is False  # untouched ticker keeps its seed
    assert rows["asml"]["value"] == 1040.0


# ── snapshot shape + L4/L5 rules ───────────────────────────────────────────
def test_snapshot_shape(snap):
    for k in ("l1", "l2", "l3", "taiwan", "l4", "l5", "analysis_engine",
              "title_en", "title_zh", "blind_spots_en", "disclaimer_zh"):
        assert k in snap
    assert snap["is_demo"] is True
    assert snap["source"] == "seed"
    assert snap["analysis_engine"] == "rules"  # no API key in tests
    assert "非投資建議" in snap["disclaimer_zh"]
    assert "investment advice" in snap["disclaimer_en"].lower()


def test_news_classified_on_live_snapshot():
    live = {"news": [
        {"title": "US bans more chips", "url": "u", "source": "s", "date": "2026-07-01"},
        {"title": "Truce eases rare earth curbs", "url": "u", "source": "s", "date": "2026-07-01"},
    ]}
    s = engine.build_snapshot(KB, live=live, generated_at="t", today=TODAY)
    assert [n["cls"] for n in s["l3"]["news"]] == ["escalation", "de_escalation"]
    nc = s["l3"]["news_counts"]
    assert nc["escalation"] == 1 and nc["de_escalation"] == 1 and nc["neutral"] == 0


def test_rules_scenarios_sum_100_all_branches(snap):
    for verdict in ("TIGHTENING", "STABLE", "EASING"):
        for comp_score in (20, 50):
            probs = analysis._scenario_probs(verdict, comp_score)
            assert sum(probs.values()) == 100, (verdict, comp_score)
            assert all(p > 0 for p in probs.values())
    assert sum(sc["prob"] for sc in snap["l5"]["scenarios"]) == 100


def test_rules_l4_bilingual_and_cites_numbers(snap):
    l4 = snap["l4"]
    for key in ("strategic_read", "moat_read", "control_read"):
        assert l4[key]["summary_en"] and l4[key]["summary_zh"]
    assert l4["confidence"] in ("high", "medium", "low")
    assert "25.4" in l4["strategic_read"]["summary_en"]  # cites the composite
    wl = snap["l5"]["watchlist"]
    assert 5 <= len(wl) <= 7
    assert all(w["freq"] for w in wl)
    assert len(snap["l5"]["falsification"]) >= 2


def test_taiwan_block(snap):
    tc = snap["taiwan"]["concentration"]
    assert 0 <= tc["pct"] <= 100
    assert tc["tier"] in VALID_TIERS and tc["est"] is True
    for r in snap["taiwan"]["diversification"]:
        assert r["tier"] in VALID_TIERS and DATE_RE.match(r["date"])
    assert snap["taiwan"]["projection"]["est"] is True


# ── committed seed snapshot ────────────────────────────────────────────────
def test_seed_snapshot_file_integrity():
    assert SEED_SNAPSHOT.exists(), "committed seed snapshot missing"
    with open(SEED_SNAPSHOT, encoding="utf-8") as f:
        s = json.load(f)
    # committed snapshot may be pure seed or a live refresh (platform practice)
    assert s["source"] in ("seed", "live")
    assert s["is_demo"] in (True, False)
    assert 0 <= s["l2"]["composite"]["score"] <= 100
    assert s["l1"]["direction"]["verdict"] in ("TIGHTENING", "STABLE", "EASING")
    assert sum(sc["prob"] for sc in s["l5"]["scenarios"]) == 100
    for r in s["l2"]["links"]:
        assert r["tier"] in VALID_TIERS and r["est"] is True
    assert s["analysis_engine"] in ("rules", "claude")


# ── Flask surface ──────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def client():
    from flask import Flask
    from geo_radar import geo_bp

    app = Flask("geo_test", template_folder=str(PKG.parent / "templates"))
    app.register_blueprint(geo_bp)
    return app.test_client()


def test_dashboard_renders(client):
    r = client.get("/geo/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "地緣與第二供應鏈雷達" in html
    assert "數據做法與假設" in html


def test_api_snapshot(client):
    r = client.get("/geo/api/snapshot")
    assert r.status_code == 200
    data = r.get_json()
    assert "l2" in data and "composite" in data["l2"]


def test_refresh_wrong_password_403(client):
    r = client.post("/geo/api/refresh", json={"password": "nope"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "wrong_password"


def test_default_refresh_password(monkeypatch):
    monkeypatch.delenv("GEO_REFRESH_PASSWORD", raising=False)
    assert _refresh_password() == "geo2026"
    monkeypatch.setenv("GEO_REFRESH_PASSWORD", "custom")
    assert _refresh_password() == "custom"
