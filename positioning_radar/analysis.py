"""Positioning & Sentiment Radar — L4 / L5 synthesis.

L4 (positioning read + who-unwinds-first queue + TSMC view + directional thesis)
and L5 (scenarios / falsification / early warning) are synthesized from the L3
quant read. Two engines:

  • Claude  — Opus 4.8, structured outputs (json_schema) + a prompt-cached
              system block. Used when ANTHROPIC_API_KEY is set. max_tokens 6000
              (zh-heavy bilingual JSON truncates below that).
  • rules   — deterministic fallback derived from the L3 numbers, so the
              dashboard is fully functional offline / without a key.

analyze(kb, l3, composite, nuance)
    → {"engine": "claude"|"rules", "l4": {...}, "l5": {...}}
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("positioning.analysis")

MODEL = "claude-opus-4-8"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "l4": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "positioning_read": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"summary_en": {"type": "string"}, "summary_zh": {"type": "string"}},
                    "required": ["summary_en", "summary_zh"],
                },
                "unwind_queue": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "rank": {"type": "integer"},
                            "player_en": {"type": "string"}, "player_zh": {"type": "string"},
                            "trigger_en": {"type": "string"}, "trigger_zh": {"type": "string"},
                        },
                        "required": ["rank", "player_en", "player_zh", "trigger_en", "trigger_zh"],
                    },
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
            "required": ["positioning_read", "unwind_queue", "tsmc_view", "thesis"],
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

SYSTEM = """You are a positioning strategist writing a bilingual (Traditional Chinese + English) dashboard for a semiconductor-industry executive. The card's question: how crowded is the AI trade, and who is already fully invested — institutions, retail, leveraged money? Crowdedness sets the violence of the drawdown and the timing risk of the top. The user has done L1–L3 (player map, indicator dictionary, live positioning quant with 3-year percentiles) and gives you the L3 read, a composite crowdedness score, and a nuance state (crowded-and-rising vs crowded-and-cracking, paired with the /credit card's decompression flag). Your job is L4 and L5.

L4:
- positioning_read: who is full and who is not. Cite the actual percentiles (COT, NAAIM, margin debt, AAII, put/call, VIX term). Call out divergences explicitly — e.g. institutions full while the retail survey is lukewarm is 'professional crowding'.
- unwind_queue: order the five player-map layers by who sells first in a drawdown, each with the concrete mechanical or behavioral trigger. Keep the ordering consistent with the player map given (leveraged → active institutions → retail → insiders/buybacks → passive) unless the data argues otherwise.
- tsmc_view: positioning is NOT a demand signal — translate honestly. Crowdedness sets how violently the market reprices the AI narrative; a forced-seller cascade shuts financing windows (pair with /credit) and freezes customer expansion decisions before any wafer order changes.
- thesis: one directional call on positioning risk, confidence tied to whether positioning, trend, and crack tells agree, plus 3–5 LEADING signals ordered by speed.

L5:
- scenarios: 3–4 scenarios with integer probabilities that SUM TO 100, each with a concrete numeric trigger. Build on the seed scenarios provided.
- falsification: 2–3 conditions that would prove the crowdedness thesis wrong.
- early_warning: 5–7 indicators ordered by which lights up earliest, each with a check frequency (daily/weekly/monthly).

