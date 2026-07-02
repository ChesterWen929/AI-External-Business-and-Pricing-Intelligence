"""Cycle Analogue Clock — L3 nearest-neighbour clock engine + snapshot assembler.

For each analogue pair the engine places TODAY's value on its 1995–2002 quarterly
curve: nearest neighbour on z-normalized (level, 1-yr slope) → a per-pair clock
reading (e.g. 1999Q1) + a distance-based confidence + a one-line reason. The
composite clock is the weighted median of the readings (weight = curated mapping
weight × confidence), expressed like 「1999.0 ± 1.3」. The DISPERSION of the seven
readings is itself a first-class signal: high dispersion = the eras are
structurally different, not just noisy.

Readings beyond the historical range are flagged (`beyond_range`) — the analogue
has no coordinate for today's value; distance decay keeps their weight low.
"""
from __future__ import annotations

import math

from . import analysis

_EPS = 1e-9


# --------------------------------------------------------------------------- #
# quarter helpers
# --------------------------------------------------------------------------- #
def quarter_to_year(label):
    """'1999Q1' → 1999.0 (start-of-quarter convention)."""
    y, q = label.split("Q")
    return int(y) + (int(q) - 1) * 0.25


def year_to_quarter(yf):
    y = int(yf)
    q = int(round((yf - y) / 0.25)) + 1
    if q > 4:
        y, q = y + 1, 1
    return f"{y}Q{q}"


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 1.0
    m = _mean(xs)
    v = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(v) or 1.0


# --------------------------------------------------------------------------- #
# nearest-neighbour placement
# --------------------------------------------------------------------------- #
def place_on_curve(points, today_value, today_slope=None, slope_weight=0.5):
    """Place (today_value, today_slope) on a quarterly historical curve.

    points: [{"q": "1995Q1", "v": float}, ...] oldest→newest.
    Candidates start at index 4 so every candidate has a 1-yr slope.
    Returns {year_frac, quarter, distance, confidence, beyond_range,
             hist_value, hist_slope}.
    """
    if not points or len(points) < 8 or today_value is None:
        return None
    vals = [p["v"] for p in points]
    cands = []  # (idx, level, slope)
    for i in range(4, len(points)):
        cands.append((i, vals[i], vals[i] - vals[i - 4]))

    lv_m, lv_s = _mean([c[1] for c in cands]), _std([c[1] for c in cands])
    sl_m, sl_s = _mean([c[2] for c in cands]), _std([c[2] for c in cands])

    zt_l = (today_value - lv_m) / lv_s
    zt_s = None if today_slope is None else (today_slope - sl_m) / sl_s

    best = None
    for i, lv, sl in cands:
        d2 = (zt_l - (lv - lv_m) / lv_s) ** 2
        if zt_s is not None:
            d2 += slope_weight * (zt_s - (sl - sl_m) / sl_s) ** 2
        d = math.sqrt(d2)
        if best is None or d < best[0]:
            best = (d, i, lv, sl)

    d, i, lv, sl = best
    beyond = today_value > max(vals) + _EPS or today_value < min(vals) - _EPS
    return {
        "year_frac": round(quarter_to_year(points[i]["q"]), 2),
        "quarter": points[i]["q"],
        "distance": round(d, 3),
        "confidence": round(math.exp(-d), 3),
        "beyond_range": beyond,
        "hist_value": lv,
        "hist_slope": round(sl, 2),
    }


