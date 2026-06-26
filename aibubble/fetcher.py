"""AI Bubble Monitor — 資料抓取與泡沫溫度計算引擎。

單一快照（data/aibubble/snapshot.json）驅動整個儀表板：
  行情（1 年日線批次）→ 基本面（PE/PS/市值）→ 巨頭季度資本支出
  → 六大子訊號評分 → 綜合泡沫溫度 → AI 執行摘要（Claude，規則備援）
  → 繁中新聞雷達。

所有評分皆為「0 = 冷靜、100 = 極端泡沫」的分段線性映射，
映射節點寫死在本檔，方法論完全透明、可重現。
"""
import json
import logging
import os
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
import yfinance as yf

from . import frontier as frontier_mod
from .config import (
    CAPEX_TICKERS,
    FRED_HY_OAS,
    FUNDAMENTAL_GROUP_KEYS,
    GROUPS,
    MOMENTUM_BASKET,
    NEWS_TOPICS,
    NO_FUNDAMENTALS,
    POWER_BASKET,
    REIT_BASKET,
    SCORE_WEIGHTS,
    SIGNAL_META,
    SMR_SPECULATIVE,
    VALUATION_BASKET,
    ZONES,
)

log = logging.getLogger("aibubble")

DATA_DIR = Path(__file__).parent.parent / "data" / "aibubble"
SNAPSHOT_PATH = DATA_DIR / "snapshot.json"

_refresh_lock = threading.Lock()

NEWS_RSS = "https://news.google.com/rss/search"
NEWS_PER_TOPIC = 6
SPARK_POINTS = 40
REL_SAMPLE = 3      # 相對表現圖每 3 個交易日取一點


def all_ticker_meta() -> list[dict]:
    metas = []
    for g in GROUPS:
        for t in g["tickers"]:
            metas.append(dict(t, group=g["key"]))
    return metas


def _pct(now: float, then: float) -> float | None:
    if not then:
        return None
    return round((now / then - 1.0) * 100.0, 2)


# ────────────────────────── 行情 ──────────────────────────

def _quote_from_closes(closes) -> dict | None:
    closes = closes.dropna()
    if len(closes) < 6:
        return None
    last = float(closes.iloc[-1])
    year = closes.index[-1].year
    ytd_base = closes[closes.index.year < year]
    spark = [round(float(v), 4) for v in closes.iloc[-SPARK_POINTS:]]
    hi_52w = float(closes.max())
    dma200 = float(closes.iloc[-200:].mean()) if len(closes) >= 150 else None
    return {
        "price": round(last, 4),
        "chg_1d": _pct(last, float(closes.iloc[-2])),
        "chg_1w": _pct(last, float(closes.iloc[-6])) if len(closes) >= 6 else None,
        "chg_1m": _pct(last, float(closes.iloc[-22])) if len(closes) >= 22 else None,
        "chg_6m": _pct(last, float(closes.iloc[-127])) if len(closes) >= 127 else None,
        "chg_1y": _pct(last, float(closes.iloc[0])) if len(closes) >= 230 else None,
        "chg_ytd": _pct(last, float(ytd_base.iloc[-1])) if len(ytd_base) else None,
        "off_high": _pct(last, hi_52w),
        "vs_200dma": _pct(last, dma200) if dma200 else None,
        "spark": spark,
        "asof": str(closes.index[-1].date()),
    }


def _download_all():
    symbols = [m["ticker"] for m in all_ticker_meta()]
    df = yf.download(
        symbols, period="1y", interval="1d",
        group_by="ticker", auto_adjust=True, threads=True, progress=False,
    )
    return df, symbols


def _closes(df, sym, n_symbols: int):
    try:
        s = df[sym]["Close"] if n_symbols > 1 else df["Close"]
        s = s.dropna()
        return s if len(s) >= 6 else None
    except Exception:
        return None


def build_quotes(df, n_symbols: int) -> dict[str, dict]:
    quotes: dict[str, dict] = {}
    for m in all_ticker_meta():
        s = _closes(df, m["ticker"], n_symbols)
        if s is None:
            log.warning("aibubble: no usable data for %s", m["ticker"])
            continue
        q = _quote_from_closes(s)
        if q:
            quotes[m["ticker"]] = q
    return quotes


