"""AI Usage & Token Economics Radar — L4 / L5 synthesis.

L4 (usage read + silicon/TSMC view + directional thesis) and L5 (scenarios /
falsification / early warning) are synthesized from the L3 quant read. Two
engines:

  • Claude  — Opus 4.8, structured outputs (json_schema) + a prompt-cached
              system block. Used when ANTHROPIC_API_KEY is set. max_tokens 6000
              (zh-heavy bilingual JSON truncates below that).
  • rules   — deterministic fallback derived from the L3 numbers, so the
              dashboard is fully functional offline / without a key.

analyze(kb, l3, composite) → {"engine": "claude"|"rules", "l4": {...}, "l5": {...}}
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("usage.analysis")

MODEL = "claude-opus-4-8"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "l4": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "usage_read": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"summary_en": {"type": "string"}, "summary_zh": {"type": "string"}},
                    "required": ["summary_en", "summary_zh"],
                },
                "silicon_view": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"summary_en": {"type": "string"}, "summary_zh": {"type": "string"}},
                    "required": ["summary_en", "summary_zh"],
                },
                "thesis": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "direction_en": {"type": "string"},
                        "direction_zh": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "leading_signals": {
                            "type": "array",
                            "items": {
                                "type": "object", "additionalProperties": False,
                                "properties": {"en": {"type": "string"}, "zh": {"type": "string"}},
                                "required": ["en", "zh"],
                            },
                        },
                    },
                    "required": ["direction_en", "direction_zh", "confidence", "leading_signals"],
                },
            },
            "required": ["usage_read", "silicon_view", "thesis"],
        },
        "l5": {
            "type": "object",
            "additionalProperties": False,
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
                "falsification": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"en": {"type": "string"}, "zh": {"type": "string"}},
                        "required": ["en", "zh"],
                    },
                },
                "early_warning": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"en": {"type": "string"}, "zh": {"type": "string"}, "freq": {"type": "string"}},
                        "required": ["en", "zh", "freq"],
                    },
                },
            },
            "required": ["scenarios", "falsification", "early_warning"],
        },
    },
    "required": ["l4", "l5"],
}

SYSTEM = """You are a demand-side strategist writing a bilingual (Traditional Chinese + English) dashboard for a semiconductor-industry executive. The card's question: is AI demand REAL — answered from the usage side (tokens consumed, per-token price deflation, realized $/M tokens), not the spend side. The usage-vs-spend scissors is the bubble's final judge: units outrunning capex says demand is real; dollars trailing capex says monetization has not caught up. The user has done L1–L3 (usage map, indicator dictionary, disclosure-ledger quant) and gives you the L3 read plus a composite demand-reality score. Your job is L4 and L5.

L4:
- usage_read: what the token ledger, deflation curve and realized $/M actually say. Cite the actual numbers (unit growth vs capex growth, deflation %/yr, dollar-growth ratio, realized $/M vs cost floor). Say clearly which side of the scissors is winning.
- silicon_view: translate into foundry/silicon durability — which usage classes (per-token API, internal cost-center, subscription, agents) back the CapEx→wafer arc, whether inference-led token growth sustains N4/N5+CoWoS demand even if training pauses. TSMC exposure is indirect (via NVIDIA/AMD/TPU).
- thesis: one directional call on demand reality (compounding / unpaid / stalling), confidence tied to whether unit growth, dollar growth and realized $/M agree, plus 3–5 LEADING signals that move before quarterly disclosures.

L5:
- scenarios: 3–4 scenarios with integer probabilities that SUM TO 100, each with a concrete numeric trigger. Build on the seed scenarios provided.
- falsification: 2–3 conditions that would prove the thesis wrong — one MUST be the token-growth-below-capex-growth-for-2-quarters bubble-confirmation line.
- early_warning: 5–7 indicators ordered by which lights up earliest, each with a check frequency (daily/weekly/monthly/quarterly).

