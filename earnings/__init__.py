"""Supply-Chain Earnings Radar — mounted as a blueprint at /earnings.

A supply-chain-aware earnings calendar across the AI semiconductor chain
(equipment/materials → foundry → IC design → hyperscalers → power/cooling/infra):
73 companies, a rolling N-day calendar with UTC-stored / PT-displayed times, and
tier/region/status filtering.

Self-contained: model.py + companies.json + supply_chain_graph.json live here; the
rendered snapshot caches at data/earnings/snapshot.json. Refresh re-syncs the
calendar from Finnhub (US + ADR coverage; non-US fall back to the bundled data).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from . import model

PKG = Path(__file__).resolve().parent
DATA = PKG.parent / "data" / "earnings"
SNAPSHOT = DATA / "snapshot.json"
REFRESH_PW = os.environ.get("EARNINGS_REFRESH_PASSWORD", "earnings2026")

earnings_bp = Blueprint("earnings", __name__, url_prefix="/earnings")


def _save(snap: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)


def load_snapshot() -> dict:
    if SNAPSHOT.exists():
        with open(SNAPSHOT, encoding="utf-8") as f:
            return json.load(f)
    snap = model.build_snapshot()
    _save(snap)
    return snap


def refresh() -> dict:
    """Re-sync the calendar (Finnhub if FINNHUB_API_KEY set, else seed), persist.
    Used by the weekly orchestrator and the manual /earnings/api/refresh endpoint."""
    snap = model.build_snapshot()
    _save(snap)
    return snap


@earnings_bp.route("/")
def dashboard():
    return render_template("earnings.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@earnings_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@earnings_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    pw = request.args.get("password") or (request.is_json and (request.json or {}).get("password"))
    if pw != REFRESH_PW:
        return jsonify({"ok": False, "error": "bad or missing password"}), 401
    snap = refresh()
    return jsonify({
        "ok": True, "generated_at": snap["generated_at"], "source": snap["source"],
        "events": snap["event_count"], "coverage": snap["coverage"],
    })
