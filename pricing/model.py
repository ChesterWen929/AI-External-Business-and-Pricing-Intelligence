"""Pricing Power Radar — L1/L2/L3 quant engine + snapshot assembler.

The three KB layers form a price stack:  upstream cost → foundry ASP → downstream ASP.
build_snapshot(kb, live, ...) merges live market proxies over the curated price
seeds, then computes, per layer:
  • momentum      — weight-mean 1-month % move of its items (competitor rows w=0 excluded)
  • signal        — TSMC-favorability of that move (cost down = good; ASP up = good)
Cross-layer it derives:
  • pass-through  — how much of the upstream cost move shows up in foundry ASP,
                    and how much of foundry ASP shows up downstream
  • margin deltas — foundry ASP vs input cost, and downstream vs foundry
  • pricing_power — a 0–100 score and a CEO verdict (DEFENSIBLE / NEUTRAL / SQUEEZED)
  • alerts        — deterministic divergence warnings (squeeze / opportunity / HBM …)
Then analysis.analyze() writes L4/L5 (Claude, or rules fallback).
"""
from __future__ import annotations

from . import analysis

_VERDICT = {
    "defensible": {"en": "DEFENSIBLE — pricing power intact", "zh": "DEFENSIBLE — 定價權穩固"},
    "neutral":    {"en": "NEUTRAL — balanced / two-way",      "zh": "NEUTRAL — 平衡 / 雙向"},
    "squeezed":   {"en": "SQUEEZED — margin under pressure",  "zh": "SQUEEZED — 毛利受壓"},
}


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _sig_round(x, n=2):
    return None if x is None else round(x, n)


def _merge_items(kb, live):
    """id → {value, chg_1w, chg_1m, live}. Live proxy wins; KB seed fills gaps."""
    live_metrics = (live or {}).get("metrics", {}) if live else {}
    out = {}
    for it in kb.get("items", []):
        iid = it["id"]
        if iid in live_metrics:
            out[iid] = live_metrics[iid]
        else:
            seed = it.get("seed", {})
            out[iid] = {
                "value": seed.get("value"),
                "chg_1w": seed.get("chg_1w"),
                "chg_1m": seed.get("chg_1m"),
                "live": False,
            }
    return out


def _item_signal(direction, chg_1m):
    """Per-item TSMC-favorability of a rising price → on/off/neutral."""
    if chg_1m is None or abs(chg_1m) < 0.2:
        return "neutral"
    rising_good = direction in ("revenue", "demand")  # cost rising is bad
    up = chg_1m > 0
    return "on" if (up == rising_good) else "off"


def _layer_signal(layer_id, momentum):
    """Favorability of a layer's aggregate move (cost layer is inverted)."""
    fav = -momentum if layer_id == "up" else momentum
    if fav > 0.3:
        return "on"
    if fav < -0.3:
        return "off"
    return "neutral"


def _passthrough(num, den):
    """% of the `den` move that shows up in `num`. None when the base is tiny."""
    if den is None or num is None or abs(den) < 0.5:
        return None
    return round(num / den * 100)


