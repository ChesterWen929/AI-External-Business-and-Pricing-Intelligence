"""Weekly refresh pipeline for the US Economic Monitor (/econ) platform.

Adapted from us-econ-monitor-industry/backend/daily_job.py. Runs on the
TaiBridge weekly cadence (Wed, alongside the stock refresh) instead of daily.

Steps:
1. Fetch all indicators from FRED (full 120-point history)
2. For high-importance indicators: generate AI commentary + industry impact (parallel batches)
3. For high-importance indicators: run validators (anomaly + consistency; freshness selective)
4. Fetch the FRED release calendar for all tracked series
5. Save snapshot to data/econ/snapshots/latest.json (+ dated copy)
6. Scan for TSMC-negative impacts → generate recommended actions → write .eml DRAFT + JSON alert
   (drafts only — nothing is ever sent)
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import threading
from datetime import datetime, date, timedelta
from email.message import EmailMessage
from email.utils import make_msgid, formatdate
from pathlib import Path

import anthropic

from .indicators_config import INDICATORS
from . import fred_client as fred
from . import claude_client as claude_ai
from . import validators as validators_mod
from . import nowcast as nowcast_mod

DATA_DIR = Path(__file__).parent.parent / "data" / "econ"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
ALERTS_DIR = DATA_DIR / "alerts"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
ALERTS_DIR.mkdir(parents=True, exist_ok=True)

ALERT_RECIPIENT = "sleptbeauty@gmail.com"
ALERT_SENDER = "us-econ-monitor@localhost"

# Guards against concurrent refreshes (scheduler + cron ping + manual button).
_refresh_lock = threading.Lock()
_refresh_running = False


def _fmt_value(value: float, fmt: str) -> str:
    if fmt == "percent":
        return f"{value:.2f}%"
    if fmt == "billions":
        return f"${value/1000:.2f}B" if abs(value) > 1000 else f"${value:.1f}M"
    if fmt == "thousands":
        # FRED value is already expressed in thousands (e.g. PAYEMS 159,001 = 159M persons)
        return f"{value:,.0f}K"
    if fmt == "count":
        # FRED value is a raw count (e.g. ICSA 225,000 claims) — abbreviate it
        if abs(value) >= 1_000_000:
            return f"{value/1_000_000:,.2f}M"
        if abs(value) >= 1_000:
            return f"{value/1_000:,.0f}K"
        return f"{value:,.0f}"
    if fmt == "index":
        return f"{value:.1f}"
    return f"{value:,.2f}"


def _estimate_next_release(frequency: str, last_obs_date: str) -> str | None:
    """Fallback when FRED has no scheduled future date: project from frequency.

    Releases lag their reference period, so we add roughly one period to the
    latest observation date and nudge past today.
    """
    try:
        d = date.fromisoformat(last_obs_date)
    except Exception:
        return None
    step = {"Daily": 1, "Weekly": 7, "Monthly": 31, "Quarterly": 92}.get(frequency, 31)
    today = date.today()
    nxt = d + timedelta(days=step)
    while nxt <= today:
        nxt += timedelta(days=step)
    return nxt.isoformat()


# ── Multi-horizon change (week / month / quarter / year) ──────────────────────
# Each indicator's change is computed against the observation closest to
# (latest_date − horizon). A horizon is reported only when a real observation
# lands near it, so a monthly series has no "1w" and a quarterly series has no
# "1m" (they show "—" in the UI rather than a misleading number).
_HORIZONS = {"1w": 7, "1m": 30, "3m": 91, "1y": 365}


def _nearest_excluding_latest(obs: list[dict], target_iso: str):
    """Observation whose date is closest to target_iso, excluding the latest point."""
    target = date.fromisoformat(target_iso)
    best = None
    best_gap = None
    for o in obs[:-1]:
        gap = abs((date.fromisoformat(o["date"]) - target).days)
        if best_gap is None or gap < best_gap:
            best_gap, best = gap, o
    return best, best_gap


def _changes_over_horizons(obs: list[dict]) -> dict:
    """{'1w'|'1m'|'3m'|'1y': {delta, pct, ref_date, ref_value} | None} from a series' own history."""
    out: dict = {k: None for k in _HORIZONS}
    if len(obs) < 2:
        return out
    latest = obs[-1]
    lv = latest["value"]
    ld = date.fromisoformat(latest["date"])
    for key, days in _HORIZONS.items():
        target = (ld - timedelta(days=days)).isoformat()
        ref, gap = _nearest_excluding_latest(obs, target)
        if ref is None or gap is None or gap > max(days * 0.4, 10) or ref["date"] == latest["date"]:
            continue
        delta = lv - ref["value"]
        pct = (delta / abs(ref["value"]) * 100) if ref["value"] != 0 else None
        out[key] = {"delta": delta, "pct": pct, "ref_date": ref["date"], "ref_value": ref["value"]}
    return out


