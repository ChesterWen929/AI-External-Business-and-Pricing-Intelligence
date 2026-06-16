"""CapEx-to-Wafer Demand Inference Engine — mounted as a blueprint at /cwengine.

Converts hyperscaler / AI-infra CapEx into implied leading-edge logic-wafer demand
through a versioned, dated, regime-tagged assumption GRAPH (knowledge_base.json),
6-12 months ahead of direct customer orders. The pure model lives in engine.py;
this module is the thin Flask shell + persistence.

Endpoints
  GET  /cwengine/                 dashboard (5 views)
  GET  /cwengine/api/snapshot     full snapshot JSON
  POST /cwengine/api/refresh      pull live capex context, recompute  (password)
  POST /cwengine/api/propose      evidence -> assumption suggestion    (password)

Refresh + propose are password-gated manual actions (env CWENGINE_REFRESH_PASSWORD,
default "cwengine2026"); /api/propose runs Claude Opus only when ANTHROPIC_API_KEY
is set, else a deterministic rules matcher. Seed snapshot caches at
data/cwengine/snapshot.json so the board renders before any refresh.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from . import analysis, engine

log = logging.getLogger("cwengine")

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "cwengine"
SNAPSHOT = DATA / "snapshot.json"

_DEFAULT_REFRESH_PW = "cwengine2026"

cwengine_bp = Blueprint("cwengine", __name__, url_prefix="/cwengine")


def _refresh_password() -> str:
    return os.environ.get("CWENGINE_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


def _kb():
    with open(KB, encoding="utf-8") as f:
        return json.load(f)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _compute(live_capex=None):
    return engine.build_snapshot(_kb(), generated_at=_now(), live_capex=live_capex)


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
            log.exception("cwengine: cached snapshot unreadable -> recomputing from seeds")
    snap = _compute()
    try:
        _save(snap)
    except Exception:
        log.exception("cwengine: could not persist seed snapshot")
    return snap


def refresh():
    """Pull live hyperscaler-capex context, recompute, persist."""
    from . import collectors

    bundle = collectors.fetch_bundle(_kb())
    snap = _compute(live_capex=bundle.get("capex_context"))
    _save(snap)
    return snap


@cwengine_bp.route("/")
def dashboard():
    return render_template("cwengine.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@cwengine_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@cwengine_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh()
    except Exception:
        log.exception("cwengine refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    ctx = snap.get("live_capex_context") or {}
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "wafers_year": snap["inference"]["wafers_year"],
        "live_capex_companies": ctx.get("n", 0),
        "live_capex_ttm_usd_bn": ctx.get("total_ttm_usd_bn"),
    })


@cwengine_bp.route("/api/propose", methods=["POST"])
def api_propose():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty_text"}), 400
    try:
        proposal = analysis.propose(_kb(), text)
    except Exception:
        log.exception("cwengine propose failed")
        return jsonify({"ok": False, "error": "propose_failed"}), 500
    return jsonify({"ok": True, "proposal": proposal,
                    "note_en": "Suggestion only — human approval required; the seed graph is unchanged.",
                    "note_zh": "僅為建議——需人工核准；種子假設圖未變更。"})
