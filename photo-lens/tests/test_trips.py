from lens.trips import MIN_PHOTOS, compute_trips


def _p(pid, ts, lat, lon, city):
    return {"id": pid, "taken_at": ts, "lat": lat, "lon": lon, "place_city": city}

HOME = (19.07, 72.88)   # Mumbai
BALI = (-8.4, 115.1)

PHOTOS = [
    _p(1, "2025-06-01T10:00:00", *HOME, "Mumbai"),
    _p(2, "2025-06-02T10:00:00", *HOME, "Mumbai"),
    _p(3, "2025-06-03T09:00:00", *HOME, "Mumbai"),
    # gap > 48h, far away → trip
    _p(4, "2025-07-10T08:00:00", *BALI, "Ubud"),
    _p(5, "2025-07-11T08:00:00", *BALI, "Ubud"),
    _p(6, "2025-07-12T08:00:00", *BALI, "Ubud"),
    # gap > 48h, back home → not a trip
    _p(7, "2025-07-20T08:00:00", *HOME, "Mumbai"),
]


def test_trip_detected_and_named():
    trips, assign = compute_trips(PHOTOS)
    assert len(trips) == 1
    t = trips[0]
    assert t["name"] == "Ubud · Jul 2025"
    assert t["start"].startswith("2025-07-10") and t["end"].startswith("2025-07-12")
    assert assign == {4: t["id"], 5: t["id"], 6: t["id"]}


def test_a_trip_needs_more_photos_than_a_stray_pair():
    """One or two photographs far from home are not a trip: a trip is a heading,
    a date range and a card, and that is more weight than two pictures. The view
    always folded them away, so emitting them only made the two halves of lens
    disagree (validate.trips_invariants counted it)."""
    away = [_p(10 + n, f"2025-09-0{n + 1}T08:00:00", *BALI, "Ubud")
            for n in range(MIN_PHOTOS - 1)]
    trips, assign = compute_trips(PHOTOS[:3] + away)
    assert trips == [] and assign == {}

    # ...and one more photograph, inside the same 48-hour chain, is a trip
    away.append(_p(99, "2025-09-03T08:00:00", *BALI, "Ubud"))
    trips, assign = compute_trips(PHOTOS[:3] + away)
    assert len(trips) == 1
    assert len(assign) == MIN_PHOTOS
    assert compute_trips(PHOTOS[:3] + away, min_photos=99) == ([], {})


def test_no_gps_photos_never_crash():
    photos = [_p(1, "2025-01-01T00:00:00", None, None, None),
              _p(2, "2025-01-05T00:00:00", None, None, None)]
    trips, assign = compute_trips(photos)
    assert trips == [] and assign == {}


def test_a_video_with_no_gps_joins_the_trip_it_was_shot_on():
    """A clip taken in the middle of a trip belonged to no trip at all: the
    computation ran over photographs only, and a container that carries no
    location has nothing to measure a distance with anyway. It joins on *time* —
    inside a segment that already qualifies on its photographs' coordinates."""
    clip = _p(50, "2025-07-11T12:00:00", None, None, None)
    trips, assign = compute_trips(PHOTOS + [clip])
    assert len(trips) == 1
    assert assign[50] == trips[0]["id"]
    # ...and one shot at home is still not on a trip
    home_clip = _p(51, "2025-06-02T12:00:00", None, None, None)
    _, assign = compute_trips(PHOTOS + [home_clip])
    assert 51 not in assign


def test_a_dateless_row_cannot_join_anything():
    """Segments are cut on gaps in time, so a row with no `taken_at` has no place
    in the sequence — it is dropped rather than parked in whichever segment it
    was listed beside."""
    trips, assign = compute_trips(PHOTOS + [_p(60, None, None, None, None)])
    assert len(trips) == 1 and 60 not in assign


def test_a_trip_nobody_could_geocode_is_still_a_trip():
    """A coordinate keeps its lat/lon even when the offline geocode fails on it,
    so a segment *can* be a hundred kilometres from home with no place name
    anywhere in it. Taking the commonest of no names raised — out of the middle of
    an index run, so no trips at all were stored, for any of them."""
    away = [dict(_p(70 + n, f"2025-09-0{n + 1}T08:00:00", *BALI, None),
                 place_region=None, place_country=None) for n in range(3)]
    trips, assign = compute_trips(PHOTOS[:3] + away)
    assert len(trips) == 1
    assert trips[0]["place"] == "Away"
    assert trips[0]["name"].startswith("Away · Sep 2025")
    assert len(assign) == 3


def test_a_region_names_a_trip_when_the_city_is_missing():
    """City, then region, then country: the fallback chain answers with the
    biggest thing that is actually known rather than with nothing."""
    away = [dict(_p(80 + n, f"2025-09-0{n + 1}T08:00:00", *BALI, None),
                 place_region="Bali", place_country="ID") for n in range(3)]
    trips, _ = compute_trips(PHOTOS[:3] + away)
    assert trips[0]["place"] == "Bali"