async def _gen_ai(ind: dict, obs: list[dict], anthropic_key: str) -> dict:
    """Generate commentary + industry impact for one indicator (parallel)."""
    try:
        comm_task = claude_ai.generate_commentary(
            api_key=anthropic_key,
            name_en=ind["name_en"], name_zh=ind["name_zh"], desc_zh=ind["desc_zh"],
            current_value=obs[-1]["value"], prev_value=obs[-2]["value"] if len(obs) >= 2 else None,
            unit=ind["unit"], observations=obs,
        )
        imp_task = claude_ai.generate_industry_impact(
            api_key=anthropic_key,
            name_en=ind["name_en"], name_zh=ind["name_zh"], desc_zh=ind["desc_zh"],
            current_value=obs[-1]["value"], prev_value=obs[-2]["value"] if len(obs) >= 2 else None,
            unit=ind["unit"], observations=obs,
        )
        comm, imp = await asyncio.gather(comm_task, imp_task)
        return {"commentary": comm, "impact": imp}
    except Exception as e:
        return {"error": str(e)}


async def _fetch_calendar(fred_key: str) -> list[dict]:
    """Recent release dates for all tracked series (same shape as the old /api/calendar)."""
    calendar: list[dict] = []
    seen: set[str] = set()
    for ind in INDICATORS:
        try:
            dates = await fred.get_release_dates(fred_key, ind["series_id"], limit=3)
        except Exception:
            continue
        for d in dates:
            key = f"{d}:{ind['series_id']}"
            if key not in seen:
                seen.add(key)
                calendar.append({
                    "date": d,
                    "series_id": ind["series_id"],
                    "name_en": ind["name_en"],
                    "name_zh": ind["name_zh"],
                    "category": ind["category"],
                    "frequency": ind["frequency"],
                })
    calendar.sort(key=lambda x: x["date"], reverse=True)
    return calendar[:60]


async def _generate_recommendations(
    anthropic_key: str,
    negative_indicators: list[dict],
) -> dict:
    """Ask Claude to synthesize actionable recommendations from the list of TSMC-negative indicators."""
    if not negative_indicators:
        return {"zh": "", "en": ""}

    client = anthropic.AsyncAnthropic(api_key=anthropic_key)

    summary = "\n".join([
        f"- {ni['name_zh']} ({ni['series_id']}): {ni['latest_display']} "
        f"({ni['delta']:+.3g}, {ni['pct_change']:+.2f}%) — TSMC sentiment: NEGATIVE\n"
        f"  Claude analysis (zh): {ni['tsmc_analysis_zh']}"
        for ni in negative_indicators
    ])

    prompt = f"""You are a TSMC equity/risk analyst. Below are {len(negative_indicators)} US macro indicators whose latest readings have been judged NEGATIVE for TSMC (2330.TW / TSM) by an earlier AI analyst:

{summary}

Synthesize a tactical action plan for the recipient (a TSMC analyst/investor) in BOTH Traditional Chinese and English. Output STRICT JSON only:
{{
  "zh": "繁中行動方案，含: 1) 風險判讀總結（80字內） 2) 立刻要做的3件事（具體可執行） 3) 短期觀察清單（具體指標/事件） 4) 部位/避險建議（具體標的或工具）",
  "en": "Same plan in English, structured as: 1) Risk summary (<80 words) 2) Immediate 3 actions 3) Short-term watch list 4) Position/hedge suggestions"
}}

Be concrete. Mention specific tickers, options strategies, or alternative chip names where relevant. No vague platitudes."""

    msg = await client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        text = text[first:last + 1]
    try:
        return json.loads(text)
    except Exception:
        return {"zh": text, "en": text}


