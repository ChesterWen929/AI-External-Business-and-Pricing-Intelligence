"""Scenario Radar — offline unit tests (stdlib unittest, no network, no API key).

Run: python3 -m unittest scenario.test_scenario -v
All tests inject fake sibling dicts; nothing reads the real sibling packages.
"""
import json
import os
import unittest
from pathlib import Path

os.environ.pop("ANTHROPIC_API_KEY", None)  # force the rules path everywhere

from scenario import _compute, _kb, analysis, model

KB = _kb()
CANON = [s["id"] for s in KB["scenarios_seed"]]
DRIVER_IDS = {d["id"] for d in KB["drivers"]}


# ── fake sibling builders ──────────────────────────────────────────────────
def econ_snap(cfnai=0.1, cpi_yoy=3.0, curve=0.5, tsmc_neg=0, date="2026-06-23"):
    return {"date": date, "tsmc_negative_count": tsmc_neg, "indicators": [
        {"id": "cfnai", "latest_value": cfnai, "changes": {"1y": {"pct": 10.0}}},
        {"id": "cpi", "latest_value": 330.0, "changes": {"1y": {"pct": cpi_yoy}}},
        {"id": "yield_curve_3m_10y", "latest_value": curve, "changes": {}},
    ]}


def aibubble_snap(composite=72.0, zone="alert"):
    return {"scores": {"composite": composite, "zone": {"key": zone, "zh": "x"}}}


def flows_snap(marginal=20.0, divergence=-4.0, warning=False, is_demo=False,
               source="live", l5_probs=(40, 25, 20, 15)):
    return {
        "source": source, "is_demo": is_demo,
        "l3": {"marginal_direction": {"score": marginal},
               "retail_vs_inst": {"divergence": divergence, "warning": warning}},
        "l5": {"scenarios": [
            {"name_en": "Liquidity holds", "name_zh": "流動性維持", "prob": l5_probs[0]},
            {"name_en": "Rotation", "name_zh": "輪動", "prob": l5_probs[1]},
            {"name_en": "Blow-off", "name_zh": "噴出", "prob": l5_probs[2]},
            {"name_en": "Regime break", "name_zh": "體制斷裂", "prob": l5_probs[3]},
        ]},
    }


def payback_snap(coverage=0.3, verdict="investing", source="live", is_demo=False):
    return {"source": source, "is_demo": is_demo,
            "headline": {"coverage": coverage, "verdict_key": verdict}}


def pricing_snap(score=50.0, verdict="neutral", source="live"):
    return {"source": source, "pricing_power": {"score": score, "verdict_key": verdict}}


def compute_snap(total=591.0, live=True):
    return {"live_present": live, "headline": {"grand_total_end_year_usd_bn": total}}


def cwengine_snap(wpm=22000, live=True):
    return {"live_present": live, "inference": {"wafers_per_month": wpm}}


def racks_snap(n=19):
    return {"live_present": True, "summary": {"n_systems": n}}


def earnings_snap(n=40, source="finnhub"):
    return {"source": source, "event_count": n}


def rival_snap(n=23):
    return {"research_date": "2026-06-11", "events": [{} for _ in range(n)]}


def full_siblings(**over):
    s = {
        "econ": econ_snap(), "aibubble": aibubble_snap(), "flows": flows_snap(),
        "payback": payback_snap(), "pricing": pricing_snap(), "compute": compute_snap(),
        "cwengine": cwengine_snap(), "racks": racks_snap(), "earnings": earnings_snap(),
        "rival": rival_snap(), "bottleneck": None,
    }
    s.update(over)
    return s


def build(siblings=None, prior_probs=None):
    return model.build_snapshot(KB, siblings=siblings, generated_at="t", today="2026-06-23",
                                prior_probs=prior_probs)


