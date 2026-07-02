"""Geopolitics & Second-Chain Radar — quant engine + snapshot assembler.

Pure functions over the curated KB (plus an optional live bundle):

  control_direction(moves, today)  — recency-weighted tighten/ease drift score
                                     (−∞..+∞ in practice ±2), verdict
                                     TIGHTENING / STABLE / EASING
  link_pct(link)                   — link completeness %; if a link declares
                                     sub_components, the MIN governs (Liebig
                                     within the link — litho governs equipment)
  composite_completeness(rows)     — 0..100 = 0.7×MIN + 0.3×MEAN across the 7
                                     links (Liebig's law, method aligned with
                                     /bottleneck; weakest link governs)
  classify_headline(title)         — keyword-rule escalation / de_escalation /
                                     neutral (bilingual keyword sets)
  build_market(kb, live)           — China-semis vs western-tools basket; a
                                     sentiment PROXY with weight 0 in the score
  build_snapshot(kb, live, ...)    — assembles L1–L3 then calls
                                     analysis.analyze() for L4/L5

No I/O, no network, no globals — trivially unit-testable.
"""
from __future__ import annotations

from . import analysis

# ── control-direction score ────────────────────────────────────────────────
RECENCY_HORIZON_M = 18   # moves older than 18 months contribute 0
TIGHTEN_THRESHOLD = 0.5  # score > +0.5 → TIGHTENING;  < −0.5 → EASING


def _month_diff(today_iso, move_ym):
    """Whole months between YYYY-MM(-DD) `move_ym` and YYYY-MM-DD `today_iso`."""
    ty, tm = int(today_iso[:4]), int(today_iso[5:7])
    my, mm = int(move_ym[:4]), int(move_ym[5:7])
    return (ty - my) * 12 + (tm - mm)


def control_direction(moves, today):
    """Recency-weighted drift of the control regime over the trailing 18 months.

    Each move: direction (tighten +1 / ease −1) × curated weight × linear
    recency decay. This measures MARGINAL drift only — the regime LEVEL is a
    separate curated judgment (kb["level"]), because a tight regime can ease
    at the margin and still be tight.
    """
    contributions, score = [], 0.0
    for mv in moves:
        months = _month_diff(today, mv["date"])
        rec = max(0.0, 1.0 - months / RECENCY_HORIZON_M)
        sign = 1.0 if mv["direction"] == "tighten" else -1.0
        c = round(sign * float(mv.get("weight", 1.0)) * rec, 4)
        score += c
        contributions.append({
            "id": mv["id"], "date": mv["date"],
            "name_en": mv["name_en"], "name_zh": mv["name_zh"],
            "direction": mv["direction"], "weight": mv.get("weight", 1.0),
            "tier": mv.get("tier", "T3"),
            "months_ago": months, "recency": round(rec, 3),
            "contribution": c,
        })
    score = round(score, 2)
    if score > TIGHTEN_THRESHOLD:
        verdict, label_en, label_zh = "TIGHTENING", "Tightening", "趨緊"
    elif score < -TIGHTEN_THRESHOLD:
        verdict, label_en, label_zh = "EASING", "Easing at the margin", "邊際緩和"
    else:
        verdict, label_en, label_zh = "STABLE", "Stable / two-way", "穩定 / 雙向"
    return {"score": score, "verdict": verdict,
            "label_en": label_en, "label_zh": label_zh,
            "contributions": contributions}


# ── second-chain completeness ──────────────────────────────────────────────
W_MIN, W_MEAN = 0.7, 0.3


def link_pct(link):
    """Completeness % of one link; sub-components MIN governs (Liebig within)."""
    subs = link.get("sub_components")
    if subs:
        return min(s["pct"] for s in subs)
    return link["completeness"]["pct"]


def completeness_verdict(score):
    if score >= 70:
        return "NEAR_PARITY", "Near parity", "接近對等"
    if score >= 45:
        return "CLOSING", "Closing fast", "快速逼近"
    if score >= 25:
        return "PARTIAL", "Partial chain", "部分成鏈"
    return "DEPENDENT", "Still dependent", "仍依賴西方鏈"