Rules: cite actual numbers; estimates are estimates — do not launder curated T3 approximations into facts; token disclosure scopes differ by platform, never compare across scopes. No hedging boilerplate. Traditional Chinese. Each summary ≤ ~120 words / 160 字. Output only the structured object."""


def _fmt_l3(kb, l3, composite):
    led = l3["token_ledger"]
    dfl = l3["price_deflation"]
    mon = l3["monetization"]
    sci = l3["scissors"]
    lines = [
        f"Composite demand-reality {composite['score']}/100 → {composite['verdict']} "
        f"(0 = spend far ahead of use, 100 = usage real & compounding)",
        "Subscores: " + ", ".join(f"{s['id']} {s['score']} (w={s['weight']})" for s in composite["subscores"]),
        f"Scissors: token unit growth {sci['token_growth_yoy_pct']}%/yr (median across platforms) vs capex "
        f"{sci['capex_growth_yoy_pct']}%/yr (aligned /aibubble) → unit ratio {sci['unit_ratio']}×; "
        f"price change {sci['mean_price_change_pct_yr']}%/yr → token-DOLLAR growth {sci['dollar_growth_yoy_pct']}%/yr "
        f"→ dollar ratio {sci['dollar_ratio']}×.",
        "Token ledger (per platform, scope differs — growth computed within platform only):",
    ]
    for r in led["platforms"]:
        lines.append(f"  {r['name']} [{r['class']}]: {r['latest_monthly_tokens_t']}T/month ({r['latest_date']}, "
                     f"{r['latest_tier']}{' EST' if r['latest_est'] else ''}), latest annualized growth "
                     f"{r['growth_yoy_pct']}%/yr, ×{r['multiple_since_first']} since {r['first_date']}")
    lines.append("Price deflation (flagship blended $/M, 3:1 in:out):")
    for c in dfl["curves"]:
        lines.append(f"  {c['family_en']}: {c['first_blended']} → {c['last_blended']} $/M, "
                     f"{c['annual_change_pct']}%/yr" + (" [live]" if c.get("live") else ""))
    lines.append(f"Mean annual price change {dfl['mean_annual_change_pct']}%/yr (capability-adjusted deflation is deeper — unmeasurable).")
    for lab in mon["labs"]:
        pt = lab["points"][-1]
        lines.append(f"Realized $/M — {lab['name']}: ${lab['realized_usd_per_m']}/M "
                     f"(${pt['revenue_runrate_usd_bn']}B run-rate ÷ {pt['monthly_tokens_t']}T/month, ALL EST; "
                     f"trend {lab['realized_trend_pct_yr']}%/yr)" )
    floor = mon["serving_cost_floor"]
    lines.append(f"Serving-cost floor ${floor.get('usd_per_m_tokens')}/M (derived from vast.ai H100 $2.16/hr per /aibubble, "
                 f"T3 EST) → realized/floor {mon['realized_over_floor_x']}×.")
    seeds = "; ".join(f"{s['name_en']} — {s['trigger_en']}" for s in kb.get("scenarios_seed", []))
    lines.append(f"Seed scenarios to build on: {seeds}")
    return "\n".join(lines)


def _claude(kb, l3, composite):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    user = ("Here is today's L3 usage & token-economics read. Produce L4 and L5 as the structured object.\n\n"
            + _fmt_l3(kb, l3, composite))
    msg = client.messages.create(
        model=MODEL,
        max_tokens=6000,
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
def _rules(kb, l3, composite):
    led = l3["token_ledger"]
    dfl = l3["price_deflation"]
    mon = l3["monetization"]
    sci = l3["scissors"]
    score, verdict = composite["score"], composite["verdict"]
    lead = mon["labs"][0] if mon["labs"] else {}
    floor = mon["serving_cost_floor"]
    fastest = max(led["platforms"], key=lambda r: (r["growth_yoy_pct"] or 0))

    # ── L4 usage read ──
    ur_en = (f"Demand-reality {score}/100 → {verdict}. In UNITS the demand side is winning: median token growth "
             f"{sci['token_growth_yoy_pct']}%/yr across disclosing platforms (fastest {fastest['name']} at "
             f"{fastest['growth_yoy_pct']}%/yr) vs capex +{sci['capex_growth_yoy_pct']}% — a {sci['unit_ratio']}× unit ratio. "
             f"But flagship prices deflate {dfl['mean_annual_change_pct']}%/yr, compressing token-DOLLAR growth to "
             f"{sci['dollar_growth_yoy_pct']}%/yr — only {sci['dollar_ratio']}× of capex growth. Realized revenue per token "
             f"holds near ${lead.get('realized_usd_per_m')}/M ({lead.get('realized_trend_pct_yr')}%/yr, EST) at "
             f"{mon['realized_over_floor_x']}× the ${floor.get('usd_per_m_tokens')}/M serving-cost floor. "
             f"Usage is real; the dollars have not caught the spend.")
    ur_zh = (f"需求真實度 {score}/100 → {verdict}。以「單位」看,需求端在贏:各揭露平台 token 增速中位 "
             f"{sci['token_growth_yoy_pct']}%/yr(最快 {fastest['name']} 達 {fastest['growth_yoy_pct']}%/yr),"
             f"對比 capex +{sci['capex_growth_yoy_pct']}%——單位比 {sci['unit_ratio']} 倍。但旗艦價格年通縮 "
             f"{dfl['mean_annual_change_pct']}%,把 token「美元」增速壓到 {sci['dollar_growth_yoy_pct']}%/yr——"
             f"僅 capex 增速的 {sci['dollar_ratio']} 倍。每 token 已實現營收撐在約 ${lead.get('realized_usd_per_m')}/M"
             f"({lead.get('realized_trend_pct_yr')}%/yr,估計值),為推論成本地板 ${floor.get('usd_per_m_tokens')}/M 的 "
             f"{mon['realized_over_floor_x']} 倍。用量是真的;美元還沒追上支出。")

    # ── L4 silicon view ──
    sv_en = (f"For foundry durability the mix matters more than the total: per-token API + agent workloads "
             f"(the elastic classes) are compounding fastest — deflation converts directly into inference silicon demand, "
             f"which favors sustained N4/N5 + CoWoS pull even if training capex pauses. Google's "
             f"internal volume is strategy-funded TPU demand: durable while search cash funds it, but it validates no unit "
             f"economics. The scissors verdict is the demand-side check on the whole /compute→/cwengine→/payback arc: "
             f"units say the wafers will be used; dollars say the payback (/payback 22.2% coverage) still lags. "
             f"Watch the mix shift to agents — token-hungry, monetization unproven.")
    sv_zh = (f"對晶圓代工的耐久性,組合比總量重要:按 token 計價的 API 與 agent 工作負載(彈性類別)複利最快——"
             f"通縮直接轉成推論矽需求,即使訓練 capex 暫停,也支撐 N4/N5 + CoWoS 的持續拉貨。Google 的內部量是"
             f"策略出資的 TPU 需求:搜尋現金還買單就耐久,但驗證不了單位經濟。剪刀差結論就是整條 "
             f"/compute→/cwengine→/payback 弧線的需求端對照:單位說晶圓會被用掉;美元說回本(/payback 覆蓋率 22.2%)"
             f"還在落後。要盯的是組合往 agent 移——最吃 token、變現最未經證明。")

    # ── L4 thesis ──
    subs = {s["id"]: s["score"] for s in composite["subscores"]}
    agree = (subs["usage_vs_spend"] > 60) == (subs["dollar_scissors"] > 60)
    if verdict == "REAL-AND-COMPOUNDING":
        dir_en, dir_zh = ("Compounding — usage dollars and units both outrun spend; the buildout is demand-backed",
                          "複利中 — 用量美元與單位都跑贏支出;建置有需求背書")
        conf = "high" if agree else "medium"
    elif verdict == "GROWING-BUT-UNPAID":
        dir_en, dir_zh = ("Unpaid growth — units outrun capex but deflation holds dollars below it; monetization is the swing factor",
                          "成長但未變現 — 單位跑贏 capex,但通縮把美元壓在其下;變現是勝負手")
        conf = "medium" if not agree else "high"
    else:
        dir_en, dir_zh = ("Stalling — even unit growth trails capex; treat further capex as speculative",
                          "失速中 — 連單位增速都輸 capex;後續 capex 應視為投機")
        conf = "high" if subs["usage_vs_spend"] < 20 else "medium"

    leads = [
        {"en": f"OpenRouter public weekly tokens & model mix — the only daily-frequency public volume read (long-tail sample)",
         "zh": "OpenRouter 公開每週 token 量與模型組合——唯一日頻的公開量讀值(長尾樣本)"},
        {"en": f"Flagship price-cut announcements — each cut restarts the elasticity experiment (mean now {dfl['mean_annual_change_pct']}%/yr)",
         "zh": f"旗艦降價公告——每次降價都重啟彈性實驗(目前均值 {dfl['mean_annual_change_pct']}%/yr)"},
        {"en": "Platform token-disclosure cadence: Google I/O + earnings, Microsoft earnings 'tokens processed' lines",
         "zh": "平台 token 揭露節奏:Google I/O 與法說、Microsoft 法說的「處理 token 數」段落"},
        {"en": f"Lab revenue run-rate updates vs token statements — the realized $/M numerator and denominator (now ${lead.get('realized_usd_per_m')}/M)",
         "zh": f"實驗室營收 run-rate 更新 vs token 聲明——已實現 $/M 的分子與分母(現約 ${lead.get('realized_usd_per_m')}/M)"},
        {"en": "Agent-platform usage stats (coding agents, computer-use) — the marginal token engine with unproven monetization",
         "zh": "Agent 平台用量統計(程式 agent、電腦操作)——變現未證明的邊際 token 引擎"},
    ]

    # ── L5 scenario probabilities from the current read ──
    if verdict == "REAL-AND-COMPOUNDING":
        base = {"real_compounding": 45, "unpaid_grind": 35, "deflation_spiral": 12, "usage_stall": 8}
    elif verdict == "GROWING-BUT-UNPAID":
        base = {"real_compounding": 25, "unpaid_grind": 45, "deflation_spiral": 18, "usage_stall": 12}
    else:
        base = {"real_compounding": 8, "unpaid_grind": 27, "deflation_spiral": 30, "usage_stall": 35}
    seed_by_id = {s["id"]: s for s in kb.get("scenarios_seed", [])}
    scenarios = []
    for sid, prob in base.items():
        s = seed_by_id.get(sid, {})
        scenarios.append({
            "name_en": s.get("name_en", sid), "name_zh": s.get("name_zh", sid), "prob": prob,
            "trigger_en": s.get("trigger_en", ""), "trigger_zh": s.get("trigger_zh", ""),
        })

    falsification = [
        {"en": "Token UNIT growth falls below capex growth (+80.6%) for 2 consecutive quarters — the bubble-confirmation signal; flip to SPEND-AHEAD-OF-USE.",
         "zh": "token「單位」增速連兩季跌破 capex 增速(+80.6%)——泡沫確認訊號;轉為 SPEND-AHEAD-OF-USE。"},
        {"en": "Realized $/M-tokens drops >30%/yr while volume decelerates — deflation stops buying growth; the elasticity thesis fails.",
         "zh": "已實現 $/M tokens 年降逾 30% 且量同步減速——通縮買不到成長;彈性論點失效。"},
        {"en": "Token-dollar growth crosses ABOVE capex growth for 2 quarters (agents monetizing) — the thesis errs the other way; flip to REAL-AND-COMPOUNDING.",
         "zh": "token 美元增速連兩季升破 capex 增速(agent 變現成立)——論點往另一邊錯;轉為 REAL-AND-COMPOUNDING。"},
    ]
    early_warning = [
        {"en": "OpenRouter weekly token aggregate & top-model churn", "zh": "OpenRouter 每週 token 彙總與熱門模型輪替", "freq": "daily"},
        {"en": "Flagship API price-page changes (OpenAI / Anthropic / Google)", "zh": "旗艦 API 價格頁變動(OpenAI / Anthropic / Google)", "freq": "weekly"},
        {"en": "GPU spot rent vs serving-cost floor (vast.ai, per /aibubble)", "zh": "GPU 現貨租金對推論成本地板(vast.ai,見 /aibubble)", "freq": "weekly"},
        {"en": "Lab revenue run-rate headlines (numerator of realized $/M)", "zh": "實驗室營收 run-rate 頭條(已實現 $/M 的分子)", "freq": "monthly"},
        {"en": "Platform 'tokens processed' disclosures on earnings calls", "zh": "法說會上的「處理 token 數」揭露", "freq": "quarterly"},
        {"en": "Capex growth updates (/aibubble scissors supply side)", "zh": "capex 增速更新(/aibubble 剪刀差供給端)", "freq": "quarterly"},
    ]

    return {
        "engine": "rules",
        "l4": {
            "usage_read": {"summary_en": ur_en, "summary_zh": ur_zh},
            "silicon_view": {"summary_en": sv_en, "summary_zh": sv_zh},
            "thesis": {"direction_en": dir_en, "direction_zh": dir_zh, "confidence": conf, "leading_signals": leads},
        },
        "l5": {"scenarios": scenarios, "falsification": falsification, "early_warning": early_warning},
    }


def analyze(kb, l3, composite):
    """L4/L5 via Claude when ANTHROPIC_API_KEY is set; deterministic rules otherwise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, l3, composite)
        except Exception:
            log.exception("usage: Claude L4/L5 failed — falling back to rules")
    return _rules(kb, l3, composite)
