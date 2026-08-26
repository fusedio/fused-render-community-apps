import math
from collections import Counter
from datetime import datetime


def _haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


# Which catalog rows a trip is computed from, as a WHERE clause.
#
# Photographs and videos, and nothing else. A graphic's `taken_at` is its file
# mtime, so a screenshot saved between two real trips bridged the 48-hour gap and
# merged them into one, and any export could stretch a trip's date range past its
# last photo — which is why is_photo exists and why this clause is not simply
# "error IS NULL". A video is the opposite case: it was shot, at a real instant,
# usually on the same day trip as the photographs around it (and, when its
# container carries a GPS fix, in the same place — see video._iso6709). Leaving
# them out meant a clip taken in the middle of a trip belonged to no trip.
#
# It lives here, beside the rule, because two callers need exactly this set and a
# second spelling of it is a disagreement waiting to happen: the indexer computes
# the trips from these rows and validate.trips_invariants re-computes them from
# the same rows to check the stored answer is still current. Two different row
# sets would make that audit fail on a correct library, for ever.
TRIP_ROWS_WHERE = "error IS NULL AND (is_photo = 1 OR kind = 'video')"

# Fewest photographs that can make a trip.
#
# A trip is a heading, a date range, a card in the trips view and a filter in the
# search box — and a "trip" of one or two photos is all of that weight over less
# content than a single row of the grid. A video counts towards it: it is a tile
# in that grid like any other, and the rule is about how much there is to look
# at, not about which decoder produced it. The view has always folded them back
# into "not part of a trip", so emitting them here only ever produced a
# disagreement between the two halves of lens (validate.trips_invariants counts
# it). The rule belongs where the trips are made.
MIN_PHOTOS = 3


def _place_name(gps):
    """What to call a trip: the commonest place name among the rows that carry a
    coordinate.

    City, then region, then country, then a label that admits it does not know.
    The fallback chain is not decoration — a coordinate keeps its lat/lon even
    when the offline geocode fails on it (metadata._place_from), so a segment
    *can* be a hundred kilometres from home with no name anywhere in it. Taking
    `most_common(1)[0]` of that empty Counter raised, and the exception came out
    of the middle of an index run: no trips at all were stored, for any of them.
    """
    for col in ("place_city", "place_region", "place_country"):
        names = Counter(p[col] for p in gps if p.get(col))
        if names:
            return names.most_common(1)[0][0]
    return "Away"


def compute_trips(photos, gap_hours: float = 48, min_km: float = 100,
                  min_photos: int = MIN_PHOTOS):
    dated = [p for p in photos if p.get("taken_at")]
    dated.sort(key=lambda p: p["taken_at"])
    cities = [p["place_city"] for p in dated if p.get("place_city")]
    if not cities:
        return [], {}
    home_city = Counter(cities).most_common(1)[0][0]
    home_pts = [(p["lat"], p["lon"]) for p in dated
                if p.get("place_city") == home_city and p.get("lat") is not None]
    if not home_pts:
        # place names without coordinates (a geocode that outlived the GPS
        # tags, or a hand-tagged city): no home point, so no distance to
        # measure a trip against.
        return [], {}
    home = (_median([x for x, _ in home_pts]), _median([y for _, y in home_pts]))

    segments, seg, prev = [], [], None
    for p in dated:
        t = datetime.fromisoformat(p["taken_at"])
        if prev is not None and (t - prev).total_seconds() > gap_hours * 3600:
            segments.append(seg)
            seg = []
        seg.append(p)
        prev = t
    if seg:
        segments.append(seg)

    trips, assign, tid = [], {}, 0
    for seg in segments:
        if len(seg) < min_photos:
            continue
        gps = [p for p in seg if p.get("lat") is not None]
        if not gps or not any(
                _haversine_km((p["lat"], p["lon"]), home) > min_km for p in gps):
            continue
        tid += 1
        place = _place_name(gps)
        start = seg[0]["taken_at"]
        mon = datetime.fromisoformat(start).strftime("%b %Y")
        trips.append({"id": tid, "name": f"{place} · {mon}", "start": start,
                      "end": seg[-1]["taken_at"], "place": place})
        for p in seg:
            assign[p["id"]] = tid
    return trips, assign
