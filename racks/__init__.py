"""AI Rack BOM × Supply-Chain Radar — mounted as a blueprint at /racks.

Decomposes AI data-center rack-scale systems: accelerators/CPUs/HBM per rack and
who supplies each part, from a curated, fully-sourced knowledge base (every spec &
supplier carries a source URL + evidence tier T1/T2/T3). Refresh adds a live news
radar (new products / design wins) + supplier stock context + staleness clock.

Self-contained: model.py + data_sources.py + knowledge_base.json live here; the
rendered snapshot caches at data/racks/snapshot.json.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from . import model

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "racks"
SNAPSHOT = DATA / "snapshot.json"
REFRESH_PW = os.environ.get("RACKS_REFRESH_PASSWORD", "racks2026")

racks_bp = Blueprint("racks", __name__, url_prefix="/racks")


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
        with open(SNAPSHOT, encoding="utf-8") as f:
            return json.load(f)
    snap = _compute()
    _save(snap)
    return snap


def refresh():
    """Pull news + supplier stocks, recompute, persist. Used by the weekly
    orchestrator and the manual /racks/api/refresh endpoint."""
    from . import data_sources
    live = data_sources.fetch_bundle(_kb())
    snap = _compute(live=live)
    _save(snap)
    return snap


@racks_bp.route("/")
def dashboard():
    return render_template("racks.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@racks_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@racks_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    pw = request.args.get("password") or (request.is_json and (request.json or {}).get("password"))
    if pw != REFRESH_PW:
        return jsonify({"ok": False, "error": "bad or missing password"}), 401
    snap = refresh()
    return jsonify({
        "ok": True, "generated_at": snap["generated_at"],
        "news": len(snap.get("news", [])),
        "stocks": sum(1 for blk in snap.get("supplier_landscape", {}).values()
                      for r in blk.get("rows", []) if r.get("live")),
    })