# ────────────────────────── 基本面 ──────────────────────────

def _fundamentals_one(sym: str):
    try:
        info = yf.Ticker(sym).info or {}
        return sym, {
            "mcap": info.get("marketCap"),
            "pe": info.get("trailingPE"),
            "fwd_pe": info.get("forwardPE"),
            "ps": info.get("priceToSalesTrailing12Months"),
            "rev_growth": info.get("revenueGrowth"),
            "gross_margin": info.get("grossMargins"),
            "op_margin": info.get("operatingMargins"),
        }
    except Exception:
        log.warning("aibubble: fundamentals failed for %s", sym)
        return sym, None


def fetch_fundamentals() -> dict[str, dict]:
    syms = [
        t["ticker"]
        for g in GROUPS if g["key"] in FUNDAMENTAL_GROUP_KEYS
        for t in g["tickers"]
        if t["ticker"] not in NO_FUNDAMENTALS
    ]
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for sym, f in ex.map(_fundamentals_one, syms):
            if f and any(v is not None for v in f.values()):
                if f.get("mcap"):
                    f["mcap_usd"] = round(f["mcap"] / 1e9, 1)   # 美元計價市場，直接折十億
                out[sym] = f
    return out


# ────────────────────────── 巨頭資本支出 ──────────────────────────

def _capex_one(sym: str):
    """單一公司近 5 季：資本支出 / 營運現金流 / 營收（十億美元）。"""
    try:
        tk = yf.Ticker(sym)
        cf = tk.quarterly_cashflow
        inc = tk.quarterly_income_stmt
        capex = cf.loc["Capital Expenditure"].dropna() if "Capital Expenditure" in cf.index else None
        ocf = cf.loc["Operating Cash Flow"].dropna() if "Operating Cash Flow" in cf.index else None
        rev = inc.loc["Total Revenue"].dropna() if "Total Revenue" in inc.index else None
        if capex is None or len(capex) < 2:
            return sym, None
        rows = []
        for ts in sorted(capex.index):
            q = f"{ts.year}Q{(ts.month - 1) // 3 + 1}"
            rows.append({
                "q": q,
                "date": str(ts.date()),
                "capex_b": round(abs(float(capex[ts])) / 1e9, 2),
                "ocf_b": round(float(ocf[ts]) / 1e9, 2) if ocf is not None and ts in ocf.index else None,
                "rev_b": round(float(rev[ts]) / 1e9, 2) if rev is not None and ts in rev.index else None,
            })
        latest = rows[-1]
        yoy = None
        for r in rows:
            if r["q"][:4] == str(int(latest["q"][:4]) - 1) and r["q"][4:] == latest["q"][4:]:
                yoy = _pct(latest["capex_b"], r["capex_b"])
        # TTM（近 4 季合計）— 對單季尖峰與財年季錯位更穩健的比率分母。
        ttm = rows[-4:] if len(rows) >= 4 else rows
        ttm_capex = round(sum(r["capex_b"] for r in ttm), 2)
        ttm_ocf_rows = [r for r in ttm if r.get("ocf_b")]
        ttm_rev_rows = [r for r in ttm if r.get("rev_b")]
        ttm_ocf = round(sum(r["ocf_b"] for r in ttm_ocf_rows), 2) if ttm_ocf_rows else None
        ttm_rev = round(sum(r["rev_b"] for r in ttm_rev_rows), 2) if ttm_rev_rows else None
        return sym, {
            "quarters": rows,
            "latest_q": latest["q"],
            "latest_capex_b": latest["capex_b"],
            "yoy": yoy,
            "ttm_capex_b": ttm_capex,
            "ttm_n": len(ttm),
            "capex_ocf": round(latest["capex_b"] / latest["ocf_b"] * 100, 1)
                if latest.get("ocf_b") else None,
            "capex_rev": round(latest["capex_b"] / latest["rev_b"] * 100, 1)
                if latest.get("rev_b") else None,
            "ttm_capex_ocf": round(ttm_capex / ttm_ocf * 100, 1) if ttm_ocf else None,
            "ttm_capex_rev": round(ttm_capex / ttm_rev * 100, 1) if ttm_rev else None,
        }
    except Exception:
        log.warning("aibubble: capex fetch failed for %s", sym, exc_info=True)
        return sym, None


