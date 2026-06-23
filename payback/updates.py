"""AI Capex Payback Radar — Module: Update pipeline (news → proposed assumption
deltas → human approval).

The hard layer (yfinance TTM capex/revenue/stock) refreshes automatically — those
are reported facts. The CURATED ESTIMATE layer (AI-capex share, AI-only revenue
band, cloud figures, GPU useful life / impairment assumptions, and the private-lab
numbers) must NOT move on its own. So this module only *proposes* changes from the
news since the last watermark; nothing is written to the knowledge base until the
user approves specific deltas (the same draft-before-send rule the project follows).

Two proposer engines:
  • Claude — Opus 4.8, structured output, proposes a value + rationale + which
             source justifies it. Used when ANTHROPIC_API_KEY is set.
  • rules  — deterministic: matches each headline to an entity + a topic and raises
             a "review this assumption" flag (no invented number). Always available.

propose(kb, news, since) → [delta, ...]        (held as pending, never applied here)
apply(kb, approved_ids, pending) → (kb, [applied, ...])   (mutates a kb copy)
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("payback.updates")

MODEL = "claude-opus-4-8"

# Which curated fields may be proposed for change, and where each lives inside a
# company dict. Hard live fields (capex/revenue/stock) are deliberately NOT here —
# they come from yfinance and need no human gate.
_PATHS = {
    "ai_capex_share": (["ai_capex_share", "value"], "%", "AI capex share", "AI capex 佔比"),
    "ai_rev_low":     (["ai_rev_band", "low_usd_bn"], "$bn", "AI-only revenue (low)", "AI-only 營收(低)"),
    "ai_rev_high":    (["ai_rev_band", "high_usd_bn"], "$bn", "AI-only revenue (high)", "AI-only 營收(高)"),
    "cloud_rev":      (["cloud", "rev_ttm_usd_bn"], "$bn", "Cloud segment revenue", "雲端分部營收"),
    "cloud_yoy":      (["cloud", "rev_yoy_pct"], "%", "Cloud growth", "雲端成長"),
    "useful_life":    (["dep", "useful_life_years"], "yrs", "GPU useful life", "GPU 耐用年限"),
    "at_risk_pct":    (["dep", "at_risk_pct_of_ppe"], "frac", "At-risk % of PP&E", "風險佔 PP&E %"),
    "impairment_pct": (["dep", "impairment_pct"], "frac", "H100 impairment %", "H100 減損 %"),
    "priv_revenue":   (["seed", "revenue_runrate_usd_bn", "value"], "$bn", "Revenue run-rate", "營收 run-rate"),
    "priv_burn":      (["seed", "annual_burn_usd_bn", "value"], "$bn", "Annual burn", "年燒錢"),
    "priv_funding":   (["seed", "funding_raised_usd_bn", "value"], "$bn", "Funding raised", "累計募資"),
    "priv_valuation": (["seed", "valuation_usd_bn", "value"], "$bn", "Valuation", "估值"),
}
PUBLIC_KEYS = ["ai_capex_share", "ai_rev_low", "ai_rev_high", "cloud_rev", "cloud_yoy",
               "useful_life", "at_risk_pct", "impairment_pct"]
PRIVATE_KEYS = ["priv_revenue", "priv_burn", "priv_funding", "priv_valuation"]

# news topic → which field key(s) it bears on
_TOPIC_KEYS = {
    "capex": (["ai_capex_share"], ("capex", "capital expenditure", "data center", "datacenter",
                                   "資本支出", "data centre")),
    "useful_life": (["useful_life"], ("useful life", "depreciation", "useful-life", "折舊", "耐用年限")),
    "impairment": (["impairment_pct", "at_risk_pct"], ("impairment", "write-down", "write down",
                                                       "writedown", "減損", "stranded", "obsolete")),
    "cloud": (["cloud_rev", "cloud_yoy"], ("cloud revenue", "cloud growth", "backlog", "azure",
                                           "aws revenue", "google cloud", "雲端")),
    "ai_rev": (["ai_rev_low", "ai_rev_high"], ("ai revenue", "gemini revenue", "copilot revenue",
                                               "ai run-rate", "ai run rate")),
    "private": (["priv_revenue", "priv_burn", "priv_funding", "priv_valuation"],
                ("funding", "raise", "valuation", "revenue", "burn", "losses", "round", "募資", "估值")),
}


# --------------------------------------------------------------------------- #
# Nested get/set + field registry
# --------------------------------------------------------------------------- #
def _get(d, path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _set(d, path, value):
    for p in path[:-1]:
        nxt = d.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            d[p] = nxt
        d = nxt
    d[path[-1]] = value


def _company_by_id(kb, cid):
    return next((c for c in kb.get("companies", []) if c.get("id") == cid), None)


def updatable_fields(kb):
    """Flat list of every field the update loop is allowed to touch, with current value."""
    out = []
    for c in kb.get("companies", []):
        keys = PUBLIC_KEYS if c.get("kind") == "public" else PRIVATE_KEYS
        for key in keys:
            path, unit, label_en, label_zh = _PATHS[key]
            out.append({
                "field_id": f"{c['id']}.{key}",
                "entity": c["id"],
                "entity_name_en": c["name_en"], "entity_name_zh": c["name_zh"],
                "key": key, "unit": unit,
                "field_label_en": label_en, "field_label_zh": label_zh,
                "current": _get(c, path),
            })
    return out


def set_field(kb, field_id, value):
    cid, _, key = field_id.partition(".")
    if key not in _PATHS:
        raise KeyError(f"unknown field key: {key}")
    c = _company_by_id(kb, cid)
    if c is None:
        raise KeyError(f"unknown company: {cid}")
    _set(c, _PATHS[key][0], value)
    return True


# --------------------------------------------------------------------------- #
# Entity detection in a headline
# --------------------------------------------------------------------------- #
_ALIASES = {
    "amzn": ("amazon", "aws"), "googl": ("alphabet", "google"), "meta": ("meta", "facebook"),
    "msft": ("microsoft", "azure"), "openai": ("openai", "chatgpt"), "anthropic": ("anthropic", "claude"),
}


def _entities_in(title, kb):
    t = (title or "").lower()
    ids = []
    for c in kb.get("companies", []):
        cid = c["id"]
        names = (cid,) + _ALIASES.get(cid, ())
        if any(n in t for n in names):
            ids.append(cid)
    return ids


def _since_ok(item_date, since):
    if not since or not item_date:
        return True
    return item_date >= since[:10]


# --------------------------------------------------------------------------- #
# Rules proposer — review flags, no invented numbers
# --------------------------------------------------------------------------- #
def _delta(field, direction, proposed, src, engine, rat_en, rat_zh, conf="low"):
    return {
        "field_id": field["field_id"], "entity": field["entity"],
        "entity_name_en": field["entity_name_en"], "entity_name_zh": field["entity_name_zh"],
        "field_label_en": field["field_label_en"], "field_label_zh": field["field_label_zh"],
        "unit": field["unit"], "current": field["current"], "proposed": proposed,
        "direction": direction, "confidence": conf, "engine": engine,
        "rationale_en": rat_en, "rationale_zh": rat_zh,
        "source_title": (src or {}).get("title"), "source_url": (src or {}).get("url"),
        "source_date": (src or {}).get("date"),
    }


def _rules_propose(kb, news, since=None, limit=10):
    by_id = {f["field_id"]: f for f in updatable_fields(kb)}
    seen, out = set(), []
    for item in news:
        if not _since_ok(item.get("date"), since):
            continue
        title = item.get("title", "")
        ents = _entities_in(title, kb)
        if not ents:
            continue
        tl = title.lower()
        for topic, (keys, kws) in _TOPIC_KEYS.items():
            if not any(k in tl for k in kws):
                continue
            for cid in ents:
                is_priv = topic == "private"
                ent_is_priv = (_company_by_id(kb, cid) or {}).get("kind") == "private"
                if is_priv != ent_is_priv:
                    continue  # private topic only for private entities and vice-versa
                for key in keys:
                    fid = f"{cid}.{key}"
                    if fid in seen or fid not in by_id:
                        continue
                    seen.add(fid)
                    f = by_id[fid]
                    out.append(_delta(
                        f, "review", None, item, "rules",
                        f"Headline may bear on this assumption — review and set manually. (\"{title[:80]}\")",
                        f"此頭條可能影響此假設 — 請人工檢視並設定。(\"{title[:60]}\")"))
        if len(out) >= limit:
            break
    return out[:limit]


# --------------------------------------------------------------------------- #
# Claude proposer — structured value proposals
# --------------------------------------------------------------------------- #
def _claude_propose(kb, news, since=None):
    import anthropic

    fields = updatable_fields(kb)
    allowed = [f["field_id"] for f in fields]
    recent = [n for n in news if _since_ok(n.get("date"), since)][:30]
    if not recent:
        return []

    field_lines = "\n".join(
        f"  {f['field_id']} = {f['current']} {f['unit']} ({f['entity_name_en']} · {f['field_label_en']})"
        for f in fields)
    news_lines = "\n".join(f"  [{i}] {n.get('date','')} {n.get('title','')} ({n.get('source','')})"
                           for i, n in enumerate(recent))

    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"deltas": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "field_id": {"type": "string", "enum": allowed},
                "proposed": {"type": ["number", "null"]},
                "direction": {"type": "string", "enum": ["up", "down", "review"]},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "source_index": {"type": "integer"},
                "rationale_en": {"type": "string"}, "rationale_zh": {"type": "string"},
            },
            "required": ["field_id", "proposed", "direction", "confidence",
                         "source_index", "rationale_en", "rationale_zh"],
        }}},
        "required": ["deltas"],
    }
    system = (
        "You maintain the ESTIMATE layer of an AI-capex payback model. From the news below, "
        "propose changes ONLY to the listed curated assumptions, and ONLY where a specific "
        "headline justifies it. Never invent precision: if the news is qualitative, set "
        "proposed=null and direction='review'. Keep numbers within a plausible step of the "
        "current value. Each delta must cite the source_index that justifies it. Output ≤8 "
        "deltas, highest-signal first. Traditional Chinese for *_zh. Output only the object."
    )
    user = (f"Updatable assumptions (field_id = current unit):\n{field_lines}\n\n"
            f"News since last update:\n{news_lines}\n\nPropose deltas.")
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL, max_tokens=4000, thinking={"type": "adaptive"},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": schema}, "effort": "medium"},
        messages=[{"role": "user", "content": user}],
    )
    import json as _json
    text = next((b.text for b in msg.content if b.type == "text"), "")
    raw = _json.loads(text).get("deltas", [])

    by_id = {f["field_id"]: f for f in fields}
    out = []
    for d in raw:
        f = by_id.get(d["field_id"])
        if not f:
            continue
        src = recent[d["source_index"]] if 0 <= d.get("source_index", -1) < len(recent) else {}
        out.append(_delta(f, d["direction"], d["proposed"], src, "claude",
                          d["rationale_en"], d["rationale_zh"], d.get("confidence", "low")))
    return out


def propose(kb, news, since=None):
    """News → proposed deltas (NOT applied). Claude when keyed, else rules."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude_propose(kb, news, since)
        except Exception:
            log.exception("updates: Claude proposer failed — falling back to rules")
    return _rules_propose(kb, news, since)


# --------------------------------------------------------------------------- #
# Apply (only approved deltas with a concrete proposed value)
# --------------------------------------------------------------------------- #
def apply(kb, approved_ids, pending):
    """Mutate kb in place for each approved delta that carries a numeric proposed
    value. Returns (kb, applied list). Review-only flags (proposed=None) are skipped."""
    by_id = {d["field_id"]: d for d in pending}
    applied = []
    for fid in approved_ids:
        d = by_id.get(fid)
        if not d or d.get("proposed") is None:
            continue
        old = d.get("current")
        set_field(kb, fid, d["proposed"])
        applied.append({"field_id": fid, "from": old, "to": d["proposed"],
                        "entity": d["entity"], "field_label_en": d["field_label_en"],
                        "source_title": d.get("source_title"), "source_url": d.get("source_url"),
                        "engine": d.get("engine")})
    return kb, applied
