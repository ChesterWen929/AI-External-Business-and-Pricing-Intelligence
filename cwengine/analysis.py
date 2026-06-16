"""Evidence -> assumption suggestion (LLM-assisted, human-in-the-loop).

Paste a transcript snippet / analyst note / supply-chain datapoint; the engine
proposes WHICH assumption node it should update, in WHICH direction, with a
proposed new value and a bilingual rationale. It NEVER auto-overwrites the graph
-- the proposal is returned for a human to approve/edit. Two engines:

  • Claude  — Opus 4.8, structured output (json_schema) constrained to the real
              node ids + a prompt-cached description of the assumption graph.
  • rules   — deterministic keyword matcher so the panel works offline / no key.

Note on the deploy target: Render's free disk is ephemeral, so an approved change
is shown and explained but not durably written back to knowledge_base.json from
the web tier; the seed graph remains the versioned source of truth. The value of
the panel is the SUGGESTION + traceability, not silent mutation.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("cwengine.analysis")

MODEL = "claude-opus-4-8"


def _node_ids(kb):
    return [n["id"] for n in kb["nodes"]]


def _schema(kb):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "affects_node": {"type": "string", "enum": _node_ids(kb)},
            "segment": {"type": "string",
                        "enum": [s["id"] for s in kb["segments"]] + ["_all", "_none"]},
            "direction": {"type": "string", "enum": ["up", "down", "mix_shift"]},
            "magnitude_pct": {"type": "number"},
            "proposed_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "regime_implication": {"type": "string",
                                   "enum": ["training_led", "inference_rotating", "inference_led", "none"]},
            "rationale_en": {"type": "string"},
            "rationale_zh": {"type": "string"},
            "tier": {"type": "string", "enum": ["T1", "T2", "T3"]},
        },
        "required": ["affects_node", "segment", "direction", "magnitude_pct",
                     "proposed_confidence", "regime_implication", "rationale_en",
                     "rationale_zh", "tier"],
    }


def _system(kb):
    nodes = "\n".join(
        f"  - {n['id']} (stage {n['stage']}, {n['kind']}): {n['name_en']}" for n in kb["nodes"])
    segs = ", ".join(f"{s['id']}={s['name_en']}" for s in kb["segments"])
    return f"""You are a semiconductor-foundry demand modeler maintaining a CapEx-to-wafer assumption graph. The chain is: CapEx $ -> silicon-addressable $ -> accelerator $ by segment -> units (/ASP) -> die area -> wafers, modulated by the training/inference regime.

Assumption nodes you may target:
{nodes}

Segments: {segs}

Given a piece of market evidence (earnings snippet, analyst note, supply-chain datapoint), decide:
- affects_node: the single node it most directly updates.
- segment: which segment (or _all for a whole-chain scalar, _none if not segment-specific).
- direction: up / down for a scalar; mix_shift if it re-weights the segment_mix.
- magnitude_pct: your best estimate of the % change to that node's value (signed; e.g. +8 means raise ~8%).
- regime_implication: if this is a tell that the regime itself is shifting, name the regime it points to, else "none".
- tier: T1 = primary (filings/calls), T2 = operator/channel, T3 = sell-side.
- rationale_en / rationale_zh: ONE concrete sentence each (Traditional Chinese), citing the mechanism (how it flows to wafer demand). No hedging boilerplate.

Be decisive. Output only the structured object."""


def _claude(kb, text):
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": _system(kb), "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": _schema(kb)}, "effort": "low"},
        messages=[{"role": "user", "content": f"Evidence:\n\"\"\"\n{text}\n\"\"\""}],
    )
    out = next((b.text for b in msg.content if b.type == "text"), "")
    data = json.loads(out)
    data["engine"] = "claude"
    return data


# --------------------------------------------------------------------------- #
# Rules fallback — keyword matcher
# --------------------------------------------------------------------------- #
_KEYWORDS = [
    # (node, segment, direction, [keywords])
    ("segment_mix", "_all", "mix_shift", ["asic", "custom silicon", "tpu", "trainium", "mtia",
                                          "maia", "inference", "推論", "自研", "rotation", "輪動"]),
    ("segment_asp", "gpu_flagship", "up", ["asp up", "price increase", "flagship", "blackwell",
                                           "gb300", "rubin", "asp 上", "漲價"]),
    ("segment_asp", "inference_chip", "down", ["asp compression", "price competition", "price war",
                                               "降價", "壓縮", "competition"]),
    ("segment_yield", "_all", "up", ["yield", "defect density", "良率", "缺陷"]),
    ("segment_die_mm2", "_all", "up", ["die size", "reticle", "multi-die", "裸晶", "光罩"]),
    ("silicon_fraction", "_all", "up", ["rack-scale", "compute tray", "silicon share", "可矽化",
                                        "機櫃", "rack scale"]),
    ("segment_hbm_gb", "_all", "up", ["hbm", "memory", "記憶體", "高頻寬"]),
]


def _rules(kb, text):
    low = (text or "").lower()
    best = None
    for node, seg, direction, kws in _KEYWORDS:
        hits = sum(1 for k in kws if k in low)
        if hits and (best is None or hits > best[0]):
            best = (hits, node, seg, direction)
    if not best:
        node, seg, direction = "segment_mix", "_all", "mix_shift"
    else:
        _, node, seg, direction = best
    node_obj = next(n for n in kb["nodes"] if n["id"] == node)
    regime = "inference_rotating" if node == "segment_mix" else "none"
    return {
        "engine": "rules",
        "affects_node": node,
        "segment": seg,
        "direction": direction,
        "magnitude_pct": 5.0 if direction != "mix_shift" else 0.0,
        "proposed_confidence": "low",
        "regime_implication": regime,
        "tier": "T3",
        "rationale_en": (f"Keyword match -> likely updates '{node_obj['name_en']}' "
                         f"({direction}). Review and set the magnitude before applying."),
        "rationale_zh": (f"關鍵字比對 → 可能更新「{node_obj['name_zh']}」（{direction}）。"
                         f"套用前請人工檢視並設定幅度。"),
    }


def propose(kb, text, force_rules=False):
    """Return a single assumption-change proposal for a piece of evidence.
    Human-in-the-loop: this is a SUGGESTION, never an applied change."""
    if not force_rules and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, text)
        except Exception:
            log.exception("cwengine: Claude proposal failed -> rules fallback")
    return _rules(kb, text)
