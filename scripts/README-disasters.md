# Natural-disaster feature extraction

Run from the repository root with Python 3.11 or newer:

```powershell
python -m pip install -r requirements-disasters.txt
python scripts/extract_natural_disasters.py --as-of 2026-10-17
```

In the prepared local environment, use `.venv\Scripts\python.exe` instead of `python`.

The program expects the five filenames listed in `FILES` in the script, in the repository root. `--input-dir` selects another download directory. It reads the original ZIPs directly or extracts GeoPackages into `.cache/disaster_extract/` for read-only SQLite access. Keep at least 3 GB free for that reusable extraction cache. Streaming avoids loading the full 1 GB bushfire GeoJSON into Python dictionaries at once; a single source feature can still be large.

## Output

`data/processed/wa_natural_disasters.csv` is the single combined dataset. There is one row per spatial 2021 ABS WA suburb/locality, identified by `sal_code_2021`. Non-spatial special records are excluded. A matching metadata JSON records inputs, source URLs, parameters, quality counts, units, limitations and the output checksum. Raw ZIPs, caches and the Python environment are ignored by Git; the compact output and source code can be committed.

The CSV contains:

| Columns | Meaning |
| --- | --- |
| `sal_code_2021`, `locality` | Stable join identifier and name. |
| `longitude`, `latitude` | A representative point inside the locality, not an address or population centre. |
| `population_2021`, `locality_area_km2`, `population_density_2021_per_km2` | Census population and density; area calculated in GDA2020 Australian Albers (EPSG:9473). |
| `as_of_date`, `history_start_date`, `history_years` | History window: start inclusive, cutoff exclusive, using source UTC dates. Default lookback: five calendar years. |
| `bushfire_prone_area_pct_snapshot`, `bushfire_designation_dates` | Percentage of the locality overlapping the supplied designated bushfire zones, and their designation dates. |
| `flood_any_mapped_extent_pct_snapshot` | Union of active supplied flood extents, which can represent different AEP/scenario classes. |
| `flood_1pct_aep_area_pct_snapshot` | Union of active extents specifically labelled 1% AEP / 1 in 100. |
| `flood_mapping_status` | `mapped_overlap` or `unknown_no_overlap`. No overlap does not establish flood safety or complete mapping. |
| `dbca_recorded_bushfire_events`, `dbca_recorded_bushfire_events_per_year`, `dbca_last_recorded_bushfire_date` | Recorded wildfire history intersecting the locality with positive area; excludes prescribed burns and non-actual-burnt geometry types. |
| `dbca_history_completeness` | Always `partial_coverage`; DBCA is not a complete statewide fire register. |
| `cyclone_radius_km`, `nearby_cyclone_track_count`, `last_nearby_cyclone_track_date` | Distinct TYPE=T cyclone IDs with a recorded track point within the radius of the locality representative point. Default radius: 100 km, measured geodesically. |
| `nearby_cyclone_centre_max_wind_ms`, `nearby_cyclone_centre_max_gust_ms` | Maximum values among those nearby historical samples, in metres per second. These are cyclone-centre estimates, **not local daily weather observations**. Blank means unavailable, not zero. |

Overlapping hazard polygons are unioned after clipping, preventing percentages above 100. Source geometry is not simplified. Invalid geometry is repaired with Shapely and counted in metadata. Fire fragments with the same district/season/fire number count once per locality; valid master keys are a fallback. Unresolved identities are excluded and reported instead of inventing extra fires. Counts across localities must not be added to obtain a statewide unique-event total.

This is a **feature snapshot, not a labelled training dataset**. Current hazard snapshots must not be treated as historical maps without checking their versions/publication dates. The history cutoff filters event dates only: it cannot undo later source revisions or reconstruct publication availability. Cyclone tracks are sampled positions, not interpolated paths or impact footprints; developing/weakening phases of TYPE=T systems remain included. Area percentages do not establish the percentage of residents exposed. No generic disaster-prone Y/N flag or outage labels are invented.

Rainfall and local daily wind still require separate weather-station data. The downloaded cyclone CSV cannot substitute for that data. Mobile sites and outage records will be joined separately during the modelling stage.

## Smaller regional extracts and checks

```powershell
python scripts/extract_natural_disasters.py --as-of 2026-10-17 --locality Dunsborough --locality Quindalup --output data/processed/southwest_natural_disasters.csv
python -m unittest discover -s scripts -p "test_extract_natural_disasters.py"
```

Optional: `--history-years 10` or `--cyclone-radius-km 150`. A changed cutoff/radius changes feature meaning; preserve the matching metadata.
