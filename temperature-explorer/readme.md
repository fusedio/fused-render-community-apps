# Temperature Explorer

![Search a place, pick a year range, and slice the daily temperature history on a D3 chart](preview.png)

A sleek, single-screen explorer for the historical daily temperature of any
one point on Earth, read live from free, no-login sources. Search a place
(or click the map) to drop a point, pick a year range and a source, press
Run, and get a D3 chart you can slice by year, date, and week.

## What it demonstrates

Two data sources behind one normalized shape: a synchronous free REST API
(Open-Meteo) and a cloud-native Zarr store read straight from a public GCS
bucket with `xarray` — the latter wired as an async detached-worker + on-disk
cache, since a cold Zarr read runs past `runPython`'s 30 s cap.

## What it does

- **Pick a point** — search (OpenStreetMap Nominatim, no key) or click/drag
  the marker on the map.
- **Two switchable sources, with visible provenance:**
  - **Open-Meteo · ERA5** — point-optimized REST, ~0.25°/9 km, 1940→present,
    returns in ~1–2 s. The default.
  - **ERA5 · Zarr on GCS** — ERA5 read straight from WeatherBench2's public
    Zarr store on Google Cloud, anonymously. Coarse (~5.6°) grid, span capped
    to 5 years. A "Data details" card always shows which source resolved,
    the exact grid cell, elevation, coverage dates, and fetch time.
- **One chart, two y-encodings** — "Compare years" (every selected year on
  the same Jan→Dec axis, plus an opt-in Average line) or "Vs. average" (each
  year's deviation from that average).
- **The year rail** below the chart doubles as legend, year selector (up to
  8 at once), and a live min/mean/max readout for whichever day is under the
  cursor.
- **Hover for a crosshair**, **click to pin** a date (arrow keys step it,
  Shift for ±7 days), toggle **7-day smooth** for a rolling weekly mean.
- **Zoom** into a date range with a video-trim-style bar (vendored
  [noUiSlider](https://refreshless.com/nouislider/), MIT).
- **Spotlight a year** by clicking its swatch in the rail; every other line
  dims. Years are told apart by color plus a fixed stroke pattern/marker
  pair, so they're distinguishable even in grayscale.

## How the Python is wired

The page calls `fused.runPython("./fetch_temps.py", {lat, lon, source,
start_year, end_year})`. Both sources return the same normalized shape
(`{source, start, end, time[], tmax[], tmin[], tmean[]}`).

- **Open-Meteo** is synchronous — one REST call, back in ~1–2 s.
- **ERA5 Zarr** is async: the first call spawns a detached worker that reads
  outside `runPython`'s 30 s window and writes the result to
  `data/zarr_cache/<key>.json`; `runPython` returns `{"status": "warming"}`
  immediately and the page polls every 4 s until the cache lands (~15–40 s the
  first time). Repeat queries for the same point/range are served straight
  from the cache.

## Dependencies

The Zarr source needs `numpy`, `xarray`, `zarr`, and `gcsfs`. They're declared
in this folder's `pyproject.toml`, so fused-render builds the environment up
front on first render (a one-time `uv sync` you wait through) — no manual
install step. The Open-Meteo source is stdlib-only.

## Notes & limits

- The two sources will not agree exactly: Open-Meteo is ~9 km, the Zarr grid
  is ~625 km, so a Zarr cell can sit far from your point (the provenance line
  shows where).
- Zarr daily max/min/mean come from 6-hourly samples (4/day); Open-Meteo's
  are true daily statistics.
- Nominatim search follows OSM's usage policy (debounced, ≤1 req/keystroke-pause).
- The map tiles (CARTO) and geocoding (Nominatim) are fetched from the network
  at runtime; the temperature data comes from Open-Meteo or Google Cloud.
