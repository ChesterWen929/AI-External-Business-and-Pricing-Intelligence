"""Cycle Analogue Clock — L4 / L5 synthesis.

L4: where this cycle looks like 1999 and where it is STRUCTURALLY unlike
    (hyperscaler profitability & real cash flows vs levered telcos; physical
    scarcity of power/packaging vs fiber glut; 4–6-yr GPU depreciation vs
    25-yr fiber), plus a one-paragraph clock take.
L5: falsification / monitor list — 「若 X 發生, 時鐘跳到 Y」— and the leading
    indicators that move the clock forward.

Two engines:
  • Claude — claude-opus-4-8, structured output (json_schema), max_tokens 6000
             (zh-heavy bilingual JSON truncates below that). Used when
             ANTHROPIC_API_KEY is set.
  • rules  — deterministic fallback assembled from the curated KB seeds plus
             the computed L3 numbers, so the card is fully functional offline.

analyze(kb, l3) → {"engine": "claude"|"rules", "l4": {...}, "l5": {...}}
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("cycle_clock.analysis")

MODEL = "claude-opus-4-8"

_BI = {
    "type": "object", "additionalProperties": False,
    "properties": {"en": {"type": "string"}, "zh": {"type": "string"}},
    "required": ["en", "zh"],
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "l4": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "clock_take": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"summary_en": {"type": "string"}, "summary_zh": {"type": "string"}},
                    "required": ["summary_en", "summary_zh"],
                },
                "like_1999": {"type": "array", "items": _BI},
                "structurally_unlike": {"type": "array", "items": _BI},
            },
            "required": ["clock_take", "like_1999", "structurally_unlike"],
        },
        "l5": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "jumps": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "condition_en": {"type": "string"}, "condition_zh": {"type": "string"},
                            "jump_to": {"type": "string"},
                            "direction": {"type": "string", "enum": ["forward", "backward"]},
                        },
                        "required": ["condition_en", "condition_zh", "jump_to", "direction"],
                    },
                },
                "forward_movers": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"en": {"type": "string"}, "zh": {"type": "string"}, "freq": {"type": "string"}},
                        "required": ["en", "zh", "freq"],
                    },
                },
            },
            "required": ["jumps", "forward_movers"],
        },
    },
    "required": ["l4", "l5"],
}

SYSTEM = """You are a market historian writing a bilingual (Traditional Chinese + English) dashboard for a semiconductor-industry executive. The card maps TODAY's AI build-out onto the 1996–2002 telecom/fiber/dotcom cycle. The user has computed L3: seven analogue pairs each placed on their 1995–2002 curve (nearest neighbour on level + 1-yr slope), a weighted-median composite clock like 1999.0 ± 1.3, and the dispersion of readings.

Your job is L4 and L5.

L4:
- clock_take: one paragraph reading the composite clock AND its dispersion. High dispersion means the analogy is structurally strained — say so plainly. Cite the actual numbers given.
- like_1999: 3–5 concrete rhymes with 1998–2000 (capex pace, vendor/circular financing, concentration, loss-making demand engines), each citing today's numbers.
- structurally_unlike: 3–5 structural differences, and you MUST cover: (a) hyperscaler profitability & real operating cash flows vs 1990s levered telcos/CLECs; (b) physical scarcity of power/CoWoS-packaging/HBM vs the fiber glut (95% dark); (c) depreciation-life difference (4–6-yr GPUs vs 25-yr fiber) and what it does to overbuild self-correction.

L5:
- jumps: 4–6 falsification/monitor items of the form "if X happens, the clock jumps to Y" — X must be concrete and observable, Y a specific year like "2000.5", direction forward or backward. Build on the seed jumps provided; keep the plausible ones, sharpen triggers.
- forward_movers: 4–6 leading indicators that would move the clock forward, each with a check frequency (weekly/monthly/quarterly).

Rules: cite the given numbers; no hedging boilerplate; Traditional Chinese; analogy is a coordinate, not a forecast — never present the clock as a prediction. Keep clock_take ≤ ~130 words / 180 字. Output only the structured object."""


def _fmt_l3(kb, l3):
    comp = l3["composite"]
    lines = [
        f"Composite clock: {comp['clock']} ({comp['clock_label']}) ± {comp['plus_minus']} — "
        f"dispersion {comp['dispersion_key']}; cycle-position score {comp['score']}/100 ({comp['verdict_en']}); "
        f"{comp['n_beyond']}/{comp['n_pairs']} pairs read beyond the 1995–2002 range.",
        "Per-pair readings (today value → nearest historical quarter, confidence, beyond-range?):",
    ]
    for p in l3["pairs"]:
        r = p["reading"]
        if not r:
            continue
        t = p["today"]
        lines.append(
            f"  {p['name_en']} [{t['tier']}{' EST' if t['est'] else ''}]: {t['value']}{p['unit']} "
            f"(1y Δ {t['change_1y']}) → {r['quarter']} conf {r['confidence']}"
            f"{' BEYOND-RANGE' if r['beyond_range'] else ''} — breaks: {p['breaks_en'][:140]}"
        )
    seeds = "; ".join(f"if {j['condition_en']} → {j['jump_to']}" for j in kb.get("jumps_seed", []))
    lines.append(f"Seed jumps to build on: {seeds}")
    return "\n".join(lines)


def _claude(kb, l3):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    user = ("Here is today's L3 clock read. Produce L4 and L5 as the structured object.\n\n"
            + _fmt_l3(kb, l3))
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
# Rules fallback — deterministic L4/L5 from KB seeds + L3 numbers
# --------------------------------------------------------------------------- #
def _rules(kb, l3):
    comp = l3["composite"]
    seed = kb.get("l4_seed", {})

    clock, pm = comp.get("clock"), comp.get("plus_minus", 0.0)
    take_en = (
        f"The weighted-median clock reads {clock} ({comp.get('clock_label')}) ± {pm} — "
        f"{comp.get('verdict_en')}. Dispersion is {comp.get('dispersion_key')}: "
        f"{comp.get('n_beyond')}/{comp.get('n_pairs')} pairs sit beyond the whole 1995–2002 range, "
        "so part of this cycle simply has no 1990s coordinate. Read the clock as a coordinate on one "
        "historical sample, not a forecast — the structural-difference column is where the judgment lives."
    )
    take_zh = (
        f"加權中位數時鐘讀 {clock}（{comp.get('clock_label')}）± {pm}——{comp.get('verdict_zh')}。"
        f"分歧度為「{comp.get('dispersion_zh')}」：{comp.get('n_beyond')}/{comp.get('n_pairs')} 組配對"
        "超出 1995–2002 全區間，代表本輪有一段在 90 年代根本沒有座標。時鐘是單一歷史樣本上的座標，"
        "不是預測——判斷力在「哪裡結構性不像」那一欄。"
    )

    jumps = [dict(j) for j in kb.get("jumps_seed", [])]
    movers = [dict(m) for m in kb.get("forward_movers", [])]

    return {
        "engine": "rules",
        "l4": {
            "clock_take": {"summary_en": take_en, "summary_zh": take_zh},
            "like_1999": list(seed.get("like", [])),
            "structurally_unlike": list(seed.get("unlike", [])),
        },
        "l5": {"jumps": jumps, "forward_movers": movers},
    }


def analyze(kb, l3):
    """L4/L5 via Claude when ANTHROPIC_API_KEY is set; deterministic rules otherwise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, l3)
        except Exception:
            log.exception("cycle_clock: Claude L4/L5 failed — falling back to rules")
    return _rules(kb, l3)
