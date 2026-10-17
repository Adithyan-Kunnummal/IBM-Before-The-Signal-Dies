"""Small checks for the data errors that would change the project conclusions."""

import csv
from datetime import date
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

from shapely.geometry import box, mapping

import extract_natural_disasters as extract


def gpkg_blob(geometry, envelope=False):
    header = b"GP" + bytes([0, 3 if envelope else 1]) + struct.pack("<i", 7844)
    return header + (struct.pack("<4d", *geometry.bounds) if envelope else b"") + geometry.wkb


class ExtractionChecks(unittest.TestCase):
    def test_geometry_header_and_overlap(self):
        polygon = box(0, 0, 2, 2)
        self.assertTrue(extract.gpkg_geometry(gpkg_blob(polygon, True)).equals(polygon))
        self.assertEqual(extract.overlap_percent([polygon, polygon], 4), 100)
        self.assertIsNone(extract.gpkg_geometry(None))
        with self.assertRaises(ValueError):
            extract.gpkg_geometry(b"not a GeoPackage")

    def test_flood_scenario_classification(self):
        for label in ["1 in 100 (1%) AEP floodplain", "1% AEP"]:
            self.assertTrue(extract.is_one_percent(label))
        for label in ["1 in 1000 (0.1%)", "10% AEP", "1 in 10"]:
            self.assertFalse(extract.is_one_percent(label))
        self.assertEqual(extract.years_before(date(2024, 2, 29), 4), date(2020, 2, 29))
        self.assertEqual(extract.years_before(date(2024, 2, 29), 1), date(2023, 2, 28))

    def test_complete_extract_excludes_future_and_prescribed_events(self):
        # Files exercise the real ZIP, SQLite, geometry, CSV and CLI paths.
        cache = Path(__file__).resolve().parents[1] / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as tmp:
            root = Path(tmp)
            town = box(115.9, -32.1, 116.1, -31.9)
            other = box(120, -32.1, 120.2, -31.9)
            database = root / "fixture.gpkg"
            db = sqlite3.connect(database)
            db.executescript("""
                CREATE TABLE gpkg_geometry_columns(table_name TEXT,column_name TEXT,srs_id INT);
                INSERT INTO gpkg_geometry_columns VALUES('G01_SAL_2021_WA','geom',7844);
                CREATE TABLE G01_SAL_2021_WA(SAL_CODE_2021 TEXT,SAL_NAME_2021 TEXT,Tot_P_P INT,geom BLOB);
            """)
            db.executemany("INSERT INTO G01_SAL_2021_WA VALUES(?,?,?,?)", [
                ("50001", "Test Town", 100, gpkg_blob(town)),
                ("50002", "Other Town", 50, gpkg_blob(other)),
            ])
            db.commit()
            db.close()
            with zipfile.ZipFile(root / extract.FILES["localities"], "w") as archive:
                archive.write(database, "fixture.gpkg")
            database.unlink()

            db = sqlite3.connect(database)
            db.executescript("""
                CREATE TABLE gpkg_geometry_columns(table_name TEXT,column_name TEXT,srs_id INT);
                INSERT INTO gpkg_geometry_columns VALUES('DBCA_Fire_History_DBCA_060','SHAPE',7844);
                CREATE TABLE DBCA_Fire_History_DBCA_060(object_id INT,SHAPE BLOB,fih_number TEXT,
                  fih_district TEXT,fih_fire_seaso TEXT,fih_master_key INT,fih_date1 TEXT,
                  fih_fire_type TEXT,fih_poly_type TEXT);
            """)
            records = [
                (1, "same-fire", "2024-01-01", "WF", "Actual Burnt"),
                (2, "same-fire", "2024-01-01", "wf", "Actual burnt"),
                (3, "prescribed", "2024-01-01", "PB", "Actual Burnt"),
                (4, "future", "2026-10-17", "WF", "Actual Burnt"),
                (5, "planned", "2024-01-01", "WF", "Cell Treatment"),
                (6, "999", "2024-01-01", "WF", "Actual Burnt"),
            ]
            db.executemany("INSERT INTO DBCA_Fire_History_DBCA_060 VALUES(?,?,?,?,?,?,?,?,?)", [
                (oid, gpkg_blob(town), number, "TEST", "2024/2025", 0, day, kind, poly)
                for oid, number, day, kind, poly in records
            ])
            db.commit()
            db.close()
            with zipfile.ZipFile(root / extract.FILES["fire_history"], "w") as archive:
                archive.write(database, "fixture_fire.gpkg")
            for kind in ["bushfire_zones", "flood_zones"]:
                properties = {"designationdate": "2025-12-13"} if kind == "bushfire_zones" else {
                    "status": "Active", "ext_type": "1 in 100 (1%) AEP floodplain"
                }
                feature = {"type": "Feature", "properties": properties, "geometry": mapping(town)}
                content = {"type": "FeatureCollection", "crs": {
                    "type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::7844"}
                }, "features": [feature, feature]}
                with zipfile.ZipFile(root / extract.FILES[kind], "w") as archive:
                    archive.writestr("fixture.geojson", json.dumps(content))
            (root / extract.FILES["cyclones"]).write_text(
                "BOM preamble\n\nNAME,DISTURBANCE_ID,TM,TYPE,LAT,LON,MAX_WIND_SPD,MAX_WIND_GUST\n"
                "Example,ONE,2024-01-01 06:00,T,-32,116,20,30\n"
                "Example,ONE,2024-01-01 06:00,T,-32,116,20,30\n"
                "Example,ONE,2024-01-01 12:00,T,-32,116,22,\n"
                "Future,TWO,2026-10-17 00:00,T,-32,116,90,100\n"
                "Low,THREE,2024-01-01 00:00,L,-32,116,10,15\n", encoding="utf-8"
            )
            output = root / "combined.csv"
            subprocess.run([
                sys.executable, str(Path(extract.__file__)), "--input-dir", str(root),
                "--output", str(output), "--as-of", "2026-10-17"
            ], check=True, capture_output=True, text=True)
            with output.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            town_row, other_row = rows
            self.assertEqual(len(rows), 2)
            self.assertEqual(town_row["dbca_recorded_bushfire_events"], "1")
            self.assertEqual(town_row["nearby_cyclone_track_count"], "1")
            self.assertEqual(town_row["nearby_cyclone_centre_max_wind_ms"], "22.0")
            self.assertEqual(float(town_row["bushfire_prone_area_pct_snapshot"]), 100)
            self.assertEqual(float(town_row["flood_1pct_aep_area_pct_snapshot"]), 100)
            self.assertEqual(other_row["flood_mapping_status"], "unknown_no_overlap")
            self.assertEqual(other_row["nearby_cyclone_centre_max_wind_ms"], "")
            quality = json.loads(output.with_suffix(".metadata.json").read_text())["quality_counts"]
            self.assertEqual(quality["fire_polygons_with_unresolved_event_identity"], 1)


if __name__ == "__main__":
    unittest.main()
