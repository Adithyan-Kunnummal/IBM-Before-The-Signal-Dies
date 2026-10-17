"""Build a small WA locality hazard/history CSV from the original downloads.

Install requirements-disasters.txt, then run with --as-of YYYY-MM-DD.
Raw archives are read unchanged; large GeoJSON files are streamed from ZIP.
"""

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import zipfile

import ijson
from pyproj import Geod, Transformer
import shapely
from shapely.geometry import shape


FILES = {
    "localities": "Geopackage_2021_G01_WA_GDA2020.zip",
    "bushfire_zones": "Bushfire_Prone_Areas_OBRM_026_WA_GDA2020_Public_View_GeoJSON.zip",
    "fire_history": "DBCA_Fire_History_DBCA_060_WA_GDA2020_Public_Geopackage.zip",
    "flood_zones": "FPM_Floodplain_Area_DWER_020_WA_GDA2020_Public_GeoJSON.zip",
    "cyclones": "IDCKMSTM0S.csv",
}
SOURCES = {
    "localities": "https://www.abs.gov.au/census/find-census-data/geopackages",
    "bushfire_zones": "https://catalogue.data.wa.gov.au/dataset/bushfire-prone-areas-obrm-026",
    "fire_history": "https://catalogue.data.wa.gov.au/dataset/dbca-fire-history",
    "flood_zones": "https://catalogue.data.wa.gov.au/dataset/fpm-floodplain-area",
    "cyclones": "https://www.bom.gov.au/cyclone/tropical-cyclone-knowledge-centre/databases/",
}
PROJECT = Transformer.from_crs(7844, 9473, always_xy=True).transform
TO_LONLAT = Transformer.from_crs(9473, 4326, always_xy=True).transform
GEOD = Geod(ellps="WGS84")


def log(message):
    print(message, flush=True)


def years_before(day, years):
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def gpkg_geometry(blob):
    """Decode the OGC GeoPackage header and its embedded WKB geometry."""
    if blob is None:
        return None
    if blob[:2] != b"GP" or blob[2] != 0:
        raise ValueError("Unsupported GeoPackage geometry header")
    envelope = (blob[3] >> 1) & 7
    if envelope > 4 or blob[3] & 32:
        raise ValueError("Unsupported GeoPackage envelope or extended geometry")
    if blob[3] & 16:
        return None
    return shapely.from_wkb(blob[8 + (0, 32, 48, 48, 64)[envelope]:])


def valid_geometry(geometry, stats):
    if geometry is None or geometry.is_empty:
        stats["empty_geometries"] += 1
        return None
    if not geometry.is_valid:
        stats["repaired_geometries"] += 1
        geometry = shapely.make_valid(geometry)
    return None if geometry.is_empty else geometry


