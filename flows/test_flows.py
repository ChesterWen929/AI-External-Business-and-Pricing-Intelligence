"""Capital Flow Radar tests — stdlib unittest (no pytest dep, no network).

Run:  python3 -m unittest flows.test_flows -v
"""
import json
import os
import unittest
from pathlib import Path

from flows import _compute, _kb, model, analysis, collectors

KB = _kb()


class TestKnowledgeBase(unittest.TestCase):
    def test_indicators_well_formed(self):
        ids = set()
        res_ids = {r["id"] for r in KB["reservoirs"]}
        for ind in KB["indicators"]:
            for k in ("id", "name_en", "name_zh", "reservoir", "fetch", "rising_means", "seed"):
                self.assertIn(k, ind, f"{ind.get('id')} missing {k}")
            self.assertNotIn(ind["id"], ids, f"duplicate id {ind['id']}")
            ids.add(ind["id"])
            self.assertIn(ind["reservoir"], res_ids, f"{ind['id']} bad reservoir")
            self.assertIn(ind["fetch"]["kind"], ("yfinance", "fred", "defillama"))
            self.assertIn(ind["rising_means"], ("risk_on", "risk_off", "hedge", "rotation"))
            self.assertIn("value", ind["seed"])

    def test_bilingual_parity(self):
        for r in KB["reservoirs"]:
            for base in ("name", "role", "size"):
                self.assertTrue(r.get(f"{base}_en") and r.get(f"{base}_zh"), f"reservoir {r['id']} {base}")
        for c in KB["channels"]:
            self.assertTrue(c.get("trigger_en") and c.get("trigger_zh"))
        for s in KB["scenarios_seed"]:
            self.assertTrue(s.get("name_en") and s.get("name_zh") and s.get("trigger_en") and s.get("trigger_zh"))
        self.assertEqual(len(KB["blind_spots_en"]), len(KB["blind_spots_zh"]))

    def test_proxy_refs_exist(self):
        ids = {i["id"] for i in KB["indicators"]}
        for key in ("retail_proxies", "institution_proxies"):
            for pid in KB[key]:
                self.assertIn(pid, ids, f"{key} → unknown {pid}")


class TestL3Compute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = _compute()  # seed-based; no network, no API key

    def test_snapshot_shape(self):
        for k in ("l3", "l4", "l5", "money_map", "analysis_engine", "as_of", "title_en"):
            self.assertIn(k, self.snap)
        self.assertTrue(self.snap["is_demo"])
        self.assertEqual(self.snap["source"], "seed")
        self.assertEqual(self.snap["analysis_engine"], "rules")  # no API key in CI

    def test_marginal_in_range(self):
        score = self.snap["l3"]["marginal_direction"]["score"]
        self.assertGreaterEqual(score, -100)
        self.assertLessEqual(score, 100)

    def test_reservoir_signals_valid(self):
        for r in self.snap["l3"]["reservoirs"]:
            self.assertIn(r["signal"], ("risk_on", "risk_off", "neutral"))
            for i in r["indicators"]:
                self.assertIn(i["signal"], ("risk_on", "risk_off", "neutral"))

    def test_ai_signal_scale(self):
        s = self.snap["l3"]["ai_signal"]["score"]
        self.assertGreaterEqual(s, 0)
        self.assertLessEqual(s, 100)

    def test_lenses_present(self):
        lz = self.snap["l3"]["lenses"]
        for k in ("liquidity", "price", "positioning"):
            self.assertIn(k, lz)
        self.assertIn("aligned", lz)

    def test_net_liquidity_formula(self):
        # seeds: walcl 6600 - rrp 250 - tga 750 = 5600
        self.assertAlmostEqual(self.snap["l3"]["derived"]["net_liquidity"]["value"], 5600.0, places=1)


class TestAnalysisRules(unittest.TestCase):
    def setUp(self):
        # ensure the rules engine (no live key) regardless of environment
        self._saved = os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._saved

    def test_rules_contract(self):
        snap = _compute()
        out = analysis.analyze(KB, snap["l3"])
        self.assertEqual(out["engine"], "rules")
        l4, l5 = out["l4"], out["l5"]
        for k in ("retail_vs_institution", "ai_mapping", "thesis"):
            self.assertIn(k, l4)
        self.assertIn(l4["thesis"]["confidence"], ("high", "medium", "low"))
        self.assertGreaterEqual(len(l4["thesis"]["leading_signals"]), 3)
        self.assertEqual(sum(s["prob"] for s in l5["scenarios"]), 100)
        self.assertTrue(l5["falsification"] and l5["early_warning"])
        # every text field bilingual & non-empty
        for s in l5["scenarios"]:
            self.assertTrue(s["name_en"] and s["name_zh"])