def weighted_median(readings, weights):
    """Weighted median of readings (falls back to plain median on zero weight)."""
    if not readings:
        return None
    pairs = sorted(zip(readings, weights))
    total = sum(w for _, w in pairs)
    if total <= _EPS:
        return pairs[len(pairs) // 2][0]
    cum = 0.0
    for r, w in pairs:
        cum += w
        if cum >= total / 2 - _EPS:
            return r
    return pairs[-1][0]


def weighted_mad(readings, weights, center):
    """Weighted mean absolute deviation — the clock's ± / dispersion signal."""
    total = sum(weights)
    if total <= _EPS or center is None:
        return 0.0
    return sum(w * abs(r - center) for r, w in zip(readings, weights)) / total


# --------------------------------------------------------------------------- #
# verdict / labels
# --------------------------------------------------------------------------- #
def verdict_for(clock, peak=2000.0):
    if clock is None:
        return {"key": "unknown", "en": "No reading", "zh": "無讀數"}
    if clock < 1997.0:
        return {"key": "early_cycle", "en": "EARLY CYCLE — pre-1997 territory", "zh": "週期早段——1997 之前的座標"}
    if clock < 1998.5:
        return {"key": "mid_cycle", "en": "MID CYCLE — the 1997 zone", "zh": "週期中段——1997 區間"}
    if clock < 1999.5:
        return {"key": "late_cycle", "en": "LATE CYCLE — the late-1998/1999 zone", "zh": "週期後段——1998 末～1999 區間"}
    if clock <= peak + _EPS:
        return {"key": "peak_zone", "en": "PEAK ZONE — late-1999 / spring-2000", "zh": "見頂區——1999 末～2000 春"}
    return {"key": "post_peak", "en": "POST-PEAK — past the 2000Q1 analogue", "zh": "峰後——已越過 2000Q1 類比點"}


def dispersion_label(pm):
    if pm <= 0.6:
        return {"key": "coherent", "en": "coherent — the analogy holds together", "zh": "一致——類比站得住"}
    if pm <= 1.2:
        return {"key": "mixed", "en": "mixed — pairs disagree; read with care", "zh": "分歧——各組配對不同調,判讀需保留"}
    return {"key": "structural", "en": "structurally divergent — the eras differ where it matters",
            "zh": "結構性偏離——兩個時代在關鍵處不像"}


def _answer(clock, pm, low_pair, high_pair):
    """Deterministic bilingual answer to the Fortune question (1997 or 1999?)."""
    if clock is None:
        return {"en": "No composite reading.", "zh": "無合成讀數。"}
    ylab = f"{clock:.1f}"
    if clock < 1998.0:
        en = f"Closer to 1997 than 1999: composite {ylab} ± {pm:.1f}."
        zh = f"比較像 1997 而非 1999：合成讀數 {ylab} ± {pm:.1f}。"
    elif clock <= 2000.0:
        en = f"Closer to 1999 than 1997: composite {ylab} ± {pm:.1f}."
        zh = f"比較像 1999 而非 1997：合成讀數 {ylab} ± {pm:.1f}。"
    else:
        en = f"Past the 2000 analogue point: composite {ylab} ± {pm:.1f}."
        zh = f"已越過 2000 類比點：合成讀數 {ylab} ± {pm:.1f}。"
    if low_pair and high_pair and low_pair["id"] != high_pair["id"]:
        en += (f" But the pairs disagree: {low_pair['name_en']} reads {low_pair['reading']['quarter']}"
               f" while {high_pair['name_en']} reads {high_pair['reading']['quarter']} — the dispersion IS the message:"
               " part of this cycle has no 1990s coordinate.")
        zh += (f" 但各組配對不同調：{low_pair['name_zh']}讀 {low_pair['reading']['quarter']}、"
               f"{high_pair['name_zh']}讀 {high_pair['reading']['quarter']}——分歧本身就是訊息：本輪有一段在 90 年代沒有座標。")
    return {"en": en, "zh": zh}


# --------------------------------------------------------------------------- #
# snapshot assembly
# --------------------------------------------------------------------------- #
def _merge_today(kb, live):
    """Per pair-id → today input dict. Live (sibling/FRED) value wins; seed fills."""
    live_vals = (live or {}).get("pair_values", {}) if live else {}
    out = {}
    for pair in kb.get("pairs", []):
        pid = pair["id"]
        seed = dict(pair.get("today_seed", {}))
        row = {
            "value": seed.get("value"), "change_1y": seed.get("change_1y"),
            "tier": seed.get("tier", "T3"), "est": bool(seed.get("est")),
            "as_of": seed.get("as_of", ""), "source_en": seed.get("source_en", ""),
            "source_zh": seed.get("source_zh", ""), "live": False, "via": "seed",
        }
        lv = live_vals.get(pid)
        if lv and lv.get("value") is not None:
            row["value"] = lv["value"]
            if lv.get("change_1y") is not None:
                row["change_1y"] = lv["change_1y"]
            row["live"] = True
            row["via"] = lv.get("via", "live")
            if lv.get("as_of"):
                row["as_of"] = lv["as_of"]
        out[pid] = row
    return out


def build_snapshot(kb, live=None, generated_at="", today=""):
    cycle = kb.get("cycle", {})
    peak = cycle.get("peak", 2000.0)
    start = cycle.get("start", 1995.0)
    slope_w = kb.get("slope_weight", 0.5)
    series_by_id = {s["id"]: s for s in kb.get("series", [])}
    today_inputs = _merge_today(kb, live)

    pairs_out, readings, weights = [], [], []
    for pair in kb.get("pairs", []):
        ser = series_by_id.get(pair["series_id"])
        tin = today_inputs[pair["id"]]
        reading = place_on_curve(ser["points"], tin["value"], tin.get("change_1y"),
                                 slope_weight=slope_w) if ser else None
        eff_w = 0.0
        if reading:
            eff_w = round(pair.get("weight", 1.0) * reading["confidence"], 4)
            readings.append(reading["year_frac"])
            weights.append(eff_w)
        unit = pair.get("unit", "")
        reason_en = reason_zh = ""
        if reading:
            reason_en = (f"Today {tin['value']}{unit} (1-yr Δ {tin.get('change_1y')}) sits nearest "
                         f"{reading['quarter']} ({reading['hist_value']}{unit}, Δ {reading['hist_slope']})"
                         + ("; today exceeds the whole 1995–2002 range — no true coordinate."
                            if reading["beyond_range"] else "."))
            reason_zh = (f"今日 {tin['value']}{unit}（一年變化 {tin.get('change_1y')}）最近鄰為 "
                         f"{reading['quarter']}（{reading['hist_value']}{unit}，變化 {reading['hist_slope']}）"
                         + ("；今日值超出 1995–2002 全區間——類比已無真座標。" if reading["beyond_range"] else "。"))
        pairs_out.append({
            "id": pair["id"], "name_en": pair["name_en"], "name_zh": pair["name_zh"],
            "counterpart_en": pair.get("counterpart_en", ""), "counterpart_zh": pair.get("counterpart_zh", ""),
            "unit": unit, "series_id": pair["series_id"],
            "series_tier": (ser or {}).get("tier", ""),
            "weight": pair.get("weight", 1.0), "eff_weight": eff_w,
            "today": tin, "reading": reading,
            "reason_en": reason_en, "reason_zh": reason_zh,
            "why_en": pair.get("why_en", ""), "why_zh": pair.get("why_zh", ""),
            "breaks_en": pair.get("breaks_en", ""), "breaks_zh": pair.get("breaks_zh", ""),
        })

    clock = weighted_median(readings, weights)
    pm = round(weighted_mad(readings, weights, clock), 2) if clock is not None else 0.0
    clock = round(clock, 2) if clock is not None else None

    score = None
    if clock is not None:
        score = round(max(0.0, min(100.0, (clock - start) / (peak - start) * 100.0)), 1)
    verdict = verdict_for(clock, peak=peak)
    disp = dispersion_label(pm)

    scored = [p for p in pairs_out if p["reading"]]
    low_pair = min(scored, key=lambda p: p["reading"]["year_frac"], default=None)
    high_pair = max(scored, key=lambda p: p["reading"]["year_frac"], default=None)
    answer = _answer(clock, pm, low_pair, high_pair)

    l3 = {
        "pairs": pairs_out,
        "composite": {
            "clock": clock,
            "clock_label": year_to_quarter(clock) if clock is not None else "",
            "plus_minus": pm,
            "dispersion": pm,
            "dispersion_key": disp["key"], "dispersion_en": disp["en"], "dispersion_zh": disp["zh"],
            "score": score,
            "verdict_key": verdict["key"], "verdict_en": verdict["en"], "verdict_zh": verdict["zh"],
            "n_pairs": len(scored),
            "n_beyond": sum(1 for p in scored if p["reading"]["beyond_range"]),
            "answer_en": answer["en"], "answer_zh": answer["zh"],
        },
        "cycle": {"start": start, "peak": peak, "peak_label": cycle.get("peak_label", "2000Q1"),
                  "end": cycle.get("end", 2002.75)},
    }

    analysis_out = analysis.analyze(kb, l3)

    backdrop = series_by_id.get(kb.get("backdrop_series", ""), {})
    return {
        "generated_at": generated_at,
        "as_of": today or kb.get("as_of_curated", ""),
        "source": "live" if live else "seed",
        "is_demo": live is None,
        "kb_version": kb.get("kb_version", ""),
        "title_en": kb.get("title_en", "Cycle Analogue Clock"),
        "title_zh": kb.get("title_zh", "週期類比時鐘"),
        "sub_en": kb.get("sub_en", ""), "sub_zh": kb.get("sub_zh", ""),
        "method_en": kb.get("method_en", ""), "method_zh": kb.get("method_zh", ""),
        "disclaimer_en": kb.get("disclaimer_en", ""), "disclaimer_zh": kb.get("disclaimer_zh", ""),
        "tier_legend": kb.get("tier_legend", {}),
        "l3": l3,
        "l4": analysis_out["l4"],
        "l5": analysis_out["l5"],
        "analysis_engine": analysis_out["engine"],
        "backdrop": {
            "id": backdrop.get("id", ""), "name_en": backdrop.get("name_en", ""),
            "name_zh": backdrop.get("name_zh", ""), "tier": backdrop.get("tier", ""),
            "source_en": backdrop.get("source_en", ""), "source_zh": backdrop.get("source_zh", ""),
            "points": backdrop.get("points", []),
        },
        "blind_spots_en": kb.get("blind_spots_en", []),
        "blind_spots_zh": kb.get("blind_spots_zh", []),
        "sibling_context": (live or {}).get("context", {}) if live else {},
        "news": (live or {}).get("news", []) if live else [],
        "fetched_at": (live or {}).get("fetched_at") if live else None,
    }
