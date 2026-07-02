"""Geopolitics & Second-Chain Radar — mounted as a blueprint at /geo.

CEO question: China is building a second AI supply chain — link by link, how
far along is it? Where is the control red line, and which way is it moving?
Can policy redraw the demand/customer map overnight? Every closed link removes
a section of the foundry moat.

Four blocks turned into a refresh pipeline:
  L1 control-regime table            → curated knowledge_base.json (T1 rules, dated)
  L2 second-chain link completeness  → engine.py Liebig MIN-law (method aligned
                                       with /bottleneck; weakest link governs)
  L3 live market proxy + news radar  → collectors.py (yfinance basket, weight 0;
                                       Google News RSS bilingual, keyword-rule
                                       headline classification)
  L4/L5 strategic synthesis          → analysis.py — Claude Opus 4.8 (structured
                                       output) or deterministic rules fallback

Package dir is *_radar (not geo/) per platform convention to avoid pip-name
collisions (hard-learned with bottleneck/pandas). Refresh is a password-gated
manual button only (env GEO_REFRESH_PASSWORD, default "geo2026"). The rendered
snapshot caches at data/geo/snapshot.json; seed values render offline.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from . import engine

log = logging.getLogger("geo")

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "geo"
SNAPSHOT = DATA / "snapshot.json"

_DEFAULT_REFRESH_PW = "geo2026"

geo_bp = Blueprint("geo", __name__, url_prefix="/geo")


def _refresh_password() -> str:
    return os.environ.get("GEO_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


def _kb():
    with open(KB, encoding="utf-8") as f:
        return json.load(f)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _compute(live=None):
    return engine.build_snapshot(_kb(), live=live, generated_at=_now(),
                                 today=date.today().isoformat())


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
            log.exception("geo: cached snapshot unreadable — recomputing from seeds")
    snap = _compute()  # seed-based demo
    try:
        _save(snap)
    except Exception:
        log.exception("geo: could not persist seed snapshot")
    return snap


def refresh():
    """Pull live market basket + news, recompute (incl. Claude L4/L5), persist."""
    from . import collectors

    live = collectors.fetch_bundle(_kb())
    snap = _compute(live=live)
    _save(snap)
    return snap


@geo_bp.route("/")
def dashboard():
    return render_template("geo.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@geo_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@geo_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh()
    except Exception:
        log.exception("geo refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    live_rows = sum(1 for r in snap["l3"]["market"]["rows"] if r.get("live"))
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "engine": snap.get("analysis_engine"),
        "completeness": snap["l2"]["composite"]["score"],
        "control_verdict": snap["l1"]["direction"]["verdict"],
        "live_tickers": live_rows,
        "news": len(snap["l3"]["news"]),
    })
