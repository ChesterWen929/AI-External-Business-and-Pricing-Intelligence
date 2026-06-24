"""Offline unit tests for the nowcast engine (no network / no Claude).

Run: python3 -m unittest econ.test_nowcast
Covers the rules model math, official-anchor priority, schema completeness,
the <3-observation skip, and graceful handling of missing leading series.
"""
import asyncio
import unittest

from . import nowcast


def _ind(series_id, values, name_en="X", fmt="index", freq="Monthly"):
    """Build a minimal indicator dict shaped like refresh_job's indicators_data."""
    obs = [{"date": f"2026-{(i % 12) + 1:02d}-01", "value": float(v)} for i, v in enumerate(values)]
    return {
        "series_id": series_id,
        "name_en": name_en,
        "name_zh": name_en,
        "unit": "u",
        "unit_zh": "u",
        "format": fmt,
        "frequency": freq,
        "latest_value": float(values[-1]),
        "latest_display": str(values[-1]),
        "changes": {},
        "observations": obs,
    }


def _run(indicators, **kw):
    return asyncio.run(nowcast.build_nowcasts(indicators, **kw))


class TestRulesModel(unittest.TestCase):
    def test_drift_and_zero_sigma(self):
        # steady +1/period → drift +1, σ 0 → predicted = last+1, band collapses
        target = _ind("UNRATE", [100, 101, 102, 103, 104, 105, 106], fmt="percent")
        out = _run([target], anthropic_key="", gen_ai=False)
        self.assertIn("UNRATE", out)
        fc = out["UNRATE"]
        self.assertEqual(fc["basis"], "rules")
        self.assertAlmostEqual(fc["predicted_value"], 107.0, places=6)
        self.assertAlmostEqual(fc["low"], fc["high"], places=6)  # σ == 0
        self.assertEqual(fc["confidence"], "high")

    def test_sigma_widens_band(self):
        target = _ind("UNRATE", [10, 12, 11, 14, 9, 15, 12], fmt="percent")
        fc = _run([target], anthropic_key="", gen_ai=False)["UNRATE"]
        self.assertLess(fc["low"], fc["predicted_value"])
        self.assertGreater(fc["high"], fc["predicted_value"])


class TestOfficialAnchor(unittest.TestCase):
    def test_anchor_takes_priority(self):
        # GDPC1 has anchor GDPNOW → forecast must come straight from GDPNow, not rules
        gdp = _ind("GDPC1", [22000, 22100, 22200], fmt="billions", freq="Quarterly")
        gdpnow = _ind("GDPNOW", [2.0, 2.2, 2.4], name_en="GDPNow", fmt="percent", freq="Quarterly")
        out = _run([gdp, gdpnow], anthropic_key="", gen_ai=False)
        fc = out["GDPC1"]
        self.assertEqual(fc["basis"], "official")
        self.assertEqual(fc["anchor_series"], "GDPNOW")
        self.assertAlmostEqual(fc["predicted_value"], 2.4, places=6)
        self.assertEqual(fc["confidence"], "high")
        self.assertLess(fc["low"], fc["high"])


class TestSchemaAndEdges(unittest.TestCase):
    REQUIRED = {"predicted_value", "low", "high", "basis", "confidence", "drivers", "drivers_zh"}

    def test_schema_complete(self):
        fc = _run([_ind("CPIAUCSL", [300, 301, 302, 303])], anthropic_key="", gen_ai=False)["CPIAUCSL"]
        self.assertTrue(self.REQUIRED.issubset(fc.keys()))
        self.assertIsInstance(fc["drivers"], list)
        self.assertIsInstance(fc["drivers_zh"], list)

    def test_missing_leading_is_graceful(self):
        # CPIAUCSL's leading series absent + no Claude → rules path, no crash
        out = _run([_ind("CPIAUCSL", [300, 301, 302, 303, 304])], anthropic_key="", gen_ai=False)
        self.assertEqual(out["CPIAUCSL"]["basis"], "rules")

    def test_too_few_observations_skipped(self):
        out = _run([_ind("CPIAUCSL", [300, 301])], anthropic_key="", gen_ai=False)
        self.assertNotIn("CPIAUCSL", out)

    def test_untracked_series_ignored(self):
        out = _run([_ind("NOT_A_TARGET", [1, 2, 3, 4])], anthropic_key="", gen_ai=False)
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
