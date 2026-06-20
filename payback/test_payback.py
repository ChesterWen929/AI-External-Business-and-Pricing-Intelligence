"""AI Capex Payback Radar tests — stdlib unittest (no pytest dep, no network).

Run:  python3 -m unittest payback.test_payback -v
"""
import os
import unittest

from payback import _compute, _kb, model, analysis

KB = _kb()


class TestKnowledgeBase(unittest.TestCase):
    def test_companies_well_formed(self):
        ids = set()
        for c in KB["companies"]:
            for k in ("id", "kind", "name_en", "name_zh", "seed"):
                self.assertIn(k, c, f"{c.get('id')} missing {k}")
            self.assertNotIn(c["id"], ids, f"duplicate id {c['id']}")
            ids.add(c["id"])
            self.assertIn(c["kind"], ("public", "private"))
            if c["kind"] == "public":
                for k in ("ticker", "ai_capex_share", "cloud", "ai_rev_band", "fetch", "series"):
                    self.assertIn(k, c, f"public {c['id']} missing {k}")
                self.assertIn("value", c["ai_capex_share"])
                self.assertIn("rev_yoy_pct", c["cloud"])
                self.assertLessEqual(c["ai_rev_band"]["low_usd_bn"], c["ai_rev_band"]["high_usd_bn"])
            else:
                for k in ("revenue_runrate_usd_bn", "annual_burn_usd_bn", "funding_raised_usd_bn"):
                    self.assertIn(k, c["seed"], f"private {c['id']} missing seed.{k}")

    def test_has_four_public_two_private(self):
        kinds = [c["kind"] for c in KB["companies"]]
        self.assertEqual(kinds.count("public"), 4)
        self.assertEqual(kinds.count("private"), 2)
        pub_ids = {c["id"] for c in KB["companies"] if c["kind"] == "public"}
        self.assertEqual(pub_ids, {"googl", "meta", "msft", "amzn"})

    def test_series_match_quarter_count(self):
        nq = len(KB["quarters"])
        for c in KB["companies"]:
            if c["kind"] == "public":
                self.assertEqual(len(c["series"]), nq, f"{c['id']} series length")

    def test_bilingual_parity(self):
        for s in KB["scenarios_seed"]:
            self.assertTrue(s["name_en"] and s["name_zh"] and s["trigger_en"] and s["trigger_zh"])
        for x in KB["falsification_seed"]:
            self.assertTrue(x["en"] and x["zh"])
        for w in KB["watch_seed"]:
            self.assertTrue(w["en"] and w["zh"] and w["freq"])
        self.assertEqual(len(KB["blind_spots_en"]), len(KB["blind_spots_zh"]))


class TestScoring(unittest.TestCase):
    def test_public_score_bounds_and_direction(self):
        # higher coverage & cloud growth → higher score; heavy intensity/accel → lower
        hi = model.public_score(coverage=0.6, cloud_growth=35, intensity=15, capex_yoy=20, rev_yoy=15)
        lo = model.public_score(coverage=0.1, cloud_growth=5, intensity=40, capex_yoy=80, rev_yoy=10)
        self.assertGreater(hi, lo)
        for v in (hi, lo):
            self.assertGreaterEqual(v, 0)
            self.assertLessEqual(v, 100)

    def test_private_score_bounds_and_direction(self):
        hi = model.private_score(coverage=1.0, rev_growth=200)
        lo = model.private_score(coverage=0.2, rev_growth=50)
        self.assertGreater(hi, lo)
        for v in (hi, lo):
            self.assertGreaterEqual(v, 0)
            self.assertLessEqual(v, 100)

    def test_verdict_thresholds(self):
        self.assertEqual(model._verdict_key(70), "monetizing")
        self.assertEqual(model._verdict_key(50), "investing")
        self.assertEqual(model._verdict_key(30), "burning")


