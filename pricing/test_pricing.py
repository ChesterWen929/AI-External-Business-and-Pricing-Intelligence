"""Pricing Power Radar tests — stdlib unittest (no pytest dep, no network).

Run:  python3 -m unittest pricing.test_pricing -v
"""
import os
import unittest

from pricing import _compute, _kb, model, analysis, collectors

KB = _kb()


class TestKnowledgeBase(unittest.TestCase):
    def test_items_well_formed(self):
        ids = set()
        layer_ids = {ly["id"] for ly in KB["layers"]}
        for it in KB["items"]:
            for k in ("id", "layer", "name_en", "name_zh", "metric", "direction",
                      "weight", "tier", "is_estimate", "seed"):
                self.assertIn(k, it, f"{it.get('id')} missing {k}")
            self.assertNotIn(it["id"], ids, f"duplicate id {it['id']}")
            ids.add(it["id"])
            self.assertIn(it["layer"], layer_ids, f"{it['id']} bad layer")
            self.assertIn(it["metric"], ("proxy", "price"))
            self.assertIn(it["direction"], ("cost", "revenue", "demand"))
            self.assertIn(it["tier"], ("T1", "T2", "T3"))
            self.assertIn("value", it["seed"])
            if "fetch" in it:
                self.assertIn(it["fetch"]["kind"], ("yfinance", "fred"))
            # proxies are live-fetchable (not estimates); price rows are estimates
            if it["metric"] == "proxy":
                self.assertFalse(it["is_estimate"], f"{it['id']} proxy should not be estimate")
                self.assertIn("fetch", it, f"{it['id']} proxy needs a fetch spec")

    def test_three_layers(self):
        self.assertEqual({ly["id"] for ly in KB["layers"]}, {"up", "fab", "down"})

    def test_bilingual_parity(self):
        for ly in KB["layers"]:
            for base in ("name", "role"):
                self.assertTrue(ly.get(f"{base}_en") and ly.get(f"{base}_zh"), f"layer {ly['id']} {base}")
        for s in KB["scenarios_seed"]:
            self.assertTrue(s["name_en"] and s["name_zh"] and s["trigger_en"] and s["trigger_zh"])
        for key in ("falsification_seed",):
            for x in KB[key]:
                self.assertTrue(x["en"] and x["zh"])
        for w in KB["watch_seed"]:
            self.assertTrue(w["en"] and w["zh"] and w["freq"])
        self.assertEqual(len(KB["blind_spots_en"]), len(KB["blind_spots_zh"]))

    def test_each_layer_has_momentum_items(self):
        # every layer must have at least one weight>0 item, or momentum is meaningless
        for lid in ("up", "fab", "down"):
            w = sum(float(it.get("weight", 0)) for it in KB["items"] if it["layer"] == lid)
            self.assertGreater(w, 0, f"layer {lid} has no weighted items")


class TestL3Compute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = _compute()  # seed-based; no network, no API key

    def test_snapshot_shape(self):
        for k in ("l3", "l4", "l5", "pricing_power", "analysis_engine", "as_of", "title_en"):
            self.assertIn(k, self.snap)
        self.assertTrue(self.snap["is_demo"])
        self.assertEqual(self.snap["source"], "seed")
        self.assertEqual(self.snap["analysis_engine"], "rules")  # no API key in CI

    def test_pricing_power_range(self):
        pp = self.snap["pricing_power"]
        self.assertGreaterEqual(pp["score"], 0)
        self.assertLessEqual(pp["score"], 100)
        self.assertIn(pp["verdict_key"], ("defensible", "neutral", "squeezed"))

    def test_verdict_matches_score(self):
        pp = self.snap["pricing_power"]
        s, k = pp["score"], pp["verdict_key"]
        expect = "defensible" if s >= 60 else "squeezed" if s <= 40 else "neutral"
        self.assertEqual(k, expect)

    def test_score_formula_wired(self):
        # score must equal the documented formula applied to the stack momenta
        st = self.snap["l3"]["stack"]
        u, f, d = st["upstream"], st["foundry"], st["downstream"]
        expect = round(max(0, min(100, 50 + 6 * (f - u) + 3 * d)), 1)
        self.assertAlmostEqual(self.snap["pricing_power"]["score"], expect, places=1)

    def test_layers_and_signals(self):
        layers = self.snap["l3"]["layers"]
        self.assertEqual([ly["id"] for ly in layers], ["up", "fab", "down"])  # ordered
        for ly in layers:
            self.assertIn(ly["signal"], ("on", "off", "neutral"))
            self.assertIsInstance(ly["momentum"], (int, float))
            for it in ly["items"]:
                self.assertIn(it["signal"], ("on", "off", "neutral"))

    def test_competitor_rows_excluded_from_momentum(self):
        # samsung_n3 / intel_18a carry weight 0 → present but not in the fab aggregate
        fab = next(ly for ly in self.snap["l3"]["layers"] if ly["id"] == "fab")
        ids = {it["id"] for it in fab["items"]}
        self.assertIn("samsung_n3", ids)
        weighted = [it for it in fab["items"] if it["weight"] > 0]
        self.assertTrue(all(it["id"] != "samsung_n3" for it in weighted))

    def test_transmission_and_margin(self):
        l3 = self.snap["l3"]
        self.assertIn("up_to_fab", l3["transmission"])
        self.assertIn("fab_to_down", l3["transmission"])
        self.assertIn("fab_delta", l3["margin"])
        self.assertIn("chain_delta", l3["margin"])

    def test_alerts_present_and_valid(self):
        alerts = self.snap["l3"]["alerts"]
        self.assertTrue(alerts)
        for a in alerts:
            self.assertIn(a["level"], ("squeeze", "opportunity", "strong", "watch"))
            self.assertTrue(a["en"] and a["zh"])


class TestAnalysisRules(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._saved

    def test_rules_contract(self):
        snap = _compute()
        out = analysis.analyze(KB, snap["l3"])
        self.assertEqual(out["engine"], "rules")
        l4, l5 = out["l4"], out["l5"]
        for k in ("pricing_power_read", "transmission_read", "layers", "leading_signals"):
            self.assertIn(k, l4)
        for lk in ("upstream", "foundry", "downstream"):
            self.assertTrue(l4["layers"][lk]["en"] and l4["layers"][lk]["zh"])
        self.assertGreaterEqual(len(l4["leading_signals"]), 3)
        self.assertEqual(sum(s["prob"] for s in l5["scenarios"]), 100)
        self.assertTrue(l5["falsification"] and l5["watch"])


class TestCollectorsParsing(unittest.TestCase):
    def test_fred_csv_parse(self):
        sample = "observation_date,PCU334413334413\n2026-04-01,104\n2026-05-01,105\n2026-06-01,106\n"
        orig = collectors._get
        collectors._get = lambda url, timeout=20: sample
        try:
            row = collectors.fetch_fred("PCU334413334413")
        finally:
            collectors._get = orig
        self.assertEqual(row["value"], 106.0)
        self.assertTrue(row["live"])

    def test_live_overrides_seed(self):
        live = {"metrics": {"tsm_proxy": {"value": 999.0, "chg_1w": 1.0, "chg_1m": 9.0, "live": True}}}
        merged = model._merge_items(KB, live)
        self.assertEqual(merged["tsm_proxy"]["value"], 999.0)
        self.assertTrue(merged["tsm_proxy"]["live"])
        # a curated price estimate keeps its seed
        self.assertFalse(merged["n3_asp"]["live"])


if __name__ == "__main__":
    unittest.main()
