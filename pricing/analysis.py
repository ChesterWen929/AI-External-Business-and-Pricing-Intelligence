"""Pricing Power Radar — L4 / L5 synthesis.

L4 (CEO pricing-power read + pass-through interpretation + per-layer commentary
+ leading signals) and L5 (scenarios / falsification / watch-list) are written
from the L3 quant read. Two engines:

  • Claude — Opus 4.8, structured outputs (json_schema) + a prompt-cached
             framework system block. Used when ANTHROPIC_API_KEY is set.
  • rules  — deterministic fallback from the L3 numbers, so the board is fully
             functional offline / without a key.

The divergence ALERTS are computed deterministically in model._alerts() (data,
not prose) and live on l3; this module only narrates. The system prompt forbids
inventing confidential transaction prices — Claude may only reframe the public
proxies & curated estimates it is handed.

analyze(kb, l3) → {"engine": "claude"|"rules", "l4": {...}, "l5": {...}}
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("pricing.analysis")

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
                "pricing_power_read": _BI,
                "transmission_read": _BI,
                "layers": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"upstream": _BI, "foundry": _BI, "downstream": _BI},
                    "required": ["upstream", "foundry", "downstream"],
                },
                "leading_signals": {"type": "array", "items": _BI},
            },
            "required": ["pricing_power_read", "transmission_read", "layers", "leading_signals"],
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

SYSTEM = """You are the external pricing-intelligence officer for a leading foundry's CEO (think TSMC), writing a bilingual (Traditional Chinese + English) dashboard. You reason about a three-layer PRICE STACK: upstream supplier cost → foundry wafer/packaging ASP → downstream customer end-product ASP. The user has done the quant (L3) and hands you the live read. Your job is L4 and L5.

HARD RULES — anti-fabrication:
- Real per-customer wafer prices are CONFIDENTIAL. Never state a specific secret transaction price as fact. You may only reframe the public market proxies and the tier-graded curated estimates you are given.
- Cite the actual numbers handed to you (the layer momenta, pass-through %, score). If evidence is insufficient for a claim, say so — do not guess.
- Chinese in Traditional characters. No hedging boilerplate. Each text field ≤ ~110 words / 150 字.

L4 — the CEO read:
- pricing_power_read: in plain CEO language, can we raise prices right now? Tie it to the score and the foundry-vs-cost margin delta. State the verdict (DEFENSIBLE / NEUTRAL / SQUEEZED) and why.
- transmission_read: interpret the two pass-through numbers (upstream→foundry, foundry→downstream). >100% upstream→foundry = expanding margin / fully passing cost through; <100% = absorbing cost. foundry→downstream >100% = customers raising faster than us = room to raise; <100% = we are ahead of the end market.
- layers.upstream / foundry / downstream: one tight sentence each on what that layer is doing and what it means for TSMC.
- leading_signals: 3–5 things that move BEFORE TSMC's reported ASP (e.g. CoWoS lead-times, HBM contract resets, customer capex guides).

L5 — scenarios, falsification, watch:
- scenarios: 3–4 scenarios with integer probabilities that SUM TO 100, each with a concrete numeric trigger. Build on the seed scenarios but set probabilities from the current read.
- falsification: 2–3 conditions that would force the verdict to flip.
- watch: 5–7 monitoring items ordered by which moves earliest, each with a check frequency (daily/weekly/monthly).