def _build_eml(
    subject: str,
    html_body: str,
    text_body: str,
    to_addr: str = ALERT_RECIPIENT,
    from_addr: str = ALERT_SENDER,
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return bytes(msg)


def _render_alert_html(date_str: str, negatives: list[dict], recs: dict) -> str:
    rows = "\n".join([
        f"""<tr style="border-bottom:1px solid #e5e7eb;">
          <td style="padding:10px 6px;">
            <strong>{ni['name_zh']}</strong><br>
            <span style="color:#6b7280;font-size:12px;">{ni['series_id']} · {ni['latest_date']}</span>
          </td>
          <td style="padding:10px 6px;text-align:right;">
            <span style="font-size:18px;font-weight:bold;color:#dc2626;">{ni['latest_display']}</span><br>
            <span style="color:{'#dc2626' if (ni.get('delta') or 0) > 0 else '#10b981'};font-size:12px;">
              {('+' if (ni.get('delta') or 0) > 0 else '')}{ni.get('delta', 0):.3g} ({(ni.get('pct_change') or 0):+.2f}%)
            </span>
          </td>
          <td style="padding:10px 6px;font-size:13px;color:#374151;line-height:1.5;">{ni['tsmc_analysis_zh']}</td>
        </tr>"""
        for ni in negatives
    ])

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>TSMC Alert {date_str}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Microsoft JhengHei',sans-serif;max-width:760px;margin:0 auto;padding:20px;background:#f9fafb;color:#111827;">
  <div style="background:linear-gradient(135deg,#dc2626,#991b1b);color:white;padding:20px;border-radius:12px 12px 0 0;">
    <h1 style="margin:0;font-size:22px;">⚠️ TSMC 警示報告 · {date_str}</h1>
    <p style="margin:6px 0 0 0;opacity:0.9;font-size:13px;">偵測到 <strong>{len(negatives)}</strong> 項對台積電不利的美國經濟指標</p>
  </div>

  <div style="background:white;padding:20px;border:1px solid #e5e7eb;border-top:none;">
    <h2 style="font-size:16px;color:#dc2626;margin:0 0 12px 0;">📊 不利指標清單</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="background:#f3f4f6;text-align:left;">
        <th style="padding:8px 6px;">指標</th><th style="padding:8px 6px;text-align:right;">最新數據</th><th style="padding:8px 6px;">對台積電分析</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <div style="background:#fef3c7;padding:20px;border:1px solid #fde68a;border-radius:0 0 12px 12px;">
    <h2 style="font-size:16px;color:#92400e;margin:0 0 12px 0;">🎯 建議行動方案（Claude AI 綜合分析）</h2>
    <div style="background:white;padding:14px;border-radius:8px;white-space:pre-wrap;font-size:14px;line-height:1.7;color:#1f2937;">{recs.get('zh', '(no recommendations)')}</div>
    <details style="margin-top:12px;">
      <summary style="cursor:pointer;color:#92400e;font-size:13px;">English version ▼</summary>
      <div style="background:white;padding:14px;border-radius:8px;white-space:pre-wrap;font-size:13px;line-height:1.7;color:#1f2937;margin-top:8px;">{recs.get('en', '')}</div>
    </details>
  </div>

  <p style="text-align:center;color:#9ca3af;font-size:11px;margin:16px 0;">
    🤖 自動產生 · US Economic Monitor + Claude AI · {datetime.now().isoformat(timespec='seconds')}<br>
    本郵件為 <strong>草稿</strong>，請手動確認後再決定是否寄送。
  </p>
</body></html>"""


def _render_alert_text(date_str: str, negatives: list[dict], recs: dict) -> str:
    lines = [f"⚠️ TSMC 警示報告 · {date_str}", "=" * 60, ""]
    lines.append(f"偵測到 {len(negatives)} 項對台積電不利的美國經濟指標：\n")
    for ni in negatives:
        lines.append(f"▶ {ni['name_zh']} ({ni['series_id']})")
        lines.append(f"  最新: {ni['latest_display']} on {ni['latest_date']}")
        lines.append(f"  變化: {ni.get('delta', 0):+.3g} ({(ni.get('pct_change') or 0):+.2f}%)")
        lines.append(f"  分析: {ni['tsmc_analysis_zh']}")
        lines.append("")
    lines.append("─" * 60)
    lines.append("🎯 建議行動方案：")
    lines.append("")
    lines.append(recs.get("zh", ""))
    lines.append("")
    lines.append("─" * 60)
    lines.append(f"🤖 自動產生 · {datetime.now().isoformat(timespec='seconds')}")
    return "\n".join(lines)


def snapshot_is_fresh_today() -> bool:
    """True when latest.json was already generated today (skip duplicate runs)."""
    latest = SNAPSHOTS_DIR / "latest.json"
    if not latest.exists():
        return False
    try:
        snap = json.loads(latest.read_text(encoding="utf-8"))
        return snap.get("date") == datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return False


async def run_weekly_refresh(anthropic_key: str, fred_key: str, gen_ai: bool = True, force: bool = False) -> dict:
    """Main pipeline. Returns the summary dict written to disk."""
    today = datetime.now().strftime("%Y-%m-%d")
    if not force and snapshot_is_fresh_today():
        print(f"  econ refresh skipped — snapshot for {today} already exists")
        return {"status": "skipped", "date": today}

    print(f"\n{'='*60}\n  Econ weekly refresh @ {datetime.now().isoformat(timespec='seconds')}\n{'='*60}\n")

    # ── 1. Fetch all indicators ──
    print(f"[1/6] Fetching {len(INDICATORS)} indicators from FRED…")
    indicators_data = []
    for ind in INDICATORS:
        try:
            obs = await fred.get_series_observations(fred_key, ind["series_id"], limit=120)
            if not obs:
                continue
            latest = obs[-1]
            prev = obs[-2] if len(obs) >= 2 else None
            delta = (latest["value"] - prev["value"]) if prev else None
            pct = (delta / abs(prev["value"]) * 100) if (delta is not None and prev["value"] != 0) else None
            indicators_data.append({
                **ind,
                "latest_date": latest["date"],
                "latest_value": latest["value"],
                "latest_display": _fmt_value(latest["value"], ind["format"]),
                "prev_value": prev["value"] if prev else None,
                "delta": delta,
                "pct_change": pct,
                "changes": _changes_over_horizons(obs),
                "observations": obs,
            })
        except Exception as e:
            print(f"  ✗ {ind['series_id']}: {e}")
    print(f"  ✓ Fetched {len(indicators_data)} indicators\n")

    # ── 2. AI commentary + impact for high-importance ──
    ai_cache: dict[str, dict] = {}
    if gen_ai and anthropic_key:
        high = [d for d in indicators_data if d["importance"] == "high"]
        print(f"[2/6] Generating AI analysis for {len(high)} high-importance indicators (parallel batches of 3)…")
        BATCH = 3
        for i in range(0, len(high), BATCH):
            batch = high[i:i + BATCH]
            results = await asyncio.gather(*[_gen_ai(d, d["observations"], anthropic_key) for d in batch])
            for d, r in zip(batch, results):
                if "error" not in r:
                    ai_cache[d["series_id"]] = r
                    print(f"  ✓ {d['series_id']:<15} {d['name_zh']}")
                else:
                    print(f"  ✗ {d['series_id']:<15} {r['error']}")
    else:
        print("[2/6] Skipped AI generation\n")

    # ── 3. Run validators (anomaly + consistency, skip freshness in batch) ──
    validation_cache: dict[str, dict] = {}
    if anthropic_key:
        print(f"\n[3/6] Running validators on {len(indicators_data)} indicators…")
        val_results = await asyncio.gather(*[
            validators_mod.validate_indicator(anthropic_key, d, d["observations"], skip_freshness=True)
            for d in indicators_data
        ], return_exceptions=True)
        for d, r in zip(indicators_data, val_results):
            if isinstance(r, dict):
                validation_cache[d["series_id"]] = r
        flagged = [d for d in indicators_data
                   if validation_cache.get(d["series_id"], {}).get("trust_score", 100) < 80]
        print(f"  ✓ {len(validation_cache)} validated · {len(flagged)} flagged (trust<80)")
    else:
        print("\n[3/6] Skipped validators (no ANTHROPIC_API_KEY)")

    # ── 4. Release calendar ──
    print(f"\n[4/6] Fetching FRED release calendar…")
    calendar = await _fetch_calendar(fred_key)
    print(f"  ✓ {len(calendar)} calendar entries")

    # ── 5. Scan for TSMC-negative & build alert ──
    print(f"\n[5/6] Scanning TSMC sentiment…")
    negatives = []
    for d in indicators_data:
        sid = d["series_id"]
        if sid not in ai_cache:
            continue
        tsmc_block = ai_cache[sid].get("impact", {}).get("tsmc", {})
        if tsmc_block.get("sentiment") == "negative":
            negatives.append({
                "series_id": sid,
                "name_zh": d["name_zh"],
                "name_en": d["name_en"],
                "latest_date": d["latest_date"],
                "latest_display": d["latest_display"],
                "delta": d.get("delta"),
                "pct_change": d.get("pct_change"),
                "tsmc_analysis_zh": tsmc_block.get("zh", ""),
                "tsmc_analysis_en": tsmc_block.get("en", ""),
            })
    print(f"  ⚠️  Found {len(negatives)} TSMC-negative indicators")

    # ── 6. Nowcast upcoming releases (predict the next print before publication) ──
    basis_label = "Claude + leading indicators" if (gen_ai and anthropic_key) else "rules fallback"
    print(f"\n[6/6] Nowcasting upcoming releases ({basis_label})…")
    nowcasts: list[dict] = []
    try:
        forecasts = await nowcast_mod.build_nowcasts(
            indicators_data, anthropic_key=anthropic_key, gen_ai=gen_ai
        )
    except Exception as e:
        forecasts = {}
        print(f"  ✗ nowcast engine failed: {e}")
    by_sid = {d["series_id"]: d for d in indicators_data}
    for sid, fc in forecasts.items():
        d = by_sid.get(sid)
        if not d:
            continue
        # next scheduled release date (FRED) → frequency estimate fallback
        try:
            rel = await fred.get_next_release_date(fred_key, sid)
        except Exception:
            rel = None
        if not rel:
            rel = _estimate_next_release(d["frequency"], d["latest_date"])
        # An official anchor (e.g. GDP ← GDPNow) predicts in the ANCHOR's unit
        # (annualized growth %), not the target's level — format with the anchor's
        # format and drop the level "last" so we don't compare % against $B.
        disp_fmt = d["format"]
        anchor_sid = fc.get("anchor_series")
        unit_shift = bool(anchor_sid and anchor_sid in by_sid
                          and by_sid[anchor_sid]["format"] != d["format"])
        if anchor_sid and anchor_sid in by_sid:
            disp_fmt = by_sid[anchor_sid]["format"]
        fc["release_date"] = rel
        fc["display_format"] = disp_fmt
        fc["predicted_display"] = _fmt_value(fc["predicted_value"], disp_fmt)
        fc["low_display"] = _fmt_value(fc["low"], disp_fmt)
        fc["high_display"] = _fmt_value(fc["high"], disp_fmt)
        fc["last_display"] = None if unit_shift else d["latest_display"]
        fc["last_value"] = None if unit_shift else d["latest_value"]
        d["forecast"] = fc
        nowcasts.append({
            "series_id": sid,
            "name_en": d["name_en"], "name_zh": d["name_zh"],
            "category": d["category"], "frequency": d["frequency"],
            "format": fmt, "unit": d["unit"], "unit_zh": d["unit_zh"],
            **fc,
        })
    nowcasts.sort(key=lambda n: (n.get("release_date") or "9999-99-99"))
    n_official = sum(1 for n in nowcasts if n["basis"] == "official")
    n_claude = sum(1 for n in nowcasts if n["basis"] == "claude")
    n_rules = sum(1 for n in nowcasts if n["basis"] == "rules")
    print(f"  ✓ {len(nowcasts)} nowcasts — {n_official} official · {n_claude} claude · {n_rules} rules")

    # ── Save snapshot ──
    snapshot = {
        "date": today,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "indicator_count": len(indicators_data),
        "high_importance_count": sum(1 for d in indicators_data if d["importance"] == "high"),
        "tsmc_negative_count": len(negatives),
        "indicators": [{k: v for k, v in d.items() if k != "observations"} for d in indicators_data],
        "ai_cache": ai_cache,
        "validation": validation_cache,
        "calendar": calendar,
        "nowcasts": nowcasts,
    }
    # also write observation series separately to keep snapshot lean
    snapshot["observations_by_series"] = {d["series_id"]: d["observations"] for d in indicators_data}

    snap_path = SNAPSHOTS_DIR / f"{today}.json"
    snap_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    latest_path = SNAPSHOTS_DIR / "latest.json"
    latest_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    print(f"  ✓ Snapshot saved: {snap_path.name} ({snap_path.stat().st_size//1024} KB)")

    # ── Build alert DRAFT (if any negatives) — written to disk, never sent ──
    alert_summary = {"date": today, "negative_count": len(negatives), "negatives": negatives, "recommendations": None}
    if negatives and anthropic_key:
        print(f"\n  Generating Claude recommendations for {len(negatives)} negative indicators…")
        recs = await _generate_recommendations(anthropic_key, negatives)
        alert_summary["recommendations"] = recs

        html_body = _render_alert_html(today, negatives, recs)
        text_body = _render_alert_text(today, negatives, recs)
        eml_bytes = _build_eml(
            subject=f"⚠️ TSMC Alert · {today} · {len(negatives)} negative indicators",
            html_body=html_body,
            text_body=text_body,
        )
        eml_path = ALERTS_DIR / f"{today}-tsmc-alert.eml"
        eml_path.write_bytes(eml_bytes)
        html_path = ALERTS_DIR / f"{today}-tsmc-alert.html"
        html_path.write_text(html_body, encoding="utf-8")
        print(f"  ✓ .eml draft: {eml_path}")
        print(f"  ✓ HTML preview: {html_path}")

    alert_path = ALERTS_DIR / f"{today}-summary.json"
    alert_path.write_text(json.dumps(alert_summary, ensure_ascii=False, indent=2))
    latest_alert = ALERTS_DIR / "latest-summary.json"
    latest_alert.write_text(json.dumps(alert_summary, ensure_ascii=False, indent=2))

    print(f"\n{'='*60}\n  ✅ Econ weekly refresh complete\n{'='*60}\n")
    return alert_summary


def run_weekly_refresh_sync(gen_ai: bool = True, force: bool = False) -> dict:
    """Sync entrypoint for the Flask scheduler / cron endpoint / admin button.

    Reads API keys from the environment. No-op (with status) when keys are
    missing, a refresh is already running, or today's snapshot already exists.
    """
    global _refresh_running
    fred_key = os.environ.get("FRED_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not fred_key:
        return {"status": "skipped", "reason": "FRED_API_KEY not set"}
    if not _refresh_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "refresh already running"}
    _refresh_running = True
    try:
        return asyncio.run(run_weekly_refresh(anthropic_key, fred_key, gen_ai=gen_ai, force=force))
    finally:
        _refresh_running = False
        _refresh_lock.release()
