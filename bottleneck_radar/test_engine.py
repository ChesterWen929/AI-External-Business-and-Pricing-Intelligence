"""Bottleneck-radar engine tests — stdlib unittest (no pytest, no network, no key).

Run:  python3 -m unittest bottleneck.test_engine -v
"""
import unittest

from bottleneck_radar import engine, _kb, _compute

KB = _kb()


class TestKnowledgeBase(unittest.TestCase):
    def test_links_well_formed(self):
        ids = set()
        for ln in KB["links"]:
            for k in ("id", "category", "name_en", "name_zh", "owner_en", "owner_zh",
                      "desc_en", "desc_zh", "native_unit_en", "native_unit_zh",
                      "derivation", "curve"):
                self.assertIn(k, ln, f"{ln.get('id')} missing {k}")
            self.assertNotIn(ln["id"], ids, f"duplicate link {ln['id']}")
            ids.add(ln["id"])

    def test_curves_align_with_quarters(self):
        n = len(KB["quarters"])
        for ln in KB["links"]:
            self.assertEqual(len(ln["curve"]), n,
                             f"{ln['id']} curve length != #quarters")
            for v in ln["curve"]:
                self.assertGreater(v, 0)

    def test_current_quarter_in_quarters(self):
        self.assertIn(KB["_meta"]["current_quarter"], KB["quarters"])

    def test_derivation_ops_valid(self):
        for ln in KB["links"]:
            self.assertEqual(ln["derivation"][0]["op"], "base",
                             f"{ln['id']} first factor must be base")
            for f in ln["derivation"]:
                self.assertIn(f["op"], ("base", "x", "/"))
                for k in ("point", "low", "high"):
                    self.assertIn(k, f["value"])
                self.assertLessEqual(f["value"]["low"], f["value"]["point"])
                self.assertLessEqual(f["value"]["point"], f["value"]["high"])

    def test_bilingual_parity(self):
        for ln in KB["links"]:
            self.assertTrue(ln["name_en"] and ln["name_zh"])
            self.assertTrue(ln["desc_en"] and ln["desc_zh"])
        for sc in KB["scenarios"]:
            self.assertTrue(sc["name_en"] and sc["name_zh"])
            self.assertTrue(sc["desc_en"] and sc["desc_zh"])
        m = KB["_meta"]
        for k in ("title", "subtitle", "unit", "domain_caveat"):
            self.assertTrue(m[f"{k}_en"] and m[f"{k}_zh"])

    def test_scenario_overrides_reference_real_links(self):
        ids = {ln["id"] for ln in KB["links"]}
        for sc in KB["scenarios"]:
            for lid in sc.get("overrides", {}):
                self.assertIn(lid, ids, f"scenario {sc['id']} -> unknown link {lid}")

    def test_evidence_links_valid(self):
        ids = {ln["id"] for ln in KB["links"]}
        for e in KB["evidence"]:
            self.assertIn(e["link"], ids, f"evidence {e['id']} -> unknown link {e['link']}")
            for k in ("id", "date", "tier", "source_en", "source_zh"):
                self.assertIn(k, e)


class TestDerivation(unittest.TestCase):
    def test_fold_band_ordering(self):
        for ln in KB["links"]:
            folded = engine.link_capacity(ln)
            self.assertLessEqual(folded["low"], folded["point"])
            self.assertLessEqual(folded["point"], folded["high"])
            self.assertGreater(folded["low"], 0)

    def test_derivation_matches_curve_at_current_quarter(self):
        # the transparent derivation product must agree with the timeline curve
        # at the current quarter (within rounding/seed tolerance)
        i = engine.quarter_index(KB, KB["_meta"]["current_quarter"])
        for ln in KB["links"]:
            derived = engine.link_capacity(ln)["point"]
            curve = ln["curve"][i]
            self.assertAlmostEqual(derived / curve, 1.0, delta=0.01,
                                   msg=f"{ln['id']}: derivation {derived} vs curve {curve}")

    def test_hbm_math_by_hand(self):
        ln = engine.get_link(KB, "hbm")
        # 440e6 GB * 0.75 / 192 GB = 1,718,750 accelerators/qtr
        derived = engine.link_capacity(ln)["point"]
        self.assertAlmostEqual(derived, round(440000000 * 0.75 / 192), delta=2)

    def test_divide_factor_band_inverts(self):
        # for a '/' factor, the band must widen the right way: dividing by the
        # HIGH end yields the LOW result
        ln = engine.get_link(KB, "power")
        folded = engine.link_capacity(ln)
        base = ln["derivation"][0]["value"]
        div = ln["derivation"][1]["value"]
        self.assertAlmostEqual(folded["low"], round(base["low"] / div["high"]), delta=2)
        self.assertAlmostEqual(folded["high"], round(base["high"] / div["low"]), delta=2)


