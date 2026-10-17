"""Clean WA Optus reports and prepare locality features without weather data.

Outputs positive/unlabelled observations, never assumed no-outage negatives.
Historical disaster features stop before the register's observation window.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

import shapely
from shapely.geometry import Point
from pyproj import Transformer

from extract_natural_disasters import (
    FILES, GEOD, extract_cyclones, extract_fires, load_localities,
    unpack_gpkg, years_before,
)


WA_TIME = timezone(timedelta(hours=8))
OFFSETS = {"AEST": 10, "AEDT": 11}
SITE_PROJECT = Transformer.from_crs(4326, 9473, always_xy=True).transform
MODEL_FEATURES = [
    "population_2021", "population_density_2021_per_km2", "locality_area_km2",
    "optus_site_count_2025", "optus_site_density_2025_per_km2",
    "nearest_optus_site_km", "optus_sites_within_5km", "optus_sites_within_10km",
    "recorded_bushfire_count_before_window", "recorded_bushfires_per_year_before_window",
    "nearby_cyclone_track_count_before_window",
]


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def write_csv(path, rows):
    if not rows:
        raise ValueError(f"No rows for {path.name}")
    temporary = path.with_suffix(".csv.partial")
    with temporary.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def normalize_place(text):
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    text = re.sub(r"\s*\(wa\)\s*$", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def split_values(text):
    return sorted({value.strip() for value in re.split(r"[;,]", text) if value.strip()})


def timestamp(day, clock):
    time_text, abbreviation = clock.strip().split()
    if abbreviation not in OFFSETS:
        raise ValueError(f"Unknown timezone: {abbreviation}")
    return datetime.strptime(day.strip() + " " + time_text, "%d/%m/%Y %H:%M").replace(
        tzinfo=timezone(timedelta(hours=OFFSETS[abbreviation]))
    )


def duration_minutes(value):
    match = re.fullmatch(r"\s*(\d+)\.(\d{2})\s*", value)
    if not match or int(match[2]) >= 60:
        raise ValueError(f"Invalid hours.minutes duration: {value!r}")
    return int(match[1]) * 60 + int(match[2])


def clean_event(raw):
    start = timestamp(raw["Start date of outage"], raw["Start time of outage"])
    end = timestamp(raw["End date of outage"], raw["End time of outage"])
    actual = round((end - start).total_seconds() / 60)
    if actual < 0:
        raise ValueError(f"Negative outage duration: {raw['Reference']}")
    declared = duration_minutes(raw["Duration of outage"])
    states = split_values(raw["Affected states/territories"])
    towns = list(dict.fromkeys(t.strip() for t in raw["Affected suburbs/towns"].split(";") if t.strip()))
    services = raw["Number of each type of services affected"]
    def count(name):
        match = re.search(re.escape(name) + r"\s+(\d+)\b", services)
        return int(match[1]) if match else None
    start_wa = start.astimezone(WA_TIME)
    return {
        "event_id": raw["Reference"].strip(), "outage_type": raw["Outage type"].strip(),
        "start_utc": start.astimezone(timezone.utc).isoformat(),
        "end_utc": end.astimezone(timezone.utc).isoformat(),
        "start_wa": start_wa.isoformat(), "start_date_wa": start_wa.date().isoformat(),
        "end_wa": end.astimezone(WA_TIME).isoformat(),
        "duration_minutes": actual, "duration_hours": round(actual / 60, 6),
        "reported_duration_minutes": declared, "duration_matches": int(actual == declared),
        "affected_states": ";".join(states), "geographic_scope": "wa_only" if states == ["WA"] else "multi_state",
        "towns_reported": ";".join(towns),
        "postcodes_reported_unpaired": ";".join(split_values(raw["Affected postcodes"])),
        "mobile_data_services_affected": count("Mobile data"),
        "mobile_voice_services_affected": count("Mobile voice"),
        "cause_high_level": raw["Cause (high level)"].strip(),
        "weather_related_diagnostic_only": {"Yes": 1, "No": 0}.get(raw["Cause (related to natural event or weather)"].strip()),
        "first_published_date": datetime.strptime(raw["Date and time entry first published"], "%d/%m/%Y").date().isoformat(),
    }


def match_locations(events, localities):
    lookup = defaultdict(list)
    for row in localities:
        lookup[normalize_place(row["locality"])].append(row)
    links = []
    for event in events:
        for town in event["towns_reported"].split(";"):
            candidates = lookup.get(normalize_place(town), [])
            if event["geographic_scope"] != "wa_only":
                status = "multi_state_requires_review"
            elif len(candidates) == 1:
                status = "matched_exact_normalized"
            elif candidates:
                status = "ambiguous_name"
            else:
                status = "unmatched_name"
            match = candidates[0] if status == "matched_exact_normalized" else None
            links.append({
                "event_id": event["event_id"], "town_reported": town,
                "match_status": status, "sal_code_2021": match["sal_code_2021"] if match else "",
                "locality": match["locality"] if match else "",
                "positive_label_eligible": int(match is not None),
            })
    return links


def tower_features(path, geometries, rows, stats):
    source = json.loads(path.read_text(encoding="utf-8-sig"))
    unique = defaultdict(set)
    all_positions, position_ids = [], []
    for feature in source["features"]:
        p = feature["properties"]
        if p["Year"] != 2025 or p["MNO"] != "Optus":
            raise ValueError("Expected only 2025 Optus sites")
        identifier = p["RFNSA_ID"]
        if identifier is None:
            raise ValueError("Missing RFNSA site identifier")
        position = tuple(feature["geometry"]["coordinates"][:2])
        if position not in unique[identifier]:
            all_positions.append(position)
            position_ids.append(identifier)
        unique[identifier].add(position)
    stats["unique_optus_sites"] = len(unique)
    stats["site_ids_with_multiple_source_positions"] = sorted(k for k, positions in unique.items() if len(positions) > 1)
    tree = shapely.STRtree(geometries)
    counts = [set() for _ in rows]
    site_lons, site_lats = zip(*all_positions)
    for (lon, lat), identifier in zip(all_positions, position_ids):
        # Source site coordinates are EPSG:4326; project explicitly, not as GDA2020.
        point = Point(SITE_PROJECT(lon, lat))
        matches = tree.query(point, predicate="intersects")
        if len(matches):
            counts[min(matches)].add(identifier)
        else:
            stats["sites_outside_spatial_localities"] += 1
    result = []
    for index, row in enumerate(rows):
        _, _, distances = GEOD.inv(
            [float(row["longitude"])] * len(all_positions), [float(row["latitude"])] * len(all_positions), site_lons, site_lats
        )
        nearest_by_id = {}
        for identifier, distance in zip(position_ids, distances):
            nearest_by_id[identifier] = min(distance, nearest_by_id.get(identifier, math.inf))
        site_distances = list(nearest_by_id.values())
        result.append({
            "optus_site_count_2025": len(counts[index]),
            "optus_site_density_2025_per_km2": round(len(counts[index]) / float(row["locality_area_km2"]), 8),
            "nearest_optus_site_km": round(min(site_distances) / 1000, 6),
            "optus_sites_within_5km": sum(d <= 5000 for d in site_distances),
            "optus_sites_within_10km": sum(d <= 10000 for d in site_distances),
        })
    return result


def modelling_rows(rows, links, events, tower, fires, cyclones, start, cutoff, end):
    labels = defaultdict(set)
    for link in links:
        if link["positive_label_eligible"]:
            labels[link["sal_code_2021"]].add(link["event_id"])
    result = []
    for index, row in enumerate(rows):
        reports = labels[row["sal_code_2021"]]
        result.append({
            **{k: row[k] for k in ["sal_code_2021", "locality", "longitude", "latitude", "population_2021", "locality_area_km2", "population_density_2021_per_km2"]},
            **tower[index],
            "historical_feature_cutoff_utc": cutoff.isoformat(),
            "historical_feature_start_utc": start.isoformat(),
            "recorded_bushfire_count_before_window": len(fires[index]),
            "recorded_bushfires_per_year_before_window": round(len(fires[index]) / 5, 6),
            "nearby_cyclone_track_count_before_window": len(cyclones[index]),
            "label_window_start_wa": min(e["start_date_wa"] for e in events),
            "label_window_end_exclusive_wa": end.isoformat(),
            "reported_outage_label": 1 if reports else None,
            "label_status": "reported_positive" if reports else "unlabelled",
            "matched_report_count_diagnostic_only": len(reports),
            "source_event_ids_diagnostic_only": ";".join(sorted(reports)),
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root, stats = args.root.resolve(), Counter()
    directory = root / "data/processed"
    disaster_path = directory / "wa_natural_disasters.csv"
    metadata = json.loads(disaster_path.with_suffix(".metadata.json").read_text())
    if hashlib.sha256(disaster_path.read_bytes()).hexdigest() != metadata["csv_sha256"]:
        raise ValueError("Disaster table does not match its metadata checksum")
    as_of = date.fromisoformat(metadata["parameters"]["as_of_exclusive"])
    raw = read_csv(root / "data/raw/optus-outage-register-17_10_2026.csv")
    events, seen = [], {}
    for row in raw:
        if "WA" not in split_values(row["Affected states/territories"]) or "Mobile" not in row["Types of services affected"]:
            continue
        event = clean_event(row)
        if event["start_date_wa"] >= as_of.isoformat():
            stats["future_events_excluded"] += 1
            continue
        if event["first_published_date"] >= as_of.isoformat():
            stats["reports_published_after_cutoff_excluded"] += 1
            continue
        if event["event_id"] in seen:
            if event != seen[event["event_id"]]:
                raise ValueError("Conflicting duplicate outage reference; review source revisions")
            stats["duplicate_events_removed"] += 1
            continue
        seen[event["event_id"]] = event
        events.append(event)
    if not events:
        raise ValueError("No WA mobile outage reports")
    events.sort(key=lambda e: (e["start_utc"], e["event_id"]))
    localities = read_csv(disaster_path)
    links = match_locations(events, localities)
    stats["location_match_status_counts"] = dict(Counter(r["match_status"] for r in links))
    # Stop historical covariates before the earliest WA observation day. The
    # UTC date is floored conservatively because fire records have day precision.
    first_day = min(date.fromisoformat(e["start_date_wa"]) for e in events)
    cutoff = datetime.combine(first_day, datetime.min.time(), WA_TIME).astimezone(timezone.utc).date()
    start = years_before(cutoff, 5)
    end = max(date.fromisoformat(e["start_date_wa"]) for e in events) + timedelta(days=1)
    cache = root / ".cache/disaster_extract"
    abs_rows, geometries = load_localities(unpack_gpkg(root / FILES["localities"], cache), set(), stats)
    # Both readers sort ABS codes; verify alignment rather than assuming it.
    if [r["sal_code_2021"] for r in abs_rows] != [r["sal_code_2021"] for r in localities]:
        raise ValueError("ABS geometry and feature rows are not aligned")
    if cutoff <= date(2025, 1, 31):
        raise ValueError("Observation window begins before the available tower snapshot")
    print("Calculating Optus site density and distances...", flush=True)
    towers = tower_features(root / "data/raw/accc/optus_sites_wa_envelope_2025-01-31.geojson", geometries, localities, stats)
    print(f"Rebuilding historical features strictly before {cutoff} UTC...", flush=True)
    fires, fire_dates = extract_fires(
        unpack_gpkg(root / FILES["fire_history"], cache), start, cutoff, geometries, shapely.STRtree(geometries), stats
    )
    cyclones, cyclone_dates, _, _ = extract_cyclones(root / FILES["cyclones"], start, cutoff, localities, 100, stats)
    model = modelling_rows(localities, links, events, towers, fires, cyclones, start, cutoff, end)
    for row in model:
        for feature in MODEL_FEATURES:
            if row[feature] not in (None, "") and not math.isfinite(float(row[feature])):
                raise ValueError(f"Non-finite model feature {feature}")
    current = [{**row, **towers[i], "rainfall_and_local_wind_included": False} for i, row in enumerate(localities)]
    outputs = {
        "wa_locality_features.csv": current,
        "wa_outages_clean.csv": events,
        "wa_outage_location_matches.csv": links,
        "wa_outage_modelling.csv": model,
    }
    for name, rows in outputs.items():
        write_csv(directory / name, rows)
    report = {
        "as_of_date": str(as_of), "raw_outage_records": len(raw), "wa_mobile_reports": len(events),
        "wa_only_reports": sum(e["geographic_scope"] == "wa_only" for e in events),
        "duration_mismatches": [e["event_id"] for e in events if not e["duration_matches"]],
        "positive_localities": sum(r["label_status"] == "reported_positive" for r in model),
        "unlabelled_localities": sum(r["label_status"] == "unlabelled" for r in model),
        "confirmed_negative_localities": 0, "supervised_binary_training_ready": False,
        "available_experiment": "positive-unlabelled retrospective locality modelling; not a calibrated next-day outage forecast",
        "target_column": "reported_outage_label", "target_values": {"1": "Matched WA-only mobile outage report in supplied register", "blank": "Unknown; absence of a report is not confirmed normal service"},
        "model_features": MODEL_FEATURES,
        "history_start_inclusive_utc": str(start), "history_cutoff_exclusive_utc": str(cutoff),
        "label_window_start_wa": str(first_day), "label_window_end_exclusive_wa": str(end),
        "quality_counts": dict(stats),
        "outputs": {name: {"rows": len(rows), "bytes": (directory / name).stat().st_size,
                          "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()} for name, rows in outputs.items()},
        "excluded_from_model_inputs": ["Outage causes, affected-service counts, duration, report IDs and matched-report counts are diagnostics/labels only.",
            "Current hazard-zone percentages and October-2026 fire/cyclone aggregates are in the current planning table only. They are excluded from historical model inputs.",
            "Rainfall and local daily wind are excluded by user choice. Cyclone-centre winds are not substituted for local wind."],
        "limitations": ["The register is a selected set of reported incidents, not complete network availability. Unlabelled localities can contain unreported outages.",
            "Multi-state reports lack per-town state assignment and are held for review. Unmatched names are not fuzzily assigned.",
            "Five-year fire/cyclone features use event dates before the observation window, but current source snapshots may contain later revisions and do not reconstruct publication availability.",
            "Census population is 2021 and infrastructure is January 2025. Site proximity is measured at locality interior representative points, not every resident's address.",
            "All sites in the supplied bounding-rectangle extract are used for nearest/radius searches. Only sites intersecting a locality count towards its area density.",
            "Site counts are distinct RFNSA identifiers, not confirmed physical towers. Multiple coordinates for one identifier are preserved; radius counts use the minimum distance per identifier. An identifier with positions in different localities can count once in each locality.",
            "Hazard-only history omits live disaster conditions. This table cannot establish a time-specific disaster outage probability.",
            "No row split, imputation, scaling, invented negative labels or model fitting is performed. Fit preprocessing on a future training split only."]
    }
    (directory / "outage_data_quality.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["wa_mobile_reports", "positive_localities", "unlabelled_localities", "supervised_binary_training_ready"]}), flush=True)


if __name__ == "__main__":
    main()
