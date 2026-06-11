"""前瞻訊號層 Frontier Signals — 實體算力經濟的即時讀數。

設計目標：領先市場價格訊號 6–12 個月。六個子訊號：
  1. GPU 現貨租金（vast.ai 即時市場）—— 本輪的「暗光纖點亮率」
  2. 算力供需剪刀差（CapEx 年增 vs npm SDK 下載年增）
  3. 資料中心壓力事件雷達（新聞自動分類：退租/取消 vs 動工/擴建）
  4. 循環交易強度（vendor financing 種子台帳 + 新增事件）
  5. HN 徵才部署成熟度（AI 職缺生產期 vs 探索期）
  6. 收入缺口倍數（$6000 億問題即時版）

所有資料源免費、免金鑰（Claude 分類為選配升級）。任何單一來源失敗
都安靜降級（分數缺項自動重新歸一），不會讓整體更新失敗。
"""
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from .config import (
    AI_REVENUE_ESTIMATES,
    CIRC_KEYWORDS,
    CIRC_NEWS_QUERIES,
    CIRCULAR_DEALS_SEED,
    DC_EXPANSION_KEYWORDS,
    DC_NEWS_QUERIES,
    DC_STRESS_KEYWORDS,
    FRONTIER_META,
    FRONTIER_WEIGHTS,
    GPU_BREAKEVEN_BAND,
    GPU_PRICE_SEED_HISTORY,
    GPU_SPOT_MODELS,
    HN_AI_KEYWORDS,
    HN_PROD_KEYWORDS,
    NPM_PACKAGES,
    PYPI_PACKAGES,
)

log = logging.getLogger("aibubble.frontier")