def fetch_capex() -> dict:
    """五大巨頭逐家 + 合計（僅累計同時有今年/去年同季資料的公司）。"""
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for sym, c in ex.map(_capex_one, CAPEX_TICKERS):
            if c:
                out[sym] = c

    agg = None
    if out:
        latest_total = round(sum(c["latest_capex_b"] for c in out.values()), 1)
        # 單一公司（如 ORCL 單季 YoY +218%、CapEx/營收 108%、/OCF 261%）會扭曲合計。
        # 對策：(a) 合計 YoY 對每家 YoY 設上限（winsorize）再彙總；
        #       (b) 比率（/OCF、/營收）改用 TTM 合計分母，淡化單季尖峰與財年季錯位。
        YOY_CAP = 150.0   # 單一公司 YoY 貢獻上限（百分點），避免極端值主導
        yoy_pairs = []
        for c in out.values():
            if c.get("yoy") is None:
                continue
            yoy_capped = min(c["yoy"], YOY_CAP)
            yoy_pairs.append((c["latest_capex_b"], c["latest_capex_b"] / (1 + yoy_capped / 100)))
        agg_yoy = None
        if yoy_pairs:
            now_sum = sum(p[0] for p in yoy_pairs)
            then_sum = sum(p[1] for p in yoy_pairs)
            agg_yoy = _pct(now_sum, then_sum)
        # TTM 合計比率：對單季波動（含 ORCL/AMZN 的 OCF 大幅震盪）穩健。
        ttm_capex_total = sum(c["ttm_capex_b"] for c in out.values() if c.get("ttm_capex_b"))
        ocf_num = sum(c["ttm_capex_b"] for c in out.values() if c.get("ttm_capex_ocf"))
        ocf_den = sum(c["ttm_capex_b"] / (c["ttm_capex_ocf"] / 100)
                      for c in out.values() if c.get("ttm_capex_ocf"))
        agg_ocf = round(ocf_num / ocf_den * 100, 1) if ocf_den else None
        rev_num = sum(c["ttm_capex_b"] for c in out.values() if c.get("ttm_capex_rev"))
        rev_den = sum(c["ttm_capex_b"] / (c["ttm_capex_rev"] / 100)
                      for c in out.values() if c.get("ttm_capex_rev"))
        agg_rev = round(rev_num / rev_den * 100, 1) if rev_den else None
        annual_run_rate = round(latest_total * 4 / 1000, 2)  # 兆美元年化
        agg = {
            "latest_total_b": latest_total,
            "yoy": agg_yoy,
            "capex_ocf": agg_ocf,
            "capex_rev": agg_rev,
            "ttm_capex_total_b": round(ttm_capex_total, 1) if ttm_capex_total else None,
            "annual_run_rate_t": annual_run_rate,
            "companies": len(out),
        }
    return {"companies": out, "agg": agg}


# ────────────────────────── FRED（選配） ──────────────────────────

def fetch_hy_oas() -> dict | None:
    """ICE BofA 美國高收益債 OAS（基點）。需 FRED_API_KEY，失敗安靜略過。"""
    key = os.environ.get("FRED_API_KEY", "")
    if not key:
        return None
    try:
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": FRED_HY_OAS, "api_key": key, "file_type": "json",
                    "sort_order": "desc", "limit": 130},
            timeout=15,
        )
        resp.raise_for_status()
        obs = [o for o in resp.json().get("observations", []) if o.get("value") not in (".", None)]
        if not obs:
            return None
        latest = float(obs[0]["value"]) * 100          # 百分點 → 基點
        ago_3m = float(obs[65]["value"]) * 100 if len(obs) > 65 else None
        return {"bps": round(latest), "bps_3m_ago": round(ago_3m) if ago_3m else None,
                "asof": obs[0]["date"]}
    except Exception:
        log.warning("aibubble: FRED HY OAS fetch failed", exc_info=True)
        return None


# ────────────────────────── 評分引擎 ──────────────────────────

