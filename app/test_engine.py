"""Checks for the simulation engine and explanations. Run: python3 -m unittest discover -s app -p "test_*.py" """
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import Engine, haversine_km  # noqa: E402
import explain  # noqa: E402

ENGINE = Engine()
PERTH_HILLS = {"lat": -31.90, "lon": 116.17, "radius_km": 20, "failure_share": 100}


class EngineTests(unittest.TestCase):
    def test_loads_all_localities_and_sites(self):
        self.assertEqual(len(ENGINE.localities), 1699)
        self.assertEqual(len(ENGINE.sites), 943)  # distinct RFNSA identifiers

    def test_no_disaster_loses_nothing(self):
        result = ENGINE.simulate(coverage_km=10)
        self.assertEqual(result["sites_failed"], 0)
        self.assertEqual(result["lost_coverage"]["localities"], 0)

    def test_lost_localities_have_no_working_site_in_range(self):
        result = ENGINE.simulate(hazard=PERTH_HILLS, coverage_km=10)
        failed = set(result["failed_site_ids"])
        self.assertGreater(result["lost_coverage"]["localities"], 0)
        for item in result["lost_coverage"]["list"]:
            loc = ENGINE.by_sal[item["sal"]]
            working_in_range = [
                s for s in ENGINE.sites if s["id"] not in failed
                and min(haversine_km(loc["lat"], loc["lon"], a, b) for a, b in s["positions"]) <= 10
            ]
            self.assertEqual(working_in_range, [], loc["name"])

    def test_failure_share_is_deterministic_and_proportional(self):
        full = ENGINE.failed_sites({**PERTH_HILLS, "failure_share": 100})
        half_a = ENGINE.failed_sites({**PERTH_HILLS, "failure_share": 50})
        half_b = ENGINE.failed_sites({**PERTH_HILLS, "failure_share": 50})
        self.assertEqual(half_a, half_b)
        self.assertTrue(half_a <= full)
        self.assertEqual(len(half_a), round(len(full) * 0.5))

    def test_units_only_restore_lost_localities_within_range(self):
        result = ENGINE.simulate(hazard=PERTH_HILLS, coverage_km=10, units=3, unit_radius_km=15)
        lost_res = result["lost_coverage"]["residents"]
        self.assertLessEqual(result["restored"]["residents"], lost_res)
        lost_names = {i["name"] for i in result["lost_coverage"]["list"]}
        for unit in result["units"]:
            for name in unit["restored_names"]:
                self.assertIn(name, lost_names)
        gains = [u["restored_residents"] for u in result["units"]]
        self.assertEqual(gains, sorted(gains, reverse=True))  # greedy order

    def test_manual_site_switch_off(self):
        site_id = ENGINE.sites[0]["id"]
        result = ENGINE.simulate(manual_ids=[site_id], coverage_km=10)
        self.assertEqual(result["failed_site_ids"], [site_id])


class ExplainTests(unittest.TestCase):
    def test_template_never_claims_100_percent(self):
        top = min(ENGINE.localities, key=lambda l: -l["hazard_percentile"])
        text = explain.template_explanation(top, len(ENGINE.localities))
        self.assertNotIn("100%", text.split("Main factors")[0])
        self.assertIn(top["index_band"], text)

    def test_falls_back_to_template_without_granite(self):
        if explain.GRANITE_URL:
            self.skipTest("Granite configured")
        out = explain.explain(ENGINE.localities[0], len(ENGINE.localities))
        self.assertEqual(out["source"], "template")
        self.assertIn("not connected", out["note"])


if __name__ == "__main__":
    unittest.main()
