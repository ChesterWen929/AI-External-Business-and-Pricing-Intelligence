"""AI Rack BOM × Supply-Chain Radar — offline unit tests (stdlib unittest, no
network, no API key). Run: python3 -m pytest racks/ -q

Covers the 2026-06-24 data audit fixes:
  - supplier_index aggregates by *company entity* (TSMC's many process/package
    nodes count as ONE chokepoint, not a dozen fragmented keys).
  - per-block landscape notes (e.g. HBM single-source caveat) pass through.
  - power_kw == 0 is treated as undisclosed, not zero.
"""
import json
import unittest
from pathlib import Path

from racks import model

PKG = Path(__file__).resolve().parent
KB = json.load(open(PKG / "knowledge_base.json", encoding="utf-8"))


class SupplierIndexEntity(unittest.TestCase):
    def setUp(self):
        self.idx = model.supplier_index(KB["systems"])
        self.by_name = {e["name"]: e for e in self.idx}

    def test_tsmc_aggregated_not_fragmented(self):
        # No fragmented TSMC keys like "TSMC CoWoS-L" / "TSMC 3.5D" survive.
        frags = [e["name"] for e in self.idx if "TSMC" in e["name"] and e["name"] != "TSMC"]
        self.assertEqual(frags, [], f"TSMC still fragmented: {frags}")
        self.assertIn("TSMC", self.by_name)

    def test_tsmc_is_top_chokepoint(self):
        # Every system in the KB uses TSMC foundry/packaging -> it must be #1.
        self.assertEqual(self.idx[0]["name"], "TSMC")
        self.assertEqual(self.by_name["TSMC"]["count"], len(KB["systems"]))

    def test_count_is_distinct_systems(self):
        for e in self.idx:
            self.assertEqual(e["count"], len(set(e["systems"])))
            self.assertEqual(len(e["systems"]), len(set(e["systems"])))  # no dups

    def test_entity_normalizer(self):
        self.assertEqual(model._entity("TSMC CoWoS-L"), "TSMC")
        self.assertEqual(model._entity("TSMC N3P + 6nm, 3.5D"), "TSMC")
        self.assertEqual(model._entity("Microsoft→TSMC (GUC supply-chain)"), "TSMC")
        self.assertEqual(model._entity("Foxconn/Hon Hai"), "Foxconn/Hon Hai")
        self.assertEqual(model._entity("NVIDIA"), "NVIDIA")
        self.assertEqual(model._entity(""), "")


class SnapshotFields(unittest.TestCase):
    def setUp(self):
        self.snap = model.build_snapshot(KB, generated_at="t", today="2026-06-21")

    def test_hbm_block_note_passes_through(self):
        hbm = self.snap["supplier_landscape"]["hbm"]
        self.assertTrue(hbm.get("note_zh"))
        self.assertTrue(hbm.get("note_en"))

    def test_undisclosed_power_stays_zero(self):
        # power_kw==0 means undisclosed; the model must preserve 0 (front-end
        # renders it as "未揭露 / not disclosed", never a literal 0).
        helios = next(s for s in self.snap["systems"] if s["id"] == "amd-helios-mi400")
        self.assertEqual(helios["power_kw"], 0)


if __name__ == "__main__":
    unittest.main()