def build_snapshot(kb, live=None, generated_at="", today=""):
    merged = _merge_items(kb, live)
    items_by_id = {it["id"]: it for it in kb.get("items", [])}

    # ── per-item rows + per-layer momentum ──
    rows_by_layer = {ly["id"]: [] for ly in kb.get("layers", [])}
    num = {ly["id"]: 0.0 for ly in kb.get("layers", [])}
    wsum = {ly["id"]: 0.0 for ly in kb.get("layers", [])}

    for it in kb.get("items", []):
        m = merged.get(it["id"], {})
        chg_1m = m.get("chg_1m")
        w = float(it.get("weight", 0.0))
        if w > 0 and chg_1m is not None:
            num[it["layer"]] += w * chg_1m
            wsum[it["layer"]] += w
        rows_by_layer[it["layer"]].append({
            "id": it["id"], "name_en": it["name_en"], "name_zh": it["name_zh"],
            "value": m.get("value"), "chg_1w": m.get("chg_1w"), "chg_1m": chg_1m,
            "unit": it.get("unit", ""), "metric": it.get("metric"), "direction": it.get("direction"),
            "tier": it.get("tier"), "is_estimate": bool(it.get("is_estimate")),
            "weight": w, "live": m.get("live", False),
            "signal": _item_signal(it.get("direction"), chg_1m),
            "source_en": it.get("source_en", ""), "source_zh": it.get("source_zh", ""),
            "source_url": it.get("source_url", ""),
            "note_en": it.get("note_en", ""), "note_zh": it.get("note_zh", ""),
        })

    momentum = {lid: (round(num[lid] / wsum[lid], 2) if wsum[lid] else 0.0) for lid in num}
    u = momentum.get("up", 0.0)
    f = momentum.get("fab", 0.0)
    d = momentum.get("down", 0.0)

    layers = []
    for ly in sorted(kb.get("layers", []), key=lambda x: x.get("order", 0)):
        lid = ly["id"]
        layers.append({
            "id": lid, "order": ly.get("order", 0),
            "name_en": ly["name_en"], "name_zh": ly["name_zh"],
            "role_en": ly["role_en"], "role_zh": ly["role_zh"],
            "momentum": momentum.get(lid, 0.0),
            "signal": _layer_signal(lid, momentum.get(lid, 0.0)),
            "items": rows_by_layer[lid],
        })

    # ── pricing-power score (0–100) & CEO verdict ──
    # base 50, reward foundry ASP outpacing input cost (margin) + downstream room.
    score = round(_clamp(50 + 6 * (f - u) + 3 * d, 0, 100), 1)
    key = "defensible" if score >= 60 else "squeezed" if score <= 40 else "neutral"
    pricing_power = {
        "score": score, "verdict_key": key,
        "verdict_en": _VERDICT[key]["en"], "verdict_zh": _VERDICT[key]["zh"],
    }

    # ── pass-through & margin deltas ──
    transmission = {
        "up_to_fab": _passthrough(f, u),    # cost move → foundry ASP
        "fab_to_down": _passthrough(d, f),  # foundry ASP → downstream ASP
    }
    margin = {
        "fab_delta": round(f - u, 2),    # foundry ASP vs input cost
        "chain_delta": round(d - f, 2),  # downstream vs foundry
    }

    # ── deterministic divergence alerts ──
    alerts = _alerts(u, f, d, merged)

    l3 = {
        "stack": {"upstream": u, "foundry": f, "downstream": d},
        "layers": layers,
        "pricing_power": pricing_power,
        "transmission": transmission,
        "margin": margin,
        "alerts": alerts,
    }

    analysis_out = analysis.analyze(kb, l3)

    return {
        "generated_at": generated_at,
        "as_of": today or kb.get("as_of_curated", ""),
        "source": "live" if live else "seed",
        "is_demo": live is None,
        "title_en": kb.get("title_en", "Pricing Power Radar"),
        "title_zh": kb.get("title_zh", "議價能力雷達"),
        "method_en": kb.get("method_en", ""),
        "method_zh": kb.get("method_zh", ""),
        "pricing_power": pricing_power,   # surfaced for the portal card
        "l3": l3,
        "l4": analysis_out["l4"],
        "l5": analysis_out["l5"],
        "analysis_engine": analysis_out["engine"],
        "blind_spots_en": kb.get("blind_spots_en", []),
        "blind_spots_zh": kb.get("blind_spots_zh", []),
        "news": (live or {}).get("news", []) if live else [],
        "fetched_at": (live or {}).get("fetched_at") if live else None,
    }


def _alerts(u, f, d, merged):
    """Deterministic, data-driven divergence flags. level ∈ squeeze|opportunity|strong|watch."""
    out = []
    if u > 1.5 and (f - u) < -1.0:
        out.append({"level": "squeeze",
                    "en": f"Cost squeeze: upstream cost +{u}%/m but foundry ASP only +{f}%/m — margin compressing.",
                    "zh": f"成本擠壓:上游成本月漲 +{u}% 但代工 ASP 僅 +{f}% — 毛利正被壓縮。"})
    if (f - u) > 2.0 and d >= 0:
        out.append({"level": "strong",
                    "en": f"Strong pricing power: foundry ASP (+{f}%/m) expanding ahead of input cost (+{u}%/m) with demand firm.",
                    "zh": f"定價權強勁:代工 ASP(+{f}%/月)領先投入成本(+{u}%/月)且需求穩固。"})
    if d > 1.5 and (d - f) > 1.0:
        out.append({"level": "opportunity",
                    "en": f"Room to raise: downstream ASP +{d}%/m is outrunning foundry ASP +{f}%/m — pricing left on the table.",
                    "zh": f"漲價空間:下游終端 ASP +{d}%/月 跑贏代工 ASP +{f}%/月 — 議價空間尚未用盡。"})
    # HBM co-input squeeze on customers
    hbm = (merged.get("hbm_price") or {}).get("chg_1m")
    n3 = (merged.get("n3_asp") or {}).get("chg_1m")
    if hbm is not None and n3 is not None and hbm > 5 and (hbm - n3) > 2:
        out.append({"level": "watch",
                    "en": f"HBM watch: HBM3E +{hbm}%/m far outpaces N3 ASP +{n3}%/m — memory cost eats customer budgets; watch the spillover onto leading-edge negotiations.",
                    "zh": f"HBM 警戒:HBM3E +{hbm}%/月 遠快於 N3 ASP +{n3}%/月 — 記憶體成本侵蝕客戶預算;留意對先進製程議價的外溢。"})
    if not out:
        out.append({"level": "watch",
                    "en": "No sharp divergence across the stack this read — cost, foundry ASP and demand are moving roughly together.",
                    "zh": "本次判讀價格堆疊無明顯背離 — 成本、代工 ASP 與需求大致同步。"})
    return out
