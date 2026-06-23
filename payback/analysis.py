"""AI Capex Payback Radar — L4 / L5 synthesis.

L4 (the CEO read: who is monetizing AI capex vs just building/burning, a one-line
take per company, and the leading signals) and L5 (scenarios / falsification /
watch-list) are written from the L3 quant read. Two engines:

  • Claude — Opus 4.8, structured outputs (json_schema) + a prompt-cached
             framework system block. Used when ANTHROPIC_API_KEY is set.
  • rules  — deterministic fallback from the L3 numbers, so the board is fully
             functional offline / without a key.

The deterministic ALERTS live on l3 (model._alerts); this module only narrates.
The system prompt forbids inventing AI-revenue precision — Claude may only reframe
the public totals, the management-guided AI share, and the flagged estimates.

analyze(kb, l3) → {"engine": "claude"|"rules", "l4": {...}, "l5": {...}}
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("payback.analysis")

MODEL = "claude-opus-4-8"

_BI = {"type": "object", "additionalProperties": False,
       "properties": {"en": {"type": "string"}, "zh": {"type": "string"}},
       "required": ["en", "zh"]}

_COMPANY_TAKE = {
    "type": "object", "additionalProperties": False,
    "properties": {"id": {"type": "string"}, "en": {"type": "string"}, "zh": {"type": "string"}},
    "required": ["id", "en", "zh"],
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "l4": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "payback_read": _BI,
                "circularity_read": _BI,
                "company_takes": {"type": "array", "items": _COMPANY_TAKE},
                "leading_signals": {"type": "array", "items": _BI},
            },
            "required": ["payback_read", "circularity_read", "company_takes", "leading_signals"],
        },
        "l5": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "scenarios": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "name_en": {"type": "string"}, "name_zh": {"type": "string"},
                            "prob": {"type": "integer"},
                            "trigger_en": {"type": "string"}, "trigger_zh": {"type": "string"},
                        },
                        "required": ["name_en", "name_zh", "prob", "trigger_en", "trigger_zh"],
                    },
                },
                "falsification": {"type": "array", "items": _BI},
                "watch": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"en": {"type": "string"}, "zh": {"type": "string"}, "freq": {"type": "string"}},
                        "required": ["en", "zh", "freq"],
                    },
                },
            },
            "required": ["scenarios", "falsification", "watch"],
        },
    },
    "required": ["l4", "l5"],
}

SYSTEM = """You are the AI-economics analyst writing a bilingual (Traditional Chinese + English) board that answers ONE question for a senior tech executive: is the hyperscalers' AI capex paying off yet — who is converting spend into revenue, and who is just burning? You cover Alphabet, Meta, Microsoft, Amazon (public) and OpenAI & Anthropic (private). The user has done the quant (L3) and hands you the live read. Your job is L4 and L5.

HARD RULES — anti-fabrication:
- Neither "AI capex" nor "AI revenue" is a reported line. TOTAL capex & revenue are real (yfinance TTM); the AI SHARE of capex is management-guided; cloud segment revenue is reported; the AI-only revenue band is an ESTIMATE. Private-lab figures are fragmentary press/estimates. NEVER present an estimate as audited fact; cite the numbers you are given (coverage, scores, growth).
- The "coverage" ratio = AI revenue ÷ AI spend. <1 means spending more than the AI revenue earned (investment phase); >1 means monetizing. Most hyperscalers are <1 today — say so plainly; that is the honest answer, not a failure.
- Chinese in Traditional characters. No hedging boilerplate. Each text field ≤ ~110 words / 150 字.

L4 — the read:
- payback_read: in plain executive language, is the AI capex paying off yet across the four? Tie it to the aggregate coverage and the headline verdict. Name who is converting fastest and who is in the heaviest build.
- circularity_read: interpret the circular-financing total — how much of the apparent "AI revenue" is the same dollars looping between Nvidia, the labs and the clouds, and why that flatters gross demand.
- company_takes: ONE tight take per company id you are given (use the exact id), public and private. State its verdict and the single number that drives it (coverage, cloud growth, capex intensity, or revenue-vs-burn).
- leading_signals: 3–5 things that move BEFORE reported AI revenue (cloud backlog, capex guidance, depreciation/useful-life changes, frontier-lab funding rounds, compute-commitment news).

L5 — scenarios, falsification, watch:
- scenarios: 3–4 scenarios with integer probabilities that SUM TO 100, each with a concrete numeric trigger. Build on the seed scenarios but set probabilities from the current read.
- falsification: 2–3 conditions that would force the headline verdict to flip.
- watch: 5–7 monitoring items ordered by which moves earliest, each with a check frequency (weekly/monthly/quarterly).

