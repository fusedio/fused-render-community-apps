"""Audit the catalog against the files it describes.

Everything lens shows is derived: a date read out of EXIF, a city name from an
offline geocode, a trip inferred from gaps in time, a ranking computed from
vectors on disk. Each of those is a claim about photos the user can open
themselves, and none of them is checked by anything else — an index run that
silently wrote the wrong date is indistinguishable, from inside the view, from
one that got it right.

These checks re-derive the claims and compare. They are deliberately *sampled*
rather than exhaustive: a full pass over a real library is minutes of work, and
a check nobody waits for is a check nobody runs. Every result carries the sample
size it was measured on, so the page can say how much the number is worth.
"""

import os
import random
import time
from collections import Counter
from datetime import datetime

import numpy as np

from lens import metadata, query, trips
from lens.thumbs import thumb_path

# Sample sizes, tuned so the whole run stays inside a few seconds on the
# reference library (1,696 images / 86 photographs). The EXIF check dominates:
# it re-opens files and re-runs the reverse geocode.
EXIF_SAMPLE = 25
RETRIEVAL_SAMPLE = 20
EMB_SAMPLE = 25

# A stored vector is normalized (see embed._normalize) and then rounded to
# float16, so its norm is 1 give or take the rounding — never exactly 1, and
# never far off it. Anything outside this band is a vector that was not
# normalized, not a rounding artefact.
NORM_TOL = 0.02

# The size a trip has to reach to be one. Taken from compute_trips rather than
# restated: this check exists to catch the two halves of lens disagreeing, and a
# second copy of the number here could only ever disagree *about the rule*, which
# is not the disagreement it is looking for. (It used to be a private 3 against a
# compute_trips that had no rule at all — which is how the audit found that
# two-photo trips were being emitted and then hidden by the view.)
TRIP_MIN = trips.MIN_PHOTOS

# How many disagreements travel back with a check. Enough to show the shape of
# the problem in an expandable panel; not so many that a systematically broken
# library sends a megabyte of JSON.
MAX_DETAILS = 10

# Which catalog columns the EXIF check re-derives. sha1 and size are excluded
# deliberately: they are read straight off the file rather than interpreted, so
# comparing them measures the filesystem, not lens.
EXIF_FIELDS = ("taken_at", "camera", "lat", "lon")

_PHOTOS_WHERE = "error IS NULL AND is_photo = 1"


def _sample(rows, n, rng):
    rng = rng or random.Random()
    return rng.sample(list(rows), min(n, len(rows)))


def _agree(field, a, b):
    """Do the catalog's value and the freshly-derived one say the same thing?

    Coordinates are floats that went through sqlite's REAL and back, so they
    are compared with a tolerance rather than for identity — a difference of
    1e-9 degrees is the storage round-trip, not a different place."""
    if field in ("lat", "lon"):
        if a is None or b is None:
            return False
        return abs(float(a) - float(b)) < 1e-6
    return a == b


def exif_consistency(store, sample_n: int = EXIF_SAMPLE, rng=None) -> dict:
    """Re-extract EXIF for a sample of photos and compare with the catalog.

    Only files still on disk are sampled: a photo on an unmounted drive stays
    in the library on purpose (see indexer.index_once), and it cannot disagree
    with a file nothing can read.

    A field neither side claims is not a comparison — a photo with no GPS
    agrees about nothing there, and counting that as a pass would inflate every
    score by however much of the library carries no coordinates."""
    rows = [r for r in store.query_photos(_PHOTOS_WHERE, [])
            if os.path.isfile(r["path"])]
    sample = _sample(rows, sample_n, rng)
    agreed = compared = unreadable = 0
    details = []
    for r in sample:
        try:
            fresh = metadata.extract(r["path"])
        except Exception as exc:
            # The catalog says this row is fine; the file says otherwise. That
            # is a finding, not a crash — it is exactly what this check is for.
            unreadable += 1
            if len(details) < MAX_DETAILS:
                details.append({"path": r["path"], "field": "readable",
                                "catalog": "no error", "fresh": str(exc)[:120]})
            continue
        for field in EXIF_FIELDS:
            a, b = r.get(field), fresh.get(field)
            if a is None and b is None:
                continue
            compared += 1
            if _agree(field, a, b):
                agreed += 1
            elif len(details) < MAX_DETAILS:
                details.append({"path": r["path"], "field": field,
                                "catalog": a, "fresh": b})
    # An unreadable file is a failure of the whole row, not of one field, so it
    # is counted as one comparison that did not agree.
    compared += unreadable
    return {"score": (agreed / compared) if compared else None,
            "pass": unreadable == 0 and agreed == compared,
            "sampled_n": len(sample),
            "compared": compared, "agreed": agreed, "unreadable": unreadable,
            "population": len(rows),
            "details": details}


