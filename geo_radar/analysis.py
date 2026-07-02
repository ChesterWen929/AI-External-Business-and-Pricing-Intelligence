"""Geopolitics & Second-Chain Radar — L4/L5 synthesis.

L4 (strategic read for a foundry + moat erosion map + control-red-line read)
and L5 (scenarios / falsification / watchlist) synthesized from the L1–L3
quant core. Two engines:

  • Claude — Opus 4.8, structured output (json_schema), prompt-cached system
             block. Used when ANTHROPIC_API_KEY is set (refresh path only).
  • rules  — deterministic fallback derived from the same numbers, so the
             dashboard is fully functional offline / without a key.

analyze(kb, core) → {"engine": "claude"|"rules", "l4": {...}, "l5": {...}}
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("geo.analysis")

MODEL = "claude-opus-4-8"

_SUMMARY = {
    "type": "object", "additionalProperties": False,
    "properties": {"summary_en": {"type": "string"}, "summary_zh": {"type": "string"}},
    "required": ["summary_en", "summary_zh"],
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "l4": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "strategic_read": _SUMMARY,
                "moat_read": _SUMMARY,
                "control_read": _SUMMARY,
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["strategic_read", "moat_read", "control_read", "confidence"],
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
                "falsification": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"en": {"type": "string"}, "zh": {"type": "string"}},
                        "required": ["en", "zh"],
                    },
                },
                "watchlist": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"en": {"type": "string"}, "zh": {"type": "string"},
                                       "freq": {"type": "string"}},
                        "required": ["en", "zh", "freq"],
                    },
                },
            },
            "required": ["scenarios", "falsification", "watchlist"],
        },
    },
    "required": ["l4", "l5"],
}

SYSTEM = """You are a geopolitics + semiconductor supply-chain strategist writing a bilingual (Traditional Chinese + English) dashboard for a foundry executive. The one question behind everything: China is building a second AI supply chain — link by link, how far along is it, where is the control red line moving, and could policy redraw the demand/customer map overnight? Every closed link removes a section of the foundry moat.

You are given the quant core: (a) a Liebig minimum-law completeness score across 7 links (EDA / equipment / advanced logic / HBM / advanced packaging / interconnect / software) where the WEAKEST link governs, (b) a recency-weighted control-direction drift score with the regime level noted separately, (c) a China-vs-western-tools stock basket (sentiment PROXY only, weight 0), (d) Taiwan advanced-logic concentration numbers.

L4 — three reads, each ≤ ~110 words / 150 字:
- strategic_read: what this means for a foundry THIS quarter — moat status, demand-side policy risk, what would actually change decisions.
- moat_read: which links erode moat fastest and why; distinguish links that need EUV-class physics (slow) from workarounds (packaging, interconnect, software — fast). Cite the given percentages.
- control_read: where the red line sits now (which chips licensable, which denied), which way it drifts, and what the level-vs-direction distinction implies.

L5:
- scenarios: exactly 4, integer probabilities SUMMING TO 100, built on the seed scenarios given, probabilities set from the current read. Concrete triggers.
- falsification: 2–3 conditions that would prove the current read wrong.
- watchlist: 5–7 items ordered by earliest-warning value, each with a check frequency.