Output only the structured object."""


def _fmt_l3(kb, l3):
    agg = l3["aggregate"]
    head = l3["headline"]
    lines = [
        f"Headline: aggregate AI coverage {head['coverage']} → verdict {head['verdict_key'].upper()}",
        f"Aggregate (4 hyperscalers, TTM $bn): total capex {agg['total_capex_ttm']}, AI capex {agg['total_ai_capex_ttm']}, "
        f"cloud revenue {agg['total_cloud_rev_ttm']}, AI-only revenue {agg['total_ai_rev_mid']}, "
        f"AI gap {agg['gap_usd_bn']}, cloud coverage {agg['cloud_coverage']}",
        "Per public company (capex / AIcapex / cloud growth / capex intensity / coverage / score / verdict):",
    ]
    for p in l3["companies"]:
        lines.append(
            f"  [{p['id']}] {p['name_en']}: capex ${p['capex_ttm']}B, AIcapex ${p['ai_capex_ttm']}B, "
            f"cloud +{p['cloud_growth']}%, intensity {p['capex_intensity']}%, coverage {p['coverage']}, "
            f"score {p['score']} → {p['verdict_key'].upper()}"
        )
    lines.append("Per private lab (revenue run-rate / burn / coverage / runway yrs / score / verdict):")
    for p in l3["private"]:
        rr = (p.get("revenue_runrate") or {}).get("value")
        bn = (p.get("annual_burn") or {}).get("value")
        lines.append(
            f"  [{p['id']}] {p['name_en']}: rev ${rr}B, burn ${bn}B, coverage {p['coverage']}, "
            f"runway {p['runway_years']}y, score {p['score']} → {p['verdict_key'].upper()}"
        )
    dep = l3.get("depreciation", {}).get("aggregate")
    if dep:
        lines.append(
            f"Depreciation engine (chip shocks on real D&A/PP&E): at-risk accelerator book "
            f"${dep.get('total_at_risk_base_usd_bn')}B; one-time H100 impairment ${dep.get('total_impairment_one_time_usd_bn')}B; "
            f"combined ANNUAL shock (useful-life reversal + stranded early-retirement) ${dep.get('total_combined_annual_usd_bn')}B "
            f"= {dep.get('combined_pct_of_op_income')}% of combined operating income; most exposed {str(dep.get('most_exposed_id')).upper()} "
            f"at {dep.get('most_exposed_pct_op_income')}% of its op income."
        )
    runways = [f"{p['id']} capex/OCF {p['runway']['capex_to_ocf_pct']}% (FCF ${p['runway']['fcf_ttm']}B, {p['runway']['status']})"
               for p in l3["companies"] if p.get("runway")]
    if runways:
        lines.append("Burn intensity (public): " + "; ".join(runways))
    circ = l3["circularity"]
    lines.append(f"Circularity: {circ['count']} edges, ${circ['total_usd_bn']}B looping (Nvidia↔labs↔clouds).")
    lines.append("Deterministic alerts already raised: " + "; ".join(f"[{a['level']}] {a['en']}" for a in l3["alerts"]))
    seeds = "; ".join(f"{s['name_en']} — {s['trigger_en']}" for s in kb.get("scenarios_seed", []))
    lines.append(f"Seed scenarios to build on: {seeds}")
    return "\n".join(lines)


def _claude(kb, l3):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    user = (
        "Here is today's live L3 AI-capex-payback read. Produce L4 and L5 as the structured object. "
        "Use the exact company ids for company_takes.\n\n" + _fmt_l3(kb, l3)
    )
    msg = client.messages.create(
        model=MODEL,
        # payback's L4/L5 JSON is larger than flows/pricing (6 companies + scenarios +
        # company_takes); with adaptive thinking sharing the budget, 4000 truncated the
        # JSON mid-string → silent rules fallback. 8000 gives headroom.
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "medium"},
        messages=[{"role": "user", "content": user}],
    )
    text = next((b.text for b in msg.content if b.type == "text"), "")
    data = json.loads(text)
    return {"engine": "claude", "l4": data["l4"], "l5": data["l5"]}


# --------------------------------------------------------------------------- #
# Rules fallback — deterministic L4/L5 from the L3 numbers
# --------------------------------------------------------------------------- #
def _rules(kb, l3):
    agg = l3["aggregate"]
    head = l3["headline"]
    cov = agg["ai_coverage"]
    publics = l3["companies"]
    privates = l3["private"]

    best = max(publics, key=lambda p: p["score"]) if publics else None
    worst = min(publics, key=lambda p: p["score"]) if publics else None

    pr_en = (f"Not yet. Across the four, AI capex ≈ ${agg['total_ai_capex_ttm']}B/yr dwarfs the AI-only revenue "
             f"≈ ${agg['total_ai_rev_mid']}B (coverage {cov}) — a ${agg['gap_usd_bn']}B gap. This is an INVESTMENT "
             f"phase, funded by hugely profitable core businesses (cloud revenue ${agg['total_cloud_rev_ttm']}B). ")
    if best and worst:
        pr_en += f"{best['name_en']} is converting fastest (score {best['score']}, cloud +{best['cloud_growth']}%); {worst['name_en']} is in the heaviest build (score {worst['score']})."
    pr_zh = (f"還沒。四家合計 AI capex ≈ ${agg['total_ai_capex_ttm']}B/年,遠大於 AI-only 營收 ≈ ${agg['total_ai_rev_mid']}B"
             f"(覆蓋率 {cov})— 缺口 ${agg['gap_usd_bn']}B。這是投入期,由極賺錢的本業(雲端營收 ${agg['total_cloud_rev_ttm']}B)支撐。")
    if best and worst:
        pr_zh += f"{best['name_zh']} 變現最快(分數 {best['score']},雲端 +{best['cloud_growth']}%);{worst['name_zh']} 投入最重(分數 {worst['score']})。"

    circ = l3["circularity"]
    cr_en = (f"≈ ${circ['total_usd_bn']}B across {circ['count']} tracked links loops between Nvidia, the labs and the "
             f"clouds. GPU purchases route compute spend back to Nvidia, whose investments seed the labs, whose "
             f"commitments fill the clouds — so part of the apparent AI demand is the same dollars circulating. "
             f"Treat gross 'AI revenue' with that discount.")
    cr_zh = (f"約 ${circ['total_usd_bn']}B、{circ['count']} 條追蹤連結在 Nvidia、實驗室與雲端之間轉圈。GPU 採購把算力支出"
             f"送回 Nvidia,其投資灌入實驗室,實驗室的承諾再填回雲端 — 故部分表面 AI 需求是同一批資金循環。看待毛額 "
             f"AI 營收應打此折扣。")

    takes = []
    for p in publics:
        takes.append({
            "id": p["id"],
            "en": f"{p['verdict_key'].upper()} — coverage {p['coverage']}, cloud +{p['cloud_growth']}%, capex intensity {p['capex_intensity']}%. {p['note_en']}",
            "zh": f"{p['verdict_key'].upper()} — 覆蓋率 {p['coverage']},雲端 +{p['cloud_growth']}%,capex 強度 {p['capex_intensity']}%。{p['note_zh']}",
        })
    for p in privates:
        rr = (p.get("revenue_runrate") or {}).get("value")
        bn = (p.get("annual_burn") or {}).get("value")
        takes.append({
            "id": p["id"],
            "en": f"{p['verdict_key'].upper()} — est. revenue ${rr}B vs burn ${bn}B (coverage {p['coverage']}), ~{p['runway_years']}y funded runway. {p['note_en']}",
            "zh": f"{p['verdict_key'].upper()} — 估營收 ${rr}B vs 燒錢 ${bn}B(覆蓋率 {p['coverage']}),約 {p['runway_years']} 年募資跑道。{p['note_zh']}",
        })

    leads = [
        {"en": "Cloud backlog / RPO (remaining performance obligations) — AI demand books before it bills.", "zh": "雲端在手訂單 / RPO(履約義務餘額)— AI 需求在入帳前先簽約。"},
        {"en": "Next-quarter capex guidance — the clearest tell on whether the build is still accelerating.", "zh": "下一季 capex 指引 — 建置是否仍在加速最清楚的訊號。"},
        {"en": "Depreciation & useful-life changes — the capex tail that lands on margins before AI revenue does.", "zh": "折舊與耐用年限變動 — 比 AI 營收更早壓上毛利的 capex 尾巴。"},
        {"en": "Frontier-lab funding rounds & valuations (OpenAI/Anthropic) — the loop's solvency.", "zh": "前沿實驗室募資輪與估值(OpenAI/Anthropic)— 循環的償付能力。"},
        {"en": "Compute-commitment headlines (Nvidia ↔ labs ↔ clouds) — circularity expanding or unwinding.", "zh": "算力承諾頭條(Nvidia ↔ 實驗室 ↔ 雲端)— 循環在擴張或瓦解。"},
    ]

    base = {"payoff": 25, "grind": 25, "digestion": 25, "bust": 25}
    if head["verdict_key"] == "monetizing":
        base = {"payoff": 45, "grind": 30, "digestion": 15, "bust": 10}
    elif head["verdict_key"] == "burning":
        base = {"payoff": 12, "grind": 35, "digestion": 28, "bust": 25}
    else:
        base = {"payoff": 25, "grind": 40, "digestion": 22, "bust": 13}
    seed_by_id = {s["id"]: s for s in kb.get("scenarios_seed", [])}
    scenarios = []
    for sid, prob in base.items():
        s = seed_by_id.get(sid, {})
        scenarios.append({
            "name_en": s.get("name_en", sid), "name_zh": s.get("name_zh", sid), "prob": prob,
            "trigger_en": s.get("trigger_en", ""), "trigger_zh": s.get("trigger_zh", ""),
        })

    return {
        "engine": "rules",
        "l4": {
            "payback_read": {"en": pr_en, "zh": pr_zh},
            "circularity_read": {"en": cr_en, "zh": cr_zh},
            "company_takes": takes,
            "leading_signals": leads,
        },
        "l5": {
            "scenarios": scenarios,
            "falsification": list(kb.get("falsification_seed", [])),
            "watch": list(kb.get("watch_seed", [])),
        },
    }


def analyze(kb, l3):
    """L4/L5 via Claude when ANTHROPIC_API_KEY is set; deterministic rules otherwise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, l3)
        except Exception:
            log.exception("payback: Claude L4/L5 failed — falling back to rules")
    return _rules(kb, l3)