def retrieval_sanity(store, known_places, sample_n: int = RETRIEVAL_SAMPLE,
                     rng=None) -> dict:
    """Can a photo be found by the place and month the catalog claims for it?

    This is the one check that exercises the query path end to end: the words
    are built from the catalog's own columns, run through the real parser and
    the real WHERE builder, and the photo they describe either comes back or it
    does not. A structured query needs no embedding, so nothing here depends on
    the model being loaded.

    A residual — a leftover word the parser could not place — means the daemon
    would also have ranked the results semantically and cut the weak tail, so
    the recall measured here is the *filter's*, an upper bound on what the user
    would have seen. Counted and reported rather than hidden."""
    rows = store.query_photos(
        _PHOTOS_WHERE + " AND place_city IS NOT NULL AND taken_at IS NOT NULL",
        [])
    sample = _sample(rows, sample_n, rng)
    cameras = store.distinct("camera")
    found = residual = 0
    details = []
    for r in sample:
        try:
            when = datetime.fromisoformat(r["taken_at"])
        except (TypeError, ValueError):
            continue
        q = f"{r['place_city']} {when.strftime('%b %Y')}"
        pq = query.parse(q, known_places(q), cameras)
        if pq.residual:
            residual += 1
        where, params = query.build_where(pq, "photos")
        ids = {row["id"] for row in store.query_photos(where, params)}
        if r["id"] in ids:
            found += 1
        elif len(details) < MAX_DETAILS:
            details.append({"path": r["path"], "query": q,
                            "matched": len(ids), "place": r["place_city"],
                            "taken_at": r["taken_at"]})
    n = len(sample)
    return {"score": (found / n) if n else None,
            "pass": n > 0 and found == n,
            "sampled_n": n, "found": found, "residual": residual,
            "population": len(rows),
            "details": details}


def embedding_integrity(store, cache, sample_n: int = EMB_SAMPLE,
                        rng=None) -> dict:
    """The vectors on disk, checked against the catalog and against themselves.

    Three separate things can be wrong, and they fail differently:
      * the id list and the matrix disagree in length — the two halves of one
        fact, and a mismatch means every vector after the first gap belongs to
        the wrong photo;
      * a vector is not unit length — cosine ranking is a dot product, so an
        unnormalized row outranks everything by being longer, not by being
        closer;
      * a row has a vector but no thumbnail on disk — the vector was computed
        from an image the view can no longer show."""
    ids, mat = store.load_embeddings()
    counts = store.scope_counts()
    total = counts["all"]
    aligned = len(ids) == mat.shape[0]
    coverage = (len(ids) / total) if total else None

    details = []
    norms_ok = norms_n = 0
    thumbs_ok = thumbs_n = 0
    if aligned and len(ids):
        picked = _sample(range(len(ids)), sample_n, rng)
        for i in picked:
            norms_n += 1
            norm = float(np.linalg.norm(mat[i].astype(np.float32)))
            if abs(norm - 1.0) <= NORM_TOL:
                norms_ok += 1
            elif len(details) < MAX_DETAILS:
                details.append({"id": int(ids[i]), "field": "norm",
                                "value": round(norm, 4)})
            row = store.get_photo_by_id(int(ids[i]))
            # A row with no sha1 never got as far as a thumbnail, so there is
            # nothing to look for; that is the error path, not this check's.
            if not row or not row.get("sha1"):
                continue
            thumbs_n += 1
            if thumb_path(cache, row["sha1"]).exists():
                thumbs_ok += 1
            elif len(details) < MAX_DETAILS:
                details.append({"id": int(ids[i]), "field": "thumb",
                                "value": row["sha1"]})

    parts = [1.0 if aligned else 0.0]
    if norms_n:
        parts.append(norms_ok / norms_n)
    if thumbs_n:
        parts.append(thumbs_ok / thumbs_n)
    if coverage is not None:
        parts.append(min(1.0, coverage))
    return {"score": sum(parts) / len(parts),
            "pass": aligned and norms_ok == norms_n and thumbs_ok == thumbs_n
                    and (coverage is None or coverage >= 1.0),
            "sampled_n": norms_n,
            "aligned": aligned, "ids": int(len(ids)),
            "rows": int(mat.shape[0]), "dims": int(mat.shape[1]) if mat.ndim > 1 else 0,
            "embedded": int(len(ids)), "total": total,
            "coverage": coverage,
            "norms_ok": norms_ok, "thumbs_ok": thumbs_ok, "thumbs_n": thumbs_n,
            "details": details}


