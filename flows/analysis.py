"""Capital Flow Radar — L4 / L5 synthesis.

L4 (retail-vs-institution split + AI-market mapping + directional thesis) and
L5 (scenarios / triggers / falsification / early-warning) are synthesized from
the L3 quant read. Two engines:

  • Claude  — Opus 4.8, structured outputs (json_schema) + a prompt-cached
              5-layer framework system block. Used when ANTHROPIC_API_KEY is set.
  • rules   — deterministic fallback derived from the L3 numbers, so the
              dashboard is fully functional offline / without a key.

analyze(kb, l3) → {"engine": "claude"|"rules", "l4": {...}, "l5": {...}}
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("flows.analysis")

MODEL = "claude-opus-4-8"

# ── output contract (shared by both engines; enforced as json_schema on Claude) ──
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "l4": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "retail_vs_institution": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"summary_en": {"type": "string"}, "summary_zh": {"type": "string"}},
                    "required": ["summary_en", "summary_zh"],
                },
                "ai_mapping": {
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
            "required": ["retail_vs_institution", "ai_mapping", "thesis"],
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

SYSTEM = """You are a cross-asset capital-flow strategist writing a bilingual (Traditional Chinese + English) dashboard. You reason in five layers; the user has already done L1–L3 and gives you the live L3 read. Your job is L4 and L5.

L4 — behavioral split + AI mapping:
- retail_vs_institution: read the retail appetite vs institution appetite and the divergence. Flag "topping-type" patterns (retail hot while institutions raise cash / credit & vol calm-but-turning). Be concrete; cite the numbers given.
- ai_mapping: connect the flow picture specifically to the AI/semiconductor market — AI leadership (SOXX vs SPX), breadth (equal-weight participation), the liquidity tide, and stablecoin/crypto risk appetite. Say whether flows support AI continuation or warn of a turn.
- thesis: one directional call on the AI market (bullish / bearish / mixed), a confidence (high/medium/low) tied to whether the three lenses agree, and 3–5 LEADING signals (things that move before price).

L5 — scenarios, triggers, falsification, monitoring:
- scenarios: 3–4 scenarios with integer probabilities that SUM TO 100. Each needs a concrete numeric trigger. Build on the seed scenarios provided but set probabilities from the current read.
- falsification: 2–3 conditions that would prove the thesis wrong and demand a flip.
- early_warning: 5–7 indicators ordered by which lights up earliest, each with a check frequency (daily/weekly/monthly).

