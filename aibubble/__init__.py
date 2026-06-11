"""AI Bubble Monitor blueprint — AI 泡沫監控平台，掛載於 /aibubble。

儀表板由持久化快照渲染；更新按鈕 POST /aibubble/api/refresh，
需提供獨立更新密碼（env AIBUBBLE_REFRESH_PASSWORD，預設 "aibubble2026"）
——與全站登入分離，方便把更新權限單獨交給特定使用者。
"""
import hmac
import logging
import os

from flask import Blueprint, jsonify, render_template, request

from . import fetcher
from .config import HISTORY

log = logging.getLogger("aibubble")

aibubble_bp = Blueprint("aibubble", __name__, url_prefix="/aibubble")

_DEFAULT_REFRESH_PW = "aibubble2026"


def _refresh_password() -> str:
    return os.environ.get("AIBUBBLE_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW


@aibubble_bp.route("/")
def dashboard():
    snap = fetcher.load_snapshot()
    return render_template("aibubble.html", snap=snap, history=HISTORY)


@aibubble_bp.route("/api/snapshot")
def api_snapshot():
    snap = fetcher.load_snapshot()
    if not snap:
        return jsonify({"error": "no snapshot yet"}), 404
    return jsonify(snap)


@aibubble_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password", ""))
    if not hmac.compare_digest(password, _refresh_password()):
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    try:
        snap = fetcher.refresh()
    except Exception:
        log.exception("aibubble refresh failed")
        return jsonify({"ok": False, "error": "refresh_failed"}), 500
    return jsonify({"ok": True, "generated_at": snap["generated_at"],
                    "composite": (snap.get("scores") or {}).get("composite")})
