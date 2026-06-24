"""Nowcast engine — predict the *next* print of key releases before publication.

Official data is backward-looking: by the time the BLS/BEA publishes CPI or GDP,
it's already history. This module estimates the upcoming value from data we can
see *now*, with an explicit evidence grade:

  • official — a real published nowcast exists on FRED (e.g. GDP ← Atlanta Fed
                GDPNow). We surface it directly. Highest trust.
  • claude   — no official nowcast, so Claude synthesizes an estimate from a
                curated set of leading / higher-frequency indicators.
  • rules    — deterministic fallback (random-walk-with-drift on the series'
                own history) when no Anthropic key or Claude fails.

`build_nowcasts()` returns raw numbers keyed by target series_id; the caller
(`refresh_job`) attaches the next release date + formatted displays. This mirrors
the platform's "hard floor + graded estimate" pattern in flows/pricing/payback.
"""
import json
import re
import statistics
from datetime import datetime, timezone

import anthropic

MODEL = "claude-sonnet-4-5-20250929"
_RECENT_N = 6  # observations used for drift/σ in the rules model

# Which releases to nowcast. Each target declares an optional official `anchor`
# (a FRED series that IS a published nowcast) and the `leading` indicators that
# inform a Claude estimate. All anchors/leading series are tracked indicators,
# so their observations are already in `indicators_data`.
NOWCAST_MAP: dict[str, dict] = {
    # ── inflation ──
    "CPIAUCSL": {
        "anchor": None, "leading": ["PPIFIS", "DCOILWTICO", "T10YIE", "EXPINF1YR"],
        "note_en": "Producer prices, oil, and market/expected inflation lead consumer CPI by 1–3 months.",
        "note_zh": "生產者物價、油價與市場/預期通膨領先消費者 CPI 約 1–3 個月。",
    },
    "CPILFESL": {
        "anchor": None, "leading": ["PPIFIS", "T10YIE", "EXPINF1YR", "CES0500000003"],
        "note_en": "Core CPI tracks services/wages and pipeline producer prices, filtering food & energy.",
        "note_zh": "核心 CPI 追蹤服務/工資與上游生產者物價,過濾食品與能源。",
    },
    "PCEPILFE": {
        "anchor": None, "leading": ["CPILFESL", "PPIFIS", "CES0500000003"],
        "note_en": "Core PCE is reconstructed largely from the already-released CPI and PPI source data.",
        "note_zh": "核心 PCE 大半由已公布的 CPI 與 PPI 來源數據重組而成。",
    },
    "PCEPI": {
        "anchor": None, "leading": ["CPIAUCSL", "PPIFIS", "DCOILWTICO"],
        "note_en": "Headline PCE follows CPI plus the energy/food swing visible in oil and PPI.",
        "note_zh": "整體 PCE 跟隨 CPI,再加上油價與 PPI 反映的能源/食品擺動。",
    },
    # ── labor ──
    "PAYEMS": {
        "anchor": None, "leading": ["ICSA", "CCSA", "JTSHIR", "JTSQUR"],
        "note_en": "Weekly claims (timely) plus JOLTS hires/quits flow point to the monthly payroll change.",
        "note_zh": "每週申領（即時）加上 JOLTS 招聘/離職流動,指向當月非農變化。",
    },
    "UNRATE": {
        "anchor": None, "leading": ["ICSA", "CCSA", "SAHMREALTIME"],
        "note_en": "Continued claims and the Sahm gap lead the household-survey unemployment rate.",
        "note_zh": "續領與 Sahm 缺口領先家計調查的失業率。",
    },
    "JTSJOL": {
        "anchor": None, "leading": ["ICSA", "JTSHIR", "JTSQUR"],
        "note_en": "Openings move with the broader hiring/quits flow and claims trend.",
        "note_zh": "職缺隨整體招聘/離職流動與申領趨勢移動。",
    },
    # ── growth / activity ──
    "GDPC1": {
        "anchor": "GDPNOW", "leading": ["WEI", "INDPRO", "ICSA", "RSXFS"],
        "metric_en": "annualized real GDP growth", "metric_zh": "年化實質 GDP 成長",
        "note_en": "Atlanta Fed GDPNow is the official running nowcast of current-quarter GDP growth.",
        "note_zh": "亞特蘭大聯儲 GDPNow 是當季 GDP 成長的官方即時估計。",
    },
    "RSXFS": {
        "anchor": None, "leading": ["UMCSENT", "PCEC96", "W875RX1"],
        "note_en": "Consumer sentiment, real spending, and real income lead retail sales.",
        "note_zh": "消費者信心、實質消費與實質所得領先零售銷售。",
    },
    "INDPRO": {
        "anchor": None, "leading": ["IPMAN", "TCU", "MANEMP"],
        "note_en": "Manufacturing output, capacity use, and factory payrolls drive total industrial production.",
        "note_zh": "製造業產出、產能利用率與工廠就業驅動整體工業生產。",
    },
    # ── housing ──
    "HOUST": {
        "anchor": None, "leading": ["PERMIT", "MORTGAGE30US", "HSN1F"],
        "note_en": "Building permits (issued before ground-breaking) and rates lead housing starts.",
        "note_zh": "建築許可（動工前核發）與利率領先新屋開工。",
    },
}