def trips_invariants(store) -> dict:
    """Do the stored trips still follow the rules that made them?

    Two rules, and the interesting one is not the size. `compute_trips` is
    re-run over the catalog as it stands now and its assignments compared with
    the stored ones: they must agree, because the same photos through the same
    gap-and-distance rule can only produce one answer. They stop agreeing when
    the catalog has moved on without the trips being rebuilt — a photo added,
    a folder removed — and that is a stale-derivation bug no view could show.

    The size rule lives in the view (a two-photo "trip" is a heading with more
    weight than its contents), so a trip below it is a disagreement between
    the two halves of lens rather than bad data."""
    stored = store.get_trips()
    # The rows the indexer computed these trips from — photographs *and* videos —
    # taken from trips.py rather than spelled again here. With `_PHOTOS_WHERE`
    # this check re-derived the trips from a smaller set than made them, so a
    # segment that only reaches three items by counting a video came back
    # unassigned and every photo in it was reported as a mismatch: an audit
    # failing on a correct library, which is worse than no audit.
    photos = store.query_photos(trips.TRIP_ROWS_WHERE, [])
    counts = Counter(r["trip_id"] for r in photos if r["trip_id"] is not None)
    undersized = [{"id": t["id"], "name": t["name"], "photos": counts.get(t["id"], 0)}
                  for t in stored if counts.get(t["id"], 0) < TRIP_MIN]

    recomputed, assign = trips.compute_trips(photos)
    mismatch = 0
    details = []
    for r in photos:
        want = assign.get(r["id"])
        if r["trip_id"] != want:
            mismatch += 1
            if len(details) < MAX_DETAILS:
                details.append({"path": r["path"], "stored": r["trip_id"],
                                "recomputed": want})
    n = len(photos)
    agreement = ((n - mismatch) / n) if n else None
    ok_size = (len(stored) - len(undersized)) / len(stored) if stored else None
    parts = [p for p in (agreement, ok_size) if p is not None] or [1.0]
    return {"score": sum(parts) / len(parts),
            "pass": mismatch == 0 and not undersized,
            "sampled_n": n,                    # exhaustive: every photo row
            "trips": len(stored), "recomputed": len(recomputed),
            "mismatch": mismatch, "agreement": agreement,
            "undersized": undersized,
            "details": details + [{"undersized": u} for u in undersized[:MAX_DETAILS]]}


