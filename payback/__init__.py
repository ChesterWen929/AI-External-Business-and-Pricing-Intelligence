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
UPDATES = DATA / "updates.json"

_DEFAULT_REFRESH_PW = "capex2026"

payback_bp = Blueprint("payback", __name__, url_prefix="/payback")


def _refresh_password() -> str:
    return os.environ.get("PAYBACK_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


def _kb():
    with open(KB, encoding="utf-8") as f:
        return json.load(f)


def _save_kb(kb):
    with open(KB, "w", encoding="utf-8") as f:
        json.dump(kb, f, ensure_ascii=False, indent=2)


def _load_updates():
    if UPDATES.exists():
        try:
            with open(UPDATES, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("payback: updates state unreadable — resetting")
    return {"watermark": None, "kb_version": 1, "pending": [], "history": []}


def _save_updates(state):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(UPDATES, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _compute(live=None):
    return model.build_snapshot(_kb(), live=live, generated_at=_now(), today=date.today().isoformat())


def _save(snap):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)


def _attach_updates(snap):
    """Surface the draft (pending) assumption deltas + last watermark on the snapshot
    so the dashboard can show them. These are proposals only — never applied here."""
    state = _load_updates()
    snap["pending_updates"] = state.get("pending", [])
    snap["update_watermark"] = state.get("watermark")
    snap["kb_version"] = state.get("kb_version", 1)
    return snap


def load_snapshot():
    if SNAPSHOT.exists():
        try:
            with open(SNAPSHOT, encoding="utf-8") as f:
                return _attach_updates(json.load(f))
        except Exception:
            log.exception("payback: cached snapshot unreadable — recomputing from seeds")
    snap = _compute()  # seed-based demo
    try:
        _save(snap)
    except Exception:
        log.exception("payback: could not persist seed snapshot")
    return _attach_updates(snap)


def refresh():
    """Pull live totals (yfinance) + news, recompute, AND draft assumption deltas
    from the news since the last watermark. Live hard totals are applied; the curated
    estimate-layer deltas are stored as PENDING only — the user approves them later."""
    from . import collectors, updates

    kb = _kb()
    live = collectors.fetch_bundle(kb)
    snap = _compute(live=live)

    state = _load_updates()
    try:
        pending = updates.propose(kb, live.get("news", []), since=state.get("watermark"))
    except Exception:
        log.exception("payback: proposing updates failed")
        pending = []
    state["pending"] = pending
    state["watermark"] = _now()
    _save_updates(state)

    _save(snap)
    return _attach_updates(snap)


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
        "pending_updates": len(snap.get("pending_updates", [])),
    })


@payback_bp.route("/api/updates")
def api_updates():
    state = _load_updates()
    return jsonify({"watermark": state.get("watermark"), "kb_version": state.get("kb_version", 1),
                    "pending": state.get("pending", []), "history": state.get("history", [])[-20:]})


@payback_bp.route("/api/apply_updates", methods=["POST"])
def api_apply_updates():
    """Apply ONLY the deltas the user explicitly approved, then version + recompute.
    This is the one path that writes the curated knowledge base."""
    from . import updates as upd

    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    approved = payload.get("approved") or []
    if not isinstance(approved, list) or not approved:
        return jsonify({"ok": False, "error": "no_approved_ids"}), 400

    state = _load_updates()
    pending = state.get("pending", [])
    try:
        kb = _kb()
        kb, applied = upd.apply(kb, approved, pending)
        if not applied:
            return jsonify({"ok": False, "error": "nothing_applied"}), 400
        _save_kb(kb)
        # version, archive applied + drop them from pending
        state["kb_version"] = int(state.get("kb_version", 1)) + 1
        applied_ids = {a["field_id"] for a in applied}
        state["pending"] = [d for d in pending if d["field_id"] not in applied_ids]
        for a in applied:
            a["applied_at"] = _now()
            a["kb_version"] = state["kb_version"]
        state.setdefault("history", []).extend(applied)
        _save_updates(state)
        snap = _compute()       # recompute from the updated KB (seed view; refresh re-pulls live)
        _save(snap)
    except Exception:
        log.exception("payback apply_updates failed")
        return jsonify({"ok": False, "error": "apply_failed"}), 500
    return jsonify({"ok": True, "applied": applied, "kb_version": state["kb_version"],
                    "remaining_pending": len(state["pending"])})