def _conf_from_sigma(predicted: float, sigma: float) -> str:
    """Coarse confidence from the spread relative to the level."""
    denom = abs(predicted) if predicted else 1.0
    rel = sigma / denom
    if rel < 0.01:
        return "high"
    if rel < 0.04:
        return "medium"
    return "low"


def _recent_deltas(obs: list[dict]) -> list[float]:
    vals = [o["value"] for o in obs]
    return [vals[i] - vals[i - 1] for i in range(1, len(vals))]


def _rules_forecast(target: dict) -> dict:
    """Random-walk-with-drift on the series' own history."""
    obs = target["observations"]
    last = obs[-1]["value"]
    deltas = _recent_deltas(obs)[-_RECENT_N:]
    drift = statistics.fmean(deltas) if deltas else 0.0
    sigma = statistics.pstdev(deltas) if len(deltas) >= 2 else abs(drift) or abs(last) * 0.01
    predicted = last + drift
    return {
        "predicted_value": predicted,
        "low": predicted - sigma,
        "high": predicted + sigma,
        "basis": "rules",
        "confidence": _conf_from_sigma(predicted, sigma),
        "drivers": [
            f"Recent trend: last {len(deltas)} prints averaged {drift:+.3g}/period",
            "No published nowcast and no Claude key — deterministic drift model",
        ],
        "drivers_zh": [
            f"近期趨勢:最近 {len(deltas)} 筆平均每期 {drift:+.3g}",
            "無官方 nowcast 亦無 Claude 金鑰 — 採確定性漂移模型",
        ],
    }


def _official_forecast(target: dict, anchor: dict, cfg: dict) -> dict:
    """Surface a real published nowcast (e.g. GDPNow) directly as the prediction."""
    val = anchor["observations"][-1]["value"]
    recent = [o["value"] for o in anchor["observations"][-4:]]
    spread = statistics.pstdev(recent) if len(recent) >= 2 else abs(val) * 0.05
    spread = max(spread, abs(val) * 0.03)
    return {
        "predicted_value": val,
        "low": val - spread,
        "high": val + spread,
        "basis": "official",
        "confidence": "high",
        "anchor_series": anchor["series_id"],
        "metric_en": cfg.get("metric_en"),
        "metric_zh": cfg.get("metric_zh"),
        "drivers": [f"{anchor['name_en']} official nowcast: {val:+.3g}", cfg.get("note_en", "")],
        "drivers_zh": [f"{anchor['name_zh']} 官方即時估計:{val:+.3g}", cfg.get("note_zh", "")],
    }


def _fmt_lead_lines(leads: list[dict], lang: str) -> str:
    out = []
    for d in leads:
        ch = (d.get("changes") or {}).get("1m") or (d.get("changes") or {}).get("3m")
        chg = f", Δ {ch['delta']:+.3g}" if ch else ""
        name = d["name_zh"] if lang == "zh" else d["name_en"]
        out.append(f"  - {name} ({d['series_id']}): {d['latest_value']:.4g}{chg}")
    return "\n".join(out)


