"""Data loading, coverage simulation and temporary-unit placement.

Standard library only, so it runs on LinuxONE without extra packages.

Coverage assumption (the ACCC data has no per-site footprints): a locality is
covered when at least one working Optus site lies within the chosen coverage
radius of its representative point.
"""
import bisect
import csv
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"
INDEX_CSV = PROCESSED / "wa_susceptibility_index.csv"
PU_CSV = PROCESSED / "wa_outage_pu_scores.csv"
PU_META = PROCESSED / "wa_outage_pu_model.metadata.json"
INDEX_META = PROCESSED / "wa_susceptibility_index.metadata.json"
SITES_GEOJSON = ROOT / "data" / "raw" / "accc" / "optus_sites_wa_envelope_2025-01-31.geojson"

# Distances are precomputed up to this radius; the UI sliders stay below it.
MAX_RADIUS_KM = 60.0
EARTH_RADIUS_KM = 6371.0088

# Illustrative scenarios for the demo. They are not reconstructions of real events.
PRESET_SCENARIOS = [
    {"id": "perth-hills-bushfire", "name": "Perth Hills bushfire", "lat": -31.90, "lon": 116.17,
     "radius_km": 20, "failure_share": 100},
    {"id": "south-west-storm", "name": "South West storm", "lat": -33.80, "lon": 115.20,
     "radius_km": 60, "failure_share": 60},
    {"id": "kimberley-cyclone", "name": "Kimberley cyclone", "lat": -17.96, "lon": 122.24,
     "radius_km": 150, "failure_share": 100},
    {"id": "wheatbelt-fire", "name": "Wheatbelt grassfire", "lat": -31.48, "lon": 118.28,
     "radius_km": 60, "failure_share": 100},
]


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_localities():
    pu = {row["sal_code_2021"]: row for row in _load_csv(PU_CSV)}
    localities = []
    for row in _load_csv(INDEX_CSV):
        p = pu.get(row["sal_code_2021"], {})
        localities.append({
            "sal": row["sal_code_2021"],
            "name": row["locality"],
            "lat": _float(row["latitude"]),
            "lon": _float(row["longitude"]),
            "population": int(_float(row["population_2021"])),
            "index_band": row["susceptibility_band"],
            "index_rank": int(_float(row["susceptibility_rank"])),
            "index_score": round(_float(row["susceptibility_score"]), 4),
            "hazard_percentile": round(_float(row["hazard_percentile"]), 3),
            "infrastructure_percentile": round(_float(row["infrastructure_percentile"]), 3),
            # Unrounded values for the live post-disaster recalculation.
            "_score": _float(row["susceptibility_score"]),
            "_hazard_pct": _float(row["hazard_percentile"]),
            "_infra_score": _float(row["infrastructure_score"]),
            "bushfire_prone_pct": round(_float(row["bushfire_prone_area_pct_snapshot"]), 1),
            "flood_mapped_pct": round(_float(row["flood_any_mapped_extent_pct_snapshot"]), 1),
            "flood_mapping_status": row["flood_mapping_status"],
            "bushfires_per_year": round(_float(row["recorded_bushfires_per_year"]), 2),
            "cyclone_tracks": int(_float(row["nearby_cyclone_track_count"])),
            "nearest_site_km": round(_float(row["nearest_optus_site_km"]), 2),
            "sites_within_5km": int(_float(row["optus_sites_within_5km"])),
            "sites_within_10km": int(_float(row["optus_sites_within_10km"])),
            "top_factors": row["top_factors"],
            "band_stable": row["band_stable"] == "True",
            "model_band": p.get("pu_band", ""),
            "model_rank": int(_float(p.get("pu_rank"), 0)),
            "model_score": round(_float(p.get("pu_score")), 4),
            "model_top_contributions": p.get("top_upward_contributions", ""),
            "reported_outage": p.get("reported_outage_in_window") == "1",
            "incident_group": p.get("incident_group", ""),
        })
    return localities


