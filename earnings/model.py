"""Supply-Chain Earnings Radar — self-contained calendar model.

Ported from the earnings-watch project (Phase 0/1). Reads the bundled company
knowledge base (73 AI-supply-chain companies + supply-chain graph) and builds a
rolling earnings calendar snapshot:

  - FinnhubAdapter (live) when FINNHUB_API_KEY is set; else SeedAdapter (offline
    estimated dates so the card renders before a key is wired in).
  - Non-US tickers that Finnhub's free tier rejects (403) are recorded as
    coverage.misses and skipped — one bad symbol never aborts the run.
  - Times stored UTC, displayed America/Los_Angeles; bmo/amc/intraday → clock.
"""
from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PKG = Path(__file__).resolve().parent
COMPANIES_JSON = PKG / "companies.json"
GRAPH_JSON = PKG / "supply_chain_graph.json"
DISPLAY_TZ = ZoneInfo("America/Los_Angeles")

TIMING_HOUR = {"bmo": (8, 0), "amc": (16, 30), "intraday": (14, 0), "unknown": (12, 0)}


def load_companies() -> list[dict]:
    return json.loads(COMPANIES_JSON.read_text(encoding="utf-8"))["companies"]


def load_graph() -> dict:
    try:
        return json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {"nodes": [], "edges": []}


def _quarter_for(d: date) -> str:
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


# ───────────────────────── adapters ─────────────────────────

class CalendarAdapter(ABC):
    name = "base"
    misses: list[dict] = []

    @abstractmethod
    def fetch(self, companies: list[dict], start: date, end: date) -> list[dict]:
        ...


class SeedAdapter(CalendarAdapter):
    name = "seed"

    def fetch(self, companies, start, end):
        self.misses = []
        span = max((end - start).days, 1)
        out = []
        for c in companies:
            if not c.get("active", True):
                continue
            ticker = c.get("primary_ticker") or (c.get("tickers") or [None])[0]
            if not ticker:
                continue
            d = start + timedelta(days=sum(ord(ch) for ch in c["id"]) % span)
            out.append({
                "company_id": c["id"], "ticker": ticker, "date": d.isoformat(),
                "fiscal_quarter": _quarter_for(d),
                "earnings_timing": c.get("earnings_timing", "unknown"),
                "source": self.name, "estimated": True,
            })
        return out


class FinnhubAccessError(Exception):
    """Non-retryable per-symbol error (4xx other than 429)."""


class FinnhubAdapter(CalendarAdapter):
    name = "finnhub"
    BASE = "https://finnhub.io/api/v1/calendar/earnings"
    NON_RETRYABLE = {400, 401, 402, 403, 404}

    def __init__(self, api_key: str | None = None, max_retries: int = 4):
        self.api_key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        self.max_retries = max_retries
        self.misses = []
        if not self.api_key:
            raise ValueError("FINNHUB_API_KEY not set")

    def _get(self, params):
        import requests
        backoff, last = 1.0, None
        for attempt in range(self.max_retries):
            try:
                r = requests.get(self.BASE, params=params, timeout=20)
                if r.status_code in self.NON_RETRYABLE:
                    raise FinnhubAccessError(f"{r.status_code} {r.reason}")
                if r.status_code == 429:
                    raise RuntimeError("rate limited (429)")
                r.raise_for_status()
                return r.json()
            except FinnhubAccessError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.max_retries - 1:
                    time.sleep(backoff)
                    backoff *= 2
        raise RuntimeError(f"Finnhub failed after retries: {last}")

    def fetch(self, companies, start, end):
        self.misses = []
        out = []
        for c in companies:
            if not c.get("active", True):
                continue
            ticker = c.get("primary_ticker") or (c.get("tickers") or [None])[0]
            if not ticker:
                continue
            try:
                data = self._get({"from": start.isoformat(), "to": end.isoformat(),
                                  "symbol": ticker, "token": self.api_key})
            except Exception as exc:  # noqa: BLE001
                self.misses.append({"company_id": c["id"], "ticker": ticker, "reason": str(exc)})
                continue
            for row in (data or {}).get("earningsCalendar", []) or []:
                d = row.get("date")
                if not d:
                    continue
                hour = (row.get("hour") or "").lower()
                timing = {"bmo": "bmo", "amc": "amc", "dmh": "intraday"}.get(hour, c.get("earnings_timing", "unknown"))
                qy = row.get("quarter"), row.get("year")
                fq = f"Q{qy[0]} {qy[1]}" if all(qy) else _quarter_for(date.fromisoformat(d))
                out.append({
                    "company_id": c["id"], "ticker": ticker, "date": d,
                    "fiscal_quarter": fq, "earnings_timing": timing,
                    "source": self.name, "estimated": False,
                })
        return out


