"""Company Deep-Dive — L4 / L5 synthesis.

L4 (the four-pillar CEO read + an integrated thesis tying compute-pricing →
AI benefit → TSMC dependency) and L5 (scenarios / falsification / watch) are
written from the L1-L3 pillar reads. Two engines:

  • Claude — Opus 4.8, structured outputs (json_schema) + a prompt-cached
             framework system block. Used when ANTHROPIC_API_KEY is set.
  • rules  — deterministic fallback from the pillar numbers, so the board is
             fully functional offline / without a key.

The system prompt forbids inventing confidential AWS transaction prices or a
reported "AI revenue" figure — Claude may only reframe the labeled estimates &
public proxies it is handed, written for a TSMC CEO.

analyze(kb, pillars, l3) → {"engine": "claude"|"rules", "l4": {...}, "l5": {...}}
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("company.analysis")

MODEL = "claude-opus-4-8"

_BI = {"type": "object", "additionalProperties": False,
       "properties": {"en": {"type": "string"}, "zh": {"type": "string"}},
       "required": ["en", "zh"]}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "l4": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "integrated_thesis": _BI,
                "pillars": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"pricing": _BI, "sources": _BI, "benefit": _BI, "silicon": _BI},
                    "required": ["pricing", "sources", "benefit", "silicon"],
                },
                "tsmc_implication": _BI,
            },
            "required": ["integrated_thesis", "pillars", "tsmc_implication"],
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

SYSTEM = """You are the external single-company intelligence officer for a leading foundry's CEO (think TSMC), writing a bilingual (Traditional Chinese + English) dashboard. You analyze ONE company at a time through four pillars and always close the loop back to what it means for TSMC's leading-edge & CoWoS business.

The four pillars (the user has done the quant; you write the read):
  A · PRICING — how the company raises its REALIZED price per unit of compute. Critical nuance: raw list prices fall every year; realized price rises via levers (custom-silicon margin capture, mix-shift up the stack, scarcity premium, commitment lock-in, value-based token pricing). Tie your read to the pricing-power SCORE and the top lever.
  B · SOURCES — the data-source dictionary: name the highest-signal places to actually observe pillar A (filings, price APIs, GPU spot trackers, TSMC disclosures).
  C · BENEFIT — how much the company makes from AI. The company discloses NO AI-only figure, so you reason from labeled multi-method ESTIMATES handed to you. Never state an estimate as a reported fact.
  D · SILICON — the chain from each accelerator → process node → CoWoS packaging → TSMC, and the TSMC-exposure %. This is where you make it matter to a foundry CEO.

HARD RULES — anti-fabrication:
- Confidential AWS transaction prices and a reported "AI revenue" line do NOT exist publicly. Never present an estimate as a disclosed number. Cite the actual figures handed to you (score, $bn estimates with their metric, exposure %).
- Chinese in Traditional characters. No hedging boilerplate. Each text field ≤ ~110 words / 150 字.

L4 — the reads:
- integrated_thesis: the one-paragraph spine — realized-price engine → AI benefit → TSMC dependency. CEO language.
- pillars.pricing / sources / benefit / silicon: one tight read each, anchored to the numbers.
- tsmc_implication: the explicit "so what for TSMC" — is this company a direct advanced-node customer, an indirect-via-NVIDIA one, both? where is the binding constraint?

L5:
- scenarios: 3-4 scenarios with integer probabilities that SUM TO 100, each with a concrete numeric trigger. Build on the seed scenarios, set probabilities from the current read.
- falsification: 2-3 conditions that would break the pricing/benefit thesis.
- watch: 5-7 monitoring items ordered by which moves earliest, each with a check frequency.