def _scale(value, points):
    """分段線性映射：points = [(x, score), ...]，x 遞增。

    中間值保留 4 位小數（避免逐錨點四捨五入後再平均產生 ~0.5 累積差），
    最終顯示精度統一交由 _avg 收斂為 1 位。
    """
    if value is None:
        return None
    if value <= points[0][0]:
        return float(points[0][1])
    if value >= points[-1][0]:
        return float(points[-1][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= value <= x1:
            return round(y0 + (value - x0) / (x1 - x0) * (y1 - y0), 4)
    return None


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _basket_avg(quotes, tickers, field):
    return _avg([(quotes.get(t) or {}).get(field) for t in tickers])


def _fmt(v, suffix="", digits=1, sign=False):
    if v is None:
        return "—"
    s = f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"
    return s + suffix


def score_valuation(fundamentals):
    pes = [f.get("pe") for s, f in fundamentals.items() if s in VALUATION_BASKET]
    pss = [f.get("ps") for s, f in fundamentals.items() if s in VALUATION_BASKET]
    med_pe, med_ps = _median(pes), _median(pss)
    nvda_ps = (fundamentals.get("NVDA") or {}).get("ps")
    sc = _avg([
        _scale(med_pe, [(15, 10), (25, 30), (35, 55), (50, 75), (70, 92), (90, 100)]),
        _scale(med_ps, [(3, 10), (6, 30), (10, 55), (15, 75), (25, 95)]),
        _scale(nvda_ps, [(8, 20), (15, 45), (25, 70), (35, 90)]),
    ])
    inputs = [
        {"zh": "AI 核心股 PE 中位數", "v": _fmt(med_pe, "x")},
        {"zh": "AI 核心股 PS 中位數", "v": _fmt(med_ps, "x")},
        {"zh": "輝達 PS（TTM）", "v": _fmt(nvda_ps, "x")},
    ]
    return sc, inputs


def score_momentum(quotes):
    y1 = _basket_avg(quotes, MOMENTUM_BASKET, "chg_1y")
    m6 = _basket_avg(quotes, MOMENTUM_BASKET, "chg_6m")
    spy6 = (quotes.get("SPY") or {}).get("chg_6m")
    excess6 = round(m6 - spy6, 2) if (m6 is not None and spy6 is not None) else None
    # 延伸上界：6M / 超額在本輪極端行情常衝破舊頂錨（80% / 50%）被夾 95，喪失解析度。
    sc = _avg([
        _scale(y1, [(0, 15), (30, 40), (60, 65), (100, 82), (200, 95), (350, 100)]),
        _scale(m6, [(0, 15), (20, 45), (40, 68), (80, 88), (130, 97), (180, 100)]),
        _scale(excess6, [(-10, 10), (0, 25), (15, 48), (30, 68), (60, 88), (110, 100)]),
    ])
    inputs = [
        {"zh": "AI 籃子近 1 年平均漲幅", "v": _fmt(y1, "%", 1, True)},
        {"zh": "AI 籃子近 6 月平均漲幅", "v": _fmt(m6, "%", 1, True)},
        {"zh": "近 6 月相對標普超額", "v": _fmt(excess6, "%", 1, True)},
    ]
    return sc, inputs


def score_capex(capex):
    agg = capex.get("agg") or {}
    yoy, ocf, rev = agg.get("yoy"), agg.get("capex_ocf"), agg.get("capex_rev")
    sc = _avg([
        _scale(yoy, [(0, 15), (20, 35), (40, 60), (60, 80), (90, 95)]),
        _scale(ocf, [(30, 15), (50, 40), (70, 70), (85, 90), (100, 100)]),
        _scale(rev, [(10, 15), (20, 45), (30, 70), (40, 90)]),
    ])
    inputs = [
        {"zh": "五巨頭最新季 CapEx 合計", "v": _fmt(agg.get("latest_total_b"), "B", 1) + "（年化 ~$" + _fmt(agg.get("annual_run_rate_t"), "T", 2) + "）"},
        {"zh": "CapEx 年增率（合計）", "v": _fmt(yoy, "%", 1, True)},
        {"zh": "CapEx / 營運現金流", "v": _fmt(ocf, "%")},
        {"zh": "CapEx / 營收", "v": _fmt(rev, "%")},
    ]
    return sc, inputs


def score_infra(quotes):
    power1y = _basket_avg(quotes, POWER_BASKET, "chg_1y")
    xlu1y = (quotes.get("XLU") or {}).get("chg_1y")
    power_ex = round(power1y - xlu1y, 2) if (power1y is not None and xlu1y is not None) else None
    smr1y = _basket_avg(quotes, SMR_SPECULATIVE, "chg_1y")
    reit6 = _basket_avg(quotes, REIT_BASKET, "chg_6m")
    vnq6 = (quotes.get("VNQ") or {}).get("chg_6m")
    reit_ex = round(reit6 - vnq6, 2) if (reit6 is not None and vnq6 is not None) else None
    sc = _avg([
        _scale(power_ex, [(-10, 10), (0, 25), (30, 50), (60, 75), (120, 95)]),
        _scale(smr1y, [(0, 20), (50, 45), (100, 70), (250, 95)]),
        _scale(reit_ex, [(-10, 15), (0, 30), (10, 55), (25, 80), (40, 95)]),
    ])
    inputs = [
        {"zh": "電力股 1 年超額（vs 公用事業）", "v": _fmt(power_ex, "%", 1, True)},
        {"zh": "SMR 投機股（Oklo/NuScale）1 年", "v": _fmt(smr1y, "%", 1, True)},
        {"zh": "資料中心 REIT 6 月超額（vs REIT 大盤）", "v": _fmt(reit_ex, "%", 1, True)},
    ]
    return sc, inputs


def score_credit(quotes, hy_oas):
    parts, inputs = [], []
    if hy_oas and hy_oas.get("bps") is not None:
        parts.append(_scale(hy_oas["bps"], [(250, 90), (300, 75), (350, 55), (450, 35), (600, 15)]))
        inputs.append({"zh": "高收益債利差 HY OAS（FRED）",
                       "v": f"{hy_oas['bps']} bps（越窄越自滿）"})
    else:
        hyg6 = (quotes.get("HYG") or {}).get("chg_6m")
        ief6 = (quotes.get("IEF") or {}).get("chg_6m")
        rel = round(hyg6 - ief6, 2) if (hyg6 is not None and ief6 is not None) else None
        parts.append(_scale(rel, [(-5, 20), (0, 40), (5, 65), (10, 85)]))
        inputs.append({"zh": "高收益債相對公債 6 月（HYG−IEF）", "v": _fmt(rel, "%", 1, True)})
    btc6 = (quotes.get("BTC-USD") or {}).get("chg_6m")
    arkk6 = (quotes.get("ARKK") or {}).get("chg_6m")
    spy6 = (quotes.get("SPY") or {}).get("chg_6m")
    arkk_ex = round(arkk6 - spy6, 2) if (arkk6 is not None and spy6 is not None) else None
    vix = (quotes.get("^VIX") or {}).get("price")
    parts += [
        _scale(btc6, [(-20, 15), (0, 30), (30, 55), (60, 75), (100, 95)]),
        _scale(arkk_ex, [(-10, 20), (0, 35), (15, 60), (30, 80), (50, 95)]),
        _scale(vix, [(12, 85), (16, 65), (20, 45), (28, 25), (40, 10)]),
    ]
    inputs += [
        {"zh": "比特幣近 6 月", "v": _fmt(btc6, "%", 1, True)},
        {"zh": "ARKK 近 6 月超額（vs 標普）", "v": _fmt(arkk_ex, "%", 1, True)},
        {"zh": "VIX 水位（越低越自滿）", "v": _fmt(vix, "", 1)},
    ]
    return _avg(parts), inputs


def score_concentration(quotes):
    spy1y = (quotes.get("SPY") or {}).get("chg_1y")
    rsp1y = (quotes.get("RSP") or {}).get("chg_1y")
    qqq1y = (quotes.get("QQQ") or {}).get("chg_1y")
    cap_vs_eq = round(spy1y - rsp1y, 2) if (spy1y is not None and rsp1y is not None) else None
    tech_vs_spy = round(qqq1y - spy1y, 2) if (qqq1y is not None and spy1y is not None) else None
    sc = _avg([
        _scale(cap_vs_eq, [(-5, 10), (0, 25), (5, 45), (10, 65), (18, 85), (25, 95)]),
        _scale(tech_vs_spy, [(-5, 15), (0, 30), (5, 50), (12, 70), (20, 90)]),
    ])
    inputs = [
        {"zh": "市值加權 − 等權重（1 年）", "v": _fmt(cap_vs_eq, "%", 1, True)},
        {"zh": "那斯達克100 − 標普500（1 年）", "v": _fmt(tech_vs_spy, "%", 1, True)},
    ]
    return sc, inputs


def _level(score):
    if score is None:
        return "na"
    return "good" if score < 40 else ("warn" if score < 65 else "bad")


def zone_for(score):
    for z in ZONES:
        if score < z["max"]:
            return z
    return ZONES[-1]


def build_scores(quotes, fundamentals, capex, hy_oas) -> dict:
    raw = {
        "capex": score_capex(capex),
        "valuation": score_valuation(fundamentals),
        "momentum": score_momentum(quotes),
        "infra": score_infra(quotes),
        "credit": score_credit(quotes, hy_oas),
        "concentration": score_concentration(quotes),
    }
    subs = []
    weighted, wsum = 0.0, 0.0
    for key, (sc, inputs) in raw.items():
        meta = SIGNAL_META[key]
        subs.append({
            "key": key, "zh": meta["zh"], "en": meta["en"], "note_zh": meta["note_zh"],
            "score": sc, "level": _level(sc), "weight": SCORE_WEIGHTS[key],
            "inputs": inputs,
        })
        if sc is not None:
            weighted += sc * SCORE_WEIGHTS[key]
            wsum += SCORE_WEIGHTS[key]
    composite = round(weighted / wsum, 1) if wsum else None
    zone = zone_for(composite) if composite is not None else None
    return {
        "composite": composite,
        "zone": {"key": zone["key"], "zh": zone["zh"], "desc_zh": zone["desc_zh"]} if zone else None,
        "subs": subs,
    }


# ────────────────────────── 集中度走勢圖 ──────────────────────────

REL_TICKERS = [("SPY", "標普500（市值加權）"), ("RSP", "標普500 等權重"), ("QQQ", "那斯達克100")]


def build_relchart(df, n_symbols: int) -> dict | None:
    import pandas as pd

    frame = {}
    for sym, _zh in REL_TICKERS:
        s = _closes(df, sym, n_symbols)
        if s is not None and len(s) >= 40:
            frame[sym] = s
    if "SPY" not in frame or "RSP" not in frame:
        return None
    fr = pd.DataFrame(frame).ffill().dropna()
    norm = (fr / fr.iloc[0] * 100.0).iloc[::REL_SAMPLE]
    series = [{"key": sym, "zh": zh,
               "values": [round(float(v), 2) for v in norm[sym]]}
              for sym, zh in REL_TICKERS if sym in norm.columns]
    return {"dates": [d.date().isoformat() for d in norm.index], "series": series}


# ────────────────────────── 新聞 ──────────────────────────

def fetch_news() -> list[dict]:
    topics = []
    for topic in NEWS_TOPICS:
        items = []
        try:
            resp = requests.get(
                NEWS_RSS,
                params={"q": topic["query"], "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"},
                timeout=12,
                headers={"User-Agent": "Mozilla/5.0 (ai-bubble-monitor)"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                src = item.find("{https://news.google.com}source")
                source = src.text.strip() if src is not None and src.text else ""
                if not source:
                    s2 = item.find("source")
                    source = s2.text.strip() if s2 is not None and s2.text else ""
                if title and link:
                    items.append({"title": title, "link": link, "pubDate": pub, "source": source})
                if len(items) >= NEWS_PER_TOPIC:
                    break
        except Exception:
            log.warning("aibubble: news fetch failed for %s", topic["key"], exc_info=True)
        topics.append({"key": topic["key"], "zh": topic["zh"], "items": items})
    return topics


# ────────────────────────── AI 執行摘要 ──────────────────────────

_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "bullets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "5-7 條判讀要點，每條一句話，引用具體數字並給出 So-what",
        },
        "watch": {"type": "string", "description": "本期最值得盯的單一變化，一句話"},
        "stance": {"type": "string", "enum": ["冷靜", "升溫", "過熱", "警戒", "極端"],
                   "description": "一詞總結當前泡沫態勢"},
    },
    "required": ["bullets", "watch", "stance"],
    "additionalProperties": False,
}


def _brief_facts(quotes, capex, scores, hy_oas, news, frontier=None) -> dict:
    agg = capex.get("agg") or {}
    fr = frontier or {}
    fr_scores = fr.get("scores") or {}
    return {
        "market_composite": scores.get("composite"),
        "zone": (scores.get("zone") or {}).get("zh"),
        "frontier_composite": fr_scores.get("composite"),
        "frontier_subs": [{"zh": s["zh"], "score": s["score"], "inputs": s["inputs"]}
                          for s in fr_scores.get("subs", [])],
        "h100_spot": ((fr.get("gpu_spot") or {}).get("gpus", {}).get("h100") or {}).get("median"),
        "sdk_demand_g1y": (fr.get("adoption") or {}).get("avg_g1y"),
        "dc_stress_ratio": (fr.get("dc_stress") or {}).get("ratio"),
        "revenue_gap_multiple": (fr.get("revenue_gap") or {}).get("multiple"),
        "subs": [{"zh": s["zh"], "score": s["score"], "inputs": s["inputs"]}
                 for s in scores.get("subs", [])],
        "capex_total_b": agg.get("latest_total_b"),
        "capex_yoy": agg.get("yoy"),
        "capex_ocf": agg.get("capex_ocf"),
        "nvda_1y": (quotes.get("NVDA") or {}).get("chg_1y"),
        "nvda_off_high": (quotes.get("NVDA") or {}).get("off_high"),
        "hy_oas_bps": (hy_oas or {}).get("bps"),
        "vix": (quotes.get("^VIX") or {}).get("price"),
        "news_titles": [it["title"] for t in news for it in t["items"][:2]][:10],
    }


def _claude_brief(facts: dict) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    prompt = (
        "以下是 AI 泡沫監控平台本期計算出的數據事實（JSON），分兩層："
        "市場訊號層（market_composite，估值/動能/利差等同步指標）與"
        "前瞻訊號層（frontier_composite，GPU 現貨租金、供需剪刀差、資料中心壓力事件、"
        "循環交易、徵才成熟度、收入缺口——領先市場 6-12 個月的實體經濟讀數）。"
        "請以繁體中文撰寫高管執行摘要：5-7 條要點（bullets），每條一句話、"
        "引用具體數字、說明對投資人的含義；務必對比兩層的分歧（哪層更熱、代表什麼）；"
        "一條本期最值得盯的變化（watch）；以及一詞態勢判定（stance）。"
        "兼顧多空兩面（哪些指標支持結構性需求、哪些指標顯示過熱）。"
        "只使用給定數據，不要編造數字，不構成投資建議。\n\n"
        + json.dumps(facts, ensure_ascii=False)
    )
    resp = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2500,
        system=(
            "你是頂級宏觀對沖基金的首席策略師，擅長辨識資產泡沫的結構性訊號，"
            "為投資委員會撰寫簡潔、數據驅動、有 So-what 的繁體中文判讀。"
        ),
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": _BRIEF_SCHEMA}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    return {"source": "ai", "bullets": data["bullets"][:7],
            "watch": data["watch"], "stance": data["stance"]}