def get_adapter(source: str | None = None) -> CalendarAdapter:
    source = (source or os.environ.get("EARNINGS_SOURCE", "")).lower()
    if source == "seed":
        return SeedAdapter()
    if source == "finnhub" or os.environ.get("FINNHUB_API_KEY"):
        try:
            return FinnhubAdapter()
        except ValueError:
            return SeedAdapter()
    return SeedAdapter()


# ───────────────────────── snapshot builder ─────────────────────────

def _to_times(ev: dict, exchange_tz: str) -> tuple[str, str]:
    d = date.fromisoformat(ev["date"])
    hh, mm = TIMING_HOUR.get(ev.get("earnings_timing", "unknown"), TIMING_HOUR["unknown"])
    try:
        tz = ZoneInfo(exchange_tz)
    except Exception:
        tz = timezone.utc
    utc = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)
    return utc.isoformat().replace("+00:00", "Z"), utc.astimezone(DISPLAY_TZ).isoformat()


def build_snapshot(horizon_days: int | None = None, source: str | None = None,
                   today: date | None = None) -> dict:
    horizon_days = horizon_days or int(os.environ.get("EARNINGS_HORIZON_DAYS", "90"))
    companies = load_companies()
    by_id = {c["id"]: c for c in companies}
    active = [c for c in companies if c.get("active", True)]

    start = today or datetime.now(timezone.utc).date()
    end = start + timedelta(days=horizon_days)

    adapter = get_adapter(source)
    raw = adapter.fetch(active, start, end)
    misses = getattr(adapter, "misses", [])

    events = []
    for ev in raw:
        c = by_id.get(ev["company_id"], {})
        utc_iso, local_iso = _to_times(ev, c.get("exchange_tz", "UTC"))
        events.append({
            "company_id": ev["company_id"], "ticker": ev["ticker"],
            "fiscal_quarter": ev.get("fiscal_quarter"),
            "datetime_utc": utc_iso, "datetime_local": local_iso,
            "earnings_timing": ev.get("earnings_timing", "unknown"),
            "processing_tier": c.get("processing_tier", "T3"),
            "region": c.get("region"), "source": ev.get("source"),
            "estimated": ev.get("estimated", False),
        })
    events.sort(key=lambda e: e["datetime_utc"])

    # slim company map for the dashboard (display fields only)
    company_map = {
        c["id"]: {
            "short_name": c.get("short_name") or c.get("name"),
            "roles": c.get("roles", []), "region": c.get("region"),
            "processing_tier": c.get("processing_tier"),
            "supply_chain_tier": c.get("supply_chain_tier"),
        }
        for c in companies
    }

    queried = sum(1 for c in active if (c.get("primary_ticker") or c.get("tickers")))
    with_events = len({e["company_id"] for e in events})
    by_tier = {"T1": 0, "T2": 0, "T3": 0}
    for e in events:
        by_tier[e["processing_tier"]] = by_tier.get(e["processing_tier"], 0) + 1

    return {
        "as_of": start.isoformat(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "horizon_days": horizon_days,
        "display_tz": "America/Los_Angeles",
        "source": adapter.name,
        "estimated_data": any(e["estimated"] for e in events),
        "event_count": len(events),
        "by_tier": by_tier,
        "coverage": {
            "queried": queried, "companies_with_events": with_events,
            "no_data_in_window": queried - with_events - len(misses),
            "access_misses": len(misses), "misses": misses,
        },
        "events": events,
        "companies": company_map,
        "universe": {"total": len(companies), "active": len(active)},
    }
