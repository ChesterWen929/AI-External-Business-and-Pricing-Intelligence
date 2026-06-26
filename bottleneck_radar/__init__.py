"""Deliverable-Compute Bottleneck Radar — mounted as a blueprint at /bottleneck.

TSMC AI capacity is necessary but NOT sufficient. This card converts every link in
the AI-accelerator chain (front-end wafer, CoWoS/SoIC packaging, HBM, ABF substrate,
grid power, cooling/water, optics) to ONE common unit — B200/GB200-class
accelerator-equivalents per quarter — then takes the minimum to find the binding
constraint. It answers: is TSMC the bottleneck? if not, which link is, by how much,
and when does the bottleneck migrate? The pure model lives in engine.py; this module
is the thin Flask shell + seed persistence.

Endpoints
  GET  /bottleneck/                 dashboard
  GET  /bottleneck/api/snapshot     full snapshot JSON
  POST /bottleneck/api/refresh      recompute (Phase 2: pull live HBM/power context)  (password)

Refresh is a password-gated manual action (env BOTTLENECK_REFRESH_PASSWORD,
default "bottleneck2026"). The seed snapshot caches at data/bottleneck/snapshot.json
so the board renders before any refresh. Phase 0 = pure engine + seed; the live
collector + Claude evidence→assumption layer arrives in Phase 2.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from . import engine

log = logging.getLogger("bottleneck")

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "bottleneck"
SNAPSHOT = DATA / "snapshot.json"

_DEFAULT_REFRESH_PW = "bottleneck2026"

bottleneck_bp = Blueprint("bottleneck", __name__, url_prefix="/bottleneck")


def _refresh_password() -> str:
    return os.environ.get("BOTTLENECK_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


def _kb():
    with open(KB, encoding="utf-8") as f:
        return json.load(f)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _compute():
    return engine.build_snapshot(_kb(), generated_at=_now())


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
            log.exception("bottleneck: cached snapshot unreadable -> recomputing from seeds")
    snap = _compute()
    try:
        _save(snap)
    except Exception:
        log.exception("bottleneck: could not persist seed snapshot")
    return snap


def refresh():
    """Phase 0: recompute from seed. Phase 2 will fold in live HBM/power context."""
    snap = _compute()
    _save(snap)
    return snap


@bottleneck_bp.route("/")
def dashboard():
    return render_template("bottleneck.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@bottleneck_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@bottleneck_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh()
    except Exception:
        log.exception("bottleneck refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    th = snap["thesis"]
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "binding_link": th["binding_link"],
        "deliverable_ea_qtr": th["deliverable_ea_qtr"],
        "tsmc_is_bottleneck": th["tsmc_is_bottleneck"],
        "tsmc_headroom_pct": th["tsmc_headroom_pct"],
    })
