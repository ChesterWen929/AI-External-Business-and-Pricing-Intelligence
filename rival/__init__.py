"""Rival Radar blueprint — 競爭者與客戶流向情報雷達，掛載於 /rival。

兩層資料：
  • 策展情報層 data/rival/intel_kb.json — 研究 agent 戰隊產出（競爭者檔案、
    客戶流向事件、市佔、SEC/法說會逐字稿、CEO 摘要），證據分級 T1/T2/T3。
  • 即時層 market_live.json / news_live.json — yfinance + Google News RSS，
    由更新按鈕重抓。更新需獨立密碼（env RIVAL_REFRESH_PASSWORD，預設
    "rival2026"）——與全站登入分離，沿用本站 aibubble 的權限模式。
"""
import hmac
import json
import logging
import os
import threading
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

log = logging.getLogger("rival")

rival_bp = Blueprint("rival", __name__, url_prefix="/rival")

DATA = Path(__file__).resolve().parent.parent / "data" / "rival"
_DEFAULT_REFRESH_PW = "rival2026"
_refresh_lock = threading.Lock()


def _refresh_password() -> str:
    return (
        os.environ.get("RIVAL_REFRESH_PASSWORD", "")
        or os.environ.get("TSMC_RADAR_REFRESH_PASSWORD", "")
        or _DEFAULT_REFRESH_PW
    )


def _load(name, default):
    path = DATA / name
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default
    return default


def load_kb() -> dict:
    """Used by the portal page for the channel card stats."""
    return _load("intel_kb.json", {})


def refresh_live() -> dict:
    """Refresh the live layer (yfinance + RSS). Called by the weekly cron too."""
    from . import collectors

    DATA.mkdir(parents=True, exist_ok=True)
    return collectors.refresh_all(DATA)


@rival_bp.route("/")
def dashboard():
    return render_template("rival.html")


@rival_bp.route("/api/data")
def api_data():
    return jsonify({
        "kb": _load("intel_kb.json", {}),
        "market_live": _load("market_live.json", {}),
        "news_live": _load("news_live.json", {}),
    })


@rival_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    body = request.get_json(silent=True) or {}
    supplied = str(body.get("password", ""))
    if not hmac.compare_digest(supplied, _refresh_password()):
        return jsonify({"ok": False, "error": "bad_password"}), 401
    if not _refresh_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "refresh_in_progress"}), 429
    try:
        result = refresh_live()
        return jsonify({"ok": True, **result})
    except Exception as exc:
        log.exception("rival live refresh failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        _refresh_lock.release()
