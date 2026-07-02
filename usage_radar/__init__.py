"""AI Usage & Token Economics Radar — mounted as a blueprint at /usage.

CEO question: is the demand REAL — answered from the USAGE side, not the spend
side. The rest of Tier III derives demand from capex (/compute → /cwengine →
/payback); this card reads actual token consumption: disclosed throughput,
per-token price deflation, and realized $/M tokens. The usage-vs-spend scissors
is the bubble's final judge. (/aibubble's scissors is the npm developer-adoption
proxy — this is the deeper token-level read; referenced, not duplicated.)

Five sequential layers turned into a refresh pipeline:
  L1 usage map + L2 indicator dictionary  → curated knowledge_base.json
  L3 token ledger / deflation / scissors  → collectors.py (keyless OpenRouter
                                            public API + Google News RSS)
  L4 usage read + silicon/TSMC view       ┐ analysis.py — Claude Opus 4.8
  L5 scenarios / falsification            ┘ (structured output) or rules fallback

Package dir is usage_radar/ (not usage/) to avoid pip-name collisions on the
import path — the hard-learned bottleneck/pandas lesson.

Refresh is a password-gated manual button only (env USAGE_REFRESH_PASSWORD,
default "usage2026"). The rendered snapshot caches at data/usage/snapshot.json;
seed values let it render offline before any refresh.
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

log = logging.getLogger("usage")

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "usage"
SNAPSHOT = DATA / "snapshot.json"

_DEFAULT_REFRESH_PW = "usage2026"

usage_bp = Blueprint("usage", __name__, url_prefix="/usage")


def _refresh_password() -> str:
    return os.environ.get("USAGE_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


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
            log.exception("usage: cached snapshot unreadable — recomputing from seeds")
    snap = _compute()  # seed-based view
    try:
        _save(snap)
    except Exception:
        log.exception("usage: could not persist seed snapshot")
    return snap


def refresh():
    """Pull live OpenRouter sample + news, recompute (incl. Claude L4/L5), persist."""
    from . import collectors

    live = collectors.fetch_bundle(_kb())
    snap = _compute(live=live)
    _save(snap)
    return snap


@usage_bp.route("/")
def dashboard():
    return render_template("usage.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@usage_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@usage_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh()
    except Exception:
        log.exception("usage refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    live_metrics = sum(1 for c in snap["l3"]["price_deflation"]["curves"] if c.get("live"))
    if (snap["l3"].get("openrouter_live") or {}).get("live"):
        live_metrics += 1
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "engine": snap.get("analysis_engine"),
        "live_metrics": live_metrics,
        "score": snap["composite"]["score"],
        "verdict": snap["composite"]["verdict"],
    })