UA = {"User-Agent": "Mozilla/5.0 (ai-bubble-monitor frontier)"}
NEWS_RSS = "https://news.google.com/rss/search"


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _scale(value, points):
    if value is None:
        return None
    if value <= points[0][0]:
        return float(points[0][1])
    if value >= points[-1][0]:
        return float(points[-1][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= value <= x1:
            return round(y0 + (value - x0) / (x1 - x0) * (y1 - y0), 1)
    return None


def _fmt(v, suffix="", digits=1, sign=False):
    if v is None:
        return "—"
    s = f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"
    return s + suffix


# ────────────────────── 1. GPU 現貨租金（vast.ai） ──────────────────────

def fetch_gpu_spot(prev: dict | None) -> dict | None:
    gpus = {}
    for m in GPU_SPOT_MODELS:
        try:
            resp = requests.post(
                "https://console.vast.ai/api/v0/bundles/",
                json={
                    "gpu_name": {"eq": m["vast_name"]},
                    "num_gpus": {"eq": 1},
                    "rentable": {"eq": True},
                    "type": "on-demand",
                    "limit": 40,
                    "order": [["dph_total", "asc"]],
                },
                timeout=20, headers=UA,
            )
            resp.raise_for_status()
            prices = [o.get("dph_total") for o in resp.json().get("offers", [])
                      if o.get("dph_total")]
            if prices:
                gpus[m["key"]] = {
                    "zh": m["zh"],
                    "median": round(_median(prices), 3),
                    "min": round(min(prices), 3),
                    "offers": len(prices),
                }
        except Exception:
            log.warning("frontier: vast.ai fetch failed for %s", m["vast_name"], exc_info=True)
    if not gpus:
        return None

    # 價格歷史：種子錨點 + 既有快照累積點 + 本期即時點（同月去重）。
    history = list(GPU_PRICE_SEED_HISTORY)
    prev_hist = ((prev or {}).get("gpu_spot") or {}).get("history") or []
    seed_dates = {h["date"] for h in history}
    for h in prev_hist:
        if h["date"] not in seed_dates:
            history.append(h)
            seed_dates.add(h["date"])
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    point = {"date": this_month,
             "h100": (gpus.get("h100") or {}).get("median"),
             "a100": (gpus.get("a100") or {}).get("median"),
             "live": True}
    history = [h for h in history if h["date"] != this_month] + [point]
    history.sort(key=lambda h: h["date"])
    return {"gpus": gpus, "history": history,
            "breakeven_band": list(GPU_BREAKEVEN_BAND),
            "asof": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# ────────────────── 2. 開發者採用脈搏（npm / PyPI） ──────────────────

def _npm_one(pkg: str) -> dict | None:
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=364)
    url = f"https://api.npmjs.org/downloads/range/{start}:{end}/{requests.utils.quote(pkg, safe='')}"
    resp = requests.get(url, timeout=20, headers=UA)
    resp.raise_for_status()
    days = resp.json().get("downloads", [])
    if len(days) < 200:
        return None
    counts = [d["downloads"] for d in days]

    def wsum(offset_from_end):  # 該時點往前 7 天合計
        seg = counts[len(counts) - offset_from_end - 7: len(counts) - offset_from_end]
        return sum(seg) if len(seg) == 7 else None

    now_w, m6_w, y1_w = wsum(0), wsum(182), wsum(357)
    # 週頻序列（給走勢圖）：每 7 天一點。
    weekly = [sum(counts[i:i + 7]) for i in range(0, len(counts) - 6, 7)]
    return {
        "weekly_now": now_w, "weekly_6m": m6_w, "weekly_1y": y1_w,
        "g6m": round((now_w / m6_w - 1) * 100, 1) if (now_w and m6_w) else None,
        "g1y": round((now_w / y1_w - 1) * 100, 1) if (now_w and y1_w) else None,
        "series": weekly,
    }


def fetch_adoption() -> dict | None:
    packages = {}
    for p in NPM_PACKAGES:
        try:
            d = _npm_one(p["pkg"])
            if d:
                packages[p["pkg"]] = dict(d, zh=p["zh"])
        except Exception:
            log.warning("frontier: npm fetch failed for %s", p["pkg"], exc_info=True)
    pypi = {}
    for pkg in PYPI_PACKAGES:
        try:
            resp = requests.get(f"https://pypistats.org/api/packages/{pkg}/recent",
                                timeout=12, headers=UA)
            if resp.status_code == 200:
                pypi[pkg] = resp.json().get("data", {}).get("last_week")
        except Exception:
            pass  # 常態限流，安靜跳過
    if not packages:
        return None
    g1ys = [p["g1y"] for p in packages.values() if p.get("g1y") is not None]
    g6ms = [p["g6m"] for p in packages.values() if p.get("g6m") is not None]
    return {"packages": packages, "pypi_last_week": pypi or None,
            "avg_g1y": round(sum(g1ys) / len(g1ys), 1) if g1ys else None,
            "avg_g6m": round(sum(g6ms) / len(g6ms), 1) if g6ms else None}


# ──────────────── 3+4. 新聞雷達（壓力事件 / 循環交易） ────────────────

def _rss_items(query_cfg: dict, n: int = 20) -> list[dict]:
    try:
        resp = requests.get(
            NEWS_RSS,
            params={"q": query_cfg["query"], "hl": query_cfg["hl"],
                    "gl": query_cfg["gl"], "ceid": query_cfg["ceid"]},
            timeout=12, headers=UA,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            src = item.find("{https://news.google.com}source")
            source = src.text.strip() if src is not None and src.text else ""
            if title and link:
                items.append({"title": title, "link": link, "pubDate": pub,
                              "source": source, "lang": query_cfg["lang"]})
            if len(items) >= n:
                break
        return items
    except Exception:
        log.warning("frontier: rss fetch failed (%s)", query_cfg.get("query", "")[:40],
                    exc_info=True)
        return []


def _kw_label(title: str, stress_kw, expand_kw) -> str:
    t = title.lower()
    s = any(k in t for k in stress_kw)
    e = any(k in t for k in expand_kw)
    if s and not e:
        return "stress"
    if e and not s:
        return "expansion"
    if s and e:
        return "stress"   # 同現時保守視為壓力
    return "neutral"


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {"type": "string", "enum": ["stress", "expansion", "neutral"]},
            "description": "與輸入標題等長、一一對應的分類",
        }
    },
    "required": ["labels"],
    "additionalProperties": False,
}


def _claude_classify_dc(titles: list[str]) -> list[str] | None:
    """Claude 升級分類（有 ANTHROPIC_API_KEY 時）；失敗回 None 改用規則。"""
    if not os.environ.get("ANTHROPIC_API_KEY") or not titles:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic()
        prompt = (
            "以下是資料中心相關新聞標題清單（JSON 陣列）。請逐一分類：\n"
            "stress = 取消/暫停/退租/延後/縮減/需求疑慮等負面供給訊號；\n"
            "expansion = 動工/擴建/簽約/啟用/新投資等正面供給訊號；\n"
            "neutral = 其他。回傳與輸入等長的 labels 陣列。\n\n"
            + json.dumps(titles, ensure_ascii=False)
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _CLASSIFY_SCHEMA}},
        )
        text = next(b.text for b in resp.content if b.type == "text")
        labels = json.loads(text)["labels"]
        if len(labels) == len(titles):
            return labels
    except Exception:
        log.warning("frontier: Claude classify failed — falling back to rules", exc_info=True)
    return None


