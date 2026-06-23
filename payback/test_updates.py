"""Tests for the Update pipeline (news → proposed assumption deltas → approval).
Rules engine only (no API key in CI); the human-gate invariant is the key thing."""
import copy
import json
import os

import pytest

from payback import updates

KB_PATH = os.path.join(os.path.dirname(__file__), "knowledge_base.json")


@pytest.fixture
def kb():
    with open(KB_PATH, encoding="utf-8") as f:
        return json.load(f)


NEWS = [
    {"title": "Amazon raises 2026 capex guidance on AI data center buildout", "url": "u1", "date": "2026-06-20", "source": "Reuters"},
    {"title": "Microsoft extends server useful life, lifting cloud margins", "url": "u2", "date": "2026-06-19", "source": "Bloomberg"},
    {"title": "OpenAI in talks to raise funding at higher valuation", "url": "u3", "date": "2026-06-18", "source": "WSJ"},
    {"title": "Analysts warn of H100 impairment risk for Meta", "url": "u4", "date": "2026-06-17", "source": "CNBC"},
    {"title": "Unrelated weather story", "url": "u5", "date": "2026-06-21", "source": "AP"},
]


# --------------------------------------------------------------------------- #
# Field registry
# --------------------------------------------------------------------------- #
def test_updatable_fields_cover_public_and_private(kb):
    ids = {f["field_id"] for f in updates.updatable_fields(kb)}
    assert "amzn.ai_capex_share" in ids
    assert "openai.priv_funding" in ids
    # private entities don't expose public keys
    assert "openai.ai_capex_share" not in ids


def test_set_field_public_and_private(kb):
    updates.set_field(kb, "amzn.ai_capex_share", 62)
    updates.set_field(kb, "openai.priv_funding", 90.0)
    amzn = next(c for c in kb["companies"] if c["id"] == "amzn")
    openai = next(c for c in kb["companies"] if c["id"] == "openai")
    assert amzn["ai_capex_share"]["value"] == 62
    assert openai["seed"]["funding_raised_usd_bn"]["value"] == 90.0


def test_set_field_rejects_unknown(kb):
    with pytest.raises(KeyError):
        updates.set_field(kb, "amzn.bogus", 1)
    with pytest.raises(KeyError):
        updates.set_field(kb, "nope.ai_capex_share", 1)


def test_entities_in_detects_aliases(kb):
    assert "amzn" in updates._entities_in("AWS announces new region", kb)
    assert "msft" in updates._entities_in("Azure revenue jumps", kb)
    assert updates._entities_in("generic headline", kb) == []


# --------------------------------------------------------------------------- #
# Rules proposer
# --------------------------------------------------------------------------- #
def test_rules_routes_news_to_right_fields(kb):
    deltas = updates._rules_propose(kb, NEWS, since="2026-06-15")
    ids = {d["field_id"] for d in deltas}
    assert "amzn.ai_capex_share" in ids       # capex news → amazon capex share
    assert "msft.useful_life" in ids          # useful-life news → microsoft
    assert "meta.impairment_pct" in ids       # H100 impairment → meta
    assert any(d["entity"] == "openai" for d in deltas)  # funding → openai private


def test_rules_ignores_unrelated_and_is_review_only(kb):
    deltas = updates._rules_propose(kb, NEWS, since="2026-06-15")
    assert all("weather" not in (d["source_title"] or "") for d in deltas)
    # rules never invents a number → everything is a review flag
    assert all(d["proposed"] is None and d["direction"] == "review" for d in deltas)


def test_rules_respects_since_watermark(kb):
    deltas = updates._rules_propose(kb, NEWS, since="2026-06-21")
    # only the 2026-06-21 weather item is >= watermark, and it matches nothing
    assert deltas == []


def test_propose_dedups_field_ids(kb):
    dup = NEWS + NEWS
    deltas = updates.propose(kb, dup, since="2026-06-15")
    ids = [d["field_id"] for d in deltas]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# Apply — the human gate
# --------------------------------------------------------------------------- #
def test_apply_only_numeric_approved(kb):
    pending = [
        {"field_id": "amzn.ai_capex_share", "current": 55, "proposed": 60, "entity": "amzn",
         "field_label_en": "AI capex share", "engine": "claude"},
        {"field_id": "msft.useful_life", "current": 6, "proposed": None, "entity": "msft",
         "field_label_en": "GPU useful life", "engine": "rules"},
    ]
    kb2, applied = updates.apply(copy.deepcopy(kb), ["amzn.ai_capex_share", "msft.useful_life"], pending)
    assert len(applied) == 1                      # review-only (None) skipped
    assert applied[0]["from"] == 55 and applied[0]["to"] == 60
    amzn = next(c for c in kb2["companies"] if c["id"] == "amzn")
    assert amzn["ai_capex_share"]["value"] == 60


def test_apply_ignores_unapproved(kb):
    pending = [{"field_id": "amzn.ai_capex_share", "current": 55, "proposed": 60, "entity": "amzn",
                "field_label_en": "x", "engine": "claude"}]
    kb2, applied = updates.apply(copy.deepcopy(kb), [], pending)   # nothing approved
    assert applied == []
    amzn = next(c for c in kb2["companies"] if c["id"] == "amzn")
    assert amzn["ai_capex_share"]["value"] == 55                  # unchanged
