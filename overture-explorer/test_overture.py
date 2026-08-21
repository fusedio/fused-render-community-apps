"""Tests for the Overture explorer's STAC release resolution.

Background: overture.py pins an Overture release and reads that release's
`collections.parquet` off the public STAC catalog. Overture only serves a
rolling window of releases, so a hard pin eventually returns HTTP 404
(this is exactly the failure that motivated these tests):

    HTTP GET error on 'https://stac.overturemaps.org/2026-06-17.0/collections.parquet'
    (HTTP 404 Not Found)

These tests exercise _resolve_release()'s fallback and then run a small live
query end-to-end. They hit the network + S3, so run them with:

    uv run --with pytest --with duckdb --with pandas --with numpy \
        --with requests --with shapely --with pyproj pytest test_overture.py -v
"""

import requests

import overture


# A release Overture has already aged out of its STAC catalog — the URL from
# the original bug report. Used to prove resolution skips dead pins.
DEAD_RELEASE = "2026-06-17.0"

# A small San Francisco bbox (well under the per-theme span caps).
SF_BBOX = (-122.435, 37.765, -122.395, 37.795)


def test_dead_release_reproduces_404():
    """The originally-pinned release is genuinely gone (404), which is what
    broke the app. If Overture ever restores it this test can be dropped."""
    r = requests.head(overture._collections_url(DEAD_RELEASE), timeout=15,
                       allow_redirects=True)
    assert r.status_code == 404


def test_available_releases_nonempty_and_reachable():
    releases = overture._available_releases()
    assert releases, "catalog returned no releases"
    # The newest advertised release must have a live collections.parquet.
    assert overture._url_ok(overture._collections_url(releases[0]))


def test_resolve_release_is_reachable():
    overture._resolve_release.cache_clear()
    release = overture._resolve_release()
    assert overture._url_ok(overture._collections_url(release))


def test_resolve_falls_back_when_pin_is_dead(monkeypatch):
    """With the pin set to a dead release, resolution must fall back to a live
    one from the catalog rather than surfacing the 404."""
    monkeypatch.setattr(overture, "OVERTURE_RELEASE", DEAD_RELEASE)
    overture._resolve_release.cache_clear()
    release = overture._resolve_release()
    assert release != DEAD_RELEASE
    assert overture._url_ok(overture._collections_url(release))
    overture._resolve_release.cache_clear()


def test_matching_files_returns_s3_hrefs():
    release = overture._resolve_release()
    files = overture._matching_files("place", release, *SF_BBOX)
    assert files, "no parquet files matched the SF bbox"
    assert all(f.startswith("s3://") for f in files)


def test_count_and_fetch_end_to_end():
    w, s, e, n = SF_BBOX
    count = overture.main(action="count", theme="places",
                          west=w, south=s, east=e, north=n)
    assert count["ok"] and count["total"] > 0
    assert count["release"] == overture._resolve_release()

    fetched = overture.main(action="fetch", theme="places", k=count["k"],
                            west=w, south=s, east=e, north=n)
    assert fetched["ok"] and fetched["count"] > 0
    assert fetched["geojson"]["features"][0]["geometry"]["type"]