Output only the structured object."""


def _fmt_l3(kb, l3):
    pp = l3["pricing_power"]
    st = l3["stack"]
    tr = l3["transmission"]
    mg = l3["margin"]
    ms = l3.get("market_sentiment", {})
    lines = [
        f"Pricing-power score: {pp['score']}/100 → verdict {pp['verdict_key'].upper()}",
        f"Bargaining momentum (1m %, equity proxies sentiment-damped & capped): upstream cost {st['upstream']}, foundry ASP {st['foundry']}, downstream ASP {st['downstream']}",
        f"Market-sentiment reference (RAW equity moves, NOT in the score — do not treat as cost/ASP): upstream {ms.get('upstream')}, foundry {ms.get('foundry')}, downstream {ms.get('downstream')}",
        f"Pass-through: upstream→foundry {tr['up_to_fab']}%, foundry→downstream {tr['fab_to_down']}%",
        f"Margin deltas: foundry−cost {mg['fab_delta']}, downstream−foundry {mg['chain_delta']}",
        "Deterministic alerts already raised: " + "; ".join(f"[{a['level']}] {a['en']}" for a in l3["alerts"]),
        "Per-layer items (1m %, tier, est?). For equity proxies the raw move and the damped value that actually entered the score are both shown — narrate from the damped score value, treat the raw move as sentiment only:",
    ]
    for ly in l3["layers"]:
        parts = []
        for r in ly["items"]:
            if r.get("weight", 0) <= 0:
                continue
            raw = r["chg_1m"]
            tag = f"{r['name_en']} {('+' if (raw or 0) >= 0 else '')}{raw}%"
            if r.get("momentum_kind") == "equity":
                tag += f" (equity·sentiment; damped→{r.get('score_chg_1m')}% in score)"
            tag += f"/{r['tier']}{'·est' if r['is_estimate'] else ''}{'·⚑calibrate' if r.get('needs_calibration') else ''}"
            parts.append(tag)
        lines.append(f"  [{ly['name_en']}] {', '.join(parts)}")
    seeds = "; ".join(f"{s['name_en']} — {s['trigger_en']}" for s in kb.get("scenarios_seed", []))
    lines.append(f"Seed scenarios to build on: {seeds}")
    return "\n".join(lines)


def _claude(kb, l3):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    user = (
        "Here is today's live L3 price-stack read for the foundry. Produce L4 and L5 as the structured object.\n\n"
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
    pp = l3["pricing_power"]
    st = l3["stack"]
    tr = l3["transmission"]
    mg = l3["margin"]
    u, f, d = st["upstream"], st["foundry"], st["downstream"]
    key = pp["verdict_key"]

    # ── L4 ──
    if key == "defensible":
        ppr_en = (f"Score {pp['score']}/100 — DEFENSIBLE. Foundry ASP (+{f}%/m) is running ahead of input cost "
                  f"(+{u}%/m), a margin delta of {mg['fab_delta']}. With downstream demand at +{d}%/m, there is room "
                  f"to push leading-edge & CoWoS pricing. You can raise — lead with the capacity-constrained nodes.")
        ppr_zh = (f"分數 {pp['score']}/100 — DEFENSIBLE。代工 ASP(+{f}%/月)領先投入成本(+{u}%/月),毛利差 "
                  f"{mg['fab_delta']}。下游需求 +{d}%/月,先進製程與 CoWoS 有漲價空間。可以漲 — 從產能受限節點先漲。")
    elif key == "squeezed":
        ppr_en = (f"Score {pp['score']}/100 — SQUEEZED. Input cost (+{u}%/m) is outrunning foundry ASP (+{f}%/m); "
                  f"margin delta {mg['fab_delta']}. Pushing list price into soft end-demand (+{d}%/m) risks order loss — "
                  f"protect margin via mix and cost pass-through, not blanket hikes.")
        ppr_zh = (f"分數 {pp['score']}/100 — SQUEEZED。投入成本(+{u}%/月)跑贏代工 ASP(+{f}%/月);毛利差 "
                  f"{mg['fab_delta']}。在疲弱終端(+{d}%/月)硬漲牌價恐丟單 — 以產品組合與成本轉嫁護毛利,而非全面漲價。")
    else:
        ppr_en = (f"Score {pp['score']}/100 — NEUTRAL. Cost (+{u}%/m), foundry ASP (+{f}%/m) and end-demand (+{d}%/m) "
                  f"are moving roughly together (margin delta {mg['fab_delta']}). Selective hikes on tight nodes only; "
                  f"hold the line elsewhere.")
        ppr_zh = (f"分數 {pp['score']}/100 — NEUTRAL。成本(+{u}%/月)、代工 ASP(+{f}%/月)與終端需求(+{d}%/月)"
                  f"大致同步(毛利差 {mg['fab_delta']})。僅對緊缺節點選擇性漲價,其餘守住。")

    def _pt(x):
        return "n/a" if x is None else f"{x}%"

    uf, fd = tr["up_to_fab"], tr["fab_to_down"]
    tr_en = (f"Upstream→foundry pass-through {_pt(uf)} "
             + ("(>100% — expanding margin, more than passing cost through). " if (uf is not None and uf > 100)
                else "(<100% — absorbing some input cost). " if uf is not None
                else "(cost move too small to read). ")
             + f"Foundry→downstream {_pt(fd)} "
             + ("(>100% — customers raising faster than you; room to raise)." if (fd is not None and fd > 100)
                else "(<100% — you are ahead of the end market)." if fd is not None
                else "(foundry move too small to read)."))
    tr_zh = (f"上游→代工轉嫁 {_pt(uf)} "
             + ("(>100% — 擴張毛利,漲幅超過成本轉嫁)。" if (uf is not None and uf > 100)
                else "(<100% — 吸收部分投入成本)。" if uf is not None
                else "(成本變動過小,難判讀)。")
             + f"代工→下游 {_pt(fd)} "
             + ("(>100% — 客戶漲價快於你;有漲價空間)。" if (fd is not None and fd > 100)
                else "(<100% — 你領先終端市場)。" if fd is not None
                else "(代工變動過小,難判讀)。"))

    def layer_line(lid):
        ly = next((x for x in l3["layers"] if x["id"] == lid), {})
        return ly.get("momentum", 0.0), ly

    u_m, _ = layer_line("up")
    f_m, _ = layer_line("fab")
    d_m, _ = layer_line("down")
    layers = {
        "upstream": {"en": f"Supplier cost +{u_m}%/m — equipment/silicon/substrate inflation feeds per-wafer cost.",
                     "zh": f"供應商成本 +{u_m}%/月 — 設備/矽晶圓/載板通膨灌入每片成本。"},
        "foundry": {"en": f"Foundry ASP +{f_m}%/m — led by capacity-tight CoWoS & N3 leading-edge.",
                    "zh": f"代工 ASP +{f_m}%/月 — 由產能緊缺的 CoWoS 與 N3 先進製程帶動。"},
        "downstream": {"en": f"Customer end-ASP +{d_m}%/m — AI accelerator & HBM demand sets the room to raise.",
                       "zh": f"客戶終端 ASP +{d_m}%/月 — AI 加速器與 HBM 需求決定漲價空間。"},
    }

    leads = [
        {"en": "CoWoS lead-times & booked capacity — the tightest bottleneck reprices first.", "zh": "CoWoS 交期與已訂產能 — 最緊瓶頸最先重訂價。"},
        {"en": "HBM3E contract resets — a co-input cost that crowds out wafer budget.", "zh": "HBM3E 合約重訂 — 排擠晶圓預算的共同投入成本。"},
        {"en": "Customer (NVDA/AMD) capex & ASP guides — demand-side room to raise.", "zh": "客戶(NVDA/AMD)capex 與 ASP 指引 — 需求端的漲價空間。"},
        {"en": "Silicon-wafer & substrate contract resets — upstream cost lead.", "zh": "矽晶圓與載板合約重訂 — 上游成本先行。"},
        {"en": "Samsung/Intel discounting — the cap on your leading-edge hikes.", "zh": "三星/Intel 折讓 — 你先進製程漲幅的天花板。"},
    ]

    # ── L5 — scenario probabilities from the verdict ──
    base = {"defensible": 25, "squeeze": 25, "passthrough_war": 25, "oversupply": 25}
    if key == "defensible":
        base = {"defensible": 48, "squeeze": 14, "passthrough_war": 23, "oversupply": 15}
    elif key == "squeezed":
        base = {"defensible": 15, "squeeze": 45, "passthrough_war": 25, "oversupply": 15}
    else:
        base = {"defensible": 30, "squeeze": 25, "passthrough_war": 27, "oversupply": 18}
    seed_by_id = {s["id"]: s for s in kb.get("scenarios_seed", [])}
    scenarios = []
    for sid, prob in base.items():
        s = seed_by_id.get(sid, {})
        scenarios.append({
            "name_en": s.get("name_en", sid), "name_zh": s.get("name_zh", sid), "prob": prob,
            "trigger_en": s.get("trigger_en", ""), "trigger_zh": s.get("trigger_zh", ""),
        })

    falsification = list(kb.get("falsification_seed", []))
    watch = list(kb.get("watch_seed", []))

    return {
        "engine": "rules",
        "l4": {
            "pricing_power_read": {"en": ppr_en, "zh": ppr_zh},
            "transmission_read": {"en": tr_en, "zh": tr_zh},
            "layers": layers,
            "leading_signals": leads,
        },
        "l5": {"scenarios": scenarios, "falsification": falsification, "watch": watch},
    }


def analyze(kb, l3):
    """L4/L5 via Claude when ANTHROPIC_API_KEY is set; deterministic rules otherwise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, l3)
        except Exception:
            log.exception("pricing: Claude L4/L5 failed — falling back to rules")
    return _rules(kb, l3)
