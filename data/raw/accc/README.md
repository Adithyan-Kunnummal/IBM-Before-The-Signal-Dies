# ACCC mobile infrastructure exports

Source: https://spatial.infrastructure.gov.au/server/rest/services/Mobile_Coverages_and_Sites_ACCC/MapServer

Infrastructure snapshot: **31 January 2025**, according to the source service.

## Files

- `optus_sites_wa_envelope_2025-01-31.geojson`: Layer 2, Optus mobile sites.
- `optus_4g_outdoor_coverage_wa_envelope_2025-01-31.geojson`: Layer 13, Optus claimed 4G outdoor coverage.
- Matching `.metadata.json` files record source queries, retrieval times, checksums, layer definitions and completeness checks.
- `_source_parts/` preserves the coverage responses used to assemble the combined file after the server rejected a single large export.

## Geographic selection

Both exports select features intersecting the rectangle from longitude 112 to 129 and latitude -36 to -13, in EPSG:4326. This covers mainland Western Australia and nearby islands; it is **not an exact WA administrative boundary clip**. Coverage polygons crossing the selection edge are retained whole. Exact clipping and selection of a smaller demo region remain preparation steps.

Coordinates and source attributes are retained. Combining coverage batches does not simplify polygons or infer per-site coverage.

## Interpretation

The site records describe infrastructure, not live outages. Coverage polygons describe modelled carrier-wide outdoor 4G coverage, not the footprint of an individual site or a guarantee of reception. Census population from another year must retain its own date label. Any simulated loss or restoration of coverage requires separate, explicit modelling assumptions.

These are local raw-data downloads. No Git commit or upload is performed as part of downloading them.
