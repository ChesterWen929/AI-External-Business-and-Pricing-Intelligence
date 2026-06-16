"""Capital Flow Radar — L3 quant engine + snapshot assembler.

build_snapshot(kb, live, ...) merges live metrics over the KB seeds, computes the
derived series (net liquidity, breadth, AI leadership), classifies every
indicator's risk tilt, then rolls those up into:
  • marginal_direction  — one risk-on/off number (−100..+100) with evidence
  • lenses              — liquidity / price / positioning cross-check (aligned?)
  • retail_vs_inst      — retail vs institution appetite + divergence
  • ai_signal           — 0..100 support for the AI-continuation thesis
Then calls analysis.analyze() for L4/L5 (Claude, or rules fallback).
"""
from __future__ import annotations

from . import analysis


def _sign(x, eps=1e-9):
    if x is None:
        return 0.0
    if x > eps:
        return 1.0
    if x < -eps:
        return -1.0
    return 0.0


def _merge_metrics(kb, live):
    """Each id → {value, chg_1w, chg_1m, live}. Live wins; KB seed fills gaps."""
    live_metrics = (live or {}).get("metrics", {}) if live else {}
    out = {}
    for ind in kb.get("indicators", []):
        iid = ind["id"]
        if iid in live_metrics:
            out[iid] = live_metrics[iid]
        else:
            seed = ind.get("seed", {})
            out[iid] = {
                "value": seed.get("value"),
                "chg_1w": seed.get("chg_1w"),
                "chg_1m": seed.get("chg_1m"),
                "live": False,
            }
    return out


def _tilt(direction, chg_1m):
    """Per-indicator contribution to RISK APPETITE in roughly [−1, +1]."""
    s = _sign(chg_1m)
    if direction == "risk_on":
        return s
    if direction == "risk_off":
        return -s
    if direction == "hedge":      # gold rising = mildly defensive
        return -0.5 * s
    return 0.0


def _signal_label(tilt):
    if tilt > 0.15:
        return "risk_on"
    if tilt < -0.15:
        return "risk_off"
    return "neutral"


# indicator sets for the roll-ups
_APPETITE = ["spx", "qqq", "semis", "btc", "hyg", "tlt", "vix", "gold", "hy_oas",
             "dxy", "net_liquidity", "stablecoins"]
_LIQUIDITY_LENS = ["net_liquidity", "walcl", "m2", "rrp", "stablecoins"]
_PRICE_LENS = ["spx", "qqq", "semis", "btc", "gold", "tlt", "vix"]
_POSITIONING_LENS = ["hy_oas", "vix", "gold", "tlt"]


def _dir_of(kb, iid):
    for ind in kb.get("indicators", []):
        if ind["id"] == iid:
            return ind.get("rising_means", "risk_on")
    for c in kb.get("computed", []):
        if c["id"] == iid:
            return c.get("rising_means", "risk_on")
    return "risk_on"


