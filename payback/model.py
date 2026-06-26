"""AI Capex Payback Radar — L3 quant engine + snapshot assembler.

The question: is the AI capex paying off yet — who converts spend into revenue,
who is just burning? Neither "AI capex" nor "AI revenue" is a reported line, so
the engine mixes a LIVE hard layer (total capex & revenue from yfinance, TTM)
with a CURATED layer (AI capex share = management-guided; cloud segment revenue =
reported; an AI-only revenue band = our estimate). For each company it derives:

  • ai_capex_ttm   = total capex × AI-share%        (estimate over a hard total)
  • coverage       = AI revenue ÷ AI spend          (>1 monetizing, <1 outspending)
  • capex_intensity= capex ÷ revenue                (how much of the top line is capex)
  • payback score  = 0–100, and a verdict MONETIZING / INVESTING / BURNING

Public hyperscalers and the private labs use different score inputs (the labs have
no cloud/intensity, only revenue-vs-burn) but share the same output shape and
verdict thresholds. Aggregate adds a cumulative AI-capex-vs-AI-revenue gap and a
circularity total. analysis.analyze() then writes the L4 CEO read & L5 scenarios.
"""
from __future__ import annotations

from . import analysis, depreciation

# Headline (aggregate) labels — driven by COVERAGE, so "covering the spend" is literal.
_VERDICT = {
    "monetizing": {"en": "MONETIZING — AI revenue covering the spend", "zh": "MONETIZING — AI 營收正在覆蓋投入"},
    "investing":  {"en": "INVESTING — heavy build, revenue ramping behind", "zh": "INVESTING — 重壓建置,營收在後爬升"},
    "burning":    {"en": "BURNING — spend far ahead of AI revenue", "zh": "BURNING — 投入遠超 AI 營收"},
}

# Per-company labels — driven by the 0–100 payback SCORE (not coverage), so the wording
# must NOT claim coverage is achieved. A high score means the conversion trajectory looks
# strong (cloud growth, contained intensity), even while coverage is still well under 1.
_VERDICT_SCORE = {
    "monetizing": {"en": "CONVERTING — strongest payback trajectory", "zh": "CONVERTING — 變現軌跡最強"},
    "investing":  {"en": "INVESTING — heavy build, revenue ramping behind", "zh": "INVESTING — 重壓建置,營收在後爬升"},
    "burning":    {"en": "OUTSPENDING — spend far ahead of AI revenue", "zh": "OUTSPENDING — 投入遠超 AI 營收"},
}


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _pct(now, then):
    if now is None or then in (None, 0):
        return None
    return round((now / then - 1) * 100, 1)


def _verdict_key(score):
    return "monetizing" if score >= 60 else "burning" if score <= 40 else "investing"


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def public_score(coverage, cloud_growth, intensity, capex_yoy, rev_yoy):
    """0–100 payback score for a hyperscaler.

    base 55, reward cloud growth (demand showing up) and coverage above ~0.3,
    penalize high capex intensity and capex growth running far ahead of revenue.
    """
    cov = coverage if coverage is not None else 0.0
    cg = cloud_growth if cloud_growth is not None else 0.0
    inten = intensity if intensity is not None else 0.0
    cyoy = capex_yoy if capex_yoy is not None else 0.0
    ryoy = rev_yoy if rev_yoy is not None else 0.0
    return round(_clamp(
        55.0
        + 0.6 * cg
        + 18.0 * (cov - 0.3)
        - 0.35 * max(0.0, inten - 25.0)
        - 0.30 * max(0.0, cyoy - ryoy - 20.0),
        0.0, 100.0), 1)


def private_score(coverage, rev_growth):
    """0–100 payback score for a private lab: revenue-vs-burn coverage + growth."""
    cov = coverage if coverage is not None else 0.0
    rg = rev_growth if rev_growth is not None else 0.0
    return round(_clamp(35.0 + 35.0 * (cov - 0.5) + 0.05 * rg, 0.0, 100.0), 1)