# ── tests ──────────────────────────────────────────────────────────────────
class KBWellFormed(unittest.TestCase):
    def test_scenarios(self):
        scen = KB["scenarios_seed"]
        self.assertEqual(len(scen), 6)
        self.assertEqual(len({s["id"] for s in scen}), 6)
        self.assertEqual(sum(s["prior"] for s in scen), 100)
        for s in scen:
            self.assertGreaterEqual(s["prior"], 1)
            for k in ("name_en", "name_zh", "narrative_en", "narrative_zh",
                      "foundry_read_en", "foundry_read_zh"):
                self.assertTrue(s[k], f"{s['id']}.{k} empty")
            self.assertTrue(s["triggers_en"] and s["triggers_zh"])
            self.assertTrue(s["falsifiers_en"] and s["falsifiers_zh"])
            self.assertTrue(set(s["affinity"].keys()) <= DRIVER_IDS, f"{s['id']} affinity key not a driver")
            for mp in ("equities", "rates", "credit", "ai_semis"):
                self.assertTrue(s["market_path"][f"{mp}_en"] and s["market_path"][f"{mp}_zh"])

    def test_drivers_and_config(self):
        self.assertEqual(len(KB["drivers"]), 10)
        for d in KB["drivers"]:
            self.assertIn("value", d["seed"])
            self.assertTrue(d["source_platform"])
        cfg = KB["config"]
        for k in ("evidence_gain", "coverage_power", "compute_baseline_bn",
                  "wafer_baseline_wpm", "sensitivity_delta", "value_clamp",
                  "unavailable_evidence_weight"):
            self.assertIn(k, cfg)

    def test_bilingual_parity(self):
        self.assertEqual(len(KB["blind_spots_en"]), len(KB["blind_spots_zh"]))
        for d in KB["drivers"]:
            self.assertTrue(d["name_en"] and d["name_zh"])


class Renormalize(unittest.TestCase):
    def test_sums_to_100(self):
        cases = [
            [33.33, 33.33, 33.34], [10, 20, 30, 40], [0.1, 0.2, 0.7],
            [0, 0, 50, 50], [60, 60, 60], [-5, 10, 95], [1e-9, 1e-9, 1e-9],
        ]
        for c in cases:
            out = model.renormalize_to_100(c)
            self.assertEqual(sum(out), 100, c)
            self.assertEqual(len(out), len(c))
            self.assertTrue(all(x >= 0 for x in out))

    def test_all_zero(self):
        out = model.renormalize_to_100([0, 0, 0, 0])
        self.assertEqual(sum(out), 100)
        self.assertEqual(len(out), 4)

    def test_deterministic(self):
        a = model.renormalize_to_100([1, 1, 1])
        b = model.renormalize_to_100([1, 1, 1])
        self.assertEqual(a, b)
        self.assertEqual(sum(a), 100)


class SeedShape(unittest.TestCase):
    def test_pure_seed(self):
        snap = build(None)
        for k in ("headline", "l3", "l4", "l5", "analysis_engine", "as_of",
                  "disclaimer_en", "disclaimer_zh", "prior_scenarios"):
            self.assertIn(k, snap)
        self.assertTrue(snap["is_demo"])
        self.assertEqual(snap["source"], "seed")
        self.assertEqual(snap["analysis_engine"], "rules")
        self.assertNotIn("money_map", snap)
        self.assertNotIn("reservoirs", snap)

    def test_compute_entrypoint(self):
        snap = _compute()
        self.assertEqual(sum(s["prob"] for s in snap["l3"]["scenarios"]), 100)


class Probabilities(unittest.TestCase):
    def test_sum_100_both_layers(self):
        for sib in (None, full_siblings()):
            snap = build(sib)
            self.assertEqual(sum(s["prob"] for s in snap["l3"]["scenarios"]), 100)
            self.assertEqual(sum(s["prob"] for s in snap["l4"]["scenarios"]), 100)

    def test_base_case_is_argmax(self):
        snap = build(full_siblings())
        l4 = snap["l4"]["scenarios"]
        argmax = max(l4, key=lambda s: s["prob"])["id"]
        self.assertEqual(snap["headline"]["base_id"], argmax)
        self.assertEqual(snap["headline"]["base_prob"], max(s["prob"] for s in l4))


class Attribution(unittest.TestCase):
    def test_auditable(self):
        snap = build(full_siblings())
        for s in snap["l3"]["scenarios"]:
            self.assertTrue(s["attribution"])
            for a in s["attribution"]:
                self.assertIn(a["driver_id"], DRIVER_IDS)
                self.assertIsInstance(a["contribution"], int)
                self.assertIn(a["direction"], ("up", "down"))