class TestInferenceAndThesis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = _compute()

    def test_snapshot_shape(self):
        for k in ("inference", "thesis", "timeline", "scenarios", "sensitivity",
                  "as_of", "current_quarter", "reference_accelerator", "links_meta"):
            self.assertIn(k, self.snap)
        self.assertTrue(self.snap["is_demo"])

    def test_deliverable_is_the_minimum(self):
        inf = self.snap["inference"]
        self.assertEqual(inf["deliverable_ea_qtr"], min(inf["capacities"].values()))
        self.assertEqual(inf["deliverable_ea_year"], inf["deliverable_ea_qtr"] * 4)

    def test_binding_is_hbm_this_quarter(self):
        # with the seed numbers, HBM is the 2026Q2 binding constraint
        self.assertEqual(self.snap["inference"]["binding_link"], "hbm")

    def test_thesis_says_tsmc_not_the_bottleneck(self):
        th = self.snap["thesis"]
        self.assertFalse(th["tsmc_is_bottleneck"])
        # TSMC's tightest link sits ABOVE the system bottleneck -> positive headroom
        self.assertGreater(th["tsmc_headroom_pct"], 0)
        self.assertEqual(th["binding_link"], "hbm")
        self.assertTrue(th["verdict_en"] and th["verdict_zh"])

    def test_ranked_ascending_and_binding_first(self):
        ranked = self.snap["inference"]["ranked"]
        caps = [r["capacity"] for r in ranked]
        self.assertEqual(caps, sorted(caps))
        self.assertTrue(ranked[0]["is_binding"])
        self.assertEqual(ranked[0]["headroom_pct"], 0.0)


class TestTimeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tl = _compute()["timeline"]

    def test_every_quarter_has_a_binding_link(self):
        for r in self.tl["rows"]:
            self.assertEqual(r["deliverable"], min(r["caps"].values()))
            self.assertEqual(r["caps"][r["binding"]], r["deliverable"])

    def test_bottleneck_migration_segments(self):
        # the seed tells a 3-regime story: CoWoS -> HBM -> Power
        seq = [s["binding"] for s in self.tl["segments"]]
        self.assertEqual(seq, ["tsmc_cowos", "hbm", "power"])
        # segments must tile the timeline with no gaps
        self.assertEqual(self.tl["segments"][0]["from"], KB["quarters"][0])
        self.assertEqual(self.tl["segments"][-1]["to"], KB["quarters"][-1])

    def test_power_becomes_binding_in_late_2026(self):
        seg = next(s for s in self.tl["segments"] if s["binding"] == "power")
        self.assertEqual(seg["from"], "2026Q4")


class TestScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sc = _compute()["scenarios"]

    def _row(self, sid):
        return next(r for r in self.sc["rows"] if r["id"] == sid)

    def test_baseline_matches_inference(self):
        self.assertEqual(self._row("baseline")["binding"], "hbm")
        self.assertEqual(self._row("baseline")["delta_pct"], 0.0)

    def test_cowos_expansion_alone_does_nothing(self):
        # expanding a NON-binding link must not change deliverable compute
        r = self._row("cowos_expansion")
        self.assertEqual(r["binding"], "hbm")
        self.assertFalse(r["moved"])
        self.assertEqual(r["delta_pct"], 0.0)

    def test_hbm4_ramp_moves_bottleneck_off_memory(self):
        r = self._row("hbm4_ramp")
        self.assertTrue(r["moved"])
        self.assertNotEqual(r["binding"], "hbm")
        self.assertGreater(r["delta_pct"], 0)

    def test_grid_delay_snaps_power_to_binding(self):
        r = self._row("grid_delay")
        self.assertEqual(r["binding"], "power")
        self.assertLess(r["delta_pct"], 0)

    def test_all_ease_exposes_next_constraint(self):
        # easing the top-3 links lifts deliverable but a NEW weakest link appears
        r = self._row("all_ease")
        self.assertGreater(r["delta_pct"], 0)
        self.assertNotIn(r["binding"], ("hbm",))
        self.assertTrue(r["moved"])


class TestSensitivity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sens = _compute()["sensitivity"]

    def test_ranked_desc_by_swing(self):
        swings = [r["swing"] for r in self.sens["rows"]]
        self.assertEqual(swings, sorted(swings, reverse=True))

    def test_binding_link_has_the_largest_swing(self):
        # HBM is binding -> moving it across its band swings deliverable the most
        self.assertEqual(self.sens["rows"][0]["id"], "hbm")
        self.assertGreater(self.sens["rows"][0]["swing"], 0)

    def test_slack_links_barely_move_deliverable(self):
        # a link with lots of headroom (front-end wafer) should swing ~0
        wafer = next(r for r in self.sens["rows"] if r["id"] == "tsmc_wafer")
        self.assertEqual(wafer["swing"], 0)


if __name__ == "__main__":
    unittest.main()