class TestAuditFixes(unittest.TestCase):
    """Locks in the 2026-06-24 data-audit fixes for /flows."""

    def test_tga_has_millions_to_billions_scale(self):
        # H1: WTREGEN comes from FRED in millions; KB must scale 0.001 → $B,
        # like WALCL, so TGA lands ~700–900 not ~880,000.
        tga = next(i for i in KB["indicators"] if i["id"] == "tga")
        self.assertEqual(tga["fetch"]["series"], "WTREGEN")
        self.assertAlmostEqual(tga["fetch"].get("scale", 1.0), 0.001)

    def test_tga_fix_yields_sane_net_liquidity(self):
        # H1+H2: a millions-scale TGA feeds net liquidity to a sane $B band.
        live = {"metrics": {
            "walcl": {"value": 6736.42, "chg_1w": 0.16, "chg_1m": 0.12, "live": True},
            "rrp":   {"value": 0.25,    "chg_1w": -45.65, "chg_1m": -96.52, "live": True},
            "tga":   {"value": 880713 * 0.001, "chg_1w": 6.35, "chg_1m": 5.02, "live": True},
        }}
        snap = model.build_snapshot(KB, live=live)
        nl = snap["l3"]["derived"]["net_liquidity"]
        self.assertAlmostEqual(nl["value"], 5855.46, places=0)
        self.assertTrue(nl["sane"])

    def test_net_liquidity_sanity_flag_trips_on_unit_bug(self):
        # M19: an un-scaled (millions) TGA explodes net liquidity → flagged unsane.
        live = {"metrics": {
            "walcl": {"value": 6736.42, "chg_1w": 0.0, "chg_1m": 0.1, "live": True},
            "rrp":   {"value": 0.25, "chg_1w": 0.0, "chg_1m": 0.0, "live": True},
            "tga":   {"value": 880713.0, "chg_1w": 0.0, "chg_1m": 5.0, "live": True},
        }}
        snap = model.build_snapshot(KB, live=live)
        self.assertFalse(snap["l3"]["derived"]["net_liquidity"]["sane"])

    def test_rollups_reproduce_from_displayed_values(self):
        # H2: institution/retail roll-ups must be recomputable from the per-
        # indicator chg_1m the dashboard displays (single shared tilt path).
        snap = _compute()
        disp, isl, dirs = {}, {}, {}
        for r in snap["l3"]["reservoirs"]:
            for i in r["indicators"]:
                disp[i["id"]], isl[i["id"]] = i["chg_1m"], i.get("is_level", False)
        for ind in KB["indicators"]:
            dirs[ind["id"]] = ind["rising_means"]

        def recompute(ids):
            ts = [model._tilt(dirs[i], disp[i], is_level=isl[i]) for i in ids]
            return round(sum(ts) / len(ts) * 100, 1)

        rvi = snap["l3"]["retail_vs_inst"]
        self.assertAlmostEqual(recompute(KB["institution_proxies"]), rvi["institution"], places=1)
        self.assertAlmostEqual(recompute(KB["retail_proxies"]), rvi["retail"], places=1)

    def test_tilt_is_magnitude_aware(self):
        # M9: a violent move must out-weigh a microscopic one (not equal sign).
        big = model._tilt("risk_on", 28.0)
        tiny = model._tilt("risk_on", 0.01)
        self.assertGreater(big, tiny)
        self.assertLess(tiny, 0.05)        # near-zero move → near-zero tilt
        self.assertLessEqual(abs(big), 1.0)  # still bounded


class TestCollectorsParsing(unittest.TestCase):
    def test_fred_csv_parse(self):
        sample = "observation_date,WALCL\n2026-05-01,6650000\n2026-06-01,6600000\n"
        orig = collectors._get
        collectors._get = lambda url, timeout=20: sample
        try:
            row = collectors.fetch_fred("WALCL", scale=0.001)
        finally:
            collectors._get = orig
        self.assertEqual(row["value"], 6600.0)  # 6,600,000 * 0.001
        self.assertTrue(row["live"])

    def test_live_overrides_seed(self):
        live = {"metrics": {"spx": {"value": 9999.0, "chg_1w": 5.0, "chg_1m": 10.0, "live": True}}}
        merged = model._merge_metrics(KB, live)
        self.assertEqual(merged["spx"]["value"], 9999.0)
        self.assertTrue(merged["spx"]["live"])
        # an untouched indicator keeps its seed
        self.assertFalse(merged["gold"]["live"])


if __name__ == "__main__":
    unittest.main()
