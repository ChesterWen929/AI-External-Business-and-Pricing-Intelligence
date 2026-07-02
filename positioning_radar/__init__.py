"""Positioning & Sentiment Radar — mounted as a blueprint at /positioning.

CEO question: how crowded is the AI trade, and who is already fully invested —
institutions, retail, leveraged money? Crowdedness sets the violence of the
drawdown and the timing risk of the top. This card fills the blind spot /flows
itself declares (no direct institutional-positioning data in the free tier)
with genuinely free official sources: CFTC COT, AAII, NAAIM, FINRA margin debt,
CBOE put/call, plus yfinance proxies.

Five sequential layers turned into a refresh pipeline:
  L1 player map + L2 indicator dictionary → curated knowledge_base.json
  L3 positioning quant (3y percentiles)   → collectors.py (CFTC Socrata +
                                            best-effort NAAIM/AAII/CBOE + yfinance)
  L4 who-unwinds-first + TSMC view        ┐ analysis.py — Claude Opus 4.8
  L5 scenarios / falsification / warning  ┘ (structured output) or rules fallback

Package dir is positioning_radar/ (not positioning/) to avoid pip-name
collisions on the import path — the hard-learned bottleneck/pandas lesson.

Refresh is a password-gated manual button only (env POSITIONING_REFRESH_PASSWORD,
default "positioning2026"). The rendered snapshot caches at
data/positioning/snapshot.json; seed values let it render offline before any refresh.
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

log = logging.getLogger("positioning")

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "positioning"
SNAPSHOT = DATA / "snapshot.json"

_DEFAULT_REFRESH_PW = "positioning2026"

positioning_bp = Blueprint("positioning", __name__, url_prefix="/positioning")


def _refresh_password() -> str:
    return os.environ.get("POSITIONING_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


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
            log.exception("positioning: cached snapshot unreadable — recomputing from seeds")
    snap = _compute()  # seed-based view
    try:
        _save(snap)
    except Exception:
        log.exception("positioning: could not persist seed snapshot")
    return snap


def refresh():
    """Pull live positioning + news, recompute (incl. Claude L4/L5), persist."""
    from . import collectors

    live = collectors.fetch_bundle(_kb())
    snap = _compute(live=live)
    _save(snap)
    return snap


@positioning_bp.route("/")
def dashboard():
    return render_template("positioning.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@positioning_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@positioning_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh()
    except Exception:
        log.exception("positioning refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "engine": snap.get("analysis_engine"),
        "live_metrics": snap["l3"]["live_count"],
        "score": snap["composite"]["score"],
        "verdict": snap["composite"]["verdict"],
        "state": snap["nuance"]["state"],
    })