def build_links(kb):
    rows = []
    for ln in kb["links"]:
        pct = link_pct(ln)
        comp = ln["completeness"]
        rows.append({
            "id": ln["id"], "name_en": ln["name_en"], "name_zh": ln["name_zh"],
            "pct": pct, "est": bool(comp.get("est", True)),
            "tier": comp.get("tier", "T3"), "as_of": comp.get("as_of", ""),
            "source_en": comp.get("source_en", ""), "source_zh": comp.get("source_zh", ""),
            "years_behind": ln.get("years_behind"),
            "slope": ln.get("slope", "stalled"),
            "evidence_en": ln.get("evidence_en", ""), "evidence_zh": ln.get("evidence_zh", ""),
            "west_anchor_en": ln.get("west_anchor_en", ""), "west_anchor_zh": ln.get("west_anchor_zh", ""),
            "sub_components": ln.get("sub_components", []),
            "is_binding": False,  # set after the composite is known
        })
    return rows


def composite_completeness(rows):
    """0..100 second-chain completeness. 0.7×MIN + 0.3×MEAN (Liebig-weighted)."""
    pcts = {r["id"]: r["pct"] for r in rows}
    binding_id = min(pcts, key=pcts.get)
    min_pct = pcts[binding_id]
    mean_pct = sum(pcts.values()) / len(pcts)
    score = round(W_MIN * min_pct + W_MEAN * mean_pct, 1)
    score = max(0.0, min(100.0, score))
    verdict, verdict_en, verdict_zh = completeness_verdict(score)
    return {
        "score": score, "verdict": verdict,
        "verdict_en": verdict_en, "verdict_zh": verdict_zh,
        "binding_id": binding_id,
        "min_pct": min_pct, "mean_pct": round(mean_pct, 1),
        "w_min": W_MIN, "w_mean": W_MEAN,
    }


def build_trajectory(rows):
    up = sum(1 for r in rows if r["slope"] == "catching_up")
    total = len(rows)
    if up > total / 2:
        label_en, label_zh, arrow = "Catching up", "追趕中", "↑"
    elif up < total / 3:
        label_en, label_zh, arrow = "Stalled", "停滯", "→"
    else:
        label_en, label_zh, arrow = "Mixed", "分歧", "↗"
    return {"up": up, "total": total, "arrow": arrow,
            "label_en": f"{label_en} ({up}/{total} links improving)",
            "label_zh": f"{label_zh}({up}/{total} 環節向上)"}


# ── news classification (keyword rules; bilingual) ─────────────────────────
ESCALATION_KW = [
    "ban", "restrict", "sanction", "blacklist", "entity list", "curb",
    "tighten", "crackdown", "block", "revoke", "escalat", "probe",
    "investigation", "retaliat", "license requirement", "export control",
    "管制", "制裁", "禁", "封鎖", "斷供", "升級", "報復", "調查", "限制",
]
DEESCALATION_KW = [
    "ease", "easing", "resume", "waiver", "truce", "exempt", "relax",
    "lift", "deal", "agreement", "approve", "license granted", "licenses granted",
    "放寬", "恢復", "豁免", "休戰", "鬆綁", "批准", "協議", "緩和",
]
# unambiguous signal words count double, so e.g. 「休戰…管制暫停」 (truce headline
# that mentions the controls being suspended) resolves to de-escalation
ESCALATION_STRONG = ["ban", "blacklist", "entity list", "禁", "斷供", "報復"]
DEESCALATION_STRONG = ["truce", "waiver", "休戰", "豁免", "放寬"]


def classify_headline(title):
    t = (title or "").lower()
    esc = (sum(1 for k in ESCALATION_KW if k in t)
           + sum(1 for k in ESCALATION_STRONG if k in t))
    de = (sum(1 for k in DEESCALATION_KW if k in t)
          + sum(1 for k in DEESCALATION_STRONG if k in t))
    if esc > de:
        return "escalation"
    if de > esc:
        return "de_escalation"
    return "neutral"


