"""Scenario Radar — mounted as a blueprint at /scenario.

A capstone / meta platform: it does not pull raw yfinance/FRED. Instead refresh()
reads every sibling platform's snapshot, L3 normalizes them into a driver state
vector, derives a baseline scenario distribution (prior + coverage-shrunk softmax),
and L4/L5 calibrate it (Claude Opus 4.8, or a deterministic rules fallback).

Five sequential layers turned into a refresh pipeline (mirrors flows/):
  L1 scenario space + L2 driver dictionary   → curated knowledge_base.json
  L3 cross-platform aggregation              → model.py (reads the siblings dict)
  L4 calibrated scenarios + base/tail/path   ┐ analysis.py — Claude Opus 4.8
  L5 watch / falsification / early-warning   ┘ (structured output) or rules fallback

Refresh is a password-gated manual button only (env SCENARIO_REFRESH_PASSWORD,
default "scenario2026") — deliberately NOT wired into the weekly scheduler, to keep
Opus cost on-demand. The rendered snapshot caches at data/scenario/snapshot.json;
seed values let it render before any refresh. load_snapshot() (the cold/portal path)
NEVER reads siblings — only refresh() does.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from . import model

log = logging.getLogger("scenario")

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "scenario"
SNAPSHOT = DATA / "snapshot.json"

_DEFAULT_REFRESH_PW = "scenario2026"

scenario_bp = Blueprint("scenario", __name__, url_prefix="/scenario")


def _refresh_password() -> str:
    return os.environ.get("SCENARIO_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


def _kb():
    with open(KB, encoding="utf-8") as f:
        return json.load(f)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _today():
    return date.today().isoformat()


def _safe(fn):
    """Run a sibling loader, swallowing ALL exceptions → None (lazy import inside fn)."""
    try:
        return fn()
    except Exception:
        return None


def _gather_siblings():
    """Read every sibling platform's snapshot. Each import is LAZY (inside the lambda)
    so importing `scenario` never boots the whole app; each read is wrapped in _safe."""
    out = {}
    out["econ"] = _safe(lambda: __import__("econ")._load_snapshot())          # may return None (not raise)
    out["aibubble"] = _safe(lambda: __import__("aibubble").fetcher.load_snapshot())
    out["flows"] = _safe(lambda: __import__("flows").load_snapshot())
    out["payback"] = _safe(lambda: __import__("payback").load_snapshot())
    out["compute"] = _safe(lambda: __import__("compute").load_snapshot())
    out["cwengine"] = _safe(lambda: __import__("cwengine").load_snapshot())
    out["pricing"] = _safe(lambda: __import__("pricing").load_snapshot())
    out["racks"] = _safe(lambda: __import__("racks").load_snapshot())
    out["earnings"] = _safe(lambda: __import__("earnings").load_snapshot())
    out["rival"] = _safe(lambda: __import__("rival").load_kb())
    out["bottleneck"] = _safe(lambda: __import__("bottleneck").load_snapshot())  # OPTIONAL — may ImportError
    return out


def _compute(siblings=None, prior_probs=None):
    return model.build_snapshot(_kb(), siblings=siblings, generated_at=_now(),
                                today=_today(), prior_probs=prior_probs)


def _save(snap):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)


def load_snapshot():
    """Cold / portal path — read the cached snapshot; never gather siblings."""
    if SNAPSHOT.exists():
        try:
            with open(SNAPSHOT, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("scenario: cached snapshot unreadable — recomputing from seeds")
    snap = _compute()  # seed-based demo (siblings=None → pure prior)
    try:
        _save(snap)
    except Exception:
        log.exception("scenario: could not persist seed snapshot")
    return snap


def refresh():
    """Read sibling snapshots, recompute (incl. Claude L4/L5), persist."""
    prior_probs = None
    if SNAPSHOT.exists():
        try:
            with open(SNAPSHOT, encoding="utf-8") as f:
                prev = json.load(f)
            # use the previous snapshot's distribution as the drift baseline
            prior_probs = {s["id"]: s["prob"] for s in (prev.get("l3", {}).get("scenarios") or [])} or None
        except Exception:
            prior_probs = None
    siblings = _gather_siblings()
    snap = _compute(siblings=siblings, prior_probs=prior_probs)
    _save(snap)
    return snap


@scenario_bp.route("/")
def dashboard():
    return render_template("scenario.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@scenario_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@scenario_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh()
    except Exception:
        log.exception("scenario refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    h = snap.get("headline", {})
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "engine": snap.get("analysis_engine"),
        "base_id": h.get("base_id"),
        "base_prob": h.get("base_prob"),
        "coverage": h.get("coverage"),
        "divergences": h.get("divergence_count"),
    })