Rules: be specific and cite the actual numbers. No hedging boilerplate. Chinese in Traditional characters. Keep each summary ≤ ~110 words / 150 字. Output only the structured object."""


def _fmt_l3(kb, l3):
    md = l3["marginal_direction"]
    rvi = l3["retail_vs_inst"]
    ai = l3["ai_signal"]
    lz = l3["lenses"]
    d = l3["derived"]
    nl = d["net_liquidity"]
    # M19 sanity guard: only cite the net-liquidity number if it passed the
    # range check (3–9 $T); otherwise tell the model the read is unreliable so a
    # unit/scale bug never gets written into the narrative.
    if nl.get("sane", True) and nl.get("value") is not None:
        nl_line = (f"Derived — net liquidity {nl['value']}$B (1m {nl['chg_1m']}%), "
                   f"breadth {d['breadth']['value']} (RSP−SPX 1m), "
                   f"AI leadership {d['ai_rel']['value']} (SOXX−SPX 1m)")
    else:
        nl_line = ("Derived — net liquidity UNRELIABLE this run (failed range check; "
                   "do NOT cite a level), "
                   f"breadth {d['breadth']['value']} (RSP−SPX 1m), "
                   f"AI leadership {d['ai_rel']['value']} (SOXX−SPX 1m)")
    lines = [
        f"Marginal capital direction: {md['score']} ({md['label_en']})",
        f"Lenses — liquidity {lz['liquidity']['score']}, price {lz['price']['score']}, positioning {lz['positioning']['score']}; aligned={lz['aligned']}",
        f"Retail appetite {rvi['retail']} vs institution {rvi['institution']}; divergence {rvi['divergence']} (warning={rvi['warning']})",
        f"AI continuation signal {ai['score']}/100 ({ai['label_en']})",
        nl_line,
        "Reservoirs (1m % / point change, signal):",
    ]
    for res in l3["reservoirs"]:
        items = ", ".join(f"{r['name_en']} {r['chg_1m']}{r['unit'] and ('' if r['unit']=='%' else '')}→{r['signal']}" for r in res["indicators"])
        lines.append(f"  [{res['name_en']} / {res['signal']}] {items}")
    seeds = "; ".join(f"{s['name_en']} — {s['trigger_en']}" for s in kb.get("scenarios_seed", []))
    lines.append(f"Seed scenarios to build on: {seeds}")
    return "\n".join(lines)


def _claude(kb, l3):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    user = (
        "Here is today's live L3 capital-flow read. Produce L4 and L5 as the structured object.\n\n"
        + _fmt_l3(kb, l3)
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4000,
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
    md = l3["marginal_direction"]["score"]
    rvi = l3["retail_vs_inst"]
    ai = l3["ai_signal"]
    lz = l3["lenses"]
    d = l3["derived"]
    nl = d["net_liquidity"]
    breadth = d["breadth"]["value"]
    ai_rel = d["ai_rel"]["value"]

    # ── L4 ──
    if rvi["warning"]:
        rvi_en = (f"Divergence is wide: retail appetite {rvi['retail']} runs hot vs institution {rvi['institution']} "
                  f"(gap {rvi['divergence']}). Retail proxies (BTC/ARKK/IWM) leading while credit & vol stay calm is a "
                  f"classic late-stage pattern — watch for the gap to close via a risk-asset wobble, not a vol collapse.")
        rvi_zh = (f"背離偏大:散戶情緒 {rvi['retail']} 偏熱,機構 {rvi['institution']}(差距 {rvi['divergence']})。"
                  f"散戶代理(BTC/ARKK/IWM)領先、同時信用與波動仍平靜,是典型的後段格局——留意缺口是以風險資產回檔收斂,而非波動崩落。")
    elif rvi["divergence"] < -25:
        rvi_en = (f"Retail appetite {rvi['retail']} runs COOLER than the credit/vol picture {rvi['institution']} "
                  f"(gap {rvi['divergence']}). Retail proxies (BTC/ARKK/IWM) are pulling back while spreads & vol stay "
                  f"calm — caution, not euphoria; not a topping tell, but watch whether retail follows risk lower.")
        rvi_zh = (f"散戶情緒 {rvi['retail']} 比信用/波動所反映的 {rvi['institution']} 更冷(差距 {rvi['divergence']})。"
                  f"散戶代理(BTC/ARKK/IWM)正回落,但利差與波動仍平靜——是謹慎而非狂熱;非見頂訊號,"
                  f"但留意散戶是否帶動風險續跌。")
    else:
        rvi_en = (f"Retail {rvi['retail']} and institution {rvi['institution']} appetite are broadly in line "
                  f"(gap {rvi['divergence']}) — no euphoric retail-vs-smart-money split right now.")
        rvi_zh = (f"散戶 {rvi['retail']} 與機構 {rvi['institution']} 情緒大致一致(差距 {rvi['divergence']})——"
                  f"目前沒有散戶狂熱對機構撤離的明顯分裂。")

    ai_state = ai["label_en"].lower()
    aim_en = (f"AI-continuation signal {ai['score']}/100 ({ai['label_en']}). AI leadership (SOXX−SPX 1m) is {ai_rel} and "
              f"breadth (RSP−SPX 1m) is {breadth}. " +
              ("Narrow breadth with AI still leading = the index leans on a few names; fragile if liquidity rolls over."
               if breadth < 0 else
               "Broadening breadth alongside AI leadership is the healthy combination for continuation."))
    aim_zh = (f"AI 延續訊號 {ai['score']}/100({ai['label_zh']})。AI 領先度(SOXX−SPX 1m)為 {ai_rel},"
              f"廣度(RSP−SPX 1m)為 {breadth}。" +
              ("廣度收窄但 AI 仍領漲 = 指數靠少數權值撐住,流動性一翻就脆弱。" if breadth < 0 else
               "廣度擴散 + AI 領漲是延續的健康組合。"))

    if md > 25 and ai["score"] >= 55:
        dir_en, dir_zh, conf = "Constructive — flows still support AI continuation", "偏多 — 資金仍支撐 AI 延續", ("high" if lz["aligned"] else "medium")
    elif md < -25 or ai["score"] <= 40:
        dir_en, dir_zh, conf = "Cautious — flows are draining from risk / AI", "偏空 — 資金正撤離風險 / AI", ("high" if lz["aligned"] else "medium")
    else:
        dir_en, dir_zh, conf = "Mixed — two-way flows, no clean trend", "分歧 — 雙向資金、無明確趨勢", "low"

    leads = [
        {"en": f"Net liquidity 1m change ({nl['chg_1m']}%) — the master tide", "zh": f"淨流動性月變化({nl['chg_1m']}%)——主潮"},
        {"en": f"Breadth RSP−SPX ({breadth}) — participation vs concentration", "zh": f"廣度 RSP−SPX({breadth})——參與度 vs 集中"},
        {"en": f"HY credit spread direction — earliest risk-off tell", "zh": "高收益信用利差方向——最早的 risk-off 訊號"},
        {"en": f"Stablecoin supply trend — crypto dry powder", "zh": "穩定幣供應趨勢——加密乾火藥"},
        {"en": f"AI leadership SOXX−SPX ({ai_rel}) — thesis temperature", "zh": f"AI 領先度 SOXX−SPX({ai_rel})——論點溫度"},
    ]

    # ── L5 — scenario probabilities from the current read ──
    base = {"continuation": 25, "rotation": 25, "blowoff": 25, "regime_break": 25}
    if md > 25 and ai["score"] >= 55:
        base = {"continuation": 45, "rotation": 25, "blowoff": 18, "regime_break": 12}
    elif md < -25 or ai["score"] <= 40:
        base = {"continuation": 18, "rotation": 27, "blowoff": 15, "regime_break": 40}
    elif rvi["warning"]:
        base = {"continuation": 28, "rotation": 27, "blowoff": 30, "regime_break": 15}
    seed_by_id = {s["id"]: s for s in kb.get("scenarios_seed", [])}
    scenarios = []
    for sid, prob in base.items():
        s = seed_by_id.get(sid, {})
        scenarios.append({
            "name_en": s.get("name_en", sid), "name_zh": s.get("name_zh", sid), "prob": prob,
            "trigger_en": s.get("trigger_en", ""), "trigger_zh": s.get("trigger_zh", ""),
        })

    falsification = [
        {"en": "HY OAS widens >50bp with VIX >22 while the index holds — credit is leading; flip cautious.",
         "zh": "HY OAS 擴張 >50bp 且 VIX >22 但指數仍撐——信用在領先;轉保守。"},
        {"en": "Net liquidity turns down for 3+ weeks while AI keeps making highs — the tide no longer supports the price.",
         "zh": "淨流動性連 3 週以上下降但 AI 仍創高——潮汐已不支撐價格。"},
        {"en": "Breadth collapses (RSP−SPX deeply negative) while marginal direction reads risk-on — concentration, not participation.",
         "zh": "廣度崩落(RSP−SPX 深度為負)但邊際方向仍 risk-on——是集中而非參與。"},
    ]
    early_warning = [
        {"en": "HY credit spread (BAMLH0A0HYM2)", "zh": "高收益信用利差", "freq": "daily"},
        {"en": "VIX level & 1w change", "zh": "VIX 水準與週變化", "freq": "daily"},
        {"en": "Net liquidity (WALCL−RRP−TGA)", "zh": "淨流動性(WALCL−RRP−TGA)", "freq": "weekly"},
        {"en": "Breadth RSP−SPX relative", "zh": "廣度 RSP−SPX 相對", "freq": "daily"},
        {"en": "Stablecoin supply trend", "zh": "穩定幣供應趨勢", "freq": "daily"},
        {"en": "Retail-vs-institution divergence", "zh": "散戶vs機構背離", "freq": "weekly"},
        {"en": "AI leadership SOXX−SPX", "zh": "AI 領先度 SOXX−SPX", "freq": "daily"},
    ]

    return {
        "engine": "rules",
        "l4": {
            "retail_vs_institution": {"summary_en": rvi_en, "summary_zh": rvi_zh},
            "ai_mapping": {"summary_en": aim_en, "summary_zh": aim_zh},
            "thesis": {"direction_en": dir_en, "direction_zh": dir_zh, "confidence": conf, "leading_signals": leads},
        },
        "l5": {"scenarios": scenarios, "falsification": falsification, "early_warning": early_warning},
    }


def analyze(kb, l3):
    """L4/L5 via Claude when ANTHROPIC_API_KEY is set; deterministic rules otherwise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, l3)
        except Exception:
            log.exception("flows: Claude L4/L5 failed — falling back to rules")
    return _rules(kb, l3)
