"""AI Compute Demand Radar — mounted as a blueprint at /compute.

Estimates global AI CPU/GPU/ASIC(TPU) demand worked backward from end demand,
triangulated three ways (top-down capex / bottom-up vendor revenue / analyst TAM),
with end-demand decomposition (inference vs training, buyer type, edge AI) and
official sources (SEC EDGAR companyfacts + FRED macro).

Self-contained: model.py + data_sources.py + assumptions.json live in this package;
the rendered snapshot is cached at data/compute/snapshot.json.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from . import model

PKG = Path(__file__).resolve().parent
ASSUMPTIONS = PKG / "assumptions.json"
DATA = PKG.parent / "data" / "compute"
SNAPSHOT = DATA / "snapshot.json"
REFRESH_PW = os.environ.get("COMPUTE_REFRESH_PASSWORD", "compute2026")

compute_bp = Blueprint("compute", __name__, url_prefix="/compute")


def _assumptions():
    with open(ASSUMPTIONS, encoding="utf-8") as f:
        return json.load(f)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _compute(live=None, macro=None, edgar=None):
    return model.build_snapshot(_assumptions(), live=live, generated_at=_now(), macro=macro, edgar=edgar)


def _save(snap):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)


def load_snapshot():
    if SNAPSHOT.exists():
        with open(SNAPSHOT, encoding="utf-8") as f:
            return json.load(f)
    snap = _compute()  # offline seed on first run
    _save(snap)
    return snap


def refresh():
    """Pull yfinance + SEC EDGAR + FRED, recompute, persist. Used by the weekly
    refresh orchestrator and the manual /compute/api/refresh endpoint."""
    from . import data_sources
    bundle = data_sources.fetch_bundle(_assumptions())
    snap = _compute(live=bundle["tickers"], macro=bundle["fred"], edgar=bundle["edgar"])
    _save(snap)
    return snap


@compute_bp.route("/")
def dashboard():
    return render_template("compute.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@compute_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@compute_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    pw = (request.json or {}).get("password") if request.is_json else request.args.get("password")
    if pw != REFRESH_PW:
        return jsonify({"ok": False, "error": "bad or missing password"}), 401
    snap = refresh()
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "edgar_companies": len(snap.get("edgar_official", {})),
        "fred_series": len(snap.get("macro", {})),
        "headline": snap["headline"],
    })