class CoverageShrink(unittest.TestCase):
    def _l1_from_prior(self, snap):
        prior = {p["id"]: p["prob"] for p in snap["prior_scenarios"]}
        return sum(abs(s["prob"] - prior[s["id"]]) for s in snap["l3"]["scenarios"])

    def test_low_coverage_closer_to_prior(self):
        # strong risk-off read, fully live
        strong = full_siblings(
            econ=econ_snap(cfnai=-0.8, cpi_yoy=5.0, curve=-0.5),
            aibubble=aibubble_snap(composite=90),
            flows=flows_snap(marginal=-80, warning=True),
            payback=payback_snap(coverage=0.05, verdict="burning"),
            pricing=pricing_snap(score=10, verdict="squeezed"),
        )
        full = build(strong)
        # same strong values but only 3 drivers available (rest unavailable → low coverage)
        partial_sibs = {k: None for k in strong}
        partial_sibs["econ"] = strong["econ"]
        partial_sibs["aibubble"] = strong["aibubble"]
        partial_sibs["flows"] = strong["flows"]
        partial = build(partial_sibs)
        self.assertLess(full["l3"]["coverage"]["coverage"], 1.01)
        self.assertGreater(full["l3"]["coverage"]["coverage"], partial["l3"]["coverage"]["coverage"])
        self.assertGreater(self._l1_from_prior(full), self._l1_from_prior(partial))


class LiveSeedDetection(unittest.TestCase):
    def _drv(self, snap, did):
        return next(d for d in snap["l3"]["drivers"] if d["id"] == did)

    def test_flows_is_demo(self):
        snap = build(full_siblings(flows=flows_snap(is_demo=True, source="seed")))
        self.assertEqual(self._drv(snap, "flow_direction")["source"], "seed")

    def test_flows_live(self):
        snap = build(full_siblings(flows=flows_snap(source="live")))
        self.assertEqual(self._drv(snap, "flow_direction")["source"], "live")

    def test_econ_present_is_live(self):
        snap = build(full_siblings())
        self.assertEqual(self._drv(snap, "macro_cycle")["source"], "live")

    def test_compute_live_present_false_is_seed(self):
        snap = build(full_siblings(compute=compute_snap(live=False)))
        self.assertEqual(self._drv(snap, "compute_demand")["source"], "seed")

    def test_coverage_counts(self):
        snap = build(full_siblings())
        cov = snap["l3"]["coverage"]
        self.assertEqual(cov["live_count"] + cov["seed_count"] + len(cov["unavailable"]), 10)


class GracefulDegrade(unittest.TestCase):
    def test_none_sibling(self):
        snap = build(full_siblings(econ=None))
        d = next(x for x in snap["l3"]["drivers"] if x["id"] == "macro_cycle")
        self.assertEqual(d["source"], "unavailable")
        self.assertFalse(d["available"])
        self.assertIn("macro_cycle", snap["l3"]["coverage"]["unavailable"])
        self.assertEqual(sum(s["prob"] for s in snap["l3"]["scenarios"]), 100)

    def test_partial_missing(self):
        sibs = {"econ": econ_snap(), "flows": flows_snap()}  # most keys absent
        snap = build(sibs)
        self.assertEqual(sum(s["prob"] for s in snap["l3"]["scenarios"]), 100)
        self.assertTrue(len(snap["l3"]["coverage"]["unavailable"]) >= 1)

    def test_extract_error_does_not_crash(self):
        snap = build(full_siblings(compute={"headline": "not-a-dict"}))
        self.assertEqual(sum(s["prob"] for s in snap["l3"]["scenarios"]), 100)

    def test_all_missing_is_prior(self):
        snap = build(None)
        self.assertEqual(snap["l3"]["coverage"]["live_count"], 0)
        self.assertEqual(snap["headline"]["confidence"], "low")
        dist = {s["id"]: s["prob"] for s in snap["l3"]["scenarios"]}
        prior = {s["id"]: p for s, p in zip(KB["scenarios_seed"],
                 model.renormalize_to_100([s["prior"] for s in KB["scenarios_seed"]]))}
        self.assertEqual(dist, prior)


class RulesContract(unittest.TestCase):
    def test_rules(self):
        snap = build(full_siblings())
        self.assertEqual(snap["analysis_engine"], "rules")
        l4, l5 = snap["l4"], snap["l5"]
        self.assertIn(l4["base_case"]["confidence"], ("high", "medium", "low"))
        self.assertTrue(l5["watch"] and l5["falsification"] and l5["early_warning"] and l5["sensitivity"])
        for ew in l5["early_warning"]:
            self.assertIn(ew["freq"], ("daily", "weekly", "monthly"))
            self.assertTrue(ew["source_platform"])
            self.assertTrue(ew["en"] and ew["zh"])
        self.assertIn(l4["tail_risk"]["id"], CANON)


