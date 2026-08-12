# Overture Map Explorer

Explore [Overture Maps](https://overturemaps.org/) open data anywhere on
Earth, straight from its public S3 parquet — no account, no download step.

- **Geocode**: type a place name (Nominatim, cached) to jump the map there.
- **Query**: pick a theme (places, buildings, transportation, …) and an
  optional category/name filter; DuckDB reads only the parquet files that
  intersect your bounding box (pruned via the release's STAC index) and the
  results land on a Leaflet map with counts and stats.
- **Story mode**: a worked example — "what % of cafes are within 100 m of a
  bike path?" — computed live for the current view.

Everything expensive is disk-cached under `./.cache`, so repeat queries are
instant. The Overture release is pinned in `overture.py` for stable results.

Python dependencies (installed on first run into the app's venv):
`duckdb`, `numpy`, `pandas`, `pyproj`, `requests`, `shapely`.
