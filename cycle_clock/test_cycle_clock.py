"""Cycle Analogue Clock — offline unit tests (no network, no API key, no Claude).

Run:  cd macro-ai-monitor && python3 -m pytest cycle_clock/ -q
      (stdlib-unittest style, pytest-compatible)
"""
import json
import os
import unittest
from pathlib import Path

os.environ.pop("ANTHROPIC_API_KEY", None)  # force the rules path everywhere

from cycle_clock import _compute, _kb, analysis, model

KB = _kb()
PAIR_IDS = [p["id"] for p in KB["pairs"]]
SERIES_IDS = {s["id"] for s in KB["series"]}
SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "analogue" / "snapshot.json"

TOY_POINTS = [{"q": f"{y}Q{q}", "v": float((y - 1995) * 4 + q)}
              for y in range(1995, 2003) for q in range(1, 5)]  # 1..32 ramp


class TestKnowledgeBase(unittest.TestCase):
    def test_has_seven_pairs_with_bilingual_mapping(self):
        self.assertEqual(len(KB["pairs"]), 7)
        for p in KB["pairs"]:
            for k in ("name_en", "name_zh", "why_en", "why_zh", "breaks_en", "breaks_zh",
                      "counterpart_en", "counterpart_zh"):
                self.assertTrue(p.get(k), f"{p['id']} missing {k}")

    def test_every_seed_has_tier_asof_source(self):
        for p in KB["pairs"]:
            seed = p["today_seed"]
            self.assertIn(seed["tier"], ("T1", "T2", "T3"), p["id"])
            self.assertTrue(seed.get("as_of"), p["id"])
            self.assertTrue(seed.get("source_en") and seed.get("source_zh"), p["id"])
            self.assertIn("est", seed, p["id"])

    def test_series_full_quarterly_1995_2002_with_tier_and_source(self):
        for s in KB["series"]:
            self.assertEqual(len(s["points"]), 32, s["id"])
            self.assertEqual(s["points"][0]["q"], "1995Q1", s["id"])
            self.assertEqual(s["points"][-1]["q"], "2002Q4", s["id"])
            self.assertIn(s["tier"], ("T2", "T3"), s["id"])
            self.assertTrue(s.get("source_en") and s.get("source_zh"), s["id"])

    def test_pairs_reference_existing_series_and_positive_weights(self):
        for p in KB["pairs"]:
            self.assertIn(p["series_id"], SERIES_IDS, p["id"])
            self.assertGreater(p["weight"], 0, p["id"])

    def test_canonical_shared_numbers(self):
        """Audit rule: aligned copies must match the canonical platform numbers."""
        by_id = {p["id"]: p for p in KB["pairs"]}
        self.assertAlmostEqual(by_id["hy_oas"]["today_seed"]["value"], 2.66)
        # loss_labs 5.9x == (122+95)/(25+12) from the payback KB v2 canon
        self.assertAlmostEqual((122 + 95) / (25 + 12), 5.86, places=2)
        self.assertAlmostEqual(by_id["loss_labs"]["today_seed"]["value"], 5.9)
        src = by_id["loss_labs"]["today_seed"]["source_en"]
        for canon in ("$122B", "$95B", "$25B", "$12B", "/payback"):
            self.assertIn(canon, src)

    def test_disclaimer_and_blind_spots_bilingual(self):
        self.assertIn("非投資建議", KB["disclaimer_zh"])
        self.assertIn("not investment advice", KB["disclaimer_en"])
        self.assertGreaterEqual(len(KB["blind_spots_en"]), 4)
        self.assertEqual(len(KB["blind_spots_en"]), len(KB["blind_spots_zh"]))


