"""AI Credit & Financing Radar — L4 / L5 synthesis.

L4 (funding-structure read + TSMC counterparty view + directional thesis) and
L5 (contagion scenarios / falsification / early warning) are synthesized from
the L3 quant read. Two engines:

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

log = logging.getLogger("credit.analysis")

MODEL = "claude-opus-4-8"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "l4": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "structure_read": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"summary_en": {"type": "string"}, "summary_zh": {"type": "string"}},
                    "required": ["summary_en", "summary_zh"],
                },
                "tsmc_view": {
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
            "required": ["structure_read", "tsmc_view", "thesis"],
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

SYSTEM = """You are a credit strategist writing a bilingual (Traditional Chinese + English) dashboard for a semiconductor-industry executive. The card's question: whose money funds this AI capex cycle — internal cash flow, equity, or debt & off-balance-sheet structures? The more the funding stack leans on debt/soft money, the later the cycle and the sharper the break. The user has done L1–L3 (funding stack, indicator dictionary, live quant) and gives you the L3 read plus a composite credit-tightness score. Your job is L4 and L5.

L4:
- structure_read: where in the hard→soft funding stack the marginal capex dollar now sits. Cite the actual numbers (funding-gap share, gross debt issuance, soft-money share, spread levels, decompression flag). Say clearly which layer is softening fastest.
- tsmc_view: translate into TSMC counterparty risk — whose orders wobble first given how they are funded, what it means for prepayments / long-term capacity agreements. TSMC's exposure is mostly indirect (demand aggregates via NVIDIA/AMD).
- thesis: one directional call on funding-structure risk (hardening / levering / stressing), confidence tied to whether cash-flow gaps, issuance, and spreads agree, plus 3–5 LEADING signals that move before public spreads.

L5:
- scenarios: 3–4 contagion scenarios with integer probabilities that SUM TO 100, each with a concrete numeric trigger. Build on the seed scenarios provided.
- falsification: 2–3 conditions that would prove the thesis wrong.
- early_warning: 5–7 indicators ordered by which lights up earliest, each with a check frequency (daily/weekly/monthly).

