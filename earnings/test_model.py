"""Supply-Chain Earnings Radar tests — stdlib unittest (no pytest dep, no network).

Run:  python3 -m unittest earnings.test_model -v
  or  python3 -m pytest earnings/ -q

Covers the 2026-06-24 audit fixes:
  H6 — supply-chain signal propagation (propagate_signals + snapshot wiring)
  H7 — non-US tickers Finnhub 403s fall back to seed estimates (no disappearance)
  M17 — non-US timing prefers the curated KB earnings_timing over Finnhub's hour
"""
from __future__ import annotations

import unittest
from datetime import date

from earnings import model

AS_OF = date(2026, 6, 25)


def _build(source="seed"):
    return model.build_snapshot(source=source, today=AS_OF)


class _StubFinnhub(model.FinnhubAdapter):
    """FinnhubAdapter with the network call stubbed: native (.suffix) symbols 403,
    US symbols return a row stamped `amc` (wrong clock for non-US ADRs like TSM)."""

    def __init__(self):
        self.api_key = "stub"
        self.max_retries = 1
        self.misses = []

    def _get(self, params):
        sym = params["symbol"]
        if "." in sym:  # native non-US listing — Finnhub free tier 403s
            raise model.FinnhubAccessError("403 Forbidden")
        return {"earningsCalendar": [
            {"date": "2026-07-16", "hour": "amc", "quarter": 2, "year": 2026}
        ]}


class TestSeedBaseline(unittest.TestCase):
    def test_every_active_company_appears(self):
        snap = _build()
        active = snap["universe"]["active"]
        # one earliest event per active company in seed mode
        self.assertEqual(snap["coverage"]["companies_with_events"], active)
        self.assertTrue(snap["estimated_data"])
        self.assertEqual(snap["coverage"]["no_data_in_window"], 0)

    def test_no_negative_coverage(self):
        cov = _build()["coverage"]
        self.assertGreaterEqual(cov["no_data_in_window"], 0)


class TestH7SeedFallback(unittest.TestCase):
    """Non-US tickers Finnhub 403s must stay on the calendar as estimates."""

    def setUp(self):
        self.adapter = _StubFinnhub()
        companies = model.load_companies()
        self.active = [c for c in companies if c.get("active", True)]
        self.raw = self.adapter.fetch(self.active, AS_OF, AS_OF.replace(month=9))

    def test_403_recorded_as_miss(self):
        # 25 native non-US listings (TW13/JP8/KR2/EU2) per the KB
        self.assertEqual(len(self.adapter.misses), 25)

    def test_403_companies_get_seed_fallback_event(self):
        missed_ids = {m["company_id"] for m in self.adapter.misses}
        fallback_ids = {r["company_id"] for r in self.raw if r["estimated"]}
        # every 403'd company still produced an (estimated) event — nothing vanished
        self.assertTrue(missed_ids.issubset(fallback_ids))
        self.assertEqual(len(fallback_ids), 25)

    def test_asia_supply_chain_not_absent(self):
        by_id = {c["id"]: c for c in self.active}
        regions = {by_id[r["company_id"]]["region"] for r in self.raw}
        for region in ("TW", "JP", "KR", "EU"):
            self.assertIn(region, regions, f"{region} disappeared from calendar")

    def test_fallback_events_flagged_estimated(self):
        for r in self.raw:
            if r["company_id"] in {m["company_id"] for m in self.adapter.misses}:
                self.assertTrue(r["estimated"], f"{r['company_id']} not flagged estimated")
                self.assertEqual(r["source"], "finnhub")


class TestM17NonUsTiming(unittest.TestCase):
    """Non-US ADRs: KB earnings_timing wins when it conflicts with Finnhub's hour."""

    def setUp(self):
        self.adapter = _StubFinnhub()
        companies = model.load_companies()
        self.active = [c for c in companies if c.get("active", True)]
        self.by_id = {c["id"]: c for c in self.active}
        self.raw = self.adapter.fetch(self.active, AS_OF, AS_OF.replace(month=9))

    def test_tsmc_uses_kb_timing_not_finnhub_amc(self):
        tsmc = next(r for r in self.raw if r["company_id"] == "tsmc")
        # Finnhub said amc; KB says bmo (Taipei morning) → KB wins
        self.assertEqual(tsmc["earnings_timing"], "bmo")

    def test_tsmc_displays_morning_not_0130(self):
        tsmc = next(r for r in self.raw if r["company_id"] == "tsmc")
        _, local = model._to_times(tsmc, self.by_id["tsmc"]["exchange_tz"])
        # bmo at Taipei 08:00 → PT previous day 17:00, NOT the broken 01:30
        self.assertNotIn("T01:30", local)
        self.assertIn("17:00", local)

    def test_us_company_keeps_finnhub_amc(self):
        # NVIDIA is US (no KB override) → Finnhub's amc is preserved
        nv = next(r for r in self.raw if r["company_id"] == "nvidia")
        self.assertEqual(nv["earnings_timing"], "amc")

    def test_is_us_exchange_helper(self):
        self.assertTrue(model._is_us_exchange("America/New_York"))
        self.assertFalse(model._is_us_exchange("Asia/Taipei"))
        self.assertFalse(model._is_us_exchange(None))


class TestH6SignalPropagation(unittest.TestCase):
    def setUp(self):
        self.snap = _build()
        self.signals = self.snap["signals"]
        self.by_id = {c["id"]: c for c in model.load_companies()}

    def test_signals_present_in_snapshot(self):
        self.assertGreater(len(self.signals), 0)
        self.assertEqual(self.snap["graph_meta"]["signal_count"], len(self.signals))
        self.assertEqual(self.snap["graph_meta"]["edges"], 132)

    def test_signal_shape(self):
        s = self.signals[0]
        for k in ("source_id", "source_name", "target_id", "target_name",
                  "relation", "strength", "intensity", "reason_en", "reason_zh"):
            self.assertIn(k, s)
        self.assertIn(s["relation"], ("supplies", "competes_with"))
        self.assertIn(s["intensity"], ("high", "medium", "low"))

    def test_high_intensity_requires_t1_and_high_edge(self):
        for s in self.signals:
            if s["intensity"] == "high":
                self.assertEqual(s["source_tier"], "T1")
                self.assertEqual(s["strength"], "high")

    def test_sources_are_known_companies(self):
        ids = set(self.by_id)
        for s in self.signals:
            self.assertIn(s["source_id"], ids)
            self.assertIn(s["target_id"], ids)
            self.assertNotEqual(s["source_id"], s["target_id"])

    def test_sorted_high_first(self):
        intensities = [s["intensity"] for s in self.signals]
        if "high" in intensities and "medium" in intensities:
            self.assertLess(intensities.index("high"), len(intensities) - intensities[::-1].index("medium"))

    def test_no_signals_when_no_events(self):
        out = model.propagate_signals([], self.by_id, model.load_graph(), 90)
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
