"""AI Credit & Financing Radar — mounted as a blueprint at /credit.

CEO question: whose money funds this AI capex cycle — internal cash flow,
equity, or debt & off-balance-sheet structures? The more the funding stack
leans on debt, the later the cycle and the sharper the break. TSMC angle:
counterparty risk on prepayments / long-term capacity agreements.

Five sequential layers turned into a refresh pipeline:
  L1 funding stack + L2 indicator dictionary → curated knowledge_base.json
  L3 funding gaps / spreads / ledger quant   → collectors.py (yfinance + keyless FRED)
  L4 structure read + TSMC counterparty view ┐ analysis.py — Claude Opus 4.8
  L5 contagion scenarios / falsification     ┘ (structured output) or rules fallback

Package dir is credit_radar/ (not credit/) to avoid pip-name collisions on the
import path — the hard-learned bottleneck/pandas lesson.

Refresh is a password-gated manual button only (env CREDIT_REFRESH_PASSWORD,
default "credit2026"). The rendered snapshot caches at data/credit/snapshot.json;
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

log = logging.getLogger("credit")

PKG = Path(__file__).resolve().parent
KB = PKG / "knowledge_base.json"
DATA = PKG.parent / "data" / "credit"
SNAPSHOT = DATA / "snapshot.json"

_DEFAULT_REFRESH_PW = "credit2026"

credit_bp = Blueprint("credit", __name__, url_prefix="/credit")


def _refresh_password() -> str:
    return os.environ.get("CREDIT_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


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
            log.exception("credit: cached snapshot unreadable — recomputing from seeds")
    snap = _compute()  # seed-based view
    try:
        _save(snap)
    except Exception:
        log.exception("credit: could not persist seed snapshot")
    return snap


def refresh():
    """Pull live OCF/capex + spreads + news, recompute (incl. Claude L4/L5), persist."""
    from . import collectors

    live = collectors.fetch_bundle(_kb())
    snap = _compute(live=live)
    _save(snap)
    return snap


@credit_bp.route("/")
def dashboard():
    return render_template("credit.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))


@credit_bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(load_snapshot())


@credit_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh()
    except Exception:
        log.exception("credit refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    live_metrics = sum(1 for r in snap["l3"]["hyperscalers"] if r.get("live"))
    live_metrics += sum(1 for k in ("ig_oas", "hy_oas", "ccc_oas")
                        if (snap["l3"]["spreads"].get(k) or {}).get("live"))
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "engine": snap.get("analysis_engine"),
        "live_metrics": live_metrics,
        "score": snap["composite"]["score"],
        "verdict": snap["composite"]["verdict"],
    })