class TestEngine(unittest.TestCase):
    def test_exact_point_match_reads_that_quarter(self):
        # today == the 1999Q1 level & slope of the toy ramp → reading 1999Q1, high conf
        idx = 16  # 1999Q1
        level = TOY_POINTS[idx]["v"]
        slope = level - TOY_POINTS[idx - 4]["v"]
        r = model.place_on_curve(TOY_POINTS, level, slope)
        self.assertEqual(r["quarter"], "1999Q1")
        self.assertAlmostEqual(r["distance"], 0.0, places=6)
        self.assertGreaterEqual(r["confidence"], 0.99)
        self.assertFalse(r["beyond_range"])

    def test_beyond_range_flag_and_low_confidence(self):
        r = model.place_on_curve(TOY_POINTS, 500.0, 4.0)
        self.assertTrue(r["beyond_range"])
        self.assertLess(r["confidence"], 0.5)
        self.assertEqual(r["quarter"], "2002Q4")  # clamps to the nearest = max point

    def test_level_only_when_slope_missing(self):
        r = model.place_on_curve(TOY_POINTS, TOY_POINTS[8]["v"], None)
        self.assertEqual(r["quarter"], "1997Q1")

    def test_weighted_median_hand_check(self):
        self.assertEqual(model.weighted_median([1997.0, 1999.0, 2000.0], [0.8, 0.4, 0.1]), 1997.0)
        self.assertEqual(model.weighted_median([1997.0, 1999.0, 2000.0], [0.2, 0.4, 0.1]), 1999.0)

    def test_dispersion_zero_when_readings_agree(self):
        self.assertEqual(model.weighted_mad([1999.0, 1999.0], [1.0, 2.0], 1999.0), 0.0)
        self.assertEqual(model.dispersion_label(0.0)["key"], "coherent")
        self.assertEqual(model.dispersion_label(1.0)["key"], "mixed")
        self.assertEqual(model.dispersion_label(2.0)["key"], "structural")

    def test_verdict_mapping(self):
        self.assertEqual(model.verdict_for(1996.0)["key"], "early_cycle")
        self.assertEqual(model.verdict_for(1997.5)["key"], "mid_cycle")
        self.assertEqual(model.verdict_for(1999.0)["key"], "late_cycle")
        self.assertEqual(model.verdict_for(1999.75)["key"], "peak_zone")
        self.assertEqual(model.verdict_for(2001.0)["key"], "post_peak")

    def test_quarter_year_roundtrip(self):
        self.assertEqual(model.quarter_to_year("1999Q3"), 1999.5)
        self.assertEqual(model.year_to_quarter(1999.5), "1999Q3")
        self.assertEqual(model.year_to_quarter(2000.0), "2000Q1")