async def _claude_forecast(target: dict, leads: list[dict], cfg: dict, api_key: str) -> dict:
    """Synthesize an estimate from leading indicators. Raises on failure → caller falls back."""
    client = anthropic.AsyncAnthropic(api_key=api_key)
    recent = target["observations"][-8:]
    hist = "\n".join(f"  {o['date']}: {o['value']:.5g}" for o in recent)
    prompt = f"""You are a US macro nowcasting analyst. Estimate the NEXT, not-yet-published value of this release from leading data available today.

Target: {target['name_en']} / {target['name_zh']} ({target['series_id']})
Unit: {target['unit']}  ·  Frequency: {target['frequency']}
Recent published prints (oldest→newest):
{hist}

Leading indicators (latest value, recent change):
{_fmt_lead_lines(leads, 'en')}

How they lead: {cfg.get('note_en','')}

Predict the next print in the SAME unit as the target. Give a realistic low/high band (≈70% interval), not a point. Output ONLY this JSON (no markdown):
{{
  "predicted_value": <number>,
  "low": <number>,
  "high": <number>,
  "confidence": "high|medium|low",
  "drivers": ["3-6 word English driver", "..."],
  "drivers_zh": ["3-6 字繁中驅動因子", "..."]
}}
Cite the leading signals' direction in the drivers. Be numeric and specific."""
    msg = await client.messages.create(
        model=MODEL,
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        text = text[first:last + 1]
    data = json.loads(text)
    # validate required numeric fields
    pv, lo, hi = float(data["predicted_value"]), float(data["low"]), float(data["high"])
    if lo > hi:
        lo, hi = hi, lo
    conf = data.get("confidence", "medium")
    return {
        "predicted_value": pv,
        "low": lo,
        "high": hi,
        "basis": "claude",
        "confidence": conf if conf in ("high", "medium", "low") else "medium",
        "drivers": [str(x) for x in (data.get("drivers") or [])][:6],
        "drivers_zh": [str(x) for x in (data.get("drivers_zh") or [])][:6],
        "metric_en": cfg.get("metric_en"),
        "metric_zh": cfg.get("metric_zh"),
    }


async def build_nowcasts(indicators_data: list[dict], *, anthropic_key: str = "",
                         gen_ai: bool = True) -> dict[str, dict]:
    """Return {target_series_id: forecast_dict}. forecast_dict has predicted_value/
    low/high/basis/confidence/drivers[/_zh]/anchor_series?/metric_*?; the caller
    adds release_date + formatted displays + as_of."""
    by_sid = {d["series_id"]: d for d in indicators_data}
    out: dict[str, dict] = {}
    as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # collect Claude jobs to run them in parallel batches (cheaper wall-clock)
    import asyncio
    claude_jobs: list[tuple[str, dict, list[dict], dict]] = []

    for sid, cfg in NOWCAST_MAP.items():
        target = by_sid.get(sid)
        if not target or len(target.get("observations") or []) < 3:
            continue
        anchor_sid = cfg.get("anchor")
        anchor = by_sid.get(anchor_sid) if anchor_sid else None
        if anchor and anchor.get("observations"):
            fc = _official_forecast(target, anchor, cfg)
            fc["as_of"] = as_of
            out[sid] = fc
            continue
        leads = [by_sid[s] for s in cfg.get("leading", []) if s in by_sid and by_sid[s].get("observations")]
        if gen_ai and anthropic_key:
            claude_jobs.append((sid, target, leads, cfg))
        else:
            fc = _rules_forecast(target)
            fc["as_of"] = as_of
            out[sid] = fc

    # run Claude estimates in parallel; any failure → rules fallback for that target
    if claude_jobs:
        results = await asyncio.gather(
            *[_claude_forecast(t, leads, cfg, anthropic_key) for (_s, t, leads, cfg) in claude_jobs],
            return_exceptions=True,
        )
        for (sid, target, _leads, _cfg), res in zip(claude_jobs, results):
            fc = res if isinstance(res, dict) else _rules_forecast(target)
            fc["as_of"] = as_of
            out[sid] = fc

    return out