def _rules_brief(facts: dict) -> dict:
    bullets = []
    comp, zone = facts.get("market_composite"), facts.get("zone")
    fr = facts.get("frontier_composite")
    if comp is not None:
        line = f"市場訊號溫度 {comp:.0f}/100（{zone}）"
        if fr is not None:
            diff_zh = "前瞻層更冷，實體經濟尚未確認泡沫頂部" if fr < comp else "前瞻層更熱，實體經濟惡化領先於市場價格——警訊"
            line += f"、前瞻訊號溫度 {fr:.0f}/100——{diff_zh}。"
        else:
            line += "——六大維度加權，分數越高越偏離常態。"
        bullets.append(line)
    if facts.get("h100_spot") is not None:
        bullets.append(f"H100 現貨租金中位數 ${facts['h100_spot']:.2f}/hr（vast.ai）——"
                       "本輪的「暗光纖點亮率」，跌入 $1.3–1.9 回本帶即是過建實證。")
    if facts.get("sdk_demand_g1y") is not None and facts.get("capex_yoy") is not None:
        gap = facts["capex_yoy"] - facts["sdk_demand_g1y"]
        verdict = "需求增速仍跑贏供給——結構性支撐" if gap < 0 else "供給增速已超越需求——泡沫指紋"
        bullets.append(f"供需剪刀差：CapEx 年增 {facts['capex_yoy']:+.0f}% vs SDK 下載年增 "
                       f"{facts['sdk_demand_g1y']:+.0f}%——{verdict}。")
    if facts.get("revenue_gap_multiple") is not None:
        bullets.append(f"收入缺口倍數 {facts['revenue_gap_multiple']:.1f}x——隱含必要收入遠超目前 AI 終端收入，"
                       "缺口由誰買單是本輪核心問題。")
    if facts.get("capex_total_b"):
        bullets.append(f"五大雲端巨頭最新季資本支出合計約 ${facts['capex_total_b']:.0f}B"
                       + (f"、年增 {facts['capex_yoy']:+.0f}%" if facts.get("capex_yoy") is not None else "")
                       + (f"、佔營運現金流 {facts['capex_ocf']:.0f}%" if facts.get("capex_ocf") is not None else "")
                       + "——燒錢強度是本輪週期的核心變數。")
    if facts.get("nvda_1y") is not None:
        bullets.append(f"輝達近一年 {facts['nvda_1y']:+.0f}%、距 52 週高點 "
                       f"{facts.get('nvda_off_high', 0):+.0f}%——AI 龍頭動能是市場情緒的即時讀數。")
    if facts.get("hy_oas_bps"):
        bullets.append(f"高收益債利差 {facts['hy_oas_bps']} bps——利差越窄，市場對風險的定價越自滿。")
    if facts.get("vix"):
        bullets.append(f"VIX {facts['vix']:.0f}——波動率水位反映市場戒心。")
    hi = max((s for s in facts.get("subs", []) if s.get("score") is not None),
             key=lambda s: s["score"], default=None)
    watch = (f"六維中最熱的是「{hi['zh']}」（{hi['score']:.0f} 分）——優先盯它的邊際變化。"
             if hi else "關注巨頭財報的資本支出指引與信用利差變化。")
    stance = zone or "升溫"
    return {"source": "rules", "bullets": bullets[:7], "watch": watch, "stance": stance}


