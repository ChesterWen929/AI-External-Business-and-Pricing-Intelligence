import json
import re
import anthropic

_client: anthropic.AsyncAnthropic | None = None


def get_client(api_key: str) -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


async def generate_commentary(
    api_key: str,
    name_en: str,
    name_zh: str,
    desc_zh: str,
    current_value: float,
    prev_value: float | None,
    unit: str,
    observations: list[dict],  # [{date, value}, ...]
) -> dict:
    """Return {zh: str, en: str} AI commentary on the latest reading."""
    client = get_client(api_key)

    # Build compact history summary (last 12 readings)
    recent = observations[-12:] if len(observations) >= 12 else observations
    history_lines = "\n".join(f"  {o['date']}: {o['value']}" for o in recent)

    change_str = ""
    if prev_value is not None:
        delta = current_value - prev_value
        pct = (delta / abs(prev_value) * 100) if prev_value != 0 else 0
        change_str = f"Change from prior period: {delta:+.3g} ({pct:+.1f}%)"

    prompt = f"""You are an expert US macroeconomist writing a brief data commentary for a bilingual (Traditional Chinese / English) economic dashboard.

Indicator: {name_en} / {name_zh}
Background: {desc_zh}
Unit: {unit}
Latest value: {current_value}
{change_str}

Recent history (oldest to newest):
{history_lines}

Write a concise commentary in BOTH languages. Output JSON exactly like this (no markdown fences):
{{
  "zh": "150字以內的繁體中文評論，分析最新數據的意涵、與近期趨勢的關係，以及對貨幣政策或市場的可能影響。",
  "en": "Same commentary in English, under 120 words. Analyze the latest reading in context of recent trend and implications for Fed policy or markets."
}}

Be specific—cite the numbers. Do not repeat the indicator name as the first word. No markdown, just the JSON object."""

    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    import json, re
    text = msg.content[0].text.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        return {"zh": text, "en": text}


async def generate_industry_impact(
    api_key: str,
    name_en: str,
    name_zh: str,
    desc_zh: str,
    current_value: float,
    prev_value: float | None,
    unit: str,
    observations: list[dict],
) -> dict:
    """Return impact analysis on 4 industries: AI / Mobile / Auto / TSMC.
    Output schema: {ai: {zh, en, sentiment}, mobile: {...}, auto: {...}, tsmc: {...}}
    sentiment ∈ {positive, negative, neutral, mixed}
    """
    client = get_client(api_key)

    recent = observations[-12:] if len(observations) >= 12 else observations
    history_lines = "\n".join(f"  {o['date']}: {o['value']}" for o in recent)

    change_str = ""
    if prev_value is not None:
        delta = current_value - prev_value
        pct = (delta / abs(prev_value) * 100) if prev_value != 0 else 0
        change_str = f"Change from prior period: {delta:+.3g} ({pct:+.1f}%)"

    prompt = f"""You are an industry analyst translating a US macro indicator into industry-specific impact assessments for a bilingual (Traditional Chinese / English) dashboard.

Indicator: {name_en} / {name_zh}
Background: {desc_zh}
Unit: {unit}
Latest value: {current_value}
{change_str}

Recent history (oldest to newest):
{history_lines}

Analyze how this indicator's latest reading and recent trend impacts FOUR specific areas:
1. AI industry (data centers, GPU demand, AI capex, hyperscalers like NVIDIA/Microsoft/Google)
2. Mobile phone industry (smartphone demand, Apple/Samsung shipments, consumer electronics)
3. Automotive industry (auto sales, EV adoption, supply chain, financing)
4. TSMC specifically (wafer demand, advanced node utilization, capex outlook, customer mix)

Output ONLY a JSON object (no markdown fences) with this exact schema:
{{
  "ai":     {{"sentiment": "positive|negative|neutral|mixed", "zh": "70-100字繁中分析", "en": "60-90 word English analysis"}},
  "mobile": {{"sentiment": "...", "zh": "...", "en": "..."}},
  "auto":   {{"sentiment": "...", "zh": "...", "en": "..."}},
  "tsmc":   {{"sentiment": "...", "zh": "...", "en": "..."}}
}}

Be specific—reference the actual number, the direction of change, and concrete industry mechanisms (e.g. "rising rates → higher auto loan costs → soft pickup demand → less MCU content"). Avoid generic platitudes."""

    msg = await client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Extract first balanced JSON object (in case of trailing/preceding text)
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        text = text[first:last + 1]
    try:
        return json.loads(text)
    except Exception:
        return {
            "ai":     {"sentiment": "neutral", "zh": text, "en": text},
            "mobile": {"sentiment": "neutral", "zh": "", "en": ""},
            "auto":   {"sentiment": "neutral", "zh": "", "en": ""},
            "tsmc":   {"sentiment": "neutral", "zh": "", "en": ""},
        }