def build_snapshot(kb, live=None, generated_at="", today=""):
    metrics = _merge_metrics(kb, live)

    # ── derived series ──
    def chg(iid):
        return (metrics.get(iid) or {}).get("chg_1m")

    walcl, rrp, tga = metrics.get("walcl"), metrics.get("rrp"), metrics.get("tga")
    net_liq_val = None
    if walcl and rrp and tga and None not in (walcl["value"], rrp["value"], tga["value"]):
        net_liq_val = round(walcl["value"] - rrp["value"] - tga["value"], 1)
    # approximate net-liquidity 1m change from components (walcl − rrp − tga, in $B)
    def comp_delta(m):
        if not m or m.get("value") is None or m.get("chg_1m") is None:
            return 0.0
        return m["value"] * (m["chg_1m"] / 100.0)
    net_liq_chg_bn = round(comp_delta(walcl) - comp_delta(rrp) - comp_delta(tga), 1) if walcl else None
    net_liq_chg_pct = round(net_liq_chg_bn / net_liq_val * 100, 2) if (net_liq_val and net_liq_chg_bn is not None) else None

    breadth = round((chg("rsp") or 0) - (chg("spx") or 0), 2)
    ai_rel = round((chg("semis") or 0) - (chg("spx") or 0), 2)

    derived = {
        "net_liquidity": {"value": net_liq_val, "chg_1m": net_liq_chg_pct, "chg_bn": net_liq_chg_bn},
        "breadth": {"value": breadth},
        "ai_rel": {"value": ai_rel},
    }

    # ── per-reservoir + per-indicator signals ──
    reservoirs = []
    for res in kb.get("reservoirs", []):
        inds = [i for i in kb.get("indicators", []) if i.get("reservoir") == res["id"]]
        rows, tilts = [], []
        for ind in inds:
            m = metrics.get(ind["id"], {})
            t = _tilt(ind.get("rising_means"), m.get("chg_1m"))
            tilts.append(t)
            rows.append({
                "id": ind["id"], "name_en": ind["name_en"], "name_zh": ind["name_zh"],
                "value": m.get("value"), "chg_1w": m.get("chg_1w"), "chg_1m": m.get("chg_1m"),
                "unit": ind.get("unit", ""), "live": m.get("live", False),
                "signal": _signal_label(t),
                "rising_en": ind.get("rising_en", ""), "rising_zh": ind.get("rising_zh", ""),
            })
        res_tilt = sum(tilts) / len(tilts) if tilts else 0.0
        reservoirs.append({
            "id": res["id"], "name_en": res["name_en"], "name_zh": res["name_zh"],
            "size_en": res.get("size_en", ""), "size_zh": res.get("size_zh", ""),
            "role_en": res.get("role_en", ""), "role_zh": res.get("role_zh", ""),
            "group": res.get("group", ""), "signal": _signal_label(res_tilt),
            "tilt": round(res_tilt, 2), "indicators": rows,
        })

    # ── lens roll-ups ──
    def lens_score(ids):
        ts = []
        for iid in ids:
            if iid in derived:
                ts.append(_tilt(_dir_of(kb, iid), derived[iid].get("chg_1m") or derived[iid].get("value")))
            else:
                ts.append(_tilt(_dir_of(kb, iid), chg(iid)))
        return round(sum(ts) / len(ts), 2) if ts else 0.0

    liq_s, price_s, pos_s = lens_score(_LIQUIDITY_LENS), lens_score(_PRICE_LENS), lens_score(_POSITIONING_LENS)
    lens_scores = [liq_s, price_s, pos_s]
    aligned = all(s >= 0.1 for s in lens_scores) or all(s <= -0.1 for s in lens_scores)
    lenses = {
        "liquidity": {"score": liq_s, "label": _signal_label(liq_s)},
        "price": {"score": price_s, "label": _signal_label(price_s)},
        "positioning": {"score": pos_s, "label": _signal_label(pos_s)},
        "aligned": aligned,
    }

    # ── marginal capital direction (−100..+100) ──
    app_tilts = []
    for iid in _APPETITE:
        if iid in derived:
            app_tilts.append(_tilt(_dir_of(kb, iid), derived[iid].get("chg_1m") or 0))
        else:
            app_tilts.append(_tilt(_dir_of(kb, iid), chg(iid)))
    marginal = round(100 * (sum(app_tilts) / len(app_tilts)), 1) if app_tilts else 0.0
    marg_label_en = "Risk-on" if marginal > 25 else "Risk-off" if marginal < -25 else "Mixed / two-way"
    marg_label_zh = "Risk-on 進場" if marginal > 25 else "Risk-off 撤離" if marginal < -25 else "分歧 / 雙向"

    # ── retail vs institution ──
    # _tilt() already maps each indicator to a RISK-APPETITE contribution
    # (a risk_off indicator falling → positive appetite), so the mean tilt of
    # each proxy set IS that cohort's appetite — no further inversion.
    def appetite(ids):
        ts = [_tilt(_dir_of(kb, i), chg(i)) for i in ids]
        return round((sum(ts) / len(ts) if ts else 0.0) * 100, 1)
    retail = appetite(kb.get("retail_proxies", []))
    inst = appetite(kb.get("institution_proxies", []))
    divergence = round(retail - inst, 1)

    retail_vs_inst = {
        "retail": retail, "institution": inst, "divergence": divergence,
        "warning": divergence > 30,  # retail hot while institutions cautious
    }

    # ── AI continuation signal (0..100) ──
    ai_tilts = []
    for iid in kb.get("ai_signal_inputs", []):
        if iid in derived:
            ai_tilts.append(_tilt(_dir_of(kb, iid), derived[iid].get("chg_1m") or derived[iid].get("value")))
        else:
            ai_tilts.append(_tilt(_dir_of(kb, iid), chg(iid)))
    ai_raw = sum(ai_tilts) / len(ai_tilts) if ai_tilts else 0.0
    ai_score = round((ai_raw + 1) * 50, 1)  # map [−1,1] → [0,100]
    ai_label_en = "Supportive" if ai_score >= 60 else "Fragile" if ai_score <= 40 else "Mixed"
    ai_label_zh = "支撐" if ai_score >= 60 else "脆弱" if ai_score <= 40 else "分歧"

    l3 = {
        "reservoirs": reservoirs,
        "derived": derived,
        "lenses": lenses,
        "marginal_direction": {
            "score": marginal, "label_en": marg_label_en, "label_zh": marg_label_zh,
        },
        "retail_vs_inst": retail_vs_inst,
        "ai_signal": {"score": ai_score, "label_en": ai_label_en, "label_zh": ai_label_zh},
    }

    # ── L4 / L5 synthesis (Claude or rules) ──
    analysis_out = analysis.analyze(kb, l3)

    return {
        "generated_at": generated_at,
        "as_of": today or kb.get("as_of_curated", ""),
        "source": "live" if live else "seed",
        "is_demo": live is None,
        "title_en": kb.get("title_en", "Capital Flow Radar"),
        "title_zh": kb.get("title_zh", "資金流向雷達"),
        "method_en": kb.get("method_en", ""),
        "method_zh": kb.get("method_zh", ""),
        "l3": l3,
        "l4": analysis_out["l4"],
        "l5": analysis_out["l5"],
        "analysis_engine": analysis_out["engine"],
        "money_map": {
            "reservoirs": kb.get("reservoirs", []),
            "channels": kb.get("channels", []),
            "tap": kb.get("tap", {}),
        },
        "blind_spots_en": kb.get("blind_spots_en", []),
        "blind_spots_zh": kb.get("blind_spots_zh", []),
        "news": (live or {}).get("news", []) if live else [],
        "fetched_at": (live or {}).get("fetched_at") if live else None,
    }