def _public_runway(fin, capex, ai_capex):
    """"How hard must they keep burning?" for a profitable hyperscaler.

    Not runway-to-zero (they print cash) but how much of their cash generation the
    AI build consumes: capex ÷ operating cash flow, AI capex ÷ OCF, and the free
    cash flow left after the build (self-funded headroom). If FCF is negative the
    build is no longer self-funding and we add a cash-buffer-in-years read.
    OCF is approximated as levered FCF + capex (FCF = OCF − capex).
    """
    fcf = fin.get("fcf_ttm_usd_bn")
    cash = fin.get("cash_usd_bn")
    if fcf is None or capex is None:
        return None
    ocf = round(fcf + capex, 1)
    status = "self_funded" if fcf >= 20 else "tight" if fcf > 0 else "external"
    out = {
        "fcf_ttm": round(fcf, 1),
        "cash": cash,
        "ocf_ttm": ocf,
        "capex_to_ocf_pct": round(capex / ocf * 100, 1) if ocf else None,
        "ai_capex_to_ocf_pct": round(ai_capex / ocf * 100, 1) if ocf else None,
        "self_funded_headroom_usd_bn": round(fcf, 1),
        "status": status,
    }
    if status == "external" and cash:
        out["cash_buffer_years"] = round(cash / abs(fcf), 1)
    return out


# --------------------------------------------------------------------------- #
# Per-company compute
# --------------------------------------------------------------------------- #
def _merge_public(c, live):
    """latest totals: live wins, KB seed fills gaps."""
    seed = c.get("seed", {})
    lm = (live or {}).get("metrics", {}).get(c["id"]) if live else None
    if lm:
        return {
            "capex_ttm": lm.get("capex_ttm_usd_bn") or seed.get("capex_ttm_usd_bn"),
            "capex_ttm_prev": lm.get("capex_ttm_prev_usd_bn") or seed.get("capex_ttm_prev_usd_bn"),
            "revenue_ttm": lm.get("revenue_ttm_usd_bn") or seed.get("revenue_ttm_usd_bn"),
            "revenue_ttm_prev": lm.get("revenue_ttm_prev_usd_bn") or seed.get("revenue_ttm_prev_usd_bn"),
            "capex_yoy": lm.get("capex_yoy"),   # from live (TTM or single-q YoY); None → model falls back to prev TTM
            "rev_yoy": lm.get("rev_yoy"),
            "stock": lm.get("stock") or seed.get("stock"),
            "stock_chg_1m": lm.get("stock_chg_1m", seed.get("stock_chg_1m")),
            "as_of_q": lm.get("as_of_q") or seed.get("as_of_q"),
            "live": True,
        }
    return {
        "capex_ttm": seed.get("capex_ttm_usd_bn"),
        "capex_ttm_prev": seed.get("capex_ttm_prev_usd_bn"),
        "revenue_ttm": seed.get("revenue_ttm_usd_bn"),
        "revenue_ttm_prev": seed.get("revenue_ttm_prev_usd_bn"),
        "capex_yoy": None,
        "rev_yoy": None,
        "stock": seed.get("stock"),
        "stock_chg_1m": seed.get("stock_chg_1m"),
        "as_of_q": seed.get("as_of_q"),
        "live": False,
    }


def _compute_public(c, live):
    m = _merge_public(c, live)
    capex = m["capex_ttm"] or 0.0
    rev = m["revenue_ttm"] or 0.0
    share = float(c.get("ai_capex_share", {}).get("value", 0)) / 100.0
    ai_capex = round(capex * share, 1)

    cloud = c.get("cloud", {})
    band = c.get("ai_rev_band", {})
    ai_rev_mid = round((float(band.get("low_usd_bn", 0)) + float(band.get("high_usd_bn", 0))) / 2.0, 1)

    capex_yoy = m.get("capex_yoy")
    if capex_yoy is None:
        capex_yoy = _pct(capex, m.get("capex_ttm_prev"))
    rev_yoy = m.get("rev_yoy")
    if rev_yoy is None:
        rev_yoy = _pct(rev, m.get("revenue_ttm_prev"))
    intensity = round(capex / rev * 100, 1) if rev else None
    cloud_growth = cloud.get("rev_yoy_pct")
    coverage = round(ai_rev_mid / ai_capex, 3) if ai_capex else None

    score = public_score(coverage, cloud_growth, intensity, capex_yoy, rev_yoy)
    key = _verdict_key(score)
    runway = _public_runway(c.get("financials", {}), capex, ai_capex)
    return {
        "id": c["id"], "kind": "public",
        "runway": runway,
        "name_en": c["name_en"], "name_zh": c["name_zh"], "ticker": c.get("ticker"),
        "cloud_name_en": c.get("cloud_name_en"), "cloud_name_zh": c.get("cloud_name_zh"),
        "capex_ttm": round(capex, 1), "revenue_ttm": round(rev, 1),
        "ai_capex_share": c.get("ai_capex_share"),
        "ai_capex_ttm": ai_capex,
        "cloud": cloud,
        "ai_rev_band": band, "ai_rev_mid": ai_rev_mid,
        "capex_yoy": capex_yoy, "rev_yoy": rev_yoy,
        "cloud_growth": cloud_growth, "capex_intensity": intensity,
        "coverage": coverage,
        "score": score, "verdict_key": key,
        "verdict_en": _VERDICT_SCORE[key]["en"], "verdict_zh": _VERDICT_SCORE[key]["zh"],
        "stock": m["stock"], "stock_chg_1m": m["stock_chg_1m"],
        "as_of_q": m["as_of_q"], "live": m["live"],
        "mgmt_quote": c.get("mgmt_quote", {}),
        "series": c.get("series", []),
        "note_en": c.get("note_en", ""), "note_zh": c.get("note_zh", ""),
    }


