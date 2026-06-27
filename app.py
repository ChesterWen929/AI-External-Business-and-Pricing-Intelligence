"""Macro & AI Monitor — standalone Flask app.

Bundles three read-only research dashboards behind one discreet shared login:
  • /aibubble — AI Bubble Monitor (twin thermometer: market layer + frontier layer)
  • /econ     — US Economic Monitor (FRED indicators, multi-horizon change)
  • /rival    — Rival Radar (foundry competitor & customer-flow intelligence)

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
from rival import load_kb as rival_load_kb
from rival import refresh_live as rival_refresh_live
from rival import rival_bp
from compute import compute_bp
from compute import load_snapshot as compute_load_snapshot
from compute import refresh as compute_refresh
from racks import racks_bp
from racks import load_snapshot as racks_load_snapshot
from racks import refresh as racks_refresh
from flows import flows_bp
from flows import load_snapshot as flows_load_snapshot
from cwengine import cwengine_bp
from cwengine import load_snapshot as cwengine_load_snapshot
from cwengine import refresh as cwengine_refresh
from earnings import earnings_bp
from earnings import load_snapshot as earnings_load_snapshot
from earnings import refresh as earnings_refresh
from pricing import pricing_bp
from pricing import load_snapshot as pricing_load_snapshot
# pricing refresh is manual-only (password-gated button), like flows — not wired into the weekly scheduler
from payback import payback_bp
from payback import load_snapshot as payback_load_snapshot
# payback refresh is manual-only (password-gated button), like pricing — not wired into the weekly scheduler
from scenario import scenario_bp
from scenario import load_snapshot as scenario_load_snapshot
# scenario refresh is manual-only (password-gated button), like flows/pricing/payback —
# NOT wired into the weekly scheduler (keep Opus cost on-demand).
from company import company_bp
from company import load_snapshot as company_load_snapshot
# company (single-company deep-dive) refresh is manual-only (password-gated), like
# pricing/payback/scenario — NOT wired into the weekly scheduler (keep Opus cost on-demand).
from bottleneck_radar import bottleneck_bp
from bottleneck_radar import load_snapshot as bottleneck_load_snapshot
# bottleneck (deliverable-compute bottleneck radar) is a pure seed-engine card; refresh is
# manual-only (password-gated). NB: the package dir is bottleneck_radar/ NOT bottleneck/ —
# a dir literally named "bottleneck" shadows pandas' optional bottleneck dependency and
# breaks boot under gunicorn (repo root on sys.path). Keep this name.

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("macro-ai")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.register_blueprint(aibubble_bp)
app.register_blueprint(econ_bp)
app.register_blueprint(rival_bp)
app.register_blueprint(compute_bp)
app.register_blueprint(racks_bp)
app.register_blueprint(flows_bp)
app.register_blueprint(cwengine_bp)
app.register_blueprint(earnings_bp)
app.register_blueprint(pricing_bp)
app.register_blueprint(payback_bp)
app.register_blueprint(scenario_bp)
app.register_blueprint(company_bp)
app.register_blueprint(bottleneck_bp)

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
    "title":     {"en": "AI-Assisted External Market Analysis", "zh": "AI 輔助外部市場分析平台"},
    "brand":     {"en": "External Market Analysis",  "zh": "外部市場分析"},
    "eyebrow":   {"en": "AI-Assisted · External Market Intelligence", "zh": "AI 輔助 · 外部市場情報"},
    "subtitle":  {"en": "External signals across macro & AI markets",  "zh": "整合總體經濟與 AI 市場的外部訊號"},
    "enter":     {"en": "Enter",                      "zh": "進入"},
    "aib_name":  {"en": "Bubble Monitor",             "zh": "泡沫監控"},
    "aib_desc":  {"en": "Twin thermometer — market signals against leading frontier signals.",
                  "zh": "雙溫度計 — 市場訊號對照前沿領先訊號。"},
    "econ_name": {"en": "US Economic Monitor",        "zh": "美國經濟指標"},
    "econ_desc": {"en": "FRED indicators with week / month / quarter / year change.",
                  "zh": "FRED 指標，含 週 / 月 / 季 / 年 變化切換。"},
    "bn_name":   {"en": "Bottleneck Radar",           "zh": "瓶頸雷達"},
    "bn_desc":   {"en": "TSMC capacity is necessary but not sufficient — deliverable compute is set by the WEAKEST link (wafer / CoWoS / HBM / substrate / power / cooling / optics). Every link in one common unit, then take the minimum.",
                 "zh": "台積電產能是必要、但非充分 — 可交付算力由最弱一環決定（晶圓／CoWoS／HBM／載板／電力／散熱／光通訊）。每環換算成同一單位，再取最小值。"},
    "bn_lbl":    {"en": "binding",                    "zh": "綁定約束"},
    "rival_name":{"en": "Rival Radar",                "zh": "競爭者雷達"},
    "rival_desc":{"en": "Foundry rivals & customer-flow intelligence — tiered evidence, primary sources.",
                  "zh": "晶圓代工競爭格局與客戶流向 — 證據分級、一手來源。"},
    "compute_name":{"en": "AI Compute Demand Radar",  "zh": "AI 算力需求雷達"},
    "compute_desc":{"en": "Global AI CPU/GPU/ASIC demand worked backward from end demand — triangulated 3 ways + SEC EDGAR & FRED.",
                    "zh": "從終端需求回推全球 AI CPU/GPU/ASIC 需求 — 三鏡頭交叉驗證 ＋ SEC EDGAR、FRED。"},
    "compute_lbl":{"en": "2030E $B",                  "zh": "2030E $B"},
    "racks_name":{"en": "AI Rack BOM Radar",          "zh": "AI 機櫃 BOM 雷達"},
    "racks_desc":{"en": "Rack-scale system BOMs — GPUs/CPUs/HBM per rack and who supplies each part — fully sourced (T1/T2/T3).",
                  "zh": "Rack-scale 系統 BOM — 每櫃 GPU/CPU/HBM 數量與各零件供應商 — 全程附證據分級(T1/T2/T3)。"},
    "racks_lbl":{"en": "systems",                     "zh": "系統"},
    "flows_name":{"en": "Capital Flow Radar",         "zh": "資金流向雷達"},
    "flows_desc":{"en": "Money flow across cash, gold, crypto, bonds & equities → a read on where AI is heading. Five layers: live flow diagnosis + Claude scenarios.",
                  "zh": "現金/黃金/加密/債券/股票的資金流向 → 推估 AI 走向。五層:即時流向診斷 + Claude 情境。"},
    "flows_lbl":{"en": "marg. dir.",                  "zh": "邊際方向"},
    "events_lbl":{"en": "flow events",                "zh": "流向事件"},
    "cwe_name": {"en": "CapEx → Wafer Engine",        "zh": "CapEx → 晶圓引擎"},
    "cwe_desc": {"en": "Convert hyperscaler CapEx into implied leading-edge wafer demand — a versioned, regime-aware assumption graph with drift detection.",
                 "zh": "把雲端巨頭 CapEx 推估為隱含先進製程晶圓需求 — 帶版本、體制感知的假設圖，附漂移偵測。"},
    "cwe_lbl":  {"en": "wpm",                          "zh": "片/月"},
    "earn_name": {"en": "Supply-Chain Earnings Radar", "zh": "供應鏈法說雷達"},
    "earn_desc": {"en": "Supply-chain-aware earnings calendar across the AI semi chain (equipment → foundry → design → hyperscalers → power/cooling). 73 companies, UTC-stored / PT-shown, tier-filtered.",
                  "zh": "supply-chain-aware 法說行事曆，涵蓋 AI 半導體全鏈（設備→代工→設計→雲端→電力/散熱）。73 家公司，UTC 儲存 / PT 顯示，分層篩選。"},
    "earn_lbl":  {"en": "upcoming",                    "zh": "場法說"},
    "pricing_name":{"en": "Pricing Power Radar",       "zh": "議價能力雷達"},
    "pricing_desc":{"en": "Three-layer price stack — supplier cost → foundry ASP → customer ASP — scored into one CEO verdict: can we raise prices?",
                    "zh": "三層價格堆疊 — 供應商成本 → 代工 ASP → 客戶 ASP — 收斂成一個 CEO 結論:現在能不能漲價?"},
    "pricing_lbl":{"en": "/100",                       "zh": "/100"},
    "v_defensible":{"en": "defensible",                "zh": "定價權"},
    "v_neutral": {"en": "neutral",                     "zh": "中性"},
    "v_squeezed":{"en": "squeezed",                    "zh": "受擠壓"},
    "payback_name":{"en": "AI Capex Payback Radar",    "zh": "AI 資本支出回本雷達"},
    "payback_desc":{"en": "Is the hyperscalers' AI capex paying off yet? Capex vs revenue for Google · Meta · Microsoft · Amazon, plus OpenAI & Anthropic from fragmentary data — one coverage ratio answers it.",
                    "zh": "巨頭的 AI 資本支出回本了沒?Google · Meta · 微軟 · Amazon 的花錢 vs 賺錢,加上零碎情報拼出的 OpenAI 與 Anthropic — 一個覆蓋率回答。"},
    "payback_lbl":{"en": "coverage",                   "zh": "覆蓋率"},
    "v_monetizing":{"en": "monetizing",                "zh": "變現中"},
    "v_investing":{"en": "investing",                  "zh": "投入期"},
    "v_burning": {"en": "burning",                     "zh": "燒錢"},
    "scenario_name":{"en": "Scenario Radar",           "zh": "情境雷達"},
    "scenario_desc":{"en": "Synthesizes every platform below into the capital-market states that could come next — each with a model-assisted probability and the driver signals that move it.",
                     "zh": "把下方所有平台合成成接下來可能的資本市場狀態 — 每個狀態附 model-assisted 機率,以及推動它的 driver 訊號。"},
    "scenario_div_lbl":{"en": "divergences",           "zh": "項背離"},
    "company_name":{"en": "Amazon (AWS)",              "zh": "Amazon（AWS）"},
    "company_desc":{"en": "Four pillars: how AWS raises realized compute price · what to watch it with · how much it makes from AI · and how all of it rests on TSMC leading-edge & CoWoS.",
                    "zh": "四支柱:AWS 怎麼提高已實現算力售價 · 用什麼資料觀察 · 從 AI 賺多少 · 以及這一切如何押在台積電先進製程與 CoWoS。"},
    "company_nv_name":{"en": "NVIDIA",                  "zh": "NVIDIA（輝達）"},
    "company_nv_desc":{"en": "Four pillars on TSMC's anchor customer: how NVIDIA raises realized compute price (generational ASP · rack systems · CUDA lock-in) · what to watch it with · its AI revenue · and how ~100% of it is TSMC N4/N3 + CoWoS.",
                       "zh": "對台積電頭號客戶的四支柱:NVIDIA 怎麼提高已實現算力售價（世代 ASP · 機櫃整機 · CUDA 鎖定）· 用什麼觀察 · AI 營收多少 · 以及這一切如何幾乎 100% 是台積電 N4/N3＋CoWoS。"},
    "company_lbl":{"en": "/100 pricing power",         "zh": "/100 定價權"},
    "v_raising": {"en": "raising",                     "zh": "漲價中"},
    "v_holding": {"en": "holding",                     "zh": "持平"},
    "v_eroding": {"en": "eroding",                     "zh": "受壓"},
    "tier5":     {"en": "Company Deep-Dive",           "zh": "個股深掘"},
    "tier5_sub": {"en": "Zoom all the way into one company — and trace its AI benefit back to TSMC.",
                  "zh": "鑽進單一公司 — 再把它的 AI 利益一路追回台積電。"},
    # ── pyramid: thesis + tier headers (macro → industry) ──
    "thesis":    {"en": "One question — is AI a bubble? Read top-down: the macro economy at the apex, down through the AI capital cycle and demand, to the foundry supply chain and competitive strategy, and finally all the way into a single company.",
                  "zh": "一個問題:AI 是不是泡沫?由上而下閱讀 — 頂端是總體經濟,往下經過 AI 資本循環與需求,落到代工供應鏈與競爭策略,最後一路鑽進單一公司。"},
    "axis_top":  {"en": "MACRO ECONOMY",               "zh": "經濟面"},
    "axis_bottom":{"en": "INDUSTRY STRATEGY",          "zh": "產業分析"},
    "tier1":     {"en": "Macroeconomy",                "zh": "經濟面"},
    "tier1_sub": {"en": "The backdrop and the capstone — the macro cycle, plus one synthesis of every layer below into scenarios.",
                  "zh": "大環境與總綱 — 景氣循環,以及把下方每一層收斂成情境的綜合判讀。"},
    "tier2":     {"en": "Capital Flows & Bubble Heat", "zh": "資金流向與泡沫溫度"},
    "tier2_sub": {"en": "Where capital is flowing, and how hot the bubble runs — direction paired with temperature.",
                  "zh": "資金往哪裡流、泡沫燒得多熱 — 方向搭配溫度兩個面向。"},
    "tier3":     {"en": "AI Demand & Payback",         "zh": "AI 需求與回本"},
    "tier3_sub": {"en": "One arc, left to right: CapEx → silicon demand → leading-edge wafers → is it paying off yet?",
                  "zh": "一條主線,由左到右:資本支出 → 晶片需求 → 先進製程晶圓 → 回本了沒?"},
    "tier4":     {"en": "Supply Chain & Competitive Strategy", "zh": "供應鏈與產業策略"},
    "tier4_sub": {"en": "The foundry supply chain itself — rack BOMs, the binding bottleneck, the earnings calendar, pricing power and rivals.",
                  "zh": "代工供應鏈本身 — 機櫃 BOM、最弱環節瓶頸、法說行事曆、定價權與競爭者。"},
    "updated":   {"en": "Updated",                    "zh": "更新"},
    "indicators":{"en": "indicators",                 "zh": "指標"},
    "signout":   {"en": "Sign out",                   "zh": "登出"},
    "login_h":   {"en": "Sign in",                    "zh": "登入"},
    "login_sub": {"en": "Authorized access only",     "zh": "僅限授權存取"},
    "user":      {"en": "Username",                   "zh": "帳號"},
    "pass":      {"en": "Password",                   "zh": "密碼"},
    "login_btn": {"en": "Continue",                   "zh": "繼續"},
    "login_err": {"en": "Incorrect username or password.", "zh": "帳號或密碼錯誤。"},
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
    if request.path.startswith(("/api/", "/econ/api/", "/aibubble/api/", "/rival/api/", "/compute/api/", "/racks/api/", "/flows/api/", "/cwengine/api/", "/earnings/api/", "/pricing/api/", "/payback/api/", "/scenario/api/", "/bottleneck/api/")) or (request.path.startswith("/company/") and "/api/" in request.path):
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
    rival_kb = rival_load_kb()
    try:
        compute_snap = compute_load_snapshot()
    except Exception:
        compute_snap = None
    try:
        racks_snap = racks_load_snapshot()
    except Exception:
        racks_snap = None
    try:
        flows_snap = flows_load_snapshot()
    except Exception:
        flows_snap = None
    try:
        cwe_snap = cwengine_load_snapshot()
    except Exception:
        cwe_snap = None
    try:
        earnings_snap = earnings_load_snapshot()
    except Exception:
        earnings_snap = None
    try:
        pricing_snap = pricing_load_snapshot()
    except Exception:
        pricing_snap = None
    try:
        payback_snap = payback_load_snapshot()
    except Exception:
        payback_snap = None
    try:
        scenario_snap = scenario_load_snapshot()
    except Exception:
        scenario_snap = None
    try:
        company_snap = company_load_snapshot("amazon")
    except Exception:
        company_snap = None
    try:
        company_nv_snap = company_load_snapshot("nvidia")
    except Exception:
        company_nv_snap = None
    try:
        bottleneck_snap = bottleneck_load_snapshot()
    except Exception:
        bottleneck_snap = None
    return render_template(
        "portal.html",
        econ_updated=econ_snap.get("date") if econ_snap else None,
        econ_count=econ_snap.get("indicator_count") if econ_snap else 0,
        aib_updated=(aib_snap.get("generated_at") or "")[:10] if aib_snap else None,
        aib_score=((aib_snap.get("scores") or {}).get("composite")) if aib_snap else None,
        aib_zone=(((aib_snap.get("scores") or {}).get("zone") or {}).get("zh" if ui_lang() == "zh" else "en"))
        if aib_snap else None,
        rival_events=len(rival_kb.get("events") or []),
        rival_updated=rival_kb.get("research_date"),
        compute_updated=(compute_snap.get("generated_at") or "")[:10] if compute_snap else None,
        compute_2030=((compute_snap.get("headline") or {}).get("grand_total_end_year_usd_bn")) if compute_snap else None,
        racks_updated=(racks_snap.get("as_of") if racks_snap else None),
        racks_count=((racks_snap.get("summary") or {}).get("n_systems")) if racks_snap else None,
        flows_updated=(flows_snap.get("as_of") if flows_snap else None),
        flows_score=((flows_snap.get("l3") or {}).get("marginal_direction") or {}).get("score") if flows_snap else None,
        cwe_updated=(cwe_snap.get("as_of") if cwe_snap else None),
        cwe_wpm=((cwe_snap.get("inference") or {}).get("wafers_per_month")) if cwe_snap else None,
        earnings_updated=(earnings_snap.get("as_of") if earnings_snap else None),
        earnings_count=(earnings_snap.get("event_count") if earnings_snap else None),
        pricing_updated=(pricing_snap.get("as_of") if pricing_snap else None),
        pricing_score=((pricing_snap.get("pricing_power") or {}).get("score")) if pricing_snap else None,
        pricing_verdict=(((pricing_snap.get("pricing_power") or {}).get("verdict_key"))) if pricing_snap else None,
        payback_updated=(payback_snap.get("as_of") if payback_snap else None),
        payback_coverage=((payback_snap.get("headline") or {}).get("coverage")) if payback_snap else None,
        payback_verdict=((payback_snap.get("headline") or {}).get("verdict_key")) if payback_snap else None,
        scenario_updated=(scenario_snap.get("as_of") if scenario_snap else None),
        scenario_prob=((scenario_snap.get("headline") or {}).get("base_prob")) if scenario_snap else None,
        scenario_base_label=(((scenario_snap.get("headline") or {}).get("base_label") or {}).get("zh" if ui_lang() == "zh" else "en")) if scenario_snap else None,
        scenario_divergences=((scenario_snap.get("headline") or {}).get("divergence_count")) if scenario_snap else None,
        company_updated=(company_snap.get("as_of") if company_snap else None),
        company_score=((company_snap.get("headline") or {}).get("compute_pricing_score")) if company_snap else None,
        company_verdict=((company_snap.get("headline") or {}).get("verdict_key")) if company_snap else None,
        company_benefit=((company_snap.get("headline") or {}).get("ai_benefit_usd_bn")) if company_snap else None,
        company_name=(((company_snap.get("company") or {}).get("name_zh" if ui_lang() == "zh" else "name_en")) if company_snap else None),
        company_nv_updated=(company_nv_snap.get("as_of") if company_nv_snap else None),
        company_nv_score=((company_nv_snap.get("headline") or {}).get("compute_pricing_score")) if company_nv_snap else None,
        company_nv_verdict=((company_nv_snap.get("headline") or {}).get("verdict_key")) if company_nv_snap else None,
        company_nv_benefit=((company_nv_snap.get("headline") or {}).get("ai_benefit_usd_bn")) if company_nv_snap else None,
        bottleneck_updated=(bottleneck_snap.get("as_of") if bottleneck_snap else None),
        bottleneck_binding=(((bottleneck_snap.get("thesis") or {}).get("binding_name_zh" if ui_lang() == "zh" else "binding_name_en")) if bottleneck_snap else None),
        bottleneck_deliv=(((bottleneck_snap.get("thesis") or {}).get("deliverable_ea_qtr")) if bottleneck_snap else None),
    )


# ────────────────────────────── refresh / health ──────────────────────────────

def _refresh_all_bg() -> None:
    """Refresh all dashboards in the background (econ can take minutes)."""
    try:
        aibubble_fetcher.refresh()
    except Exception:
        log.exception("aibubble refresh failed")
    try:
        econ_refresh_job.run_weekly_refresh_sync()
    except Exception:
        log.exception("econ refresh failed")
    try:
        rival_refresh_live()
    except Exception:
        log.exception("rival live refresh failed")
    try:
        compute_refresh()
    except Exception:
        log.exception("compute refresh failed")
    try:
        racks_refresh()
    except Exception:
        log.exception("racks refresh failed")
    try:
        cwengine_refresh()
    except Exception:
        log.exception("cwengine refresh failed")
    try:
        earnings_refresh()
    except Exception:
        log.exception("earnings refresh failed")


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
