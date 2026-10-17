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


class LiveRebandingTests(unittest.TestCase):
    ORDER = {"Low": 0, "Medium": 1, "High": 2}

    def test_no_failures_changes_no_band(self):
        self.assertEqual(ENGINE.simulate(coverage_km=10)["band_changes"]["list"], [])

    def test_failures_only_raise_bands_and_increase_distance(self):
        result = ENGINE.simulate(hazard=PERTH_HILLS, coverage_km=10)
        changes = result["band_changes"]["list"]
        self.assertGreater(len(changes), 0)
        for c in changes:
            self.assertGreater(self.ORDER[c["band_after"]], self.ORDER[c["band_before"]], c["name"])
            self.assertGreaterEqual(c["nearest_site_km_after"], c["nearest_site_km_before"] - 0.5, c["name"])

    def test_kimberley_cyclone_raises_broome_to_high(self):
        result = ENGINE.simulate(hazard={"lat": -17.96, "lon": 122.24, "radius_km": 150, "failure_share": 100})
        broome = [c for c in result["band_changes"]["list"] if c["name"] == "Broome"]
        self.assertEqual(len(broome), 1)
        self.assertEqual(broome[0]["band_after"], "High")


class ModelServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from model_service import ModelService
        cls.service = ModelService(ENGINE.localities)

    def test_status_is_reported_even_without_libraries(self):
        self.assertIn("cpu", self.service.status)
        if not self.service.backends:
            self.assertTrue(self.service.status["errors"])

    def test_backends_agree_with_stored_scores(self):
        if not self.service.backends:
            self.skipTest("No model library installed")
        for name, info in self.service.status["backends"].items():
            self.assertTrue(info["agrees_with_stored"], f"{name}: {info}")
        live = self.service.score(ENGINE.index_of["SAL50190"])  # Broome
        for s in live["scores"].values():
            self.assertAlmostEqual(s["score"], ENGINE.by_sal["SAL50190"]["model_score"], places=3)


class ExplainTests(unittest.TestCase):
    def test_template_never_claims_100_percent(self):
        top = min(ENGINE.localities, key=lambda l: -l["hazard_percentile"])
        text = explain.explanation(top, len(ENGINE.localities))
        self.assertNotIn("100%", text.split("Main factors")[0])
        self.assertIn(top["index_band"], text)

    def test_explanation_is_plain_text_with_caveat(self):
        out = explain.explanation(ENGINE.localities[0], len(ENGINE.localities))
        self.assertIsInstance(out, str)
        self.assertIn(explain.CAVEAT, out)


if __name__ == "__main__":
    unittest.main()