def _v(field):
    """unwrap a {value, tier, source...} seed field → its numeric value."""
    return float(field.get("value")) if field and field.get("value") is not None else None


def _compute_private(c):
    s = c.get("seed", {})
    rev = _v(s.get("revenue_runrate_usd_bn"))
    burn = _v(s.get("annual_burn_usd_bn"))
    funding = _v(s.get("funding_raised_usd_bn"))
    rev_growth = _v(s.get("revenue_yoy_pct"))
    coverage = round(rev / burn, 3) if (rev is not None and burn) else None
    runway_years = round(funding / burn, 1) if (funding is not None and burn) else None

    score = private_score(coverage, rev_growth)
    key = _verdict_key(score)
    return {
        "id": c["id"], "kind": "private",
        "name_en": c["name_en"], "name_zh": c["name_zh"],
        "revenue_runrate": s.get("revenue_runrate_usd_bn"),
        "revenue_yoy": s.get("revenue_yoy_pct"),
        "annual_burn": s.get("annual_burn_usd_bn"),
        "funding_raised": s.get("funding_raised_usd_bn"),
        "valuation": s.get("valuation_usd_bn"),
        "compute_commitment": s.get("compute_commitment_usd_bn"),
        "coverage": coverage, "runway_years": runway_years,
        "score": score, "verdict_key": key,
        "verdict_en": _VERDICT_SCORE[key]["en"], "verdict_zh": _VERDICT_SCORE[key]["zh"],
        "mgmt_quote": c.get("mgmt_quote", {}),
        "note_en": c.get("note_en", ""), "note_zh": c.get("note_zh", ""),
    }


# --------------------------------------------------------------------------- #
# Aggregate + scissors series + circularity
# --------------------------------------------------------------------------- #
def _aggregate(publics):
    tot_capex = round(sum(p["capex_ttm"] for p in publics), 1)
    tot_ai_capex = round(sum(p["ai_capex_ttm"] for p in publics), 1)
    tot_cloud = round(sum(float(p["cloud"].get("rev_ttm_usd_bn", 0) or 0) for p in publics), 1)
    tot_ai_rev = round(sum(p["ai_rev_mid"] for p in publics), 1)
    coverage = round(tot_ai_rev / tot_ai_capex, 3) if tot_ai_capex else None
    cloud_coverage = round(tot_cloud / tot_capex, 3) if tot_capex else None
    return {
        "total_capex_ttm": tot_capex,
        "total_ai_capex_ttm": tot_ai_capex,
        "total_cloud_rev_ttm": tot_cloud,
        "total_ai_rev_mid": tot_ai_rev,
        "ai_coverage": coverage,            # AI revenue ÷ AI capex (the headline gap)
        "cloud_coverage": cloud_coverage,   # cloud revenue ÷ total capex (hard floor)
        "gap_usd_bn": round(tot_ai_capex - tot_ai_rev, 1),
    }


def _scissors(kb, publics):
    """Aggregate capex-vs-cloud-revenue per quarter, for the divergence chart."""
    quarters = kb.get("quarters", [])
    by_id = {p["id"]: p for p in publics}
    out = []
    for i, q in enumerate(quarters):
        capex = 0.0
        cloud = 0.0
        for c in kb.get("companies", []):
            if c.get("kind") != "public":
                continue
            series = c.get("series", [])
            if i < len(series):
                capex += float(series[i].get("capex", 0) or 0)
                cloud += float(series[i].get("cloud_rev", 0) or 0)
        out.append({"q": q, "capex": round(capex, 1), "cloud_rev": round(cloud, 1)})
    # cumulative
    cum_capex = 0.0
    cum_cloud = 0.0
    for row in out:
        cum_capex += row["capex"]
        cum_cloud += row["cloud_rev"]
        row["cum_capex"] = round(cum_capex, 1)
        row["cum_cloud_rev"] = round(cum_cloud, 1)
    return out


