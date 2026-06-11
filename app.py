"""Macro & AI Monitor — standalone Flask app.

Bundles two read-only research dashboards behind one discreet shared login:
  • /aibubble — AI Bubble Monitor (twin thermometer: market layer + frontier layer)
  • /econ     — US Economic Monitor (FRED indicators, multi-horizon change)

Auth:    single shared username/password (env APP_USERNAME / APP_PASSWORD).
Refresh: weekly via APScheduler (Wed 07:00 US Pacific) + a token-protected
         /cron/refresh endpoint for an external weekly pinger (cron-job.org).
Both dashboards are self-contained and bilingual (each has its own EN/中文 toggle);
the low-key portal + login below carry their own lightweight language switch.
"""
import hmac
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

from aibubble import aibubble_bp
from aibubble import fetcher as aibubble_fetcher
from econ import econ_bp, _load_snapshot as econ_load_snapshot
from econ import refresh_job as econ_refresh_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("macro-ai")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.register_blueprint(aibubble_bp)
app.register_blueprint(econ_bp)

# ── secrets / auth (all from env; never hard-code real values) ──
_DEFAULT_SECRET = "dev-insecure-change-me"
app.secret_key = os.environ.get("SECRET_KEY", _DEFAULT_SECRET)
APP_USERNAME = os.environ.get("APP_USERNAME", "analyst")  # neutral default; override in env
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")          # must be set or login rejects everyone
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "")

if app.secret_key == _DEFAULT_SECRET:
    log.warning("SECRET_KEY not set — using insecure dev key. Set SECRET_KEY in production.")
if not APP_PASSWORD:
    log.warning("APP_PASSWORD not set — login will reject everyone. Set APP_PASSWORD.")

PUBLIC_ENDPOINTS = {"login", "logout", "static", "set_lang", "cron_refresh", "healthz"}


# ────────────────────────────── i18n (portal + login only) ──────────────────────────────

STRINGS = {
    "title":    {"en": "Macro & AI Monitor",       "zh": "總經與 AI 觀測"},
    "subtitle": {"en": "Private research dashboards", "zh": "私人研究儀表板"},
    "enter":    {"en": "Open",                       "zh": "進入"},
    "aib_name": {"en": "AI Bubble Monitor",          "zh": "AI 泡沫監控"},
    "aib_desc": {"en": "Twin thermometer — market signals vs. leading frontier signals.",
                 "zh": "雙溫度計 — 市場訊號 vs. 前沿領先訊號。"},
    "econ_name":{"en": "US Economic Monitor",        "zh": "美國經濟指標"},
    "econ_desc":{"en": "FRED indicators with weekly / monthly / quarterly / yearly change.",
                 "zh": "FRED 指標，含 週 / 月 / 季 / 年 變化切換。"},
    "updated":  {"en": "Updated",                    "zh": "更新"},
    "signout":  {"en": "Sign out",                   "zh": "登出"},
    "login_h":  {"en": "Sign in",                    "zh": "登入"},
    "user":     {"en": "Username",                   "zh": "帳號"},
    "pass":     {"en": "Password",                   "zh": "密碼"},
    "login_btn":{"en": "Continue",                   "zh": "繼續"},
    "login_err":{"en": "Incorrect username or password.", "zh": "帳號或密碼錯誤。"},
}


def ui_lang() -> str:
    lang = request.args.get("lang") or request.cookies.get("lang") or "en"
    return lang if lang in ("en", "zh") else "en"


def make_t(lang: str):
    return lambda key: STRINGS.get(key, {}).get(lang, key)


@app.context_processor
def inject_globals():
    lang = ui_lang()
    return {"lang": lang, "t": make_t(lang)}


# ────────────────────────────── auth ──────────────────────────────

@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if session.get("auth"):
        return None
    # Unauthenticated: API/JSON callers get 401, humans go to the login page.
    if request.path.startswith(("/api/", "/econ/api/", "/aibubble/api/")):
        return jsonify({"error": "auth required"}), 401
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("auth"):
        return redirect(request.args.get("next") or "/")
    error = None
    if request.method == "POST":
        user = request.form.get("username", "")
        pw = request.form.get("password", "")
        ok_user = hmac.compare_digest(user, APP_USERNAME)
        ok_pw = bool(APP_PASSWORD) and hmac.compare_digest(pw, APP_PASSWORD)
        if ok_user and ok_pw:
            session.permanent = True
            session["auth"] = True
            nxt = request.form.get("next") or request.args.get("next") or "/"
            if not nxt.startswith("/"):  # avoid open-redirect
                nxt = "/"
            return redirect(nxt)
        error = True
    return render_template("login.html", error=error, next=request.args.get("next", "/"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/lang/<new_lang>")
def set_lang(new_lang):
    if new_lang not in ("en", "zh"):
        new_lang = "en"
    target = request.args.get("next") or request.referrer or "/"
    resp = make_response(redirect(target))
    resp.set_cookie("lang", new_lang, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


# ────────────────────────────── portal ──────────────────────────────

@app.route("/")
def portal():
    econ_snap = econ_load_snapshot()
    aib_snap = aibubble_fetcher.load_snapshot()
    return render_template(
        "portal.html",
        econ_updated=econ_snap.get("date") if econ_snap else None,
        econ_count=econ_snap.get("indicator_count") if econ_snap else 0,
        aib_updated=(aib_snap.get("generated_at") or "")[:10] if aib_snap else None,
        aib_score=((aib_snap.get("scores") or {}).get("composite")) if aib_snap else None,
        aib_zone=(((aib_snap.get("scores") or {}).get("zone") or {}).get("zh" if ui_lang() == "zh" else "en"))
        if aib_snap else None,
    )


# ────────────────────────────── refresh / health ──────────────────────────────

def _refresh_all_bg() -> None:
    """Refresh both dashboards in the background (econ can take minutes)."""
    try:
        aibubble_fetcher.refresh()
    except Exception:
        log.exception("aibubble refresh failed")
    try:
        econ_refresh_job.run_weekly_refresh_sync()
    except Exception:
        log.exception("econ refresh failed")


@app.route("/cron/refresh")
def cron_refresh():
    """Token-protected weekly refresh for an external cron pinger (cron-job.org)."""
    token = request.args.get("token", "")
    if not REFRESH_TOKEN or not hmac.compare_digest(token, REFRESH_TOKEN):
        abort(403)
    threading.Thread(target=_refresh_all_bg, daemon=True).start()
    return jsonify({"ok": True, "refresh": "started", "at": datetime.now(timezone.utc).isoformat()})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


# ────────────────────────────── scheduler ──────────────────────────────

def _start_scheduler():
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" and not app.debug:
        sched = BackgroundScheduler(daemon=True, timezone="America/Los_Angeles")
        sched.add_job(_refresh_all_bg, "cron", day_of_week="wed", hour=7, minute=0, id="weekly_refresh")
        sched.start()
        log.info("scheduler started — weekly refresh @ Wed 07:00 America/Los_Angeles")


_start_scheduler()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5267))
    log.info("Macro & AI Monitor starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