Rules: cite actual numbers; estimates are estimates — do not launder curated T3 approximations into facts. No hedging boilerplate. Traditional Chinese. Each summary ≤ ~120 words / 160 字. Output only the structured object."""


def _fmt_l3(kb, l3, composite):
    agg = l3["aggregate"]
    sp = l3["spreads"]
    sm = l3["soft_money"]
    lines = [
        f"Composite credit-tightness {composite['score']}/100 → {composite['verdict']} "
        f"(0 = fully self-funded/easy, 100 = debt & off-BS extreme)",
        "Subscores: " + ", ".join(f"{s['id']} {s['score']} (w={s['weight']})" for s in composite["subscores"]),
        f"Aggregate: hyperscaler TTM capex ${agg['capex_total_usd_bn']}B, positive funding gaps "
        f"${agg['gap_total_usd_bn']}B ({agg['gap_share_pct']}% of capex); observed gross debt+SPV issuance "
        f"${agg['debt_issuance_usd_bn']}B ({agg['debt_issuance_share_pct']}% of capex).",
        "Per company (capex / OCF / gap $B, external share):",
    ]
    for r in l3["hyperscalers"]:
        lines.append(f"  {r['name']}: {r['capex_ttm_usd_bn']} / {r['ocf_ttm_usd_bn']} / {r['gap_usd_bn']} "
                     f"→ {r['external_share_pct']}% external (capex/OCF {r['capex_ocf_pct']}%)"
                     + (" [live]" if r["live"] else " [seed]"))
    ig, hy, ccc = sp.get("ig_oas", {}), sp.get("hy_oas", {}), sp.get("ccc_oas", {})
    lines.append(f"Spreads: IG {ig.get('value')}% (6m {ig.get('chg_6m')}), HY {hy.get('value')}% (6m {hy.get('chg_6m')}), "
                 f"CCC {ccc.get('value')}% (6m {ccc.get('chg_6m')}); CCC−IG {sp.get('ccc_minus_ig')}pp; "
                 f"decompression={sp.get('decompression')}")
    lines.append(f"Soft-money share of curated financing ledger: {sm['score']}/100 over ${sm['total_drawn_usd_bn']}B drawn "
                 "(ALL ledger rows are curated estimates, T2/T3).")
    lines.append("Ledger (drawn $B, softness 1 hard…5 softest, tier):")
    for e in l3["ledger"]:
        lines.append(f"  {e['name_en']}: {e['size_drawn_usd_bn']} drawn / {e['size_committed_usd_bn']} committed, "
                     f"softness {e['softness']}, {e['tier']}, cost {e['cost_hint_en']}")
    lines.append("Labs (burn ÷ revenue run-rate):")
    for l in l3["labs"]:
        lines.append(f"  {l['name']}: revenue ${l['revenue_runrate_usd_bn']}B rr, burn ${l['annual_burn_usd_bn']}B "
                     f"→ {l['burn_multiple']}×; funding raised ${l['funding_raised_usd_bn']}B, "
                     f"valuation ${l['valuation_usd_bn']}B (all EST)")
    seeds = "; ".join(f"{s['name_en']} — {s['trigger_en']}" for s in kb.get("scenarios_seed", []))
    lines.append(f"Seed scenarios to build on: {seeds}")
    return "\n".join(lines)


def _claude(kb, l3, composite):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    user = ("Here is today's L3 credit & financing read. Produce L4 and L5 as the structured object.\n\n"
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
    agg = l3["aggregate"]
    sp = l3["spreads"]
    sm = l3["soft_money"]
    score, verdict = composite["score"], composite["verdict"]
    hy = sp.get("hy_oas", {})
    decomp = sp.get("decompression")
    softest = max(l3["hyperscalers"], key=lambda r: (r["external_share_pct"] or 0))

    # ── L4 structure read ──
    sr_en = (f"Composite credit-tightness {score}/100 → {verdict}. Cash flow still carries most of the cycle "
             f"(positive funding gaps only {agg['gap_share_pct']}% of ${agg['capex_total_usd_bn']}B TTM capex, "
             f"led by {softest['name']} at {softest['external_share_pct']}% external), but the structure is softening "
             f"anyway: observed gross bond+SPV issuance is ${agg['debt_issuance_usd_bn']}B "
             f"({agg['debt_issuance_share_pct']}% of capex) and the curated ledger's soft-money share reads "
             f"{sm['score']}/100. Spreads do not price this yet (HY {hy.get('value')}%), " +
             ("and CCC is quietly decompressing from IG — the bottom of the stack cracks first."
              if decomp else "with no decompression at the CCC end yet."))
    sr_zh = (f"綜合信用鬆緊 {score}/100 → {verdict}。現金流仍扛著這輪週期(正資金缺口僅占 "
             f"${agg['capex_total_usd_bn']}B TTM capex 的 {agg['gap_share_pct']}%,最缺的是 {softest['name']} "
             f"外部融資占 {softest['external_share_pct']}%),但結構已在變軟:可觀察的發債+SPV 毛額達 "
             f"${agg['debt_issuance_usd_bn']}B(占 capex {agg['debt_issuance_share_pct']}%),策展台帳軟錢分數 "
             f"{sm['score']}/100。利差還沒為此定價(HY {hy.get('value')}%)," +
             ("而 CCC 已悄悄與 IG 解壓縮——結構最底層先裂。" if decomp else "CCC 端也尚未出現解壓縮。"))

    # ── L4 TSMC view ──
    tv_en = (f"For TSMC the exposure is indirect — demand aggregates through NVIDIA/AMD — so funding softness shows up "
             f"as order-mix wobble, not defaults. Softest-funded demand first: neoclouds on GPU-collateral debt "
             f"(~9–11% cost, EST) and labs burning >1.4× revenue; then {softest['name']} whose capex runs "
             f"{softest['capex_ocf_pct']}% of OCF on bond-market oxygen. MSFT/GOOGL/AMZN OCF-funded orders remain the "
             f"durable book. Prepayments and long-term capacity agreements are TSMC's own counterparty ledger: price them "
             f"against the payer's funding layer, not the headline backlog.")
    tv_zh = (f"對台積電,曝險是間接的——需求經 NVIDIA/AMD 匯集——融資變軟會以「訂單組合晃動」而非違約呈現。"
             f"最軟錢的需求最先晃:靠 GPU 抵押債(成本約 9–11%,估計值)的新雲、以及燒錢逾營收 1.4 倍的實驗室;"
             f"其次是 {softest['name']},其 capex 達 OCF 的 {softest['capex_ocf_pct']}%,靠債券市場供氧。"
             f"MSFT/GOOGL/AMZN 用 OCF 下的單仍是最耐震的訂單簿。預付款與產能長約就是台積電自己的交易對手台帳:"
             f"該按付款方的融資層定價,而非看表面在手訂單。")

    # ── L4 thesis ──
    subs = {s["id"]: s["score"] for s in composite["subscores"]}
    agree = (subs["funding_gap"] > 30) == (subs["soft_money"] > 50)
    if verdict == "STRESSED":
        dir_en, dir_zh = "Stressing — funding is now the story; expect order air-pockets", "壓力中 — 融資本身就是行情;預期訂單真空"
        conf = "high" if subs["spreads"] > 40 else "medium"
    elif verdict == "LEVERING":
        dir_en, dir_zh = "Levering — structure softening while spreads stay calm; later-cycle than it looks", "加槓桿中 — 結構變軟而利差平靜;週期比表面更晚"
        conf = "medium" if agree else "low"
    else:
        dir_en, dir_zh = "Hardening / self-funded — cash flow still pays for the cycle", "偏硬/自籌 — 現金流仍付得起這輪週期"
        conf = "medium"

    leads = [
        {"en": f"CCC−IG differential ({sp.get('ccc_minus_ig')}pp) — soft-money pricing before HY moves",
         "zh": f"CCC−IG 利差差({sp.get('ccc_minus_ig')}pp)——軟錢定價比 HY 先動"},
        {"en": "Neocloud GPU-loan terms & LTV resets (news-level; not in public spreads)",
         "zh": "新雲 GPU 貸款條款與 LTV 重設(新聞層級;不在公開利差內)"},
        {"en": f"Hyperscaler gross issuance vs capex ({agg['debt_issuance_share_pct']}%) — softening without a price signal",
         "zh": f"巨頭發債毛額對 capex 比({agg['debt_issuance_share_pct']}%)——沒有價格訊號的變軟"},
        {"en": "Lab mega-round cadence & prepayment renewals — the equity window on/off switch",
         "zh": "實驗室巨輪節奏與預付款續約——股權窗口的開關"},
        {"en": f"Oracle-style capex/OCF outliers ({softest['name']} {softest['capex_ocf_pct']}%) guiding capex on earnings calls",
         "zh": f"Oracle 型 capex/OCF 異數({softest['name']} {softest['capex_ocf_pct']}%)法說上的 capex 指引"},
    ]

    # ── L5 scenario probabilities from the current read ──
    if verdict == "STRESSED":
        base = {"self_funded_regime": 5, "levering_grind": 25, "private_credit_crack": 40, "credit_event": 30}
    elif verdict == "LEVERING" and decomp:
        base = {"self_funded_regime": 15, "levering_grind": 45, "private_credit_crack": 25, "credit_event": 15}
    elif verdict == "LEVERING":
        base = {"self_funded_regime": 20, "levering_grind": 45, "private_credit_crack": 22, "credit_event": 13}
    else:
        base = {"self_funded_regime": 40, "levering_grind": 35, "private_credit_crack": 15, "credit_event": 10}
    seed_by_id = {s["id"]: s for s in kb.get("scenarios_seed", [])}
    scenarios = []
    for sid, prob in base.items():
        s = seed_by_id.get(sid, {})
        scenarios.append({
            "name_en": s.get("name_en", sid), "name_zh": s.get("name_zh", sid), "prob": prob,
            "trigger_en": s.get("trigger_en", ""), "trigger_zh": s.get("trigger_zh", ""),
        })

    falsification = [
        {"en": "Hyperscaler OCF outgrows capex for 2+ quarters while bond programs shrink — the stack re-hardens; flip toward SELF-FUNDED.",
         "zh": "巨頭 OCF 連兩季以上跑贏 capex 且發債計畫縮小——結構重新變硬;轉向 SELF-FUNDED。"},
        {"en": "CCC−IG compresses below 5pp AND neocloud debt refinances at tighter cost — the soft layer is priced healthier than our curated assumptions.",
         "zh": "CCC−IG 壓縮到 5 個百分點以下、且新雲債務以更低成本再融資——軟錢層的實際定價比我們的策展假設健康。"},
        {"en": "Lab burn multiples fall below 0.8× as revenue outgrows burn — the equity layer self-hardens and prepayments stop being sentiment-gated.",
         "zh": "實驗室燒錢倍數降到 0.8 倍以下、營收跑贏燒錢——股權層自我變硬,預付款不再看募資窗口臉色。"},
    ]
    early_warning = [
        {"en": "Neocloud / GPU-loan restructuring headlines (news radar)", "zh": "新雲 / GPU 貸款重整頭條(新聞雷達)", "freq": "daily"},
        {"en": "CCC OAS level & CCC−IG differential (BAMLH0A3HYC)", "zh": "CCC 利差水準與 CCC−IG 差(BAMLH0A3HYC)", "freq": "weekly"},
        {"en": "HY OAS trend (BAMLH0A0HYM2)", "zh": "HY 利差趨勢(BAMLH0A0HYM2)", "freq": "weekly"},
        {"en": "Hyperscaler bond / SPV issuance announcements", "zh": "巨頭發債 / SPV 融資公告", "freq": "monthly"},
        {"en": "Lab funding rounds & compute-prepayment news", "zh": "實驗室募資輪與算力預付款新聞", "freq": "monthly"},
        {"en": "Oracle-style capex/OCF ratios & capex guidance", "zh": "Oracle 型 capex/OCF 比與 capex 指引", "freq": "quarterly"},
    ]

    return {
        "engine": "rules",
        "l4": {
            "structure_read": {"summary_en": sr_en, "summary_zh": sr_zh},
            "tsmc_view": {"summary_en": tv_en, "summary_zh": tv_zh},
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
            log.exception("credit: Claude L4/L5 failed — falling back to rules")
    return _rules(kb, l3, composite)