def faces_integrity(store, sample_n: int = EMB_SAMPLE, rng=None) -> dict:
    """The faces and the people, checked against each other.

    Four things can be wrong here, and they are not variations of one thing:

      * **the face table and faces.npz disagree** — a face row with no vector
        cannot be clustered (so its person is missing a photograph), and a vector
        with no row is a vector of a face nothing can show. The two are written
        one after the other per photo, so a kill lands exactly here, and the
        repair is a re-detect of that photo;
      * **a vector is not unit length** — the clustering is a dot product, so a
        long vector is "similar" to everything and drags strangers into a person;
      * **a face points at a person who does not exist**, or at one that has been
        merged away. Person ids are in URLs and carry names; a dangling one is a
        card the People view cannot draw and a filter that returns nothing;
      * **a person's cover face is not one of their faces** — the cover is what
        the card shows, so a cover belonging to somebody else is the most visible
        error this whole feature can make.

    Coverage — how much of the library the face pass has been over — is reported
    and deliberately **not** scored. The embedding check does score its coverage,
    because a photograph with no vector is unreachable by search: a promise the
    view makes and the index has not kept. A photograph nobody has looked for
    faces in makes no promise at all; it is simply not in the People view yet. So
    the number is shown (it is what stops "3 people" from reading as "you know
    three people") without a partly-finished sweep being reported as a fault.
    """
    ids, mat = store.load_faces()
    rows = store.all_faces()
    counts = store.face_counts()
    have = {int(i) for i in ids}
    table = {r["id"] for r in rows}
    aligned = len(ids) == (mat.shape[0] if mat.ndim > 1 else len(mat))
    missing = sorted(table - have)                # rows with no vector
    orphans = sorted(have - table)                # vectors with no row
    coverage = ((counts["scanned"] / counts["eligible"])
                if counts["eligible"] else None)

    details = []
    for fid in missing[:MAX_DETAILS]:
        details.append({"face": fid, "field": "vector", "value": "missing"})
    for fid in orphans[:MAX_DETAILS]:
        details.append({"face": fid, "field": "row", "value": "missing"})

    # Every person a face claims must exist and must not have been merged away.
    people = {p["id"] for p in store.get_persons()}
    dangling = sorted({r["cluster_id"] for r in rows
                       if r["cluster_id"] is not None
                       and r["cluster_id"] not in people})
    for pid in dangling[:MAX_DETAILS]:
        details.append({"person": pid, "field": "person", "value": "no row"})

    # ...and every person's cover has to be one of their own faces.
    owner = {r["id"]: r["cluster_id"] for r in rows}
    bad_cover = []
    for p in store.get_persons():
        cover = p.get("cover_face_id")
        if cover is None or owner.get(cover) != p["id"]:
            # A person whose faces have all gone keeps their row (that is how a
            # name survives a missing drive), and their cover went with the
            # faces. Not a finding — there is nothing to be wrong about.
            if not any(v == p["id"] for v in owner.values()):
                continue
            bad_cover.append(p["id"])
            if len(details) < MAX_DETAILS * 2:
                details.append({"person": p["id"], "field": "cover",
                                "value": cover})

    norms_ok = norms_n = 0
    if aligned and len(ids):
        for i in _sample(range(len(ids)), sample_n, rng):
            norms_n += 1
            norm = float(np.linalg.norm(mat[i].astype(np.float32)))
            if abs(norm - 1.0) <= NORM_TOL:
                norms_ok += 1
            elif len(details) < MAX_DETAILS * 2:
                details.append({"face": int(ids[i]), "field": "norm",
                                "value": round(norm, 4)})

    linked = 1.0 if not table else (len(table & have) / len(table))
    parts = [1.0 if aligned else 0.0, linked,
             1.0 if not dangling else 0.0,
             1.0 if not bad_cover else 0.0]
    if norms_n:
        parts.append(norms_ok / norms_n)
    return {"score": sum(parts) / len(parts),
            "pass": (aligned and not missing and not orphans and not dangling
                     and not bad_cover and norms_ok == norms_n),
            "sampled_n": norms_n,
            "aligned": aligned, "faces": len(table), "vectors": len(have),
            "missing_vectors": len(missing), "orphan_vectors": len(orphans),
            "dangling_persons": len(dangling), "bad_covers": len(bad_cover),
            "norms_ok": norms_ok,
            "people": counts["people"], "named": counts["named"],
            "clustered": counts["clustered"],
            "scanned": counts["scanned"], "eligible": counts["eligible"],
            "coverage": coverage,
            "details": details[:MAX_DETAILS * 2]}


CHECKS = ("exif_consistency", "retrieval_sanity", "embedding_integrity",
          "trips_invariants", "faces_integrity")


def run(store, cache, known_places, rng=None) -> dict:
    """Every check, plus the composite the page puts in its ring.

    A check that raised is reported as a check that raised. Half an audit is
    worth more than a 500, and "this check could not run" is itself the kind of
    thing an audit exists to surface — so a failure is caught here, scored 0,
    and carries its own message."""
    t0 = time.monotonic()
    runners = {
        "exif_consistency": lambda: exif_consistency(store, rng=rng),
        "retrieval_sanity": lambda: retrieval_sanity(store, known_places, rng=rng),
        "embedding_integrity": lambda: embedding_integrity(store, cache, rng=rng),
        "trips_invariants": lambda: trips_invariants(store),
        "faces_integrity": lambda: faces_integrity(store, rng=rng),
    }
    checks = {}
    for name in CHECKS:
        try:
            checks[name] = runners[name]()
        except Exception as exc:
            checks[name] = {"score": 0.0, "pass": False, "sampled_n": 0,
                            "error": str(exc)[:300], "details": []}
    scores = [c["score"] for c in checks.values() if c.get("score") is not None]
    counts = store.scope_counts()
    return {"composite": (sum(scores) / len(scores)) if scores else None,
            "checks": checks,
            "library": {"images": counts["all"], "photos": counts["photos"],
                        "videos": counts["videos"],
                        "trips": len(store.get_trips()),
                        "faces": store.face_counts()},
            "elapsed_ms": round((time.monotonic() - t0) * 1000)}