# ── market basket (sentiment PROXY, weight 0 in the score) ─────────────────
def build_market(kb, live=None):
    live_m = (live or {}).get("market", {}) if live else {}
    rows = []
    for t in kb.get("market_basket", []):
        m = live_m.get(t["id"])
        if m and m.get("value") is not None:
            row = {"value": m["value"], "chg_1m": m.get("chg_1m"), "live": True}
        else:
            seed = t.get("seed", {})
            row = {"value": seed.get("value"), "chg_1m": seed.get("chg_1m"),
                   "live": False}
        rows.append({
            "id": t["id"], "ticker": t["ticker"],
            "name_en": t["name_en"], "name_zh": t["name_zh"],
            "group": t["group"], **row,
        })

    def avg(group):
        vals = [r["chg_1m"] for r in rows if r["group"] == group and r["chg_1m"] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    china, west = avg("china"), avg("west")
    spread = round(china - west, 2) if (china is not None and west is not None) else None
    if spread is None:
        read_en, read_zh = "insufficient data", "數據不足"
    elif spread > 1.0:
        read_en = "Market is bidding the second chain over western tools"
        read_zh = "市場正在押注第二鏈,相對西方設備股"
    elif spread < -1.0:
        read_en = "Market is bidding western tools over the second chain"
        read_zh = "市場偏向西方設備股,相對第二鏈"
    else:
        read_en, read_zh = "No clear relative bid either way", "兩籃相對強弱不明顯"
    return {
        "rows": rows,
        "china_avg_chg_1m": china, "west_avg_chg_1m": west, "spread": spread,
        "read_en": read_en, "read_zh": read_zh,
        "proxy_note_en": ("PROXY ONLY — 1-month price momentum is sentiment, not capability. "
                          "Weight 0 in the completeness score (same honesty rule as /pricing)."),
        "proxy_note_zh": "僅為代理——1 個月價格動能是情緒,不是能力。在完整度分數中權重為 0(與 /pricing 同一誠實規則)。",
    }


# ── snapshot assembly ──────────────────────────────────────────────────────
def build_snapshot(kb, live=None, generated_at="", today=""):
    today = today or kb.get("as_of_curated", "")

    # L1 — control regime + direction drift
    direction = control_direction(kb.get("moves", []), today)
    l1 = {
        "regime": kb.get("regime", []),
        "direction": direction,
        "level": kb.get("level", {}),
    }

    # L2 — second-chain completeness (Liebig)
    links = build_links(kb)
    composite = composite_completeness(links)
    for r in links:
        r["is_binding"] = (r["id"] == composite["binding_id"])
    binding = next(r for r in links if r["is_binding"])
    composite["binding_name_en"] = binding["name_en"]
    composite["binding_name_zh"] = binding["name_zh"]
    composite["note_en"] = kb.get("composite_note_en", "")
    composite["note_zh"] = kb.get("composite_note_zh", "")
    l2 = {
        "links": links,
        "composite": composite,
        "trajectory": build_trajectory(links),
    }

    # L3 — market proxy + news radar
    market = build_market(kb, live)
    news = []
    for n in (live or {}).get("news", []) if live else []:
        news.append({**n, "cls": classify_headline(n.get("title", ""))})
    counts = {
        "escalation": sum(1 for n in news if n["cls"] == "escalation"),
        "de_escalation": sum(1 for n in news if n["cls"] == "de_escalation"),
        "neutral": sum(1 for n in news if n["cls"] == "neutral"),
    }
    l3 = {"market": market, "news": news, "news_counts": counts}

    taiwan = kb.get("taiwan", {})

    # L4/L5 — Claude synthesis or deterministic rules
    core = {
        "composite": composite, "links": links, "trajectory": l2["trajectory"],
        "direction": direction, "level": l1["level"],
        "market": market, "news_counts": counts, "taiwan": taiwan,
    }
    out = analysis.analyze(kb, core)

    return {
        "generated_at": generated_at,
        "as_of": today,
        "source": "live" if live else "seed",
        "is_demo": live is None,
        "title_en": kb.get("title_en", "Geopolitics & Second-Chain Radar"),
        "title_zh": kb.get("title_zh", "地緣與第二供應鏈雷達"),
        "method_en": kb.get("method_en", ""), "method_zh": kb.get("method_zh", ""),
        "l1": l1,
        "l2": l2,
        "l3": l3,
        "taiwan": taiwan,
        "l4": out["l4"],
        "l5": out["l5"],
        "analysis_engine": out["engine"],
        "blind_spots_en": kb.get("blind_spots_en", []),
        "blind_spots_zh": kb.get("blind_spots_zh", []),
        "disclaimer_en": kb.get("disclaimer_en", ""),
        "disclaimer_zh": kb.get("disclaimer_zh", ""),
        "fetched_at": (live or {}).get("fetched_at") if live else None,
    }
