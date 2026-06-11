import asyncio
import time
import httpx
from typing import Any

FRED_BASE = "https://api.stlouisfed.org/fred"
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 3600  # 1 hour


def _cached(key: str, ttl: int, fetch_fn):
    now = time.time()
    if key in _cache:
        ts, val = _cache[key]
        if now - ts < ttl:
            return val
    val = fetch_fn()
    _cache[key] = (now, val)
    return val


async def get_series_observations(api_key: str, series_id: str, limit: int = 120) -> list[dict]:
    """Return recent observations [{date, value}, ...] newest-last."""
    cache_key = f"obs:{series_id}:{limit}"

    async def fetch():
        last_err = None
        # Retry on FRED's transient 5xx (often when hammered with parallel calls)
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.get(
                        f"{FRED_BASE}/series/observations",
                        params={
                            "series_id": series_id,
                            "api_key": api_key,
                            "file_type": "json",
                            "sort_order": "desc",
                            "limit": limit,
                        },
                    )
                    if r.status_code >= 500:
                        raise httpx.HTTPStatusError(f"FRED 5xx {r.status_code}", request=r.request, response=r)
                    r.raise_for_status()
                    data = r.json()
                    obs = [
                        {"date": o["date"], "value": float(o["value"])}
                        for o in data.get("observations", [])
                        if o["value"] not in (".", "")
                    ]
                    obs.reverse()
                    return obs
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response is not None and e.response.status_code < 500:
                    raise  # 4xx — don't retry
                await asyncio.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s, 4s
            except (httpx.RequestError, httpx.TimeoutException) as e:
                last_err = e
                await asyncio.sleep(0.5 * (2 ** attempt))
        raise last_err if last_err else RuntimeError(f"FRED fetch failed for {series_id}")

    # run sync wrapper for cache lookup, but we need async — handle directly
    now = time.time()
    if cache_key in _cache:
        ts, val = _cache[cache_key]
        if now - ts < CACHE_TTL:
            return val
    val = await fetch()
    _cache[cache_key] = (now, val)
    return val


async def get_series_info(api_key: str, series_id: str) -> dict:
    cache_key = f"info:{series_id}"
    now = time.time()
    if cache_key in _cache:
        ts, val = _cache[cache_key]
        if now - ts < CACHE_TTL * 24:
            return val

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{FRED_BASE}/series",
            params={"series_id": series_id, "api_key": api_key, "file_type": "json"},
        )
        r.raise_for_status()
        serieses = r.json().get("serieses", [{}])
        val = serieses[0] if serieses else {}

    _cache[cache_key] = (time.time(), val)
    return val


async def get_release_dates(api_key: str, series_id: str, limit: int = 5) -> list[str]:
    """Return upcoming/recent release dates for a series."""
    cache_key = f"releases:{series_id}"
    now = time.time()
    if cache_key in _cache:
        ts, val = _cache[cache_key]
        if now - ts < 3600 * 6:
            return val

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{FRED_BASE}/series/release",
                params={"series_id": series_id, "api_key": api_key, "file_type": "json"},
            )
            r.raise_for_status()
            releases = r.json().get("releases", [])
            if not releases:
                return []
            release_id = releases[0]["id"]

            r2 = await client.get(
                f"{FRED_BASE}/release/dates",
                params={
                    "release_id": release_id,
                    "api_key": api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": limit,
                },
            )
            r2.raise_for_status()
            dates = [rd["date"] for rd in r2.json().get("release_dates", [])]
    except Exception:
        dates = []

    _cache[cache_key] = (time.time(), dates)
    return dates