class TestSnapshot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = _compute()  # seed compute, rules engine

    def test_seed_mode_flags(self):
        self.assertEqual(self.snap["source"], "seed")
        self.assertTrue(self.snap["is_demo"])
        self.assertEqual(self.snap["analysis_engine"], "rules")

    def test_composite_bounds_and_labels(self):
        c = self.snap["l3"]["composite"]
        self.assertGreaterEqual(c["clock"], 1995.0)
        self.assertLessEqual(c["clock"], 2002.75)
        self.assertGreaterEqual(c["plus_minus"], 0.0)
        self.assertGreaterEqual(c["score"], 0.0)
        self.assertLessEqual(c["score"], 100.0)
        self.assertIn(c["verdict_key"], ("early_cycle", "mid_cycle", "late_cycle", "peak_zone", "post_peak"))
        self.assertIn(c["dispersion_key"], ("coherent", "mixed", "structural"))
        self.assertTrue(c["answer_en"] and c["answer_zh"])
        self.assertEqual(c["n_pairs"], 7)

    def test_every_pair_scored_with_confidence_bounds(self):
        for p in self.snap["l3"]["pairs"]:
            r = p["reading"]
            self.assertIsNotNone(r, p["id"])
            # vendor_fin reads so far beyond the 1990s range that exp(−d) rounds
            # to 0.000 — honest: the pair self-excludes from the composite.
            self.assertGreaterEqual(r["confidence"], 0.0, p["id"])
            self.assertLessEqual(r["confidence"], 1.0, p["id"])
            self.assertAlmostEqual(p["eff_weight"], round(p["weight"] * r["confidence"], 4),
                                   places=3, msg=p["id"])
            self.assertTrue(p["reason_en"] and p["reason_zh"], p["id"])

    def test_beyond_range_pairs_flagged(self):
        by_id = {p["id"]: p for p in self.snap["l3"]["pairs"]}
        # capex +80.6% and vendor_fin 108% both exceed the whole 1990s range
        self.assertTrue(by_id["capex"]["reading"]["beyond_range"])
        self.assertTrue(by_id["vendor_fin"]["reading"]["beyond_range"])
        # hy_oas 2.66% is squarely inside the 1997 zone
        self.assertFalse(by_id["hy_oas"]["reading"]["beyond_range"])
        self.assertTrue(by_id["hy_oas"]["reading"]["quarter"].startswith("1997"))

    def test_rules_l4_l5_bilingual_and_seeded(self):
        l4, l5 = self.snap["l4"], self.snap["l5"]
        self.assertGreaterEqual(len(l4["like_1999"]), 3)
        self.assertGreaterEqual(len(l4["structurally_unlike"]), 3)
        for item in l4["like_1999"] + l4["structurally_unlike"]:
            self.assertTrue(item["en"] and item["zh"])
        self.assertTrue(l4["clock_take"]["summary_en"] and l4["clock_take"]["summary_zh"])
        self.assertGreaterEqual(len(l5["jumps"]), 4)
        for j in l5["jumps"]:
            self.assertTrue(j["condition_en"] and j["condition_zh"] and j["jump_to"])
            self.assertIn(j["direction"], ("forward", "backward"))
        self.assertGreaterEqual(len(l5["forward_movers"]), 4)
        for m in l5["forward_movers"]:
            self.assertTrue(m["en"] and m["zh"] and m["freq"])

    def test_backdrop_series_present_for_chart(self):
        bd = self.snap["backdrop"]
        self.assertEqual(bd["id"], "nasdaq")
        self.assertEqual(len(bd["points"]), 32)
        self.assertTrue(bd["tier"])

    def test_live_override_wins_and_sets_flag(self):
        live = {"pair_values": {"hy_oas": {"value": 4.6, "change_1y": 1.5,
                                           "via": "fred:BAMLH0A0HYM2", "as_of": "2026-07-02"}},
                "context": {}, "news": [], "fetched_at": "x"}
        snap = model.build_snapshot(KB, live=live, generated_at="t", today="2026-07-02")
        by_id = {p["id"]: p for p in snap["l3"]["pairs"]}
        hy = by_id["hy_oas"]
        self.assertEqual(hy["today"]["value"], 4.6)
        self.assertTrue(hy["today"]["live"])
        self.assertEqual(hy["today"]["via"], "fred:BAMLH0A0HYM2")
        # 4.6% widening +1.5pp/yr belongs to the 1998–2000 stretch, not 1997
        self.assertGreaterEqual(hy["reading"]["year_frac"], 1998.0)
        # untouched pairs stay on seed
        self.assertFalse(by_id["capex"]["today"]["live"])
        self.assertEqual(snap["source"], "live")
        self.assertFalse(snap["is_demo"])

    def test_analysis_falls_back_to_rules_without_key(self):
        out = analysis.analyze(KB, self.snap["l3"])
        self.assertEqual(out["engine"], "rules")


class TestCommittedSeed(unittest.TestCase):
    def test_committed_snapshot_exists_and_matches_recompute(self):
        self.assertTrue(SNAPSHOT_PATH.exists(), "seed snapshot must be committed")
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            disk = json.load(f)
        fresh = _compute()
        d, fcomp = disk["l3"]["composite"], fresh["l3"]["composite"]
        # committed snapshot may be pure seed (strict determinism) or a live
        # refresh (platform practice: flows/pricing/payback commit live too)
        if disk.get("source") == "seed":
            self.assertEqual(d["clock"], fcomp["clock"])
            self.assertEqual(d["plus_minus"], fcomp["plus_minus"])
            self.assertEqual(d["verdict_key"], fcomp["verdict_key"])
        else:
            self.assertTrue(1995.0 <= d["clock"] <= 2002.75)
            self.assertGreaterEqual(d["plus_minus"], 0)
        self.assertIn(disk["analysis_engine"], ("rules", "claude"))
        self.assertTrue(disk["title_zh"] and disk["title_en"])


if __name__ == "__main__":
    unittest.main()