Rules: cite actual numbers; percentiles on curated bands are estimates — say so; margin debt is ~2 months stale — say so. No hedging boilerplate. Traditional Chinese. Each summary ≤ ~120 words / 160 字. Output only the structured object."""


def _fmt_l3(kb, l3, composite, nuance):
    lines = [
        f"Composite crowdedness {composite['score']}/100 → {composite['verdict']} "
        f"(0 = uncrowded, 100 = one-sided)",
        f"Nuance state: {nuance['state']} — crack tells {nuance['cracks_on']}/{len(nuance['crack_tells'])} on "
        f"(threshold {nuance['cracks_min']}); positioning trend rising in "
        f"{nuance['rising_count']}/{nuance['rising_total']} build indicators.",
        "Crack tells: " + "; ".join(f"{t['id']}={'ON' if t['on'] else 'off'}" for t in nuance["crack_tells"]),
        "Indicators (value, chg/window, 3y percentile [source], crowd contribution, weight):",
    ]
    for r in l3["indicators"]:
        lines.append(
            f"  {r['name_en']}: {r['value']} {r['unit_en']} (chg {r['chg']}/{r['chg_window']}), "
            f"pct {r['pct3y']} [{r['pct_source']}], crowd {r['crowd']}, w={r['weight']}"
            + (" [live]" if r["live"] else " [seed]")
            + (" [PROXY]" if r["proxy"] else "")
            + (f" as_of {r['as_of']}")
        )
    cd = kb.get("cross_card", {}).get("credit_decompression", {})
    lines.append(f"Cross-card: /credit decompression flag = {cd.get('value')} ({cd.get('align_note', '')})")
    lines.append("Player map (who sells first — unwind_rank 1 sells first):")
    for p in sorted(kb.get("player_map", []), key=lambda x: x["unwind_rank"]):
        lines.append(f"  #{p['unwind_rank']} {p['name_en']}: {p['unwind_en']}")
    seeds = "; ".join(f"{s['name_en']} — {s['trigger_en']}" for s in kb.get("scenarios_seed", []))
    lines.append(f"Seed scenarios to build on: {seeds}")
    return "\n".join(lines)


def _claude(kb, l3, composite, nuance):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    user = ("Here is today's L3 positioning & sentiment read. Produce L4 and L5 as the structured object.\n\n"
            + _fmt_l3(kb, l3, composite, nuance))
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
def _rules(kb, l3, composite, nuance):
    by = {r["id"]: r for r in l3["indicators"]}
    score, verdict = composite["score"], composite["verdict"]
    state = nuance["state"]
    naaim, cot, mdebt = by.get("naaim", {}), by.get("cot_nq", {}), by.get("margin_debt", {})
    aaii, pc, vix = by.get("aaii", {}), by.get("put_call", {}), by.get("vix_term", {})
    cd = kb.get("cross_card", {}).get("credit_decompression", {})

    pro_full = (naaim.get("pct3y") or 0) >= 80 and (mdebt.get("pct3y") or 0) >= 70
    retail_lukewarm = (aaii.get("pct3y") or 0) < 65

    # ── L4 positioning read ──
    div_en = (" The divergence is the story: institutions and leverage are full while the retail survey "
              f"sits at only the {aaii.get('pct3y')}th percentile — professional crowding, not retail euphoria."
              if pro_full and retail_lukewarm else
              " Institutions, leverage and retail read from the same side — crowding is broad.")
    div_zh = (f" 背離就是重點:機構與槓桿已滿,散戶問卷卻只有第 {aaii.get('pct3y')} 百分位——是專業擁擠,不是散戶亢奮。"
              if pro_full and retail_lukewarm else
              " 機構、槓桿與散戶站在同一邊——擁擠是全面性的。")
    pr_en = (f"Composite crowdedness {score}/100 → {verdict}. NAAIM exposure {naaim.get('value')} "
             f"({naaim.get('pct3y')}th pct est.), COT NQ net long {cot.get('value')}k contracts "
             f"({cot.get('pct3y')}th pct), margin debt {'+' if (mdebt.get('value') or 0) > 0 else ''}"
             f"{mdebt.get('value')}% YoY ({mdebt.get('pct3y')}th pct — a ~2-month-stale official print), "
             f"put/call {pc.get('value')} (calls crowded), VIX term {vix.get('value')} (contango carry)."
             + div_en +
             f" Percentiles on curated bands are estimates; only COT recomputes on live refresh.")
    pr_zh = (f"綜合擁擠度 {score}/100 → {verdict}。NAAIM 曝險 {naaim.get('value')}(約第 {naaim.get('pct3y')} 百分位)、"
             f"COT 那斯達克淨多 {cot.get('value')} 千口(第 {cot.get('pct3y')} 百分位)、融資餘額年增 "
             f"{'+' if (mdebt.get('value') or 0) > 0 else ''}{mdebt.get('value')}%(第 {mdebt.get('pct3y')} 百分位——"
             f"官方數字但落後約兩個月)、put/call {pc.get('value')}(買權擁擠)、VIX 期限結構 {vix.get('value')}(正價差套利)。"
             + div_zh + " 策展區間上的百分位為估計值;僅 COT 在即時刷新時重算。")

    # ── L4 unwind queue (KB order, annotated with current readings) ──
    annotations = {
        "leveraged": {
            "en": f"Margin debt at the {mdebt.get('pct3y')}th pct and VIX term at {vix.get('value')} — the trigger wire is a VIX-ratio move through {kb.get('nuance_rules', {}).get('vix_ratio_crack', 0.95)}, not a news event.",
            "zh": f"融資餘額在第 {mdebt.get('pct3y')} 百分位、VIX 期限結構 {vix.get('value')}——引線是比值升破 {kb.get('nuance_rules', {}).get('vix_ratio_crack', 0.95)},不是某則新聞。",
        },
        "active_inst": {
            "en": f"NAAIM {naaim.get('value')} and COT at the {cot.get('pct3y')}th pct — de-risks on the first broken momentum week; NAAIM shows it within days.",
            "zh": f"NAAIM {naaim.get('value')}、COT 第 {cot.get('pct3y')} 百分位——動能第一週斷裂就降風險;NAAIM 幾天內顯形。",
        },
        "retail": {
            "en": f"AAII spread only {aaii.get('value')}pp and put/call {pc.get('value')} — the dip-buy reflex is intact; watch put/call through {kb.get('nuance_rules', {}).get('put_call_crack', 0.70)} for the flip.",
            "zh": f"AAII 多空差僅 {aaii.get('value')}pp、put/call {pc.get('value')}——逢低買進反射仍在;翻轉看 put/call 升破 {kb.get('nuance_rules', {}).get('put_call_crack', 0.70)}。",
        },
        "insiders": {
            "en": "Schedule-driven: buyback blackout windows remove the steadiest bid around earnings; insider-sales tone is qualitative (see dictionary).",
            "zh": "行事曆驅動:財報前後的庫藏股靜默期抽走最穩的買盤;內部人賣股基調屬定性(見字典)。",
        },
        "passive": {
            "en": "Mechanical and last — moves only on net redemptions, then sells the same few AI names regardless of price.",
            "zh": "機械且最後——只因淨贖回而動,一動就是不計價格賣同幾檔 AI 權值股。",
        },
    }
    queue = []
    for p in sorted(kb.get("player_map", []), key=lambda x: x["unwind_rank"]):
        a = annotations.get(p["id"], {"en": p["unwind_en"], "zh": p["unwind_zh"]})
        queue.append({"rank": p["unwind_rank"], "player_en": p["name_en"], "player_zh": p["name_zh"],
                      "trigger_en": a["en"], "trigger_zh": a["zh"]})

    # ── L4 TSMC view ──
    tv_en = ("Positioning is NOT a demand signal — nothing here changes wafer starts by itself. What crowdedness sets is "
             "the violence of the repricing when the AI narrative wobbles: a forced-seller cascade (leverage first, "
             "institutions second) can cut the sector's multiple in weeks, shut the equity/credit financing windows "
             f"(/credit already flags CCC−IG decompression: {'ON' if cd.get('value') else 'OFF'}), and freeze customers' "
             "expansion decisions before a single order moves. For TSMC the read is sequencing: crowded markets turn "
             "small demand news into large capex-planning noise — separate the price action from the order book.")
    tv_zh = ("部位不是需求訊號——這張卡本身不會改變投片量。擁擠度決定的是 AI 敘事一晃時「重新定價的猛烈度」:"
             "強制賣壓連鎖(槓桿先、機構次)可以在數週內壓縮整個板塊的估值倍數、關閉股權/信用融資窗口"
             f"(/credit 已標記 CCC−IG 解壓縮:{'ON' if cd.get('value') else 'OFF'}),並在任何一張訂單變動之前,"
             "先凍結客戶的擴產決策。對台積電,重點是先後順序:擁擠的市場會把小的需求新聞放大成大的 capex 規劃噪音"
             "——把股價行為和訂單簿分開讀。")

    # ── L4 thesis ──
    if state == "crowded_cracking":
        dir_en = "Crowded & cracking — de-risking window; the forced-seller queue is armed"
        dir_zh = "擁擠且龜裂中 — 降風險窗口;強制賣壓隊列已上膛"
        conf = "high" if nuance["cracks_on"] >= 3 else "medium"
    elif state == "crowded_rising":
        dir_en = "Crowded & rising — momentum intact, but drawdown violence is pre-loaded and hedges are cheap (VIX in contango)"
        dir_zh = "擁擠且仍在加碼 — 動能未壞,但回檔猛烈度已預先裝填;避險成本便宜(VIX 正價差)"
        conf = "medium"
    elif state == "crowded_stalling":
        dir_en = "Crowded & stalling — the ambiguous state; crack tells decide the next move"
        dir_zh = "擁擠但停止加碼 — 最曖昧的狀態;下一步由裂紋訊號決定"
        conf = "low"
    else:
        dir_en = "Uncrowded — positioning is not the risk; drawdowns should be shallow"
        dir_zh = "未達擁擠 — 部位不是風險所在;回檔應屬淺層"
        conf = "medium"

    leads = [
        {"en": f"VIX÷VIX3M through {kb.get('nuance_rules', {}).get('vix_ratio_crack', 0.95)} (now {vix.get('value')}) — the mechanical sellers' trigger, visible daily",
         "zh": f"VIX÷VIX3M 升破 {kb.get('nuance_rules', {}).get('vix_ratio_crack', 0.95)}(現 {vix.get('value')})——機械賣家的扳機,每天可見"},
        {"en": f"Equity put/call through {kb.get('nuance_rules', {}).get('put_call_crack', 0.70)} while indices hold (now {pc.get('value')}) — the hedging bid returning",
         "zh": f"指數未跌而 put/call 升破 {kb.get('nuance_rules', {}).get('put_call_crack', 0.70)}(現 {pc.get('value')})——避險買盤回歸"},
        {"en": f"NAAIM dropping >20 points in a week from {naaim.get('value')} — institutions de-risking before they talk about it",
         "zh": f"NAAIM 一週內從 {naaim.get('value')} 掉超過 20 點——機構先動手再開口"},
        {"en": "COT NQ net long unwinding two weeks in a row — the futures overlay coming off",
         "zh": "COT 那斯達克淨多連兩週回吐——期貨部位開始撤"},
        {"en": "/credit CCC−IG decompression persisting or widening — soft money repricing under full positioning",
         "zh": "/credit 的 CCC−IG 解壓縮持續或擴大——軟錢在滿倉之下重新定價"},
    ]

    # ── L5 scenario probabilities from the current state ──
    if state == "crowded_cracking":
        base = {"melt_up_grind": 15, "violent_flush": 40, "distribution_top": 30, "uncrowding_reset": 15}
    elif state == "crowded_rising":
        base = {"melt_up_grind": 35, "violent_flush": 30, "distribution_top": 20, "uncrowding_reset": 15}
    elif state == "crowded_stalling":
        base = {"melt_up_grind": 25, "violent_flush": 30, "distribution_top": 30, "uncrowding_reset": 15}
    else:
        base = {"melt_up_grind": 40, "violent_flush": 15, "distribution_top": 15, "uncrowding_reset": 30}
    seed_by_id = {s["id"]: s for s in kb.get("scenarios_seed", [])}
    scenarios = []
    for sid, prob in base.items():
        s = seed_by_id.get(sid, {})
        scenarios.append({
            "name_en": s.get("name_en", sid), "name_zh": s.get("name_zh", sid), "prob": prob,
            "trigger_en": s.get("trigger_en", ""), "trigger_zh": s.get("trigger_zh", ""),
        })

    falsification = [
        {"en": "A 5%+ pullback arrives and stays orderly — no vol-target cascade, put/call peaks below 0.90 — crowding was less loaded than the percentiles implied.",
         "zh": "出現 5% 以上回檔卻始終有序——沒有 vol-target 連鎖、put/call 峰值不到 0.90——擁擠的裝填量比百分位暗示的低。"},
        {"en": "COT and NAAIM unwind to 3y medians while indices grind flat — positioning resets without a flush; the drawdown-violence thesis loses its fuel.",
         "zh": "COT 與 NAAIM 回落到 3 年中位數而指數橫盤——部位不經洗倉就重置;「回檔猛烈度」論點失去燃料。"},
        {"en": "Margin debt YoY decelerates below +5% on the next two FINRA prints — the leverage layer self-deflates while prices hold.",
         "zh": "接下來兩期 FINRA 數字顯示融資餘額年增降到 +5% 以下——槓桿層在價格未跌下自行洩壓。"},
    ]
    early_warning = [
        {"en": "VIX ÷ VIX3M term-structure ratio (yfinance, real-time)", "zh": "VIX ÷ VIX3M 期限結構比(yfinance,即時)", "freq": "daily"},
        {"en": "CBOE equity put/call 10d vs the 0.70 line", "zh": "CBOE 股票 put/call 10 日對 0.70 界線", "freq": "daily"},
        {"en": "NAAIM exposure index (Wednesday print)", "zh": "NAAIM 曝險指數(週三發布)", "freq": "weekly"},
        {"en": "CFTC COT NQ net non-commercial (Friday publish, Tuesday as-of)", "zh": "CFTC COT 那斯達克投機淨部位(週五發布、週二基準)", "freq": "weekly"},
        {"en": "AAII bull-bear spread — watch for a late-cycle euphoria print >+25pp", "zh": "AAII 多空差——留意 >+25pp 的晚週期亢奮值", "freq": "weekly"},
        {"en": "FINRA margin debt YoY (accept the 2-month lag; it confirms, not leads)", "zh": "FINRA 融資餘額年增(接受兩個月落後;它是確認,不是領先)", "freq": "monthly"},
        {"en": "/credit CCC−IG decompression flag + buyback blackout calendar", "zh": "/credit 解壓縮旗標 + 庫藏股靜默期行事曆", "freq": "weekly"},
    ]

    return {
        "engine": "rules",
        "l4": {
            "positioning_read": {"summary_en": pr_en, "summary_zh": pr_zh},
            "unwind_queue": queue,
            "tsmc_view": {"summary_en": tv_en, "summary_zh": tv_zh},
            "thesis": {"direction_en": dir_en, "direction_zh": dir_zh, "confidence": conf, "leading_signals": leads},
        },
        "l5": {"scenarios": scenarios, "falsification": falsification, "early_warning": early_warning},
    }


def analyze(kb, l3, composite, nuance):
    """L4/L5 via Claude when ANTHROPIC_API_KEY is set; deterministic rules otherwise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, l3, composite, nuance)
        except Exception:
            log.exception("positioning: Claude L4/L5 failed — falling back to rules")
    return _rules(kb, l3, composite, nuance)
