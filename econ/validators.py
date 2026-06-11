"""Validation sub-agents for indicator data quality.

Three sub-agents run in parallel via asyncio:
1. FreshnessAgent  — uses Claude with web_search_20250305 to verify FRED has the latest published value
2. AnomalyAgent    — uses Claude reasoning to flag statistically unusual readings vs. history
3. ConsistencyAgent — checks units/format/sign sanity against indicator definition

Returns a unified validation report per indicator.
"""
from __future__ import annotations
import statistics
import json
import re
from typing import Any
import anthropic

# Shared client
_client: anthropic.AsyncAnthropic | None = None

def _get_client(api_key: str) -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


def _strip_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        text = text[first:last + 1]
    return text


# ─── Agent 1: Freshness (web search) ─────────────────────────────────────────
async def check_freshness(
    api_key: str,
    series_id: str,
    name_en: str,
    fred_latest_date: str,
    fred_latest_value: float,
) -> dict:
    """Use web_search to verify FRED's latest reading matches the official source."""
    client = _get_client(api_key)

    prompt = f"""You are a data verification agent. Verify whether FRED's reported latest value for indicator "{name_en}" (FRED series: {series_id}) is current and matches the official release.

FRED reports: date={fred_latest_date}, value={fred_latest_value}

Search the web for the most recent published value of this indicator from its official source (BLS for CPI/payrolls, BEA for GDP/PCE, Census for retail/housing, ISM for PMI, Treasury for yields, etc).

Output STRICT JSON only:
{{
  "is_fresh": true/false,
  "official_source": "<source name>",
  "official_latest_date": "YYYY-MM-DD or 'unknown'",
  "official_latest_value": <number or null>,
  "verdict": "MATCH" | "STALE" | "MISMATCH" | "UNCLEAR",
  "note": "<one short sentence>"
}}"""

    try:
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
            messages=[{"role": "user", "content": prompt}],
        )
        # Find the text block
        text = ""
        for block in msg.content:
            if hasattr(block, "text"):
                text = block.text
                break
        return json.loads(_strip_json(text))
    except Exception as e:
        return {"is_fresh": None, "verdict": "ERROR", "note": f"Agent error: {e}"}


# ─── Agent 2: Anomaly (statistical + Claude reasoning) ───────────────────────
async def check_anomaly(
    api_key: str,
    series_id: str,
    name_en: str,
    observations: list[dict],
) -> dict:
    """Detect outliers via z-score + Claude judgment on whether anomaly is real or data error."""
    if len(observations) < 12:
        return {"is_anomaly": False, "z_score": None, "note": "Insufficient history"}

    values = [o["value"] for o in observations[-60:]]
    latest = values[-1]
    history = values[:-1]
    mean = statistics.mean(history)
    stdev = statistics.stdev(history) if len(history) > 1 else 1.0
    z = (latest - mean) / stdev if stdev else 0.0

    if abs(z) < 3.0:
        # Not statistically anomalous — fast path
        return {
            "is_anomaly": False,
            "z_score": round(z, 2),
            "mean": round(mean, 3),
            "stdev": round(stdev, 3),
            "verdict": "NORMAL",
            "note": "Within 3σ of historical mean",
        }

    # Statistically anomalous — ask Claude to judge if it's real or a data glitch
    client = _get_client(api_key)
    recent_str = "\n".join(f"  {o['date']}: {o['value']}" for o in observations[-15:])
    prompt = f"""Indicator {name_en} ({series_id}) latest value is {latest} (z-score {z:+.2f} vs trailing mean).

Recent series (oldest→newest):
{recent_str}

Is this a real economic move (e.g., policy shift, shock, structural break) or likely a data quality issue (revision, glitch, FRED transformation error)? Answer in STRICT JSON:
{{"is_anomaly": true/false, "verdict": "REAL_MOVE" | "DATA_GLITCH" | "REGIME_SHIFT" | "UNCLEAR", "note": "<one sentence>"}}"""

    try:
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(_strip_json(msg.content[0].text))
        result["z_score"] = round(z, 2)
        result["mean"] = round(mean, 3)
        result["stdev"] = round(stdev, 3)
        return result
    except Exception as e:
        return {"is_anomaly": True, "z_score": round(z, 2), "verdict": "ERROR", "note": f"Agent error: {e}"}


# ─── Agent 3: Consistency (units/sign/range sanity) ──────────────────────────
async def check_consistency(
    series_id: str,
    name_en: str,
    indicator_format: str,
    indicator_unit: str,
    latest_value: float,
) -> dict:
    """Local sanity rules — no Claude needed for fast checks."""
    issues = []

    # Percent values: most are -50% to 110% (labor participation, capacity util can be 60-90%)
    if indicator_format == "percent" and not (-50 < latest_value < 110):
        issues.append(f"Percent value {latest_value} outside plausible [-50, 110]")

    # Negative values for typically-positive series
    POSITIVE_ONLY = {"PAYEMS", "ICSA", "JTSJOL", "HOUST", "PERMIT", "RSXFS", "PCE", "GDPC1", "M2SL", "CPIAUCSL", "CPILFESL", "PCEPILFE", "PPIACO", "DCOILWTICO"}
    if series_id in POSITIVE_ONLY and latest_value < 0:
        issues.append(f"Series {series_id} should be positive, got {latest_value}")

    # Index values should generally be > 0
    if indicator_format == "index" and latest_value <= 0:
        issues.append(f"Index value {latest_value} non-positive")

    return {
        "is_consistent": len(issues) == 0,
        "verdict": "PASS" if not issues else "FAIL",
        "issues": issues,
        "note": "All consistency checks passed" if not issues else "; ".join(issues),
    }


# ─── Coordinator ──────────────────────────────────────────────────────────────
async def validate_indicator(
    api_key: str,
    indicator: dict,
    observations: list[dict],
    skip_freshness: bool = False,
) -> dict:
    """Run all three agents in parallel and return a unified report.

    Set skip_freshness=True when running in batch (to save API calls / web-search quota).
    """
    import asyncio
    latest = observations[-1]
    tasks = [
        check_anomaly(api_key, indicator["series_id"], indicator["name_en"], observations),
        check_consistency(indicator["series_id"], indicator["name_en"],
                         indicator["format"], indicator["unit"], latest["value"]),
    ]
    if not skip_freshness:
        tasks.insert(0, check_freshness(api_key, indicator["series_id"], indicator["name_en"],
                                         latest["date"], latest["value"]))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    if skip_freshness:
        anomaly, consistency = results
        freshness = {"verdict": "SKIPPED", "note": "Not checked in batch mode"}
    else:
        freshness, anomaly, consistency = results

    # Compute overall trust score (0-100)
    score = 100
    if isinstance(freshness, dict) and freshness.get("verdict") in {"STALE", "MISMATCH"}:
        score -= 40
    if isinstance(anomaly, dict) and anomaly.get("verdict") == "DATA_GLITCH":
        score -= 30
    if isinstance(consistency, dict) and consistency.get("verdict") == "FAIL":
        score -= 30

    return {
        "series_id": indicator["series_id"],
        "trust_score": max(0, score),
        "freshness": freshness if isinstance(freshness, dict) else {"verdict": "ERROR", "note": str(freshness)},
        "anomaly": anomaly if isinstance(anomaly, dict) else {"verdict": "ERROR", "note": str(anomaly)},
        "consistency": consistency if isinstance(consistency, dict) else {"verdict": "ERROR", "note": str(consistency)},
    }