class TestL3Compute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = _compute()  # seed-based; no network, no API key

    def test_snapshot_shape(self):
        for k in ("l3", "l4", "l5", "headline", "analysis_engine", "as_of", "title_en"):
            self.assertIn(k, self.snap)
        self.assertTrue(self.snap["is_demo"])
        self.assertEqual(self.snap["source"], "seed")
        self.assertEqual(self.snap["analysis_engine"], "rules")  # no API key in CI

    def test_companies_computed(self):
        publics = self.snap["l3"]["companies"]
        self.assertEqual(len(publics), 4)
        for p in publics:
            self.assertGreaterEqual(p["score"], 0)
            self.assertLessEqual(p["score"], 100)
            self.assertIn(p["verdict_key"], ("monetizing", "investing", "burning"))
            self.assertIsNotNone(p["coverage"])
            self.assertFalse(p["live"])  # seed render

    def test_score_formula_wired(self):
        # each public score must equal the documented formula on its own inputs
        for p in self.snap["l3"]["companies"]:
            expect = model.public_score(p["coverage"], p["cloud_growth"], p["capex_intensity"],
                                        p["capex_yoy"], p["rev_yoy"])
            self.assertAlmostEqual(p["score"], expect, places=1, msg=p["id"])
            self.assertEqual(p["verdict_key"], model._verdict_key(p["score"]))

    def test_coverage_definition(self):
        # coverage = AI revenue mid ÷ AI capex
        for p in self.snap["l3"]["companies"]:
            if p["ai_capex_ttm"]:
                self.assertAlmostEqual(p["coverage"], round(p["ai_rev_mid"] / p["ai_capex_ttm"], 3), places=3)
        # ai_capex = total capex × share
        for c, p in zip([c for c in KB["companies"] if c["kind"] == "public"], self.snap["l3"]["companies"]):
            share = c["ai_capex_share"]["value"] / 100.0
            self.assertAlmostEqual(p["ai_capex_ttm"], round(p["capex_ttm"] * share, 1), places=1)

    def test_private_computed(self):
        privs = self.snap["l3"]["private"]
        self.assertEqual(len(privs), 2)
        for p in privs:
            self.assertIn(p["verdict_key"], ("monetizing", "investing", "burning"))
            self.assertIsNotNone(p["coverage"])
            self.assertIsNotNone(p["runway_years"])

    def test_aggregate_and_headline(self):
        agg = self.snap["l3"]["aggregate"]
        for k in ("total_capex_ttm", "total_ai_capex_ttm", "total_cloud_rev_ttm",
                  "total_ai_rev_mid", "ai_coverage", "gap_usd_bn"):
            self.assertIn(k, agg)
        self.assertAlmostEqual(agg["gap_usd_bn"], round(agg["total_ai_capex_ttm"] - agg["total_ai_rev_mid"], 1), places=1)
        self.assertIn(self.snap["headline"]["verdict_key"], ("monetizing", "investing", "burning"))

    def test_scissors_series(self):
        sc = self.snap["l3"]["scissors"]
        self.assertEqual(len(sc), len(KB["quarters"]))
        # cumulative monotonic non-decreasing
        for i in range(1, len(sc)):
            self.assertGreaterEqual(sc[i]["cum_capex"], sc[i - 1]["cum_capex"])
            self.assertGreaterEqual(sc[i]["cum_cloud_rev"], sc[i - 1]["cum_cloud_rev"])

    def test_circularity(self):
        circ = self.snap["l3"]["circularity"]
        self.assertEqual(circ["count"], len(KB["circularity_edges"]))
        self.assertGreater(circ["total_usd_bn"], 0)

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
        for k in ("payback_read", "circularity_read", "company_takes", "leading_signals"):
            self.assertIn(k, l4)
        self.assertTrue(l4["payback_read"]["en"] and l4["payback_read"]["zh"])
        # one take per company (4 public + 2 private)
        self.assertEqual(len(l4["company_takes"]), 6)
        take_ids = {t["id"] for t in l4["company_takes"]}
        self.assertEqual(take_ids, {c["id"] for c in KB["companies"]})
        self.assertGreaterEqual(len(l4["leading_signals"]), 3)
        self.assertEqual(sum(s["prob"] for s in l5["scenarios"]), 100)
        self.assertTrue(l5["falsification"] and l5["watch"])


class _FakeSeries:
    def __init__(self, vals):
        self._vals = vals

    def tolist(self):
        return list(self._vals)


class _FakeLoc:
    def __init__(self, rows):
        self._rows = rows

    def __getitem__(self, label):
        return _FakeSeries(self._rows[label])


class _FakeDF:
    """Minimal stand-in for the pandas DataFrame interface collectors._row_values
    uses (.empty / .index / .loc[label].tolist()) — keeps the test hermetic and
    independent of the local pandas/bottleneck install."""
    def __init__(self, rows):
        self._rows = rows
        self.index = list(rows.keys())
        self.empty = not rows
        self.loc = _FakeLoc(rows)


class TestCollectorsParsing(unittest.TestCase):
    def test_row_values_and_ttm(self):
        from payback import collectors

        # newest-first values, like yfinance quarterly statements (capex reported negative)
        df = _FakeDF({
            "Capital Expenditure": [-25e9, -23e9, -22e9, -20e9, -18e9, -16e9, -15e9, -14e9],
            "Total Revenue": [70e9, 69e9, 68e9, 67e9, 66e9, 65e9, 64e9, 63e9],
        })
        capex = collectors._row_values(df, ["Capital Expenditure"])
        self.assertEqual(capex[0], -25e9)
        capex_abs = [abs(v) for v in capex]
        ttm, prev = collectors._ttm_pair(capex_abs)
        self.assertAlmostEqual(ttm, 25e9 + 23e9 + 22e9 + 20e9)
        self.assertAlmostEqual(prev, 18e9 + 16e9 + 15e9 + 14e9)

    def test_pct_helper(self):
        from payback import collectors
        self.assertEqual(collectors._pct(110, 100), 10.0)
        self.assertIsNone(collectors._pct(100, 0))
        self.assertIsNone(collectors._pct(None, 100))

    def test_live_overrides_seed(self):
        live = {"metrics": {"msft": {
            "capex_ttm_usd_bn": 200.0, "revenue_ttm_usd_bn": 300.0,
            "capex_ttm_prev_usd_bn": 100.0, "revenue_ttm_prev_usd_bn": 250.0,
            "stock": 999.0, "stock_chg_1m": 5.0, "as_of_q": "2026-03-31", "live": True,
        }}}
        snap = model.build_snapshot(KB, live=live, generated_at="t", today="2026-06-20")
        msft = next(p for p in snap["l3"]["companies"] if p["id"] == "msft")
        self.assertEqual(msft["capex_ttm"], 200.0)
        self.assertTrue(msft["live"])
        # a company with no live metric keeps its seed
        googl = next(p for p in snap["l3"]["companies"] if p["id"] == "googl")
        self.assertFalse(googl["live"])
        self.assertEqual(snap["source"], "live")


if __name__ == "__main__":
    unittest.main()