def _circularity(kb):
    edges = kb.get("circularity_edges", [])

    def _sum(kind):
        return round(sum(float(e.get("amount_usd_bn", 0) or 0)
                         for e in edges if e.get("kind") == kind), 1)

    total = round(sum(float(e.get("amount_usd_bn", 0) or 0) for e in edges), 1)
    # The headline total is a HETEROGENEOUS sum: multi-year purchase commitments
    # (a FLOW) added to cumulative equity investments (a STOCK). It is a "scale of
    # entanglement" gauge, not an additive accounting quantity — so we also break it out.
    return {
        "edges": edges,
        "total_usd_bn": total,
        "count": len(edges),
        "commitment_flow_usd_bn": _sum("commitment_flow"),   # multi-year purchase commitments (flow)
        "investment_stock_usd_bn": _sum("investment_stock"),  # cumulative equity investments (stock)
        "heterogeneous": True,
    }


# --------------------------------------------------------------------------- #
# Deterministic alerts
# --------------------------------------------------------------------------- #
def _alerts(agg, publics, privates, circ, dep=None):
    out = []
    cov = agg["ai_coverage"]
    if cov is not None and cov < 0.5:
        out.append({"level": "squeeze",
                    "en": f"AI spend far ahead of AI revenue: across the four, AI capex ≈ ${agg['total_ai_capex_ttm']}B/yr vs AI-only revenue ≈ ${agg['total_ai_rev_mid']}B (coverage {cov}). Still an investment phase, not a payback one.",
                    "zh": f"AI 投入遠超 AI 營收:四家合計 AI capex ≈ ${agg['total_ai_capex_ttm']}B/年 vs AI-only 營收 ≈ ${agg['total_ai_rev_mid']}B(覆蓋率 {cov})。仍在投入期,非回本期。"})

    best = max(publics, key=lambda p: p["score"]) if publics else None
    worst = min(publics, key=lambda p: p["score"]) if publics else None
    if best and worst and best["id"] != worst["id"]:
        out.append({"level": "strong",
                    "en": f"Fastest converter: {best['name_en']} (score {best['score']}, cloud +{best['cloud_growth']}%). Heaviest builder: {worst['name_en']} (score {worst['score']}, capex intensity {worst['capex_intensity']}%).",
                    "zh": f"變現最快:{best['name_zh']}(分數 {best['score']},雲端 +{best['cloud_growth']}%)。投入最重:{worst['name_zh']}(分數 {worst['score']},capex 強度 {worst['capex_intensity']}%)。"})

    # capex outrunning revenue (any name)
    fastest = max((p for p in publics if p["capex_yoy"] is not None and p["rev_yoy"] is not None),
                  key=lambda p: (p["capex_yoy"] - p["rev_yoy"]), default=None)
    if fastest and (fastest["capex_yoy"] - fastest["rev_yoy"]) > 25:
        out.append({"level": "watch",
                    "en": f"Capex outrunning revenue: {fastest['name_en']} capex +{fastest['capex_yoy']}%/yr vs revenue +{fastest['rev_yoy']}%/yr — the depreciation tail will land on margins before the AI revenue does.",
                    "zh": f"capex 跑贏營收:{fastest['name_zh']} capex +{fastest['capex_yoy']}%/年 vs 營收 +{fastest['rev_yoy']}%/年 — 折舊尾巴會比 AI 營收更早壓上毛利。"})

    # depreciation / chip-shock exposure (the earnings tail behind the capex)
    if dep and dep.get("aggregate"):
        da = dep["aggregate"]
        me = next((r for r in dep["companies"] if r["id"] == da.get("most_exposed_id")), None)
        if me and da.get("combined_pct_of_op_income"):
            out.append({"level": "squeeze",
                        "en": f"Depreciation tail: a useful-life reversal + stranded-chip retirement would add ≈ ${da['total_combined_annual_usd_bn']}B/yr of depreciation across the four ({da['combined_pct_of_op_income']}% of combined operating income), plus a ${da['total_impairment_one_time_usd_bn']}B one-time H100 write-down. Most exposed: {me['name_en']} at {da['most_exposed_pct_op_income']}% of its operating income.",
                        "zh": f"折舊尾巴:耐用年限回調 + 擱淺晶片提前汰換,四家合計每年多 ≈ ${da['total_combined_annual_usd_bn']}B 折舊(占合計營益 {da['combined_pct_of_op_income']}%),另加一次性 H100 減損 ${da['total_impairment_one_time_usd_bn']}B。最敏感:{me['name_zh']},達其營益的 {da['most_exposed_pct_op_income']}%。"})

    # public "how hard to keep burning" — build no longer self-funding
    tight = [p for p in publics if (p.get("runway") or {}).get("status") in ("tight", "external")]
    if tight:
        names = ", ".join(f"{p['name_en']} ({p['runway']['capex_to_ocf_pct']}% of OCF)" for p in tight)
        out.append({"level": "watch",
                    "en": f"Burn intensity: capex is eating most of operating cash flow — {names}. Little free cash left to fund the AI build internally; further acceleration needs balance-sheet or debt.",
                    "zh": f"燒錢強度:capex 吃掉大部分營運現金流 — {names}。內部自籌 AI 建置的自由現金所剩無幾;再加速須動用資產負債表或舉債。"})

    if circ["total_usd_bn"] > 100:
        out.append({"level": "watch",
                    "en": f"Circularity flag: ≈ ${circ['commitment_flow_usd_bn']}B of multi-year purchase commitments (flow) plus ≈ ${circ['investment_stock_usd_bn']}B of cumulative equity investments (stock) loop between Nvidia, the labs and the clouds — some 'AI revenue' is the same dollars circulating, not independent end-demand. (Flow + stock is a heterogeneous gauge of entanglement, not an additive total.)",
                    "zh": f"循環性警示:約 ${circ['commitment_flow_usd_bn']}B 多年期採購承諾(流量)加上約 ${circ['investment_stock_usd_bn']}B 累計股權投資(存量),在 Nvidia、實驗室與雲端之間轉圈 — 部分「AI 營收」是同一批資金循環,非獨立終端需求。(流量+存量為糾纏規模,非可加總計。)"})

    burning = [p for p in privates if p["verdict_key"] == "burning"]
    if burning:
        names = ", ".join(p["name_en"] for p in burning)
        out.append({"level": "squeeze",
                    "en": f"Burning (private): {names} — revenue compounding fast but well below burn; dependent on continued funding & cloud compute commitments.",
                    "zh": f"純燒(私有):{names} — 營收快速複合但遠低於燒錢;依賴持續募資與雲端算力承諾。"})

    if not out:
        out.append({"level": "watch",
                    "en": "No sharp divergence this read — capex, cloud revenue and AI run-rates are moving roughly together.",
                    "zh": "本次判讀無明顯背離 — capex、雲端營收與 AI run-rate 大致同步。"})
    return out


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #
def build_snapshot(kb, live=None, generated_at="", today=""):
    publics = [_compute_public(c, live) for c in kb.get("companies", []) if c.get("kind") == "public"]
    privates = [_compute_private(c) for c in kb.get("companies", []) if c.get("kind") == "private"]
    agg = _aggregate(publics)
    scissors = _scissors(kb, publics)
    circ = _circularity(kb)
    dep = depreciation.build(kb.get("companies", []))
    alerts = _alerts(agg, publics, privates, circ, dep)

    # headline = overall payment-progress verdict from aggregate coverage
    head_cov = agg["ai_coverage"] or 0.0
    head_key = "monetizing" if head_cov >= 0.6 else "burning" if head_cov <= 0.25 else "investing"

    l3 = {
        "aggregate": agg,
        "headline": {"coverage": agg["ai_coverage"], "verdict_key": head_key,
                     "verdict_en": _VERDICT[head_key]["en"], "verdict_zh": _VERDICT[head_key]["zh"]},
        "companies": publics,
        "private": privates,
        "scissors": scissors,
        "circularity": circ,
        "depreciation": dep,
        "alerts": alerts,
    }

    analysis_out = analysis.analyze(kb, l3)

    return {
        "generated_at": generated_at,
        "as_of": today or kb.get("as_of_curated", ""),
        "source": "live" if live else "seed",
        "is_demo": live is None,
        "title_en": kb.get("title_en", "AI Capex Payback Radar"),
        "title_zh": kb.get("title_zh", "AI 資本支出回本雷達"),
        "method_en": kb.get("method_en", ""),
        "method_zh": kb.get("method_zh", ""),
        "tier_legend": kb.get("tier_legend", {}),
        "headline": l3["headline"],            # surfaced for the portal card
        "l3": l3,
        "l4": analysis_out["l4"],
        "l5": analysis_out["l5"],
        "analysis_engine": analysis_out["engine"],
        "blind_spots_en": kb.get("blind_spots_en", []),
        "blind_spots_zh": kb.get("blind_spots_zh", []),
        "news": (live or {}).get("news", []) if live else [],
        "fetched_at": (live or {}).get("fetched_at") if live else None,
    }