def generate_brief(quotes, capex, scores, hy_oas, news, frontier=None) -> dict:
    facts = _brief_facts(quotes, capex, scores, hy_oas, news, frontier)
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude_brief(facts)
        except Exception:
            log.exception("aibubble: Claude brief failed — falling back to rules")
    return _rules_brief(facts)


# ────────────────────────── 快照 ──────────────────────────

def build_snapshot() -> dict:
    prev = load_snapshot() or {}
    df, symbols = _download_all()
    n = len(symbols)
    quotes = build_quotes(df, n)
    relchart = build_relchart(df, n)
    fundamentals = fetch_fundamentals()
    capex = fetch_capex()
    hy_oas = fetch_hy_oas()
    news = fetch_news()
    scores = build_scores(quotes, fundamentals, capex, hy_oas)
    try:
        frontier = frontier_mod.build_frontier(capex.get("agg"), prev.get("frontier"))
    except Exception:
        log.exception("aibubble: frontier build failed — keeping previous frontier block")
        frontier = prev.get("frontier")
    brief = generate_brief(quotes, capex, scores, hy_oas, news, frontier)
    snap = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "groups": [
            {k: g[k] for k in ("key", "zh", "en", "desc_zh")} | {"tickers": g["tickers"]}
            for g in GROUPS
        ],
        "quotes": quotes,
        "fundamentals": fundamentals,
        "capex": capex,
        "hy_oas": hy_oas,
        "scores": scores,
        "frontier": frontier,
        "relchart": relchart,
        "brief": brief,
        "news": news,
        "quote_count": len(quotes),
    }
    save_snapshot(snap)
    return snap


def refresh() -> dict:
    """序列化重建——多人同時按更新共用同一次執行。"""
    with _refresh_lock:
        return build_snapshot()


def save_snapshot(snap: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")


def load_snapshot() -> dict | None:
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
