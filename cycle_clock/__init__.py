"""Cycle Analogue Clock — mounted as a blueprint at /analogue.

Answers the Fortune-headline question in the platform's news flow — "are we in
1997 or 1999?" — by aligning today's AI build-out to the 1996–2002
telecom/fiber/dotcom cycle. /scenario gives forward probabilities; this card
gives the historical coordinate.

Five layers turned into a refresh pipeline:
  L1 analogue-mapping dictionary (why it holds / WHERE IT BREAKS — first-class)
  L2 curated 1995–2002 quarterly series (T2/T3 reconstructions)  → knowledge_base.json
  L3 nearest-neighbour clock engine                              → model.py
  L4 like-1999 vs structurally-unlike + L5 clock-jump monitors   → analysis.py
     (Claude Opus, rules fallback)
Live inputs (sibling snapshots /aibubble /payback /flows + keyless FRED + news)
arrive via collectors.py on refresh only; every failure falls back to the seed.

Package dir is cycle_clock/ (NOT the route name) to avoid pip-name collisions —
same reason bottleneck_radar/ is not bottleneck/.

Refresh is a password-gated manual button only (env ANALOGUE_REFRESH_PASSWORD,
default "clock2026"). The rendered snapshot caches at data/analogue/snapshot.json;
the committed seed lets it render fully offline.
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

log = logging.getLogger("cycle_clock")

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "analogue"
SNAPSHOT = DATA / "snapshot.json"

_DEFAULT_REFRESH_PW = "clock2026"

analogue_bp = Blueprint("analogue", __name__, url_prefix="/analogue")


def _refresh_password() -> str:
    return os.environ.get("ANALOGUE_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


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
            log.exception("analogue: cached snapshot unreadable — recomputing from seeds")
    snap = _compute()  # seed-based view
    try:
        _save(snap)
    except Exception:
        log.exception("analogue: could not persist seed snapshot")
    return snap


def refresh():
    """Read sibling snapshots + FRED + news, recompute (incl. Claude L4/L5), persist."""
    from . import collectors

    live = collectors.fetch_bundle(_kb())
    snap = _compute(live=live)
    _save(snap)
    return snap


@analogue_bp.route("/")
def dashboard():
    return render_template("analogue.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@analogue_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@analogue_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh()
    except Exception:
        log.exception("analogue refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    comp = snap["l3"]["composite"]
    live_inputs = sum(1 for p in snap["l3"]["pairs"] if p["today"].get("live"))
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "engine": snap.get("analysis_engine"),
        "live_inputs": live_inputs,
        "clock": comp.get("clock"),
        "plus_minus": comp.get("plus_minus"),
        "verdict": comp.get("verdict_key"),
    })
