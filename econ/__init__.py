"""US Economic Monitor blueprint — serves the React UI and a snapshot-backed API under /econ.

Data model: everything is answered from the weekly snapshot
(data/econ/snapshots/latest.json, refreshed Wed alongside the stock refresh).
AI commentary / industry impact for non-key indicators is generated on demand
(once, then persisted back into the snapshot) so casual clicks stay cheap.

Error responses use {"detail": ...} to match the FastAPI shape the React
frontend already parses.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

from flask import Blueprint, jsonify, request, send_from_directory

from .indicators_config import CATEGORIES, INDICATOR_MAP
from . import fred_client as fred
from . import claude_client as claude_ai
from . import validators as validators_mod
from . import refresh_job

ROOT = Path(__file__).parent.parent
UI_DIR = ROOT / "econ_ui"
SNAPSHOTS_DIR = refresh_job.SNAPSHOTS_DIR
ALERTS_DIR = refresh_job.ALERTS_DIR

econ_bp = Blueprint("econ", __name__, url_prefix="/econ")

_snap_lock = threading.Lock()
_snap_cache: dict = {"mtime": None, "data": None}


def _latest_path() -> Path:
    return SNAPSHOTS_DIR / "latest.json"


def _load_snapshot() -> dict | None:
    """Read latest.json with an mtime-based in-process cache."""
    path = _latest_path()
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    with _snap_lock:
        if _snap_cache["mtime"] == mtime and _snap_cache["data"] is not None:
            return _snap_cache["data"]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        _snap_cache.update(mtime=mtime, data=data)
        return data


def _save_snapshot(snap: dict) -> None:
    path = _latest_path()
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2))
    with _snap_lock:
        _snap_cache.update(mtime=path.stat().st_mtime, data=snap)


def _err(status: int, detail: str):
    return jsonify({"detail": detail}), status


def _keys() -> tuple[str, str]:
    return os.environ.get("FRED_API_KEY", ""), os.environ.get("ANTHROPIC_API_KEY", "")


_NO_SNAPSHOT = "尚無資料快照 — 請先執行每週更新 (No data snapshot yet — run the weekly refresh first)"


# ────────────────────────────── UI ──────────────────────────────

@econ_bp.route("/")
def ui_index():
    if not (UI_DIR / "index.html").exists():
        return _err(503, "econ UI not built — run the frontend build and vendor econ_ui/")
    return send_from_directory(UI_DIR, "index.html")


@econ_bp.route("/<path:fname>")
def ui_assets(fname: str):
    return send_from_directory(UI_DIR, fname)


# ────────────────────────────── API ──────────────────────────────

@econ_bp.route("/api/categories")
def api_categories():
    return jsonify(CATEGORIES)


@econ_bp.route("/api/indicators")
def api_indicators():
    snap = _load_snapshot()
    if not snap:
        return _err(503, _NO_SNAPSHOT)
    obs_by_series = snap.get("observations_by_series", {})
    results = []
    for ind in snap.get("indicators", []):
        obs = obs_by_series.get(ind["series_id"], [])
        results.append({**ind, "sparkline": [o["value"] for o in obs[-20:]]})
    return jsonify(results)


@econ_bp.route("/api/indicators/<series_id>/history")
def api_history(series_id: str):
    snap = _load_snapshot()
    if not snap:
        return _err(503, _NO_SNAPSHOT)
    if series_id not in INDICATOR_MAP:
        return _err(404, "Unknown series_id")
    limit = request.args.get("limit", 120, type=int)
    obs = snap.get("observations_by_series", {}).get(series_id, [])
    return jsonify({
        "series_id": series_id,
        "indicator": INDICATOR_MAP[series_id],
        "observations": obs[-limit:],
    })


def _ai_cache_entry(snap: dict, series_id: str) -> dict:
    return snap.get("ai_cache", {}).get(series_id, {})


def _generate_and_store(series_id: str, kind: str):
    """On-demand AI generation for indicators outside the weekly high-importance set.

    kind: "commentary" | "impact". Persists the result into the snapshot so the
    next click is served from cache until the following weekly refresh.
    """
    fred_key, anthropic_key = _keys()
    if not fred_key or not anthropic_key:
        return None, _err(503, "此指標的 AI 分析會於每週更新時產生；伺服器未設定 API 金鑰，無法即時生成 "
                               "(AI analysis not cached and API keys not configured for on-demand generation)")
    ind = INDICATOR_MAP[series_id]
    obs = asyncio.run(fred.get_series_observations(fred_key, series_id, limit=60))
    if len(obs) < 2:
        return None, _err(422, "Not enough data")

    gen = claude_ai.generate_commentary if kind == "commentary" else claude_ai.generate_industry_impact
    result = asyncio.run(gen(
        api_key=anthropic_key,
        name_en=ind["name_en"], name_zh=ind["name_zh"], desc_zh=ind["desc_zh"],
        current_value=obs[-1]["value"], prev_value=obs[-2]["value"],
        unit=ind["unit"], observations=obs,
    ))

    snap = _load_snapshot()
    if snap is not None:
        snap.setdefault("ai_cache", {}).setdefault(series_id, {})[kind] = result
        _save_snapshot(snap)
    return result, None


@econ_bp.route("/api/commentary/<series_id>")
def api_commentary(series_id: str):
    if series_id not in INDICATOR_MAP:
        return _err(404, "Unknown series_id")
    snap = _load_snapshot()
    if snap:
        cached = _ai_cache_entry(snap, series_id).get("commentary")
        if cached:
            return jsonify(cached)
    result, err = _generate_and_store(series_id, "commentary")
    return err if err else jsonify(result)


@econ_bp.route("/api/industry-impact/<series_id>")
def api_industry_impact(series_id: str):
    if series_id not in INDICATOR_MAP:
        return _err(404, "Unknown series_id")
    snap = _load_snapshot()
    if snap:
        cached = _ai_cache_entry(snap, series_id).get("impact")
        if cached:
            return jsonify(cached)
    result, err = _generate_and_store(series_id, "impact")
    return err if err else jsonify(result)


@econ_bp.route("/api/validate/<series_id>")
def api_validate(series_id: str):
    if series_id not in INDICATOR_MAP:
        return _err(404, "Unknown series_id")
    snap = _load_snapshot()
    if snap:
        cached = snap.get("validation", {}).get(series_id)
        if cached:
            return jsonify(cached)
    fred_key, anthropic_key = _keys()
    if not fred_key or not anthropic_key:
        return _err(503, "驗證結果會於每週更新時產生；伺服器未設定 API 金鑰，無法即時驗證 "
                         "(validation not cached and API keys not configured)")
    skip_freshness = request.args.get("skip_freshness", "false").lower() == "true"
    ind = INDICATOR_MAP[series_id]
    obs = asyncio.run(fred.get_series_observations(fred_key, series_id, limit=60))
    if not obs:
        return _err(422, "No data")
    report = asyncio.run(validators_mod.validate_indicator(anthropic_key, ind, obs, skip_freshness=skip_freshness))
    if snap is not None:
        snap.setdefault("validation", {})[series_id] = report
        _save_snapshot(snap)
    return jsonify(report)


@econ_bp.route("/api/calendar")
def api_calendar():
    snap = _load_snapshot()
    if not snap:
        return jsonify([])
    calendar = snap.get("calendar")
    if calendar:
        return jsonify(calendar)
    # Older snapshots predate the embedded calendar — fetch once and persist.
    fred_key, _ = _keys()
    if not fred_key:
        return jsonify([])
    calendar = asyncio.run(refresh_job._fetch_calendar(fred_key))
    snap["calendar"] = calendar
    _save_snapshot(snap)
    return jsonify(calendar)


@econ_bp.route("/api/alerts/latest")
def api_latest_alert():
    latest = ALERTS_DIR / "latest-summary.json"
    if not latest.exists():
        return jsonify({"date": None, "negative_count": 0, "negatives": [], "recommendations": None})
    return jsonify(json.loads(latest.read_text(encoding="utf-8")))


@econ_bp.route("/api/snapshot/latest")
def api_latest_snapshot():
    snap = _load_snapshot()
    if not snap:
        return _err(404, _NO_SNAPSHOT)
    return jsonify(snap)


@econ_bp.route("/api/admin/run-daily", methods=["POST"])
def api_run_refresh():
    """Manual refresh from the UI button — runs the weekly pipeline in the background."""
    fred_key, anthropic_key = _keys()
    if not fred_key or not anthropic_key:
        return _err(500, "Missing API keys (FRED_API_KEY / ANTHROPIC_API_KEY)")
    gen_ai = request.args.get("gen_ai", "true").lower() == "true"
    threading.Thread(
        target=refresh_job.run_weekly_refresh_sync,
        kwargs={"gen_ai": gen_ai, "force": True},
        daemon=True,
    ).start()
    return jsonify({"status": "scheduled",
                    "message": "Refresh started in background. Check back in 1-2 minutes."})


@econ_bp.route("/api/health")
def api_health():
    fred_key, anthropic_key = _keys()
    snap = _load_snapshot()
    return jsonify({
        "status": "ok",
        "fred_key_set": bool(fred_key),
        "anthropic_key_set": bool(anthropic_key),
        "snapshot_date": snap.get("date") if snap else None,
        "indicator_count": snap.get("indicator_count") if snap else 0,
    })
