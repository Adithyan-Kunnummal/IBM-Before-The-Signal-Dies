import csv
from collections import Counter
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from shapely.geometry import box
import shapely

from extract_natural_disasters import PROJECT, extract_cyclones
import prepare_outage_data as prepare


class CleaningChecks(unittest.TestCase):
    def test_duration_and_timezone_change(self):
        self.assertEqual(prepare.duration_minutes(" 22.24"), 1344)
        with self.assertRaises(ValueError):
            prepare.duration_minutes("2.99")
        raw = {
            "Reference": "EXAMPLE", "Outage type": "Significant local outage",
            "Start date of outage": "04/10/2026", "Start time of outage": "01:30 AEST",
            "End date of outage": "04/10/2026", "End time of outage": "03:30 AEDT",
            "Duration of outage": "1.00", "Affected states/territories": "WA",
            "Affected suburbs/towns": "Test Town", "Affected postcodes": "0870; 6000",
            "Number of each type of services affected": "Mobile data 100; Mobile voice 100",
            "Cause (high level)": "Power interruption", "Cause (related to natural event or weather)": "No",
            "Date and time entry first published": "05/10/2026",
        }
        event = prepare.clean_event(raw)
        self.assertEqual(event["duration_minutes"], 60)
        self.assertEqual(event["start_date_wa"], "2026-10-03")
        self.assertIn("0870", event["postcodes_reported_unpaired"])
        self.assertEqual(event["mobile_data_services_affected"], 100)
        self.assertEqual(event["mobile_voice_services_affected"], 100)

    def test_unlabelled_and_ambiguous_reports_never_become_negatives(self):
        rows = [dict(sal_code_2021=code, locality=name, longitude=116, latitude=-32,
                     population_2021=100, locality_area_km2=10, population_density_2021_per_km2=10)
                for code, name in [("SAL1", "Test Town"), ("SAL2", "Other Town")]]
        events = [
            dict(event_id="ONE", geographic_scope="wa_only", towns_reported="Test Town (WA); Unmatched", start_date_wa="2026-01-01"),
            dict(event_id="TWO", geographic_scope="multi_state", towns_reported="Other Town", start_date_wa="2026-01-01"),
        ]
        links = prepare.match_locations(events, rows)
        self.assertEqual([r["match_status"] for r in links],
                         ["matched_exact_normalized", "unmatched_name", "multi_state_requires_review"])
        result = prepare.modelling_rows(rows, links, events, [{}, {}], [set(), set()], [set(), set()],
                                        date(2020, 12, 31), date(2025, 12, 31), date(2026, 1, 2))
        self.assertEqual([r["reported_outage_label"] for r in result], [1, None])
        self.assertNotIn("matched_report_count_diagnostic_only", prepare.MODEL_FEATURES)
        self.assertNotIn("bushfire_prone_area_pct_snapshot", prepare.MODEL_FEATURES)

    def test_multiple_coordinates_for_one_site_count_once(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".cache") as tmp:
            path = Path(tmp) / "sites.geojson"
            features = [{"type": "Feature", "properties": {"Year": 2025, "MNO": "Optus", "RFNSA_ID": identifier},
                         "geometry": {"type": "Point", "coordinates": position}}
                        for identifier, position in [(1, [116, -32]), (1, [116.0001, -32]), (2, [116.001, -32])]]
            path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
            polygon = shapely.transform(box(115.9, -32.1, 116.1, -31.9), PROJECT, interleaved=False)
            rows = [dict(longitude=116, latitude=-32, locality_area_km2=10)]
            stats = Counter()
            result = prepare.tower_features(path, [polygon], rows, stats)[0]
            self.assertEqual(result["optus_site_count_2025"], 2)
            self.assertEqual(result["optus_sites_within_5km"], 2)
            self.assertEqual(stats["site_ids_with_multiple_source_positions"], [1])

    def test_cyclone_join_accepts_coordinates_loaded_from_csv(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".cache") as tmp:
            path = Path(tmp) / "cyclones.csv"
            path.write_text(
                "BOM preamble\nNAME,DISTURBANCE_ID,TM,TYPE,LAT,LON,MAX_WIND_SPD,MAX_WIND_GUST\n"
                "Example,ONE,2024-01-01 00:00,T,-32,116,20,30\n"
            )
            result = extract_cyclones(path, date(2020, 1, 1), date(2025, 1, 1),
                                      [{"longitude": "116.0", "latitude": "-32.0"}], 100, Counter())
            self.assertEqual(result[0], [{"ONE"}])


if __name__ == "__main__":
    unittest.main()
