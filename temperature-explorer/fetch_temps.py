"""Fetch a historical daily temperature series for one point on Earth.

`index.html` calls this via `fused.runPython("./fetch_temps.py", {...})` when the
user drops a marker and hits Run. It returns the same normalized shape for both
data sources so the page never has to care which one it asked for:

    {
      "source": {id, label, resolution_deg, grid_lat, grid_lon, elevation_m, fetch_s, note},
      "start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
      "time": [ISO days...], "tmax": [...], "tmin": [...], "tmean": [...]   # daily degC
    }

Two sources, switchable in the UI:

  * "open_meteo"  - Open-Meteo's ERA5 archive (https://open-meteo.com/), a free
    no-login REST endpoint serving the ERA5 / ERA5-Land reanalysis point-optimized.
    High resolution, instant, deep history (1940-present). This is the default,
    and returns synchronously in ~1-2s.

  * "era5_zarr"   - ERA5 read straight from a public cloud-native Zarr store on
    Google Cloud (WeatherBench2), anonymously, with xarray. This is the
    "cloud-native format from a GCS bucket" path. A point series over gridded
    Zarr is bandwidth-bound and the cold read (imports + open_zarr + read) runs
    well past runPython's 30s cap, so this path is ASYNC: the first call spawns a
    detached worker that writes the result to a small on-disk cache and returns
    {"status": "warming"}; the page polls until the cache lands. Repeat queries
    for the same point/range are served straight from the cache, instantly. The
    store is a coarse global grid (~5.6 deg) so the read stays bounded; the UI's
    provenance line shows the actual grid cell so the coarseness is visible.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DAILY_VARS = "temperature_2m_max,temperature_2m_min,temperature_2m_mean"

# WeatherBench2's downsampled ERA5, public + anonymous. 64x32 (~5.6 deg) is
# chunked along time, so a point time-series is a handful of reads per year.
ERA5_ZARR = "gs://weatherbench2/datasets/era5/1959-2023_01_10-6h-64x32_equiangular_conservative.zarr"
ZARR_MAX_YEARS = 5
ZARR_RES_DEG = 5.625
ZARR_MIN_YEAR, ZARR_MAX_YEAR = 1959, 2023  # the store's actual coverage

# the fused-render runner exec()s this script without setting __file__, but puts
# the script's own directory first on sys.path; run as a worker subprocess it's a
# normal script and __file__ is set
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path(sys.path[0]).resolve()
_SCRIPT = _HERE / "fetch_temps.py"
CACHE = _HERE / "data" / "zarr_cache"
LOCK_STALE_S = 180


def main(lat, lon, source="open_meteo", start_year=1990, end_year=None):
    lat, lon = float(lat), float(lon)
    end_year = int(end_year) if end_year else dt.date.today().year
    start_year = int(start_year)
    if source == "era5_zarr":
        return _zarr_async(lat, lon, start_year, end_year)
    return _from_open_meteo(lat, lon, start_year, end_year)


# --------------------------------------------------------------------------- #
#  Open-Meteo — synchronous REST                                              #
# --------------------------------------------------------------------------- #
def _from_open_meteo(lat, lon, start_year, end_year):
    end = min(dt.date(end_year, 12, 31), dt.date.today() - dt.timedelta(days=6))
    start = min(dt.date(start_year, 1, 1), end)
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": DAILY_VARS, "timezone": "auto",
    }
    url = "https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(params)
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "temperature-studio/1.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=90).read())
    daily = d["daily"]
    return {
        "source": {
            "id": "open_meteo", "label": "Open-Meteo · ERA5 reanalysis",
            "resolution_deg": 0.25, "grid_lat": round(d["latitude"], 4),
            "grid_lon": round(d["longitude"], 4), "elevation_m": d.get("elevation"),
            "fetch_s": round(time.time() - t0, 2), "note": "",
        },
        "start": daily["time"][0], "end": daily["time"][-1],
        "time": daily["time"],
        "tmax": _clean(daily["temperature_2m_max"]),
        "tmin": _clean(daily["temperature_2m_min"]),
        "tmean": _clean(daily["temperature_2m_mean"]),
    }


# --------------------------------------------------------------------------- #
#  ERA5 Zarr on GCS — async: detached worker + on-disk cache + polling         #
# --------------------------------------------------------------------------- #
def _cap(start_year, end_year):
    if end_year < ZARR_MIN_YEAR or start_year > ZARR_MAX_YEAR:
        raise RuntimeError(
            f"The ERA5 Zarr store only covers {ZARR_MIN_YEAR}-{ZARR_MAX_YEAR}; "
            f"{start_year}-{end_year} doesn't overlap it. Pick years in that range, "
            f"or use the Open-Meteo source (1940-present)."
        )
    # clamp to the store's actual coverage FIRST, so the span cap below anchors
    # on the store's real last year instead of a future end_year the store
    # doesn't have (which would otherwise silently shrink the window)
    clamped_start, clamped_end = max(start_year, ZARR_MIN_YEAR), min(end_year, ZARR_MAX_YEAR)
    note = f"clamped to the store's {ZARR_MIN_YEAR}-{ZARR_MAX_YEAR} coverage" \
        if (clamped_start, clamped_end) != (start_year, end_year) else ""
    if clamped_end - clamped_start + 1 > ZARR_MAX_YEARS:
        clamped_start = clamped_end - ZARR_MAX_YEARS + 1
        span_note = f"span capped to {ZARR_MAX_YEARS} yrs ({clamped_start}-{clamped_end}) for the cloud read"
        note = f"{span_note}; {note}" if note else span_note
    return clamped_start, clamped_end, note


def _key(lat, lon, y0, y1):
    return hashlib.md5(f"{round(lat, 2)}_{round(lon, 2)}_{y0}_{y1}".encode()).hexdigest()[:12]


def _zarr_async(lat, lon, start_year, end_year):
    _require_zarr_deps()
    start_year, end_year, note = _cap(start_year, end_year)
    CACHE.mkdir(parents=True, exist_ok=True)
    key = _key(lat, lon, start_year, end_year)
    res, err, lock = CACHE / f"{key}.json", CACHE / f"{key}.error", CACHE / f"{key}.lock"

    if res.exists():
        return json.loads(res.read_text(encoding="utf-8"))
    if err.exists():
        msg = err.read_text(encoding="utf-8")
        err.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
        raise RuntimeError(msg)
    try:
        fresh = lock.exists() and time.time() - lock.stat().st_mtime < LOCK_STALE_S
    except FileNotFoundError:
        fresh = False
    if not fresh:
        lock.write_text(str(time.time()), encoding="utf-8")
        _spawn_worker(key, lat, lon, start_year, end_year, note)
    return {"status": "warming", "key": key}


def _require_zarr_deps():
    """The Zarr source needs zarr + xarray + gcsfs in whatever Python runs
    runPython. They're in the dev .venv, but the packaged desktop app's bundled
    Python may not have them all — surface that plainly instead of installing
    anything."""
    import importlib.util as u
    missing = [m for m in ("zarr", "xarray", "gcsfs") if u.find_spec(m) is None]
    if missing:
        raise RuntimeError(
            f"The ERA5 Zarr source needs these Python packages, which are missing "
            f"in this fused-render's Python ({sys.executable}): {', '.join(missing)}. "
            f"Install them there and retry — or use the Open-Meteo source, which "
            f"needs nothing extra."
        )


def _spawn_worker(key, lat, lon, start_year, end_year, note):
    """Run the slow read fully detached, so it survives past runPython's 30s cap.

    Same idiom as the templates (docs/latex/usd): DETACHED_PROCESS keeps it off
    the parent's console (no window) and CREATE_NEW_PROCESS_GROUP frees it from
    the parent on Windows; start_new_session is the POSIX equivalent. PYTHONPATH/
    PYTHONHOME are scrubbed so the worker imports this venv's stack, not an
    inherited app-bundle one (mirrors geotiff/tile_server)."""
    detach = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {"start_new_session": True}
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    subprocess.Popen(
        [sys.executable, str(_SCRIPT), "__worker__",
         key, str(lat), str(lon), str(start_year), str(end_year), note],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, cwd=str(CACHE.parent.parent), **detach,
    )


def _worker(key, lat, lon, start_year, end_year, note):
    res, err, lock = CACHE / f"{key}.json", CACHE / f"{key}.error", CACHE / f"{key}.lock"
    try:
        out = _from_era5_zarr(lat, lon, start_year, end_year, note)
        tmp = res.with_suffix(".tmp")
        tmp.write_text(json.dumps(out), encoding="utf-8")
        os.replace(tmp, res)
    except BaseException as e:
        err.write_text(f"{type(e).__name__}: {e}", encoding="utf-8")
    finally:
        lock.unlink(missing_ok=True)


def _from_era5_zarr(lat, lon, start_year, end_year, note):
    import numpy as np
    import xarray as xr

    t0 = time.time()
    ds = xr.open_zarr(ERA5_ZARR, storage_options={"token": "anon"}, chunks=None, decode_timedelta=True)
    lons = ds["longitude"].values
    tgt = lon % 360
    nearest_lon = float(lons[np.abs((lons - tgt + 180.0) % 360.0 - 180.0).argmin()])
    point = ds["2m_temperature"].sel(latitude=lat, longitude=nearest_lon, method="nearest")
    point = point.sel(time=slice(f"{start_year}-01-01", f"{end_year}-12-31")).load()

    celsius = point - 273.15
    tmax = celsius.resample(time="1D").max()
    tmin = celsius.resample(time="1D").min()
    tmean = celsius.resample(time="1D").mean()

    days = [str(d)[:10] for d in tmax.time.values]
    grid_lon = ((float(point.longitude) + 180) % 360) - 180
    cell_km = round(ZARR_RES_DEG * 111)
    note = (note + "; " if note else "") + f"coarse ~{ZARR_RES_DEG}° grid (~{cell_km} km cell), 6-hourly"
    return {
        "source": {
            "id": "era5_zarr", "label": "ERA5 · Zarr on GCS (WeatherBench2)",
            "resolution_deg": ZARR_RES_DEG, "grid_lat": round(float(point.latitude), 3),
            "grid_lon": round(grid_lon, 3), "elevation_m": None,
            "fetch_s": round(time.time() - t0, 2), "note": note,
        },
        "start": days[0], "end": days[-1], "time": days,
        "tmax": _round(tmax.values), "tmin": _round(tmin.values), "tmean": _round(tmean.values),
    }


def _clean(values):
    return [None if v is None else round(float(v), 1) for v in values]


def _round(arr):
    return [None if (v is None or math.isnan(v)) else round(float(v), 1) for v in arr]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "__worker__":
        _, _, key, la, lo, y0, y1, *rest = sys.argv
        _worker(key, float(la), float(lo), int(y0), int(y1), rest[0] if rest else "")
    else:
        src = sys.argv[1] if len(sys.argv) > 1 else "open_meteo"
        out = main(52.52, 13.41, source=src, start_year=2019, end_year=2023)
        if out.get("status") == "warming":
            print("warming… (worker spawned)")
        else:
            s = out["source"]
            print(f"{s['label']}  grid=({s['grid_lat']},{s['grid_lon']})  {s['fetch_s']}s  note='{s['note']}'")
            print(f"{out['start']} -> {out['end']}  ndays={len(out['time'])}  tmax[0:3]={out['tmax'][:3]}")
