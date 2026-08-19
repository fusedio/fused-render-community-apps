"""Fetch a historical daily temperature series for one point on Earth.

`index.html` calls this via `fused.runPython("./fetch_temps.py", {...})` when the
user drops a marker and hits Run. It returns a normalized shape:

    {
      "source": {id, label, resolution_deg, grid_lat, grid_lon, elevation_m, fetch_s, note},
      "start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
      "time": [ISO days...], "tmax": [...], "tmin": [...], "tmean": [...]   # daily degC
    }

Data comes from Open-Meteo's ERA5 archive (https://open-meteo.com/) — a free,
no-login REST endpoint serving the ERA5 reanalysis point-optimized: high
resolution, instant, deep history (1940-present). Stdlib only, no dependencies.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
import urllib.request

DAILY_VARS = "temperature_2m_max,temperature_2m_min,temperature_2m_mean"


def main(lat, lon, start_year=1990, end_year=None):
    lat, lon = float(lat), float(lon)
    end_year = int(end_year) if end_year else dt.date.today().year
    start_year = int(start_year)
    return _from_open_meteo(lat, lon, start_year, end_year)


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


def _clean(values):
    return [None if v is None else round(float(v), 1) for v in values]


if __name__ == "__main__":
    out = main(52.52, 13.41, start_year=2019, end_year=2023)
    s = out["source"]
    print(f"{s['label']}  grid=({s['grid_lat']},{s['grid_lon']})  {s['fetch_s']}s")
    print(f"{out['start']} -> {out['end']}  ndays={len(out['time'])}  tmax[0:3]={out['tmax'][:3]}")