Output only the structured object."""


def _fmt(kb, pillars, l3):
    p = pillars["pricing"]
    b = pillars["benefit"]
    s = pillars["silicon"]
    lines = [
        f"COMPANY: {kb.get('company', {}).get('name_en')} ({kb.get('company', {}).get('ticker')})",
        f"Pricing-power score: {p['score']}/100 → verdict {p['verdict_key'].upper()}. Top lever: {p.get('top_lever_id')}.",
        "Pricing levers (strength 0-100 · weight · tier):",
    ]
    for lv in p["levers"]:
        lines.append(f"  - {lv['name_en']}: strength {lv['strength']}, w{lv['weight']}, {lv['tier']}{'·est' if lv['is_estimate'] else ''}")
    lines.append("AI-benefit estimates (labeled, NOT reported):")
    for e in b["estimates"]:
        lines.append(f"  - {e['method_en']}: ${e['value_usd_bn']}B ({e['metric']}), confidence {e['confidence']}, {e['tier']}")
    lines.append(f"Benefit headline: ${b.get('headline_usd_bn')}B {b.get('headline_metric')}; consensus ${b.get('consensus_usd_bn')}B.")
    lines.append(f"TSMC exposure: ~{s['tsmc_exposure_pct']}% TSMC-fabbed, {s['critical_count']} critical link(s). Silicon chain:")
    for c in s["chain"]:
        lines.append(f"  - {c['accelerator_en']} [{c['designer']}] → {c['node']} / {c['packaging']} @ {c['fab']} — dependency {c['dependency']} ({c['tier']})")
    lines.append("Data sources (pillar B): " + "; ".join(d["name_en"] for d in pillars["sources"]["items"]))
    lines.append("Live proxies (sentiment context only): " + "; ".join(
        f"{pr['name_en']} {pr.get('chg_1m')}%/m" for pr in l3["proxies"]))
    lines.append("Deterministic alerts already raised: " + "; ".join(f"[{a['level']}] {a['en']}" for a in l3["alerts"]))
    seeds = "; ".join(f"{x['name_en']} — {x['trigger_en']}" for x in kb.get("scenarios_seed", []))
    lines.append(f"Seed scenarios to build on: {seeds}")
    return "\n".join(lines)


def _claude(kb, pillars, l3):
    import anthropic

    client = anthropic.Anthropic()
    user = (
        "Here is today's pillar read for this company. Produce L4 and L5 as the structured object.\n\n"
        + _fmt(kb, pillars, l3)
    )
    msg = client.messages.create(
        model=MODEL,
        # 6000: zh-heavy bilingual JSON for KBs with six pillar-A levers was
        # truncating at 4500 → JSONDecodeError → silent rules fallback (AMD).
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
# Rules fallback — deterministic L4/L5 from the pillar numbers
# --------------------------------------------------------------------------- #
def _rules(kb, pillars, l3):
    p = pillars["pricing"]
    b = pillars["benefit"]
    s = pillars["silicon"]
    name_en = kb.get("company", {}).get("name_en", "the company")
    name_zh = kb.get("company", {}).get("name_zh", "該公司")
    top = next((lv for lv in p["levers"] if lv["id"] == p.get("top_lever_id")), p["levers"][0] if p["levers"] else {})
    score = p["score"]
    bn = b.get("headline_usd_bn")
    metric = b.get("headline_metric")
    exp = s["tsmc_exposure_pct"]

    thesis_en = (f"{name_en} raises REALIZED compute price even as list prices fall — score {score}/100 ({p['verdict_key'].upper()}), "
                 f"led by '{top.get('name_en', '')}'. That engine underwrites an estimated ${bn}B {metric} of AI benefit, and ~{exp}% "
                 f"of the silicon beneath it is TSMC-fabbed — so the upside and its binding constraint both sit on TSMC's leading-edge & CoWoS.")
    thesis_zh = (f"{name_zh} 在牌價下跌之際仍提高已實現算力售價 — 分數 {score}/100（{p['verdict_key'].upper()}），"
                 f"由「{top.get('name_zh', '')}」帶動。這部引擎撐起估計 ${bn}B {metric} 的 AI 利益，而其底層約 {exp}% 的矽由台積電製造 — "
                 f"因此上行空間與綁定約束都押在台積電先進製程與 CoWoS。")

    pricing_en = (f"Score {score}/100 — {p['verdict_key'].upper()}. The work is done by '{top.get('name_en','')}' and the up-stack levers, "
                  f"not list-price hikes. Realized price per useful compute is rising through mix and margin capture.")
    pricing_zh = (f"分數 {score}/100 — {p['verdict_key'].upper()}。推動者是「{top.get('name_zh','')}」與服務棧上移槓桿，"
                  f"而非抬牌價。每單位有用算力的已實現價格靠組合與利差捕獲在上升。")

    src_names = ", ".join(d["name_en"] for d in pillars["sources"]["items"][:4])
    sources_en = f"Watch it via: {src_names} — segment filings + RPO are the hard floor; GPU spot rents & price APIs lead."
    sources_zh = "觀測管道：10-Q/10-K 分部與 RPO 是硬底，GPU 現貨租金與價格 API 領先。"

    is_est = b.get("headline_is_estimate", True)
    label_en = "an ESTIMATE" if is_est else "DISCLOSED"
    label_zh = "為估計值" if is_est else "為揭露值"
    disc_en = b.get("disclosure_note_en") or "no AI-only line is disclosed. Cross-checked against op-income and stake lenses."
    disc_zh = b.get("disclosure_note_zh") or "無純 AI 揭露項。已用營益與持股視角交叉驗證。"
    benefit_en = (f"AI benefit headline ≈ ${bn}B {metric} — {label_en} ({'consensus $' + str(b.get('consensus_usd_bn')) + 'B' if b.get('consensus_usd_bn') else 'single-method'}); "
                  f"{disc_en}")
    benefit_zh = (f"AI 利益頭條 ≈ ${bn}B {metric} — {label_zh}（{'共識 $' + str(b.get('consensus_usd_bn')) + 'B' if b.get('consensus_usd_bn') else '單一方法'}）；"
                  f"{disc_zh}")

    crit = s["critical_count"]
    sil_tail_en = kb.get("silicon_summary_en") or "Custom Trainium/Inferentia/Graviton AND the resold NVIDIA GPUs are all TSMC N3-N5 + CoWoS."
    sil_tail_zh = kb.get("silicon_summary_zh") or "自研 Trainium/Inferentia/Graviton 加上轉售的 NVIDIA GPU 全為台積電 N3-N5＋CoWoS。"
    silicon_en = f"~{exp}% TSMC-fabbed across {s['chain_count']} accelerators, {crit} critical. {sil_tail_en}"
    silicon_zh = f"{s['chain_count']} 顆加速器約 {exp}% 由台積電製造，{crit} 條關鍵。{sil_tail_zh}"

    tsmc_en = (s.get("tsmc_read_en") or "")[:600]
    tsmc_zh = (s.get("tsmc_read_zh") or "")[:600]

    # scenarios from verdict
    seeds = {x["id"]: x for x in kb.get("scenarios_seed", [])}
    if p["verdict_key"] == "raising":
        base = {"silicon_flywheel": 40, "managed_repricing": 27, "cowos_bottleneck": 21, "gpu_glut": 12}
    elif p["verdict_key"] == "eroding":
        base = {"gpu_glut": 38, "cowos_bottleneck": 24, "managed_repricing": 22, "silicon_flywheel": 16}
    else:
        base = {"silicon_flywheel": 30, "managed_repricing": 27, "cowos_bottleneck": 23, "gpu_glut": 20}
    scenarios = []
    for sid, prob in base.items():
        sc = seeds.get(sid, {})
        scenarios.append({
            "name_en": sc.get("name_en", sid), "name_zh": sc.get("name_zh", sid), "prob": prob,
            "trigger_en": sc.get("trigger_en", ""), "trigger_zh": sc.get("trigger_zh", ""),
        })

    return {
        "engine": "rules",
        "l4": {
            "integrated_thesis": {"en": thesis_en, "zh": thesis_zh},
            "pillars": {
                "pricing": {"en": pricing_en, "zh": pricing_zh},
                "sources": {"en": sources_en, "zh": sources_zh},
                "benefit": {"en": benefit_en, "zh": benefit_zh},
                "silicon": {"en": silicon_en, "zh": silicon_zh},
            },
            "tsmc_implication": {"en": tsmc_en, "zh": tsmc_zh},
        },
        "l5": {
            "scenarios": scenarios,
            "falsification": list(kb.get("falsification_seed", [])),
            "watch": list(kb.get("watch_seed", [])),
        },
    }


def analyze(kb, pillars, l3):
    """L4/L5 via Claude when ANTHROPIC_API_KEY is set; deterministic rules otherwise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, pillars, l3)
        except Exception:
            log.exception("company: Claude L4/L5 failed — falling back to rules")
    return _rules(kb, pillars, l3)
