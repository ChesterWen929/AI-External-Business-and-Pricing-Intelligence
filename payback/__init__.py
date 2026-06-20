"""AI Capex Payback Radar — mounted as a blueprint at /payback.

Answers one question for a senior tech exec: is the hyperscalers' AI capex paying
off yet — who converts spend into revenue, who is just burning? Four-stage pipeline:
  KB (companies, AI-share guidance, cloud revenue, AI-only bands, private labs,
      circularity edges)                       → knowledge_base.json
  L3 live diagnosis (TTM capex/revenue, stock) → collectors.py (yfinance + news)
  coverage / payback score / verdict / scissors / circularity → model.py
  L4 CEO read + L5 scenarios                   → analysis.py (Claude Opus 4.8 / rules)

"AI capex" and "AI revenue" are not reported lines, so the board mixes a live hard
layer with flagged estimates and never presents an estimate as audited fact.
Refresh is a password-gated manual button only (env PAYBACK_REFRESH_PASSWORD,
default "capex2026") — deliberately NOT wired into the weekly scheduler, to keep
Opus refresh cost on-demand. The rendered snapshot caches at
data/payback/snapshot.json; seed values render before any refresh.
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

log = logging.getLogger("payback")

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "payback"
SNAPSHOT = DATA / "snapshot.json"

_DEFAULT_REFRESH_PW = "capex2026"

payback_bp = Blueprint("payback", __name__, url_prefix="/payback")


def _refresh_password() -> str:
    return os.environ.get("PAYBACK_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


def _kb():
    with open(KB, encoding="utf-8") as f:
        return json.load(f)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _compute(live=None):
    return model.build_snapshot(_kb(), live=live, generated_at=_now(), today=date.today().isoformat())


def _save(snap):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)


def load_snapshot():
    if SNAPSHOT.exists():
        try:
            with open(SNAPSHOT, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("payback: cached snapshot unreadable — recomputing from seeds")
    snap = _compute()  # seed-based demo
    try:
        _save(snap)
    except Exception:
        log.exception("payback: could not persist seed snapshot")
    return snap


def refresh():
    """Pull live totals (yfinance) + news, recompute (incl. Claude L4/L5), persist."""
    from . import collectors

    live = collectors.fetch_bundle(_kb())
    snap = _compute(live=live)
    _save(snap)
    return snap


@payback_bp.route("/")
def dashboard():
    return render_template("payback.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@payback_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@payback_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh()
    except Exception:
        log.exception("payback refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    live_cos = sum(1 for p in snap.get("l3", {}).get("companies", []) if p.get("live"))
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "engine": snap.get("analysis_engine"),
        "live_companies": live_cos,
        "coverage": snap.get("headline", {}).get("coverage"),
        "verdict": snap.get("headline", {}).get("verdict_key"),
    })