def fetch_dc_stress() -> dict | None:
    items = []
    for q in DC_NEWS_QUERIES:
        items += _rss_items(q, n=20)
    if not items:
        return None
    # 去重（同標題）。
    seen, uniq = set(), []
    for it in items:
        k = it["title"][:60]
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    labels = _claude_classify_dc([it["title"] for it in uniq])
    classifier = "ai" if labels else "rules"
    for i, it in enumerate(uniq):
        it["label"] = labels[i] if labels else _kw_label(
            it["title"], [k.lower() for k in DC_STRESS_KEYWORDS],
            [k.lower() for k in DC_EXPANSION_KEYWORDS])
    stress = [it for it in uniq if it["label"] == "stress"]
    expand = [it for it in uniq if it["label"] == "expansion"]
    denom = len(stress) + len(expand)
    ratio = round(len(stress) / denom, 3) if denom else None
    show = stress[:8] + expand[:6]
    return {"stress_n": len(stress), "expand_n": len(expand),
            "neutral_n": len(uniq) - denom, "ratio": ratio,
            "classifier": classifier, "items": show}


def fetch_circularity() -> dict:
    items = []
    for q in CIRC_NEWS_QUERIES:
        items += _rss_items(q, n=12)
    kw = [k.lower() for k in CIRC_KEYWORDS]
    fresh = [it for it in items if any(k in it["title"].lower() for k in kw)][:8]
    seed_total = round(sum(d["value_b"] for d in CIRCULAR_DEALS_SEED), 1)
    return {"seed_deals": CIRCULAR_DEALS_SEED, "seed_total_b": seed_total,
            "fresh_items": fresh, "fresh_n": len(fresh)}


# ──────────────── 5. HN「Who is hiring」徵才成熟度 ────────────────

def fetch_hn_maturity() -> dict | None:
    try:
        resp = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"query": '"Ask HN: Who is hiring"', "tags": "story", "hitsPerPage": 5},
            timeout=15, headers=UA,
        )
        resp.raise_for_status()
        hits = [h for h in resp.json().get("hits", [])
                if h.get("author") == "whoishiring" and "hiring?" in (h.get("title") or "")]
        if not hits:
            return None
        story = hits[0]
        sid, title = story["objectID"], story["title"]
        month = re.search(r"\(([^)]+)\)", title)
        comments, page = [], 0
        while page < 3:  # 最多 ~3000 帖
            r2 = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={"tags": f"comment,story_{sid}", "hitsPerPage": 1000, "page": page},
                timeout=20, headers=UA,
            )
            r2.raise_for_status()
            batch = r2.json().get("hits", [])
            comments += batch
            if len(batch) < 1000:
                break
            page += 1
        top = [c for c in comments if str(c.get("parent_id")) == str(sid)]
        ai_kw = [k.lower() for k in HN_AI_KEYWORDS]
        prod_kw = [k.lower() for k in HN_PROD_KEYWORDS]
        total = len(top)
        ai_n = prod_n = 0
        for c in top:
            text = ((c.get("comment_text") or "") + " ").lower()
            if any(k in text for k in ai_kw):
                ai_n += 1
                if any(k in text for k in prod_kw):
                    prod_n += 1
        if total < 20:
            return None
        return {
            "month": month.group(1) if month else None,
            "story_title": title,
            "total_posts": total,
            "ai_posts": ai_n,
            "ai_share": round(ai_n / total * 100, 1),
            "prod_posts": prod_n,
            "prod_share": round(prod_n / ai_n * 100, 1) if ai_n else None,
        }
    except Exception:
        log.warning("frontier: HN maturity fetch failed", exc_info=True)
        return None


# ──────────────── 6. 收入缺口倍數（$6000 億問題） ────────────────

def build_revenue_gap(capex_agg: dict | None) -> dict | None:
    if not capex_agg or not capex_agg.get("annual_run_rate_t"):
        return None
    capex_t = capex_agg["annual_run_rate_t"]
    required_t = round(capex_t * 2, 2)            # Sequoia 倍數法（×2）
    est_b = round(sum(e["value_b"] for e in AI_REVENUE_ESTIMATES), 1)
    multiple = round(required_t * 1000 / est_b, 1) if est_b else None
    return {"capex_annual_t": capex_t, "required_t": required_t,
            "est_revenue_b": est_b, "items": AI_REVENUE_ESTIMATES,
            "multiple": multiple}


# ──────────────────────────── 評分 ────────────────────────────

