"""Company Deep-Dive (Company Lens) — mounted as a blueprint at /company.

A single-company analysis tier sitting at the base of the pyramid. One company
at a time is decomposed through four pillars, all closing the loop back to TSMC:
  A pricing  · how it raises REALIZED compute price (lever-scored, list ≠ realized)
  B sources  · the data-source dictionary to observe pillar A
  C benefit  · multi-method estimate of how much it makes from AI
  D silicon  · each accelerator → node → CoWoS → TSMC + the TSMC-exposure read

Multi-company by design: drop a `company/kb/<slug>.json` and it appears. Amazon
(`/company/amazon`) is the first. The pipeline mirrors flows/pricing/payback:
  L1 pillar KB + L2 source dictionary → kb/<slug>.json
  L3 live proxies (sentiment context, OUT of the score) → collectors.py
  L4 four-pillar CEO read + integrated thesis  ┐ analysis.py — Claude Opus 4.8
  L5 scenarios / falsification / watch          ┘ (structured output) or rules

Refresh is a password-gated manual button only (env COMPANY_REFRESH_PASSWORD,
default "companylens2026") — deliberately NOT wired into the weekly scheduler,
to keep Opus refresh cost on-demand. Rendered snapshots cache at
data/company/<slug>.json; seed values render before any refresh.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, jsonify, redirect, render_template, request

from . import model

log = logging.getLogger("company")

PKG = Path(__file__).resolve().parent
KB_DIR = PKG / "kb"
DATA = PKG.parent / "data" / "company"

_DEFAULT_REFRESH_PW = "companylens2026"
DEFAULT_SLUG = "amazon"

company_bp = Blueprint("company", __name__, url_prefix="/company")


def _refresh_password() -> str:
    return os.environ.get("COMPANY_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


def list_companies():
    """Registry — every kb/<slug>.json, lightly summarized for pickers/portal."""
    out = []
    for f in sorted(KB_DIR.glob("*.json")):
        kb = _safe_load(f)
        if not kb:
            continue
        out.append({
            "slug": kb.get("slug", f.stem),
            "name_en": kb.get("company", {}).get("name_en", f.stem),
            "name_zh": kb.get("company", {}).get("name_zh", f.stem),
            "ticker": kb.get("company", {}).get("ticker", ""),
        })
    return out


def _safe_load(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        log.exception("company: cannot read KB %s", path)
        return None


def _kb_path(slug: str) -> Path:
    # slugs come from the on-disk registry only; reject anything with path parts.
    if not slug or "/" in slug or "\\" in slug or "." in slug:
        abort(404)
    p = KB_DIR / f"{slug}.json"
    if not p.exists():
        abort(404)
    return p


def _kb(slug: str):
    kb = _safe_load(_kb_path(slug))
    if kb is None:
        abort(404)
    return kb


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _snapshot_path(slug: str) -> Path:
    return DATA / f"{slug}.json"


def _compute(slug, live=None):
    return model.build_snapshot(_kb(slug), live=live, generated_at=_now(), today=date.today().isoformat())


def _save(slug, snap):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(_snapshot_path(slug), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)


def load_snapshot(slug: str = DEFAULT_SLUG):
    sp = _snapshot_path(slug)
    if sp.exists():
        try:
            with open(sp, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("company: cached snapshot unreadable — recomputing from seeds")
    snap = _compute(slug)  # seed-based demo
    try:
        _save(slug, snap)
    except Exception:
        log.exception("company: could not persist seed snapshot")
    return snap


def refresh(slug: str = DEFAULT_SLUG):
    """Pull live proxies + news, recompute (incl. Claude L4/L5), persist."""
    from . import collectors

    live = collectors.fetch_bundle(_kb(slug))
    snap = _compute(slug, live=live)
    _save(slug, snap)
    return snap


# ────────────────────────────── routes ──────────────────────────────

@company_bp.route("/")
def index():
    # one company today — go straight to it; the registry keeps it extensible.
    return redirect(f"/company/{DEFAULT_SLUG}/")


@company_bp.route("/<slug>/")
def dashboard(slug):
    _kb_path(slug)  # 404s unknown slugs
    return render_template(
        "company.html",
        snapshot=json.dumps(load_snapshot(slug), ensure_ascii=False),
        companies=list_companies(),
    )


@company_bp.route("/<slug>/api/snapshot")
def api_snapshot(slug):
    _kb_path(slug)
    return jsonify(load_snapshot(slug))


@company_bp.route("/<slug>/api/refresh", methods=["POST"])
def api_refresh(slug):
    _kb_path(slug)
    payload = request.get_json(silent=True) or {}
    pw = str(payload.get("password") or request.args.get("password") or "")
    if not hmac.compare_digest(pw, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = refresh(slug)
    except Exception:
        log.exception("company refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    return jsonify({
        "ok": True,
        "generated_at": snap["generated_at"],
        "engine": snap.get("analysis_engine"),
        "source": snap.get("source"),
        "score": snap.get("headline", {}).get("compute_pricing_score"),
        "verdict": snap.get("headline", {}).get("verdict_key"),
    })
