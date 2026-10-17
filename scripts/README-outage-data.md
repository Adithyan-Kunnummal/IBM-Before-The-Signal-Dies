# WA outage data preparation (without rainfall or local daily wind)

From the repository root, using the environment and requirements prepared for disaster extraction:

```powershell
.venv\Scripts\python.exe scripts/prepare_outage_data.py
.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_*.py"
```

The program reuses the original outage CSV, Optus sites, ABS locality boundaries and the compact disaster table. It checks that the disaster table matches its metadata checksum. It reads the cached original fire GeoPackage and cyclone CSV to rebuild five-year history ending **before** the earliest observed WA outage day. It does not rerun the large hazard-polygon overlays.

## Outputs in data/processed

| File | Purpose |
| --- | --- |
| `wa_locality_features.csv` | Current planning/inference snapshot: all mapped WA localities, existing hazard/population fields, plus infrastructure features. No weather-station features. |
| `wa_outages_clean.csv` | One row per reported WA-involving mobile incident, with UTC/WA timestamps, duration in minutes/hours, separate voice/data service counts and diagnostic cause information. |
| `wa_outage_location_matches.csv` | An auditable link from each reported town to an ABS locality. Unmatched and multi-state names remain unresolved. Town and postcode lists are not assumed to align positionally. |
| `wa_outage_modelling.csv` | One row per locality for **positive-unlabelled retrospective experiments**. Census/tower features and pre-observation-window fire/cyclone history are included. `reported_outage_label=1` means a matched WA-only report exists; blank means unknown. |
| `outage_data_quality.json` | Counts, match results, output hashes, temporal cutoffs, model feature allowlist and training-readiness limitations. |

## Label meaning and safe model inputs

The target is **at least one matched report in the supplied observation window**, not an independently measured probability of service loss tomorrow. This is currently positive-unlabelled data: there are no verified no-outage negatives. Ordinary binary classification, calibrated outage probabilities, accuracy and false-alarm metrics require additional assumptions or independent availability evidence. A positive-unlabelled experiment is possible, but its assumptions and reporting bias must be disclosed. No model is trained here.

Use only the numeric columns explicitly listed in `model_features` in the quality JSON for that experiment. Never feed the label, its status, matched-report counts, source event IDs, outage causes, duration, service counts, or last outage dates into the predictors. The current hazard percentages and October-2026 aggregate histories are retained separately for planning, not silently joined into historical training examples. Fire/cyclone history in the modelling table ends before the observation window begins. Source data still contain revisions unavailable to reconstruct exactly at earlier dates; publication-time checks remain necessary for strict forecasting claims.

These are locality-level records over a historical window, not independent daily samples. Shared disaster episodes and reports covering many towns make random row splits misleading. A future modelling step must group related incidents/areas and fit any imputation, scaling or encoding using the training split only.

## Cleaning definitions

- AEST/AEDT labels in the source are respected even for WA incidents; timestamps are then converted to fixed UTC+08:00 for WA dates. A source date can therefore shift to the previous WA day.
- Values such as `22.24` mean 22 hours 24 minutes. Timestamps determine the cleaned duration and the supplied duration is checked separately.
- Voice/data counts remain separate. They must not be added as unique affected subscribers or copied into each town as local counts.
- Postcodes remain strings. Missing values are blank, never automatic zeros. Exact normalized locality names are used; no fuzzy spelling guesses or unverified aliases are introduced.
- Multi-state reports do not identify the state of each listed town, so they are held for review and cannot supply WA locality labels automatically.
- Infrastructure counts are **distinct RFNSA identifiers**, not proven physical towers. Multiple coordinates for one identifier are preserved. Locality counts deduplicate each identifier; radius counts use the minimum distance to any of its coordinates. A shared identifier can appear in more than one locality if the source positions cross a boundary.
- Radius distances are geodesic distances from the locality's interior representative point. They are not reception guarantees or distances for every resident. Site density is per square kilometre of locality area.
- DBCA counts measure recorded WF/Actual Burnt events; zero does not establish absence of fire. No overlap with mapped flood extents does not establish flood safety.

Rainfall and local wind are deliberately excluded. Historical cyclone-centre wind estimates are not substitutes for daily local weather, and the current dataset cannot predict the timing of a particular storm or site failure.