def polygons(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from polygons(part)


def unpack_gpkg(archive, cache):
    cache.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        members = [i for i in source.infolist() if i.filename.endswith(".gpkg")]
        if len(members) != 1:
            raise ValueError(f"Expected one GeoPackage in {archive.name}")
        member = members[0]
        target = cache / Path(member.filename).name
        stamp = target.with_suffix(".source.json")
        identity = {"bytes": member.file_size, "zip_crc32": member.CRC}
        if target.exists() and stamp.exists() and (
            target.stat().st_size == member.file_size
            and json.loads(stamp.read_text()) == identity
        ):
            return target
        log(f"Extracting {member.filename} ({member.file_size / 1e9:.2f} GB)")
        partial = target.with_suffix(".partial")
        with source.open(member) as incoming, partial.open("wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        if partial.stat().st_size != member.file_size:
            raise ValueError(f"Incomplete extraction: {member.filename}")
        partial.replace(target)
        stamp.write_text(json.dumps(identity), encoding="utf-8")
        return target


def open_gpkg(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_localities(path, selected, stats):
    rows, geometries = [], []
    with open_gpkg(path) as db:
        srs = db.execute(
            "SELECT srs_id FROM gpkg_geometry_columns WHERE table_name='G01_SAL_2021_WA'"
        ).fetchone()
        if not srs or srs[0] != 7844:
            raise ValueError("Expected ABS G01_SAL_2021_WA in GDA2020 / EPSG:7844")
        query = """SELECT SAL_CODE_2021, SAL_NAME_2021, Tot_P_P, geom
                   FROM G01_SAL_2021_WA ORDER BY SAL_CODE_2021"""
        for record in db.execute(query):
            code, name = str(record[0]), record[1]
            if selected and code.casefold() not in selected and name.casefold() not in selected:
                continue
            geometry = valid_geometry(gpkg_geometry(record[3]), stats)
            if geometry is None:
                continue
            geometry = valid_geometry(shapely.transform(geometry, PROJECT, interleaved=False), stats)
            if geometry is None or geometry.area <= 0:
                stats["non_area_localities_excluded"] += 1
                continue
            lon, lat = TO_LONLAT(*geometry.representative_point().coords[0])
            area = geometry.area / 1e6
            rows.append({
                "sal_code_2021": code, "locality": name,
                "longitude": round(lon, 6), "latitude": round(lat, 6),
                "population_2021": record[2], "locality_area_km2": round(area, 6),
                "population_density_2021_per_km2": (
                    round(record[2] / area, 6) if record[2] is not None else None
                ),
            })
            geometries.append(geometry)
    if not rows:
        raise ValueError("No spatial localities matched the selection")
    if selected:
        matched = {r[k].casefold() for r in rows for k in ("sal_code_2021", "locality")}
        if selected - matched:
            raise ValueError(f"Unmatched locality selections: {sorted(selected - matched)}")
    return rows, geometries


def geojson_features(archive):
    with zipfile.ZipFile(archive) as source:
        members = [n for n in source.namelist() if n.endswith(".geojson")]
        if len(members) != 1:
            raise ValueError(f"Expected one GeoJSON in {archive.name}")
        with source.open(members[0]) as header:
            prefix = header.read(2048).decode("utf-8-sig")
        if "EPSG::7844" not in prefix:
            raise ValueError(f"Expected EPSG:7844 GeoJSON: {archive.name}")
        with source.open(members[0]) as incoming:
            yield from ijson.items(incoming, "features.item", use_float=True)


def is_one_percent(text):
    return bool(re.search(r"\b1\s+in\s+100\b|(?<![\d.])1\s*%", text, re.I))


def overlap_percent(parts, area):
    # Union clipped pieces so overlapping records cannot double-count area.
    value = shapely.union_all(parts).area / area * 100 if parts else 0.0
    if not -1e-7 <= value <= 100.00001:
        raise ValueError(f"Invalid overlap percentage: {value}")
    return round(min(100.0, max(0.0, value)), 6)


def extract_zones(path, kind, localities, tree, stats):
    clips = [[] for _ in localities]
    one_percent = [[] for _ in localities]
    designation_dates = [set() for _ in localities]
    for number, feature in enumerate(geojson_features(path), 1):
        stats[f"{kind}_source_features"] += 1
        properties = feature["properties"]
        if kind == "flood" and str(properties.get("status", "")).strip().casefold() != "active":
            stats["flood_nonactive_features_excluded"] += 1
            continue
        geometry = valid_geometry(shape(feature["geometry"]) if feature.get("geometry") else None, stats)
        if geometry is None:
            continue
        geometry = valid_geometry(shapely.transform(geometry, PROJECT, interleaved=False), stats)
        if geometry is None:
            continue
        # Index individual polygon pieces rather than a large multipart envelope.
        for part in polygons(geometry):
            for index in tree.query(part):
                clipped = shapely.intersection(part, localities[index])
                if clipped.area <= 0:
                    continue
                clips[index].append(clipped)
                if kind == "bushfire" and properties.get("designationdate"):
                    designation_dates[index].add(properties["designationdate"][:10])
                if kind == "flood" and is_one_percent(str(properties.get("ext_type") or "")):
                    one_percent[index].append(clipped)
        if number % 25 == 0:
            log(f"{kind}: {number} source features read")
    if not stats[f"{kind}_source_features"]:
        raise ValueError(f"Empty hazard source: {path}")
    values = []
    for index, locality in enumerate(localities):
        values.append((
            overlap_percent(clips[index], locality.area),
            overlap_percent(one_percent[index], locality.area),
            ";".join(sorted(designation_dates[index])),
        ))
        clips[index].clear()
        one_percent[index].clear()
    return values


def fire_event_key(record):
    number = str(record["fih_number"] or "").strip().casefold()
    if number not in ("", "0", "999", "unknown"):
        return (record["fih_district"], record["fih_fire_seaso"], number)
    master = record["fih_master_key"]
    if master and str(master) not in ("0", "999"):
        return ("master", str(master))
    return ("undetermined_event", str(record["object_id"]))


def extract_fires(path, start, end, localities, tree, stats):
    events = [set() for _ in localities]
    last_dates = [None for _ in localities]
    with open_gpkg(path) as db:
        info = db.execute("SELECT table_name,column_name,srs_id FROM gpkg_geometry_columns").fetchall()
        if len(info) != 1 or tuple(info[0]) != ("DBCA_Fire_History_DBCA_060", "SHAPE", 7844):
            raise ValueError("Unexpected DBCA GeoPackage schema or CRS")
        stats["fire_source_type_counts"] = dict(db.execute(
            "SELECT upper(trim(fih_fire_type)),COUNT(*) FROM DBCA_Fire_History_DBCA_060 "
            "GROUP BY upper(trim(fih_fire_type))"
        ))
        stats["fire_source_latest_wildfire_date"] = db.execute(
            "SELECT MAX(fih_date1) FROM DBCA_Fire_History_DBCA_060 WHERE upper(trim(fih_fire_type))='WF'"
        ).fetchone()[0]
        query = """SELECT object_id, SHAPE, fih_number, fih_district, fih_fire_seaso,
                          fih_master_key, fih_date1 FROM DBCA_Fire_History_DBCA_060
                   WHERE upper(trim(fih_fire_type))='WF'
                   AND lower(trim(fih_poly_type))='actual burnt'
                   AND substr(fih_date1,1,10)>=? AND substr(fih_date1,1,10)<?"""
        for number, record in enumerate(db.execute(query, (start.isoformat(), end.isoformat())), 1):
            event_date = date.fromisoformat(record["fih_date1"][:10])
            key = fire_event_key(record)
            if key[0] == "undetermined_event":
                stats["fire_polygons_with_unresolved_event_identity"] += 1
                continue
            geometry = valid_geometry(gpkg_geometry(record["SHAPE"]), stats)
            if geometry is None:
                continue
            geometry = valid_geometry(shapely.transform(geometry, PROJECT, interleaved=False), stats)
            if geometry is None:
                continue
            for index in tree.query(geometry):
                # Only a positive-area intersection counts; shared boundaries do not.
                if not shapely.relate_pattern(geometry, localities[index], "2********"):
                    continue
                events[index].add(key)
                last_dates[index] = max(last_dates[index] or event_date, event_date)
            stats["fire_polygons_in_history_window"] += 1
            if number % 1000 == 0:
                log(f"fire history: {number} wildfire polygons read")
    return events, last_dates


def cyclone_records(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        for line in source:
            if line.startswith("NAME,DISTURBANCE_ID,"):
                break
        else:
            raise ValueError("BOM cyclone CSV header not found")
        reader = csv.DictReader(source, fieldnames=next(csv.reader([line])))
        for record in reader:
            yield {k: (v or "").strip() for k, v in record.items() if k is not None}


def optional_number(text):
    if not text or text.casefold() in ("null", "nan"):
        return None
    value = float(text)
    if not 0 <= value < float("inf"):
        return None
    return value


def extract_cyclones(path, start, end, rows, radius, stats):
    events = [set() for _ in rows]
    last_dates = [None for _ in rows]
    winds, gusts = [None for _ in rows], [None for _ in rows]
    lons, lats = [float(r["longitude"]) for r in rows], [float(r["latitude"]) for r in rows]
    seen = set()
    for record in cyclone_records(path):
        stats["cyclone_source_track_points"] += 1
        if record["TYPE"] != "T":
            continue
        when = datetime.strptime(record["TM"], "%Y-%m-%d %H:%M").date()
        if not start <= when < end:
            continue
        lon, lat = float(record["LON"]), float(record["LAT"])
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("Invalid cyclone coordinates")
        identifier = record["DISTURBANCE_ID"]
        if not identifier:
            raise ValueError("Missing cyclone disturbance ID")
        key = (identifier, record["TM"], lon, lat)
        if key in seen:
            stats["duplicate_cyclone_track_points_excluded"] += 1
            continue
        seen.add(key)
        _, _, distances = GEOD.inv([lon] * len(rows), [lat] * len(rows), lons, lats)
        wind, gust = optional_number(record["MAX_WIND_SPD"]), optional_number(record["MAX_WIND_GUST"])
        for index, distance in enumerate(distances):
            if distance > radius * 1000:
                continue
            events[index].add(identifier)
            last_dates[index] = max(last_dates[index] or when, when)
            if wind is not None:
                winds[index] = max(winds[index] if winds[index] is not None else wind, wind)
            if gust is not None:
                gusts[index] = max(gusts[index] if gusts[index] is not None else gust, gust)
        stats["cyclone_track_points_in_history_window"] += 1
    return events, last_dates, winds, gusts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, help="Default: data/processed/wa_natural_disasters.csv")
    parser.add_argument("--as-of", required=True, type=date.fromisoformat, help="History cutoff, exclusive (UTC dates)")
    parser.add_argument("--history-years", type=int, default=5)
    parser.add_argument("--cyclone-radius-km", type=float, default=100)
    parser.add_argument("--locality", action="append", default=[], help="Optional ABS locality name or code; repeatable")
    args = parser.parse_args()
    if not 0 < args.history_years < args.as_of.year or not math.isfinite(args.cyclone_radius_km) or args.cyclone_radius_km <= 0:
        parser.error("History years and cyclone radius must be positive")
    root = args.input_dir.resolve()
    paths = {kind: root / name for kind, name in FILES.items()}
    missing = [str(p) for p in paths.values() if not p.is_file()]
    if missing:
        parser.error("Missing input files:\n" + "\n".join(missing))
    output = args.output or root / "data/processed/wa_natural_disasters.csv"
    if output.suffix.lower() != ".csv":
        parser.error("Output filename must end in .csv")
    if output.resolve() in {p.resolve() for p in paths.values()}:
        parser.error("Output must not overwrite a source file")
    cache, stats = root / ".cache/disaster_extract", Counter()
    start = years_before(args.as_of, args.history_years)
    rows, localities = load_localities(
        unpack_gpkg(paths["localities"], cache), {s.casefold() for s in args.locality}, stats
    )
    log(f"Loaded {len(rows)} WA localities; history {start} to {args.as_of} (exclusive)")
    tree = shapely.STRtree(localities)
    log("Reading bushfire-prone zones...")
    bushfire = extract_zones(paths["bushfire_zones"], "bushfire", localities, tree, stats)
    log("Reading floodplain zones...")
    flood = extract_zones(paths["flood_zones"], "flood", localities, tree, stats)
    log("Reading dated wildfire records...")
    fires, fire_dates = extract_fires(
        unpack_gpkg(paths["fire_history"], cache), start, args.as_of, localities, tree, stats
    )
    log("Reading cyclone track history...")
    cyclones, cyclone_dates, winds, gusts = extract_cyclones(
        paths["cyclones"], start, args.as_of, rows, args.cyclone_radius_km, stats
    )
    for index, row in enumerate(rows):
        row.update({
            "as_of_date": args.as_of.isoformat(), "history_start_date": start.isoformat(),
            "history_years": args.history_years,
            "bushfire_prone_area_pct_snapshot": bushfire[index][0],
            "bushfire_designation_dates": bushfire[index][2],
            "flood_any_mapped_extent_pct_snapshot": flood[index][0],
            "flood_1pct_aep_area_pct_snapshot": flood[index][1],
            "flood_mapping_status": "mapped_overlap" if flood[index][0] > 0 else "unknown_no_overlap",
            "dbca_recorded_bushfire_events": len(fires[index]),
            "dbca_recorded_bushfire_events_per_year": round(len(fires[index]) / args.history_years, 6),
            "dbca_last_recorded_bushfire_date": fire_dates[index],
            "dbca_history_completeness": "partial_coverage",
            "cyclone_radius_km": args.cyclone_radius_km,
            "nearby_cyclone_track_count": len(cyclones[index]),
            "last_nearby_cyclone_track_date": cyclone_dates[index],
            "nearby_cyclone_centre_max_wind_ms": winds[index],
            "nearby_cyclone_centre_max_gust_ms": gusts[index],
        })
    if len({r["sal_code_2021"] for r in rows}) != len(rows):
        raise ValueError("Duplicate locality IDs in output")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".csv.partial")
    with temporary.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(output)
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(), "rows": len(rows),
        "csv_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "parameters": {"as_of_exclusive": str(args.as_of), "history_start_inclusive": str(start),
                       "history_years": args.history_years, "cyclone_radius_km": args.cyclone_radius_km},
        "sources": {kind: {"file": str(path.relative_to(root)), "bytes": path.stat().st_size,
                           "url": SOURCES[kind]} for kind, path in paths.items()},
        "quality_counts": dict(stats), "measurement_crs": "EPSG:9473 (GDA2020 Australian Albers)",
        "versions": {"shapely": shapely.__version__, "ijson": ijson.__version__},
        "limitations": [
            "Hazard percentages are area fractions, not fractions of residents exposed or outage probabilities.",
            "Hazard zones are the supplied current snapshots, not reconstructed historical maps. Do not use them as point-in-time training features without version checks.",
            "Flood extents have multiple AEP/scenario classes. The any-extent and 1% AEP unions are separate. No overlap means unknown mapping coverage, not flood-safe.",
            "Fire counts include only WF / Actual Burnt records, deduplicated by district, fire season and fire number; valid master keys are a fallback. Unresolved identities are excluded and counted.",
            "DBCA fire coverage is incomplete. Zero means no matched recorded event, not proof of no fire; record dates do not establish when the data was published.",
            "Cyclone proximity is geodesic distance from an interior locality representative point to recorded TYPE=T track samples, including weakening/developing phases. Tracks are not interpolated between observations.",
            "Cyclone wind/gust values are metres per second near the cyclone centre, not local station observations or future weather.",
            "Rainfall, local weather-station gusts, mobile infrastructure and outage labels are not included in this disaster-only extract.",
            "The as-of cutoff excludes history dates on/after that date; source snapshots may include later revisions. This output is a feature snapshot, not a labelled training dataset.",
        ],
    }
    output.with_suffix(".metadata.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"Saved {output}: {len(rows)} rows, {output.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