def load_sites():
    """One site per RFNSA identifier; an identifier can have several positions."""
    with open(SITES_GEOJSON, encoding="utf-8") as fh:
        features = json.load(fh)["features"]
    sites = {}
    for f in features:
        props = f.get("properties") or {}
        lon, lat = f["geometry"]["coordinates"][:2]
        site_id = str(props.get("RFNSA_ID") or f"obj-{props.get('OBJECTID')}")
        site = sites.setdefault(site_id, {
            "id": site_id,
            "positions": [],
            "co_funded": props.get("Co_funded") == "Y",
            "program": props.get("Co_contribution_program") or "",
        })
        site["positions"].append((lat, lon))
    return list(sites.values())


def _stable_rank(site_id, seed):
    return hashlib.sha256(f"{seed}:{site_id}".encode()).hexdigest()


class Engine:
    def __init__(self):
        self.localities = load_localities()
        self.sites = load_sites()
        self.by_sal = {loc["sal"]: loc for loc in self.localities}
        self.index_of = {loc["sal"]: i for i, loc in enumerate(self.localities)}
        self.site_index = {s["id"]: i for i, s in enumerate(self.sites)}
        self.metadata = {
            "index": json.loads(INDEX_META.read_text()) if INDEX_META.exists() else {},
            "model": json.loads(PU_META.read_text()) if PU_META.exists() else {},
        }
        # Live index recalculation uses the notebook's own scaling and band cut-offs.
        index_meta = self.metadata["index"]
        self.ref_max = index_meta.get("log_scaling_reference_max", {})
        self.w_hazard = index_meta.get("weights", {}).get("hazard", 0.5)
        self.baseline_infra = sorted(loc["_infra_score"] for loc in self.localities)
        self.band_cut = {
            band: min(loc["_score"] for loc in self.localities if loc["index_band"] == band)
            for band in ("High", "Medium")
        }

        # For each locality: (distance_km, site_idx) for sites within MAX_RADIUS_KM, nearest first.
        self.nearby = []
        for loc in self.localities:
            near = []
            for idx, site in enumerate(self.sites):
                lat0, lon0 = site["positions"][0]
                # Cheap bounding check before the exact distance.
                if abs(lat0 - loc["lat"]) > 1.0 or abs(lon0 - loc["lon"]) > 1.2:
                    continue
                d = min(haversine_km(loc["lat"], loc["lon"], lat, lon) for lat, lon in site["positions"])
                if d <= MAX_RADIUS_KM:
                    near.append((d, idx))
            near.sort()
            self.nearby.append(near)

    # ---------- live index recalculation ----------
    def _log_scaled(self, column, value):
        ref = self.ref_max.get(column, 0)
        if ref <= 0:
            return 0.0
        return max(0.0, min(1.0, math.log1p(max(0.0, value)) / math.log1p(ref)))

    def _infra_percentile(self, score):
        # Same convention as pandas rank(pct=True, method="average") against the baseline.
        lo = bisect.bisect_left(self.baseline_infra, score)
        hi = bisect.bisect_right(self.baseline_infra, score)
        return (lo + hi + 1) / (2 * len(self.baseline_infra))

    def _band_for(self, score):
        if score >= self.band_cut["High"]:
            return "High"
        if score >= self.band_cut["Medium"]:
            return "Medium"
        return "Low"

    def _infra_score(self, nearest, within5, within10):
        return (self._log_scaled("nearest_optus_site_km", nearest)
                + (1 - self._log_scaled("optus_sites_within_5km", within5))
                + (1 - self._log_scaled("optus_sites_within_10km", within10))) / 3

    def _working_infrastructure(self, loc_idx, failed):
        """Nearest working site and working-site counts within 5 and 10 km."""
        near = [(d, s) for d, s in self.nearby[loc_idx] if s not in failed]
        if near:
            nearest = near[0][0]
        else:  # nothing working within MAX_RADIUS_KM: scan every site
            loc = self.localities[loc_idx]
            nearest = min(
                (min(haversine_km(loc["lat"], loc["lon"], a, b) for a, b in s["positions"])
                 for i, s in enumerate(self.sites) if i not in failed),
                default=self.ref_max.get("nearest_optus_site_km", 500.0),
            )
        return nearest, sum(1 for d, _ in near if d <= 5), sum(1 for d, _ in near if d <= 10)

    def post_disaster_bands(self, failed):
        """Recalculate infrastructure sparsity for localities near failed sites and re-band them.

        Hazard exposure is unchanged; only the three infrastructure features move.
        """
        if not failed:
            return []
        failed_positions = [pos for i in failed for pos in self.sites[i]["positions"]]
        changes = []
        for idx, loc in enumerate(self.localities):
            reach = max(10.0, loc["nearest_site_km"] + 0.5)
            if min(haversine_km(loc["lat"], loc["lon"], a, b) for a, b in failed_positions) > reach:
                continue
            # Apply only the change the failures cause to the stored score. The app's spherical
            # distances differ slightly from the pipeline's geodesic ones, so recomputing from
            # scratch could move bands even with no failures.
            before = self._infra_score(*self._working_infrastructure(idx, set()))
            nearest, within5, within10 = self._working_infrastructure(idx, failed)
            after = self._infra_score(nearest, within5, within10)
            infra = min(1.0, max(0.0, loc["_infra_score"] + after - before))
            score = self.w_hazard * loc["_hazard_pct"] + (1 - self.w_hazard) * self._infra_percentile(infra)
            # Failures only remove infrastructure, so a band can only rise. Keeping the stored band
            # unless the score rises also respects the notebook's tie-breaks at the cut-offs.
            order = {"Low": 0, "Medium": 1, "High": 2}
            band = self._band_for(score) if after > before else loc["index_band"]
            if order[band] > order[loc["index_band"]]:
                changes.append({
                    "sal": loc["sal"], "name": loc["name"], "population": loc["population"],
                    "band_before": loc["index_band"], "band_after": band,
                    "nearest_site_km_before": loc["nearest_site_km"],
                    "nearest_site_km_after": round(nearest, 2),
                })
        order = {"High": 2, "Medium": 1, "Low": 0}
        changes.sort(key=lambda c: (-(order[c["band_after"]] - order[c["band_before"]]), -c["population"]))
        return changes

    # ---------- coverage ----------
    def _covered(self, loc_idx, radius_km, failed):
        for d, site_idx in self.nearby[loc_idx]:
            if d > radius_km:
                return False
            if site_idx not in failed:
                return True
        return False

    def sites_in_area(self, lat, lon, radius_km):
        return [i for i, s in enumerate(self.sites)
                if min(haversine_km(lat, lon, a, b) for a, b in s["positions"]) <= radius_km]

    def failed_sites(self, hazard=None, manual_ids=(), seed=1):
        """Sites in the hazard area fail at the given share (deterministic), plus manual picks."""
        failed = set()
        if hazard:
            in_area = self.sites_in_area(hazard["lat"], hazard["lon"], hazard["radius_km"])
            share = max(0.0, min(100.0, float(hazard.get("failure_share", 100)))) / 100.0
            ordered = sorted(in_area, key=lambda i: _stable_rank(self.sites[i]["id"], seed))
            failed.update(ordered[:round(len(ordered) * share)])
        for site_id in manual_ids:
            if str(site_id) in self.site_index:
                failed.add(self.site_index[str(site_id)])
        return failed

    def simulate(self, hazard=None, manual_ids=(), coverage_km=10.0, units=0, unit_radius_km=15.0, seed=1):
        coverage_km = max(1.0, min(MAX_RADIUS_KM, float(coverage_km)))
        unit_radius_km = max(1.0, min(MAX_RADIUS_KM, float(unit_radius_km)))
        failed = self.failed_sites(hazard, manual_ids, seed)

        baseline_uncovered, lost = [], []
        for i, loc in enumerate(self.localities):
            if not self._covered(i, coverage_km, set()):
                baseline_uncovered.append(i)
            elif not self._covered(i, coverage_km, failed):
                lost.append(i)

        placed, restored = self.place_units(lost, int(units), unit_radius_km)
        restored_set = {i for unit in placed for i in unit["restored_idx"]}

        def summary(idx):
            loc = self.localities[idx]
            return {"sal": loc["sal"], "name": loc["name"], "population": loc["population"],
                    "index_band": loc["index_band"], "restored": idx in restored_set}

        lost_sorted = sorted(lost, key=lambda i: -self.localities[i]["population"])
        band_changes = self.post_disaster_bands(failed)
        return {
            "band_changes": {
                "raised": sum(1 for c in band_changes if c["band_after"] != c["band_before"]),
                "to_high": sum(1 for c in band_changes if c["band_after"] == "High"),
                "list": band_changes,
            },
            "assumptions": {
                "coverage_km": coverage_km,
                "unit_radius_km": unit_radius_km,
                "rule": "A locality has coverage when a working Optus site is within the coverage radius "
                        "of its representative point.",
            },
            "failed_site_ids": [self.sites[i]["id"] for i in sorted(failed)],
            "sites_failed": len(failed),
            "sites_total": len(self.sites),
            "baseline_without_coverage": {
                "localities": len(baseline_uncovered),
                "residents": sum(self.localities[i]["population"] for i in baseline_uncovered),
            },
            "lost_coverage": {
                "localities": len(lost),
                "residents": sum(self.localities[i]["population"] for i in lost),
                "high_band_localities": sum(1 for i in lost if self.localities[i]["index_band"] == "High"),
                "list": [summary(i) for i in lost_sorted],
            },
            "units": [{k: v for k, v in u.items() if k != "restored_idx"} for u in placed],
            "restored": {
                "localities": len(restored_set),
                "residents": restored,
            },
        }

    def place_units(self, lost, units, unit_radius_km):
        """Greedy placement: each unit goes at the lost locality that restores the most residents."""
        remaining = set(lost)
        placed, total = [], 0
        for _ in range(max(0, min(units, 10))):
            best = None
            for c in remaining:
                cl = self.localities[c]
                covered = [i for i in remaining
                           if haversine_km(cl["lat"], cl["lon"], self.localities[i]["lat"],
                                           self.localities[i]["lon"]) <= unit_radius_km]
                gain = sum(self.localities[i]["population"] for i in covered)
                key = (gain, len(covered), -c)
                if best is None or key > best[0]:
                    best = (key, c, covered)
            if best is None or best[0][0] <= 0:
                break
            _, c, covered = best
            cl = self.localities[c]
            placed.append({
                "lat": cl["lat"], "lon": cl["lon"], "at": cl["name"],
                "restored_localities": len(covered),
                "restored_residents": best[0][0],
                "restored_names": [self.localities[i]["name"] for i in
                                   sorted(covered, key=lambda i: -self.localities[i]["population"])][:8],
                "restored_idx": covered,
            })
            total += best[0][0]
            remaining -= set(covered)
        return placed, total

    # ---------- payloads for the UI ----------
    def localities_payload(self):
        keys = ("sal", "name", "lat", "lon", "population", "index_band", "index_rank",
                "model_band", "model_rank", "reported_outage")
        return [{k: loc[k] for k in keys} for loc in self.localities]

    def sites_payload(self):
        return [{"id": s["id"], "lat": s["positions"][0][0], "lon": s["positions"][0][1],
                 "co_funded": s["co_funded"]} for s in self.sites]

    def summary_payload(self):
        model = self.metadata.get("model", {})
        results = model.get("evaluation_results", {}).get("All 114 reported localities", {})
        return {
            "localities": len(self.localities),
            "sites": len(self.sites),
            "index_weights": self.metadata.get("index", {}).get("weights", {}),
            "model_machine": model.get("versions", {}).get("machine", ""),
            "model_test": {name: results[name] for name in results},
            "presets": PRESET_SCENARIOS,
        }