class DivergenceEngine(unittest.TestCase):
    def test_late_cycle_topping(self):
        # demand strong, payback burning, bubble hot, flows draining
        sibs = full_siblings(
            compute=compute_snap(total=850),
            payback=payback_snap(coverage=0.05, verdict="burning"),
            aibubble=aibubble_snap(composite=88),
            flows=flows_snap(marginal=-60),
        )
        snap = build(sibs)
        keys = [d["key"] for d in snap["l3"]["divergences"]]
        self.assertIn("late_cycle_topping", keys)
        hit = next(d for d in snap["l3"]["divergences"] if d["key"] == "late_cycle_topping")
        self.assertEqual(hit["severity"], "high")
        self.assertEqual(snap["headline"]["regime_key"], "topping")


class ReflexiveReconcile(unittest.TestCase):
    def test_flows_crosscheck_not_weighted(self):
        # flows' own top scenario = regime_break (index 3 highest)
        sibs = full_siblings(flows=flows_snap(marginal=10, l5_probs=(15, 20, 15, 50)))
        snap = build(sibs)
        cc = snap["l3"]["cross_checks"]["flows_scenarios"]
        self.assertEqual(len(cc), 4)
        self.assertEqual(cc[3]["mapped_id"], "credit_liquidity_break")
        self.assertEqual(sum(s["prob"] for s in snap["l3"]["scenarios"]), 100)


class ClaudeSanitize(unittest.TestCase):
    def test_sanitize_bad_claude_output(self):
        l3 = build(full_siblings())["l3"]
        bad_l4 = {
            "scenarios": [
                {"id": "soft_landing_broadening", "name_en": "x", "name_zh": "x", "prob": 50,
                 "rationale_en": "r", "rationale_zh": "r"},
                {"id": "BOGUS_ID", "name_en": "x", "name_zh": "x", "prob": 80,
                 "rationale_en": "r", "rationale_zh": "r"},
                {"id": "late_cycle_topping", "name_en": "x", "name_zh": "x", "prob": 40,
                 "rationale_en": "r", "rationale_zh": "r"},
            ],
            "base_case": {"id": "BOGUS_ID", "thesis_en": "t", "thesis_zh": "t", "confidence": "high"},
            "tail_risk": {"id": "BOGUS_ID", "why_en": "w", "why_zh": "w"},
            "expected_market_path": {},
        }
        bad_l5 = {"watch": [], "falsification": [], "early_warning": [], "sensitivity": []}
        out = analysis._sanitize(KB, l3, bad_l4, bad_l5)
        scen = out["l4"]["scenarios"]
        ids = [s["id"] for s in scen]
        self.assertEqual(set(ids), set(CANON))            # unknown dropped, missing filled
        self.assertNotIn("BOGUS_ID", ids)
        self.assertEqual(sum(s["prob"] for s in scen), 100)
        for s in scen:
            self.assertIn("baseline_prob", s)
            self.assertIn("delta_vs_baseline", s)
        argmax = max(scen, key=lambda s: s["prob"])["id"]
        self.assertEqual(out["l4"]["base_case"]["id"], argmax)
        self.assertIn(out["l4"]["tail_risk"]["id"], CANON)


class Sensitivity(unittest.TestCase):
    def test_first_order(self):
        snap = build(full_siblings(
            compute=compute_snap(total=850),
            payback=payback_snap(coverage=0.05, verdict="burning"),
            aibubble=aibubble_snap(composite=88),
            flows=flows_snap(marginal=-60),
        ))
        raw = snap["l3"]["sensitivity_raw"]
        self.assertTrue(raw)
        for sv in raw:
            self.assertIn(sv["driver_id"], DRIVER_IDS)
            self.assertIsInstance(sv["up_effect"], int)
            self.assertIsInstance(sv["down_effect"], int)


class Drift(unittest.TestCase):
    def test_with_prior(self):
        prior = {s["id"]: 10 for s in KB["scenarios_seed"]}
        prior[CANON[0]] = 50
        snap = build(full_siblings(), prior_probs=prior)
        for s in snap["l3"]["scenarios"]:
            self.assertIsNotNone(s["drift"])
            self.assertEqual(s["drift"], s["prob"] - s["prior_prob"])

    def test_no_prior(self):
        snap = build(full_siblings())
        for s in snap["l3"]["scenarios"]:
            self.assertIsNone(s["drift"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