def build_frontier_scores(gpu, adoption, dc, circ, hn, gap, capex_agg) -> dict:
    raw = {}

    h100 = ((gpu or {}).get("gpus", {}).get("h100") or {}).get("median")
    raw["gpu_spot"] = (
        _scale(h100, [(1.2, 95), (1.5, 85), (2.0, 65), (2.5, 50), (3.0, 35), (4.0, 15)])
        if h100 is not None else None,
        [{"zh": "H100 現貨中位數（vast.ai on-demand）", "v": _fmt(h100, " $/hr", 2)},
         {"zh": "A100 現貨中位數", "v": _fmt(((gpu or {}).get('gpus', {}).get('a100') or {}).get('median'), " $/hr", 2)},
         {"zh": "回本帶（全成本粗估）", "v": f"${GPU_BREAKEVEN_BAND[0]}–{GPU_BREAKEVEN_BAND[1]}/hr"}],
    )

    supply = (capex_agg or {}).get("yoy")
    demand = (adoption or {}).get("avg_g1y")
    scissor = round(supply - demand, 1) if (supply is not None and demand is not None) else None
    raw["scissors"] = (
        _scale(scissor, [(-300, 8), (-150, 20), (-50, 35), (0, 50), (50, 75), (150, 92)]),
        [{"zh": "供給端：CapEx 年增率", "v": _fmt(supply, "%", 1, True)},
         {"zh": "需求端：SDK 下載年增率（npm 平均）", "v": _fmt(demand, "%", 1, True)},
         {"zh": "剪刀差（供給−需求）", "v": _fmt(scissor, " 個百分點", 1, True)}],
    )

    ratio = (dc or {}).get("ratio")
    raw["dc_stress"] = (
        _scale(ratio, [(0.0, 15), (0.2, 35), (0.4, 60), (0.6, 80), (0.8, 95)])
        if ratio is not None else None,
        [{"zh": "壓力事件（取消/退租/延後）", "v": _fmt((dc or {}).get('stress_n'), " 則", 0)},
         {"zh": "擴張事件（動工/簽約/擴建）", "v": _fmt((dc or {}).get('expand_n'), " 則", 0)},
         {"zh": "壓力比率", "v": _fmt(ratio * 100 if ratio is not None else None, "%", 0)}],
    )

    fresh = (circ or {}).get("fresh_n", 0)
    circ_score = min(95.0, 55.0 + 5.0 * fresh)     # 種子台帳已達思科級 → 基準 55
    raw["circularity"] = (
        circ_score,
        [{"zh": "已揭露循環結構累計（種子台帳）", "v": f"~${(circ or {}).get('seed_total_b', 0):.0f}B"},
         {"zh": "本期新增相關報導", "v": f"{fresh} 則"}],
    )

    prod_share = (hn or {}).get("prod_share")
    raw["hn_maturity"] = (
        _scale(prod_share, [(5, 85), (15, 65), (30, 45), (50, 25), (70, 12)])
        if prod_share is not None else None,
        [{"zh": "本月 AI 職缺占比", "v": _fmt((hn or {}).get('ai_share'), "%")},
         {"zh": "AI 職缺中生產期比例", "v": _fmt(prod_share, "%")},
         {"zh": "樣本（頂層徵才帖）", "v": _fmt((hn or {}).get('total_posts'), " 帖", 0)}],
    )

    mult = (gap or {}).get("multiple")
    raw["revenue_gap"] = (
        _scale(mult, [(1, 15), (2, 35), (3, 50), (5, 70), (8, 88), (12, 96)])
        if mult is not None else None,
        [{"zh": "隱含必要收入（CapEx×2 年化）", "v": f"~${(gap or {}).get('required_t', 0):.2f}T"},
         {"zh": "目前 AI 終端收入估計", "v": f"~${(gap or {}).get('est_revenue_b', 0):.0f}B"},
         {"zh": "缺口倍數", "v": _fmt(mult, "x")}],
    )

    subs, weighted, wsum = [], 0.0, 0.0
    for key, (sc, inputs) in raw.items():
        meta = FRONTIER_META[key]
        level = "na" if sc is None else ("good" if sc < 40 else "warn" if sc < 65 else "bad")
        subs.append({"key": key, "zh": meta["zh"], "en": meta["en"],
                     "note_zh": meta["note_zh"], "score": sc, "level": level,
                     "weight": FRONTIER_WEIGHTS[key], "inputs": inputs})
        if sc is not None:
            weighted += sc * FRONTIER_WEIGHTS[key]
            wsum += FRONTIER_WEIGHTS[key]
    composite = round(weighted / wsum, 1) if wsum else None
    return {"composite": composite, "subs": subs}


# ──────────────────────────── 組裝 ────────────────────────────

def build_frontier(capex_agg: dict | None, prev_frontier: dict | None) -> dict:
    gpu = fetch_gpu_spot(prev_frontier)
    adoption = fetch_adoption()
    dc = fetch_dc_stress()
    circ = fetch_circularity()
    hn = fetch_hn_maturity()
    gap = build_revenue_gap(capex_agg)
    scores = build_frontier_scores(gpu, adoption, dc, circ, hn, gap, capex_agg)
    return {
        "gpu_spot": gpu,
        "adoption": adoption,
        "dc_stress": dc,
        "circularity": circ,
        "hn": hn,
        "revenue_gap": gap,
        "scores": scores,
    }
