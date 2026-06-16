"""CapEx-to-Wafer engine tests — stdlib unittest (no pytest, no network, no key).

Run:  python3 -m unittest cwengine.test_engine -v
"""
import json
import unittest
from pathlib import Path

from cwengine import engine, _kb, _compute, analysis

KB = _kb()


class TestKnowledgeBase(unittest.TestCase):
    def test_nodes_well_formed(self):
        ids = set()
        for n in KB["nodes"]:
            for k in ("id", "stage", "kind", "unit", "name_en", "name_zh", "versions"):
                self.assertIn(k, n, f"{n.get('id')} missing {k}")
            self.assertNotIn(n["id"], ids, f"duplicate node {n['id']}")
            ids.add(n["id"])
            self.assertTrue(n["versions"], f"{n['id']} has no versions")
            for v in n["versions"]:
                for k in ("version", "regime", "valid_from", "confidence", "value",
                          "rationale_en", "rationale_zh"):
                    self.assertIn(k, v, f"{n['id']} v{v.get('version')} missing {k}")

    def test_chain_nodes_present(self):
        ids = {n["id"] for n in KB["nodes"]}
        for nid in engine.WAFER_CHAIN_NODES + ["segment_node", "segment_hbm_gb"]:
            self.assertIn(nid, ids, f"chain node {nid} missing")

    def test_mix_versions_sum_to_one(self):
        node = engine.get_node(KB, "segment_mix")
        segs = engine.segment_ids(KB)
        for v in node["versions"]:
            self.assertAlmostEqual(sum(v["value"].get(s, 0) for s in segs), 1.0, places=2,
                                   msg=f"mix v{v['version']} does not sum to 1")

    def test_regimes_and_capex(self):
        self.assertTrue(KB["regimes"])
        self.assertIn(KB["active_regime"], {r["id"] for r in KB["regimes"]})
        self.assertTrue(any(c.get("default") for c in KB["capex_inputs"]))

    def test_evidence_links_valid(self):
        ids = {n["id"] for n in KB["nodes"]}
        for e in KB["evidence"]:
            for k in ("id", "date", "tier", "source_en", "source_zh", "affects", "direction"):
                self.assertIn(k, e)
            for nid in e["affects"]:
                self.assertIn(nid, ids, f"evidence {e['id']} -> unknown node {nid}")

    def test_bilingual_parity(self):
        for n in KB["nodes"]:
            self.assertTrue(n["name_en"] and n["name_zh"])
        for r in KB["regimes"]:
            self.assertTrue(r["name_en"] and r["name_zh"] and r["desc_en"] and r["desc_zh"])
        for s in KB["segments"]:
            self.assertTrue(s["name_en"] and s["name_zh"])


class TestVersionResolution(unittest.TestCase):
    def test_dated_resolution_time_travel(self):
        node = engine.get_node(KB, "segment_mix")
        # in 2023 only the training-led v1 is valid
        v_2023 = engine.resolve_version(node, "training_led", "2023-12-31")
        self.assertEqual(v_2023["version"], 1)
        # by mid-2026 under inference-rotating, the latest applicable is v4
        v_2026 = engine.resolve_version(node, "inference_rotating", "2026-06-16")
        self.assertEqual(v_2026["version"], 4)

    def test_regime_specific_beats_all(self):
        # silicon_fraction has an _all v1 and an inference_rotating v2
        val, meta = engine.node_value(KB, "silicon_fraction", "inference_rotating", "2026-06-16")
        self.assertEqual(meta["regime_source"], "inference_rotating")
        self.assertAlmostEqual(val["point"], 0.40, places=2)
        # training-led has no override -> falls back to _all
        val_t, meta_t = engine.node_value(KB, "silicon_fraction", "training_led", "2026-06-16")
        self.assertEqual(meta_t["regime_source"], "_all")


class TestChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = _compute()

    def test_snapshot_shape(self):
        for k in ("inference", "scenarios", "drift", "sensitivity", "as_of", "active_regime", "meta"):
            self.assertIn(k, self.snap)
        self.assertTrue(self.snap["is_demo"])

    def test_wafers_positive_and_banded(self):
        inf = self.snap["inference"]
        self.assertGreater(inf["wafers_year"], 0)
        self.assertLess(inf["wafers_year_low"], inf["wafers_year"])
        self.assertGreater(inf["wafers_year_high"], inf["wafers_year"])
        self.assertEqual(inf["wafers_per_month"], round(inf["wafers_year"] / 12))

    def test_segment_shares_and_wafers_consistent(self):
        inf = self.snap["inference"]
        self.assertAlmostEqual(sum(s["share"] for s in inf["segments"]), 1.0, places=2)
        self.assertAlmostEqual(sum(s["wafers_year"] for s in inf["segments"]),
                               inf["wafers_year"], delta=2)

    def test_waterfall_has_provenance(self):
        wf = self.snap["inference"]["waterfall"]
        self.assertEqual(len(wf), 5)
        for st in wf:
            self.assertIn("meta", st)
            self.assertIn("version", st["meta"])
            self.assertIn("confidence", st["meta"])

    def test_chain_math_by_hand(self):
        # recompute stage 1 independently: capex * silicon_fraction.point
        cap = engine.default_capex(KB)["value_usd_bn"]
        sf, _ = engine.node_value(KB, "silicon_fraction", KB["active_regime"], KB["_meta"]["as_of"])
        self.assertAlmostEqual(self.snap["inference"]["silicon_usd_bn"],
                               round(cap * sf["point"], 1), places=1)


class TestScenarioComparison(unittest.TestCase):
    def test_same_capex_different_regime(self):
        snap = _compute()
        sc = snap["scenarios"]
        self.assertEqual(len(sc["rows"]), len(KB["regimes"]))
        # the core thesis: regime changes the answer at constant CapEx
        wafers = {r["regime"]: r["wafers_year"] for r in sc["rows"]}
        self.assertNotAlmostEqual(wafers["training_led"], wafers["inference_led"], delta=1)
        self.assertGreater(sc["spread_wafers"], 0)
        # asic+inference share rises as we move toward inference regimes
        by = {r["regime"]: r["asic_inf_share"] for r in sc["rows"]}
        self.assertGreater(by["inference_led"], by["training_led"])


class TestDrift(unittest.TestCase):
    def test_segment_mix_flagged(self):
        snap = _compute()
        flags = {f["node_id"]: f for f in snap["drift"]["flags"]}
        self.assertIn("segment_mix", flags, "rising ASIC+inference share must flag")
        f = flags["segment_mix"]
        self.assertEqual(f["direction"], "up")
        self.assertGreaterEqual(f["run_len"], 3)
        self.assertGreater(f["pct_change"], 0)
        # the series should be monotonically increasing
        ms = [p["metric"] for p in f["series"]]
        self.assertEqual(ms, sorted(ms))

    def test_flagship_asp_drift(self):
        snap = _compute()
        flags = {f["node_id"]: f for f in snap["drift"]["flags"]}
        self.assertIn("segment_asp", flags)
        self.assertEqual(flags["segment_asp"]["direction"], "up")


class TestSensitivity(unittest.TestCase):
    def test_ranked_and_regime_swing(self):
        snap = _compute()
        sens = snap["sensitivity"]
        rows = sens["rows"]
        self.assertEqual(len(rows), len(engine.WAFER_DIR))
        swings = [r["swing_wafers"] for r in rows]
        self.assertEqual(swings, sorted(swings, reverse=True), "must be ranked desc")
        self.assertGreater(sens["regime_swing"]["swing_wafers"], 0)


class TestAnalysisRules(unittest.TestCase):
    def test_rules_proposal_contract(self):
        # rules engine (no API key) must still return a structured proposal
        ev = KB["evidence"][0]
        out = analysis.propose(KB, ev["snippet_en"], force_rules=True)
        self.assertEqual(out["engine"], "rules")
        self.assertIn("affects_node", out)
        self.assertIn(out["affects_node"], {n["id"] for n in KB["nodes"]})
        self.assertIn(out["direction"], ("up", "down", "mix_shift"))
        self.assertTrue(out["rationale_en"] and out["rationale_zh"])


if __name__ == "__main__":
    unittest.main()