Rules: cite the actual numbers given; estimates are estimates — never assert opaque Chinese yields as fact; Traditional Chinese; no hedging boilerplate. Output only the structured object."""


def _fmt_core(kb, core):
    comp = core["composite"]
    d = core["direction"]
    mkt = core["market"]
    tw = core.get("taiwan", {}).get("concentration", {})
    lines = [
        f"Second-chain completeness: {comp['score']}/100 ({comp['verdict']}) — "
        f"binding link {comp['binding_name_en']} at {comp['min_pct']}%, mean {comp['mean_pct']}%, "
        f"formula 0.7*MIN+0.3*MEAN (Liebig).",
        f"Trajectory: {core['trajectory']['label_en']}.",
        "Links (completeness %, years behind, slope — ALL estimates):",
    ]
    for r in core["links"]:
        lines.append(f"  {r['name_en']}: {r['pct']}% [{r['tier']}], ~{r['years_behind']}y behind, {r['slope']}")
    lines += [
        f"Control direction (12m drift): {d['score']} → {d['verdict']}; regime level: "
        f"{core['level'].get('label_en', 'TIGHT')} (level ≠ direction).",
        f"Market proxy (weight 0): China basket 1m {mkt['china_avg_chg_1m']}% vs western tools "
        f"{mkt['west_avg_chg_1m']}% → spread {mkt['spread']}pp. {mkt['read_en']}.",
        f"News counts this refresh: escalation {core['news_counts']['escalation']}, "
        f"de-escalation {core['news_counts']['de_escalation']}, neutral {core['news_counts']['neutral']}.",
        f"Taiwan concentration: ~{tw.get('pct')}% of global ≤N5 logic capacity in Taiwan "
        f"[{tw.get('tier')}, {tw.get('as_of')}].",
    ]
    seeds = "; ".join(f"{s['name_en']} — {s['trigger_en']}" for s in kb.get("scenarios_seed", []))
    lines.append(f"Seed scenarios to build on: {seeds}")
    return "\n".join(lines)


def _claude(kb, core):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    user = ("Here is the current quant core. Produce L4 and L5 as the structured object.\n\n"
            + _fmt_core(kb, core))
    msg = client.messages.create(
        model=MODEL,
        max_tokens=6000,  # zh-heavy bilingual JSON truncates below this
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "medium"},
        messages=[{"role": "user", "content": user}],
    )
    text = next((b.text for b in msg.content if b.type == "text"), "")
    data = json.loads(text)
    return {"engine": "claude", "l4": data["l4"], "l5": data["l5"]}


# --------------------------------------------------------------------------- #
# Rules fallback — deterministic L4/L5 from the quant core
# --------------------------------------------------------------------------- #
def _scenario_probs(verdict, composite_score):
    if verdict == "TIGHTENING":
        base = {"status_quo": 30, "re_escalation": 45, "breakthrough": 10, "grand_bargain": 15}
    elif verdict == "EASING":
        base = {"status_quo": 45, "re_escalation": 18, "breakthrough": 12, "grand_bargain": 25}
    else:
        base = {"status_quo": 45, "re_escalation": 25, "breakthrough": 12, "grand_bargain": 18}
    if composite_score >= 40:  # a more complete chain makes breakthrough likelier
        base["breakthrough"] += 5
        base["status_quo"] -= 5
    return base


def _rules(kb, core):
    comp = core["composite"]
    d = core["direction"]
    mkt = core["market"]
    tw = core.get("taiwan", {}).get("concentration", {})
    links = {r["id"]: r for r in core["links"]}
    fast = sorted((r for r in core["links"] if not r["is_binding"]),
                  key=lambda r: r["pct"], reverse=True)[:3]
    fast_en = ", ".join(f"{r['name_en']} {r['pct']}%" for r in fast)
    fast_zh = "、".join(f"{r['name_zh']} {r['pct']}%" for r in fast)

    # L4 — strategic read
    sr_en = (f"Second-chain completeness reads {comp['score']}/100 ({comp['verdict_en']}), bound by "
             f"{comp['binding_name_en']} at {comp['min_pct']}% — at the leading edge the western chain is not "
             f"substitutable yet, so the foundry moat holds where EUV-class physics is required. The sharper "
             f"near-term risk is demand-side policy: the 2025 H20 whiplash (license required in April, resumed in "
             f"July) shows a customer list can be redrawn in one rule. With the 12-month control drift at "
             f"{d['score']} ({d['label_en']}) inside a structurally tight regime, plan for discontinuity, not trend.")
    sr_zh = (f"第二鏈完整度 {comp['score']}/100({comp['verdict_zh']}),被{comp['binding_name_zh']}綁定在 "
             f"{comp['min_pct']}%——凡需要 EUV 級物理的地方,西方鏈短期不可替代,代工護城河仍在。更尖銳的近期風險"
             f"在需求端政策:2025 年 H20 急轉(4 月需許可、7 月恢復)證明一紙規則就能重畫客戶名單。近 12 個月管制"
             f"漂移 {d['score']}({d['label_zh']}),但結構仍緊——要為不連續做計畫,而不是為趨勢做計畫。")

    # L4 — moat read
    mr_en = (f"Moat erosion is fastest where EUV is NOT required: {fast_en} are the closing links — packaging "
             f"multiplies effective compute per wafer, interconnect lashes weaker chips into competitive systems, "
             f"and model quality is nearly closed. The slow links are litho ({links['equipment']['pct']}%) and HBM "
             f"({links['hbm']['pct']}%). Every link that closes converts one section of moat into commodity; the "
             f"composite rising through ~45 (CLOSING) would be the strategic alarm level. All percentages are "
             f"estimates — yields inside China are opaque.")
    mr_zh = (f"護城河侵蝕最快的是「不需要 EUV」的環節:{fast_zh} 正在收斂——封裝放大每片晶圓的有效算力、互連把較弱"
             f"晶片綁成可用系統、模型品質差距接近抹平。慢的環節是微影({links['equipment']['pct']}%)與 HBM"
             f"({links['hbm']['pct']}%)。每補完一環,護城河就少一段;綜合分數升破約 45(快速逼近)是戰略警戒線。"
             f"所有 % 皆為推估——中國良率不透明。")

    # L4 — control read
    cr_en = (f"The red line today: H20-class licensable (with strings), Blackwell-class denied, 'B30A' debated but "
             f"unapproved (T3). Twelve-month drift {d['score']} → {d['verdict']} — but the LEVEL stays "
             f"{core['level'].get('label_en', 'TIGHT')}: EUV/advanced-DUV, HBM and entity-list walls are untouched "
             f"by the truce. Easing at the margin re-opens China demand for compliant SKUs; it does not re-open the "
             f"tool chain. Watch the 2026-11 truce review as the scheduled discontinuity.")
    cr_zh = (f"今天的紅線:H20 級可發證(附條件)、Blackwell 級拒發、「B30A」討論中未批准(T3)。12 個月漂移 "
             f"{d['score']} → {d['label_zh']}——但水位仍是{core['level'].get('label_zh', '結構性緊')}:EUV/先進 "
             f"DUV、HBM、實體清單這幾道牆未被休戰觸及。邊際緩和重開的是合規晶片的中國需求,不是設備鏈。把 2026-11 "
             f"休戰檢視當作排定好的不連續點來盯。")

    # L5 — scenarios
    probs = _scenario_probs(d["verdict"], comp["score"])
    seed_by_id = {s["id"]: s for s in kb.get("scenarios_seed", [])}
    scenarios = []
    for sid, prob in probs.items():
        s = seed_by_id.get(sid, {})
        scenarios.append({
            "name_en": s.get("name_en", sid), "name_zh": s.get("name_zh", sid),
            "prob": prob,
            "trigger_en": s.get("trigger_en", ""), "trigger_zh": s.get("trigger_zh", ""),
        })

    falsification = [
        {"en": "CXMT ships HBM3 in commercial volume within 12 months → the HBM 30% estimate is too low; "
               "re-rate the composite and shorten every moat timeline.",
         "zh": "若長鑫 12 個月內商業量產 HBM3 → HBM 30% 推估過低;重估綜合分數並縮短所有護城河時間表。"},
        {"en": "SMEE immersion DUV verified in a volume production line → the litho-bound (18%) assumption breaks; "
               "the binding link moves and the composite jumps.",
         "zh": "若上海微電子浸潤式 DUV 經證實進入量產線 → 微影綁定(18%)假設失效;綁定環節移動、綜合分數跳升。"},
        {"en": "Truce collapses into new export-control rounds → the EASING drift read is falsified overnight; "
               "switch to the re-escalation playbook.",
         "zh": "若休戰破裂、進入新一輪管制 → 「邊際緩和」判讀一夕證偽;切換到重新升級劇本。"},
    ]

    watchlist = list(kb.get("watchlist_seed", []))

    return {
        "engine": "rules",
        "l4": {
            "strategic_read": {"summary_en": sr_en, "summary_zh": sr_zh},
            "moat_read": {"summary_en": mr_en, "summary_zh": mr_zh},
            "control_read": {"summary_en": cr_en, "summary_zh": cr_zh},
            "confidence": "medium",  # capability %s are estimates; policy is discontinuous
        },
        "l5": {"scenarios": scenarios, "falsification": falsification, "watchlist": watchlist},
    }


def analyze(kb, core):
    """L4/L5 via Claude when ANTHROPIC_API_KEY is set; deterministic rules otherwise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, core)
        except Exception:
            log.exception("geo: Claude L4/L5 failed — falling back to rules")
    return _rules(kb, core)
