"""Live context layer (yfinance) — best-effort, never required.

The chain is driven by the FORWARD CapEx figures in knowledge_base.json (guidance),
not by trailing actuals. yfinance TTM capex is pulled only as live CONTEXT next to
the seeded CapEx input — a print moves the context, the modeler decides whether to
revise the dated CapEx assumption. Any network failure silently yields {} so the
dashboard always renders from seeds.
"""
from __future__ import annotations

CAPEX_TICKERS = {
    "MSFT": "Microsoft", "GOOGL": "Alphabet", "AMZN": "Amazon",
    "META": "Meta", "ORCL": "Oracle",
}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _is_num(v):
    try:
        float(v)
        return v == v
    except (TypeError, ValueError):
        return False


def _ttm_capex(t):
    q_cf = _safe(lambda: t.quarterly_cashflow)
    if q_cf is None or getattr(q_cf, "empty", True):
        return None
    for name in ("Capital Expenditure", "CapitalExpenditures", "Capital Expenditures"):
        if name in q_cf.index:
            vals = [float(v) for v in list(q_cf.loc[name].values)[:4] if _is_num(v)]
            if vals:
                return abs(sum(vals))
    return None


def fetch_capex_context(log=None):
    """Sum of trailing-twelve-month capex across the major hyperscalers, $bn."""
    import yfinance as yf

    rows = {}
    total = 0.0
    for tk, name in CAPEX_TICKERS.items():
        cap = _safe(lambda: _ttm_capex(yf.Ticker(tk)))
        if _is_num(cap):
            bn = round(cap / 1e9, 1)
            rows[tk] = {"name": name, "capex_ttm_usd_bn": bn}
            total += bn
            if log:
                log(f"  {tk}: capex_ttm={bn}")
        elif log:
            log(f"  {tk}: capex fetch failed")
    return {"by_company": rows, "total_ttm_usd_bn": round(total, 1) if rows else None,
            "n": len(rows)}


def fetch_bundle(kb, log=None):
    return {"capex_context": fetch_capex_context(log=log)}
