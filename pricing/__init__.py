"""Pricing Power Radar — mounted as a blueprint at /pricing.

A three-layer price-stack read for a foundry CEO turned into a refresh pipeline:
  L1 price-stack map + L2 source dictionary → curated knowledge_base.json
  L3 live price diagnosis                   → collectors.py (yfinance + keyless FRED)
  L4 CEO pricing-power read + per-layer       ┐ analysis.py — Claude Opus 4.8 (structured
  L5 scenarios / falsification / watch        ┘ output, prompt-cached) or rules fallback

Real transaction prices are confidential, so the board mixes LIVE market proxies
with tier-graded CURATED price estimates (each flagged) and never fabricates a
secret price. Refresh is a password-gated manual button only (env
PRICING_REFRESH_PASSWORD, default "pricing2026") — deliberately NOT wired into
the weekly scheduler, to keep Opus refresh cost on-demand. The rendered snapshot
caches at data/pricing/snapshot.json; seed values render before any refresh.
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

log = logging.getLogger("pricing")

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "pricing"
SNAPSHOT = DATA / "snapshot.json"

_DEFAULT_REFRESH_PW = "pricing2026"

pricing_bp = Blueprint("pricing", __name__, url_prefix="/pricing")


def _refresh_password() -> str:
    return os.environ.get("PRICING_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


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
            log.exception("pricing: cached snapshot unreadable — recomputing from seeds")
    snap = _compute()  # seed-based demo
    try:
        _save(snap)
    except Exception:
        log.exception("pricing: could not persist seed snapshot")
    return snap


def refresh():
    """Pull live proxies + news, recompute (incl. Claude L4/L5), persist."""
    from . import collectors

    live = collectors.fetch_bundle(_kb())
    snap = _compute(live=live)
    _save(snap)
    return snap


@pricing_bp.route("/")
def dashboard():
    return render_template("pricing.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@pricing_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@pricing_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh()
    except Exception:
        log.exception("pricing refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    live_metrics = sum(1 for ly in snap.get("l3", {}).get("layers", [])
                       for it in ly.get("items", []) if it.get("live"))
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "engine": snap.get("analysis_engine"),
        "live_metrics": live_metrics,
        "score": snap.get("pricing_power", {}).get("score"),
        "verdict": snap.get("pricing_power", {}).get("verdict_key"),
    })
