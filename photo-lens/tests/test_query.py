from datetime import datetime

import numpy as np

from lens.query import (MIN_KEEP, RELEVANCE_RATIO, build_where,
                        confidence_horizon, parse, rank, text_prompt)

NOW = datetime(2026, 8, 10)
PLACES = ["Ubud", "Mumbai", "New York"]
CAMS = ["Apple iPhone 15 Pro", "Sony A7IV"]


def test_last_year_trip():
    pq = parse("photos from my trip last year", PLACES, CAMS, now=NOW)
    assert pq.date_from == "2025-01-01T00:00:00"
    assert pq.date_to == "2025-12-31T23:59:59.999999"
    assert pq.trip_mode is True
    assert pq.residual == ""            # stopwords + parsed terms all consumed


def test_month_year_and_place():
    pq = parse("ubud july 2025 sunset", PLACES, CAMS, now=NOW)
    assert pq.places == ["Ubud"]
    assert pq.date_from == "2025-07-01T00:00:00"
    assert pq.date_to == "2025-07-31T23:59:59.999999"
    assert pq.residual == "sunset"


def test_multiword_place_and_camera():
    pq = parse("new york shot on iphone", PLACES, CAMS, now=NOW)
    assert pq.places == ["New York"]
    assert pq.cameras == ["Apple iPhone 15 Pro"]
    assert pq.residual == ""


def test_bare_year_and_residual():
    pq = parse("dog 2023", PLACES, CAMS, now=NOW)
    assert pq.date_from == "2023-01-01T00:00:00"
    assert pq.residual == "dog"


def test_build_where():
    pq = parse("ubud july 2025", PLACES, CAMS, now=NOW)
    where, params = build_where(pq)
    assert "taken_at >= ?" in where and "taken_at <= ?" in where
    # a matched name could be a city, a region or a country code
    assert "place_city IN (?)" in where
    assert "place_region IN (?)" in where
    assert "place_country IN (?)" in where
    assert "error IS NULL" in where
    assert params == ["2025-07-01T00:00:00", "2025-07-31T23:59:59.999999",
                      "Ubud", "Ubud", "Ubud"]


def test_scope_photos_filters_on_the_derived_column():
    pq = parse("ubud", PLACES, CAMS, now=NOW)
    where, params = build_where(pq, "photos")
    assert "is_photo = 1" in where
    # the filter is structural, not a bound value — it adds no parameter, so it
    # cannot shift the ones the caller already appended
    assert params == ["Ubud", "Ubud", "Ubud"]
    assert "is_photo" not in build_where(pq, "all")[0]
    assert "is_photo" not in build_where(pq)[0]        # default is everything


def test_the_photographs_scope_holds_no_videos():
    """"Photos" is where the page starts, and a library where every third tile
    wants pressing play is not what that promises. `kind` is stated as well as
    `is_photo`: the second is derived at index time and could drift, the first is
    read off the extension and cannot."""
    pq = parse("", PLACES, CAMS, now=NOW)
    where, params = build_where(pq, "photos")
    assert "is_photo = 1" in where and "kind = 'image'" in where
    assert params == []


def test_the_videos_scope_holds_every_video_and_nothing_else():
    """Not gated on is_photo, which is 0 for every video row: a screen recording
    is as much a video as a clip off a phone, and the toggle promises all of
    them."""
    pq = parse("", PLACES, CAMS, now=NOW)
    where, _ = build_where(pq, "videos")
    assert "kind = 'video'" in where
    assert "is_photo" not in where


def test_every_scope_the_daemon_offers_is_one_this_builder_knows():
    """The daemon validates its query param against SCOPES and falls back to the
    default for anything else, so a name in that tuple with no clause here would
    silently search the whole library."""
    from lens.query import SCOPES
    pq = parse("", PLACES, CAMS, now=NOW)
    for scope in SCOPES:
        where, _ = build_where(pq, scope)
        assert where.startswith("error IS NULL")
        assert (scope == "all") == (where == "error IS NULL")


def test_build_where_narrows_to_one_trip():
    """The trips view holds a row id, not a name, so a trip is not something the
    parser can find in the words — it arrives beside the parsed query and
    composes with everything the words *did* produce: a search typed while a
    trip is open searches within that trip."""
    pq = parse("ubud july 2025 shot on iphone", PLACES, CAMS, now=NOW)
    where, params = build_where(pq, "photos", trip=7)

    assert "trip_id = ?" in where
    # bound, never interpolated — the id arrives off a URL the daemon does not
    # write, so it must reach sqlite as a parameter rather than as text
    assert "7" not in where, where
    assert params == [7, "2025-07-01T00:00:00", "2025-07-31T23:59:59.999999",
                      "Ubud", "Ubud", "Ubud", "Apple iPhone 15 Pro"]
    for clause in ("is_photo = 1", "taken_at >= ?", "place_city IN (?)",
                   "camera IN (?)"):
        assert clause in where, clause

    # no trip changes nothing at all
    assert build_where(pq, "photos", None) == build_where(pq, "photos")
    assert "trip_id" not in build_where(pq, "photos")[0]

    # ...and trip 0 is a trip, not an absent one: the guard is on None, never on
    # truthiness
    where0, params0 = build_where(pq, "photos", 0)
    assert "trip_id = ?" in where0 and params0[0] == 0


ALBUMS = ["Bali 2025", "Best of", "Wedding"]


def test_an_album_name_is_matched_as_a_phrase():
    """Apple Photos album names and titles are a vocabulary like camera names:
    matched as whole phrases in the query, consumed out of the residual so the
    semantic search never sees them."""
    pq = parse("photos in album Bali 2025", PLACES, CAMS, now=NOW,
               known_albums=ALBUMS)
    assert pq.albums == ["Bali 2025"]
    assert pq.residual == ""          # "album" is scaffolding, not a search term

    where, params = build_where(pq, "photos")
    # instr, not LIKE: an album name is user text and may contain % or _, which
    # LIKE would read as wildcards
    assert "instr(lower(apple_text), lower(?)) > 0" in where
    assert "LIKE" not in where
    assert params == ["Bali 2025"]


def test_two_albums_are_an_or():
    pq = parse("best of wedding", PLACES, CAMS, now=NOW, known_albums=ALBUMS)
    assert sorted(pq.albums) == ["Best of", "Wedding"]
    where, params = build_where(pq)
    assert where.count("instr(lower(apple_text)") == 2
    assert " OR " in where
    assert sorted(params) == ["Best of", "Wedding"]


def test_an_album_wins_over_a_place_of_the_same_name():
    """An album is a label the user wrote on a set of photographs; a place name
    is what an offline geocode guessed from a coordinate. When both answer to the
    same word, the one somebody chose deliberately wins."""
    pq = parse("ubud", PLACES, CAMS, now=NOW, known_albums=["Ubud", "Bali 2025"])
    assert pq.albums == ["Ubud"] and pq.places == []


def test_an_album_that_is_only_a_date_never_swallows_one():
    """Albums are matched before dates, so an album called "July 2025" or "2025"
    would take every query about that month or year away from the date filter —
    a high price for one folder's name."""
    albums = ["2025", "July 2025", "Bali 2025"]
    pq = parse("july 2025", PLACES, CAMS, now=NOW, known_albums=albums)
    assert pq.albums == []
    assert pq.date_from == "2025-07-01T00:00:00"

    pq = parse("dog 2025", PLACES, CAMS, now=NOW, known_albums=albums)
    assert pq.albums == [] and pq.residual == "dog"
    assert pq.date_from == "2025-01-01T00:00:00"

    # ...while a name that says something as well as a date is still a name
    pq = parse("bali 2025", PLACES, CAMS, now=NOW, known_albums=albums)
    assert pq.albums == ["Bali 2025"]
    assert pq.date_from is None and pq.residual == ""


def test_no_album_vocabulary_changes_nothing():
    """A library with no Apple Photos in it must produce exactly the query it
    always did — no clause, no parameter, nothing to shift the others."""
    q = "ubud july 2025 sunset"
    assert parse(q, PLACES, CAMS, now=NOW) == parse(q, PLACES, CAMS, now=NOW,
                                                    known_albums=[])
    pq = parse(q, PLACES, CAMS, now=NOW, known_albums=None)
    assert pq.albums == []
    assert "apple_text" not in build_where(pq, "photos")[0]


def test_rank_orders_by_cosine():
    rows = [{"id": 1}, {"id": 2}, {"id": 3}]
    ids = np.array([1, 2, 3], dtype=np.int64)
    mat = np.array([[1, 0], [0, 1], [0.7, 0.7]], dtype=np.float16)
    ranked = rank(rows, ids, mat, np.array([0, 1], dtype=np.float16), limit=2)
    assert [r["id"] for r in ranked] == [2, 3]
    assert ranked[0]["score"] > ranked[1]["score"]


def test_rank_keeps_unembedded_rows_at_tail():
    rows = [{"id": 1}, {"id": 99}, {"id": 2}]
    ids = np.array([1, 2], dtype=np.int64)
    mat = np.array([[1, 0], [0, 1]], dtype=np.float16)
    ranked = rank(rows, ids, mat, np.array([0, 1], dtype=np.float16), limit=10)
    assert [r["id"] for r in ranked] == [2, 1, 99]
    assert ranked[0]["score"] is not None
    assert ranked[1]["score"] is not None
    assert ranked[-1].get("score") is None


def test_region_name_is_matched_as_a_place():
    """"bali" is an admin1 region, not a city — the user's literal query."""
    places = ["Tua", "Bali", "ID", "Mumbai", "IN"]
    pq = parse("bali", places, CAMS, now=NOW)
    assert pq.places == ["Bali"]
    assert pq.residual == ""
    where, params = build_where(pq)
    assert "place_region IN (?)" in where
    assert params == ["Bali", "Bali", "Bali"]


def test_country_code_that_is_a_stop_word_is_not_matched():
    """"IN" (India) must not swallow the "in" of "photos in bali" and drag
    every Indian photo into a Bali search."""
    places = ["Bali", "ID", "Mumbai", "IN"]
    pq = parse("photos in bali", places, CAMS, now=NOW)
    assert pq.places == ["Bali"]
    assert "IN" not in pq.places
    assert pq.residual == ""


def test_no_limit_returns_everything():
    """The daemon ranks unlimited so it can report a true total, then slices."""
    rows = [{"id": i} for i in range(1, 4)]
    ids = np.array([1, 2, 3], dtype=np.int64)
    mat = np.array([[1, 0], [0, 1], [0.7, 0.7]], dtype=np.float16)
    ranked = rank(rows, ids, mat, np.array([0, 1], dtype=np.float16), limit=None)
    assert len(ranked) == 3


def _vecs(scores):
    """(rows, ids, mat, tvec) where row i scores exactly `scores[i]`."""
    n = len(scores)
    rows = [{"id": i + 1} for i in range(n)]
    ids = np.array([i + 1 for i in range(n)], dtype=np.int64)
    mat = np.array([[s, 0] for s in scores], dtype=np.float16)
    return rows, ids, mat, np.array([1, 0], dtype=np.float16)


def test_ratio_drops_weak_matches_but_keeps_the_cluster():
    """The cut is a *fraction* of the top score, not a fixed margin behind it:
    an absolute margin is meaningless when the whole score range shrinks, which
    is what SigLIP's sigmoid-space similarities do (top hits ~0.1)."""
    rows, ids, mat, tvec = _vecs([0.100, 0.080, 0.055, 0.039, 0.039])
    ranked = rank(rows, ids, mat, tvec, limit=None, ratio=0.5, min_keep=1)

    # cut at 0.05 — the 0.039 plateau of unrelated files is gone
    assert [r["id"] for r in ranked] == [1, 2, 3]
    assert all(r["score"] >= ranked[0]["score"] * 0.5 for r in ranked)
    # without a ratio the weak rows are merely last, not gone
    assert len(rank(rows, ids, mat, tvec, limit=None)) == 5


def test_ratio_scales_with_the_top_score():
    """The same shape of result set survives whether the model's scores live
    around 0.5 or around 0.05 — which a fixed margin could never do."""
    for k in (1.0, 0.1, 0.01):
        rows, ids, mat, tvec = _vecs([1.0 * k, 0.6 * k, 0.2 * k])
        ranked = rank(rows, ids, mat, tvec, limit=None,
                      ratio=RELEVANCE_RATIO, min_keep=1)
        assert [r["id"] for r in ranked] == [1, 2], k


def test_min_keep_saves_a_weak_signal_query_from_emptiness():
    """A query whose own best hit is mediocre still has a best hit. Cutting to
    one result there reads as "lens found nothing"; offer the top few."""
    scores = [0.9] + [0.01] * 20            # everything but #1 is far behind
    rows, ids, mat, tvec = _vecs(scores)
    ranked = rank(rows, ids, mat, tvec, limit=None, ratio=RELEVANCE_RATIO)
    assert len(ranked) == MIN_KEEP
    assert ranked[0]["id"] == 1                       # best hit still first
    assert all(r["score"] is not None for r in ranked)
    # ...and min_keep is a floor, not a cap: a genuinely broad match keeps all
    rows, ids, mat, tvec = _vecs([0.9] * 20)
    assert len(rank(rows, ids, mat, tvec, limit=None,
                    ratio=RELEVANCE_RATIO)) == 20


def test_min_keep_cannot_invent_rows():
    rows, ids, mat, tvec = _vecs([0.9, 0.01])
    ranked = rank(rows, ids, mat, tvec, limit=None, ratio=RELEVANCE_RATIO)
    assert len(ranked) == 2                 # only two exist


def test_ratio_keeps_unembedded_rows_at_the_tail():
    """An unscored row means "not indexed yet", not "not relevant" — the cut is
    about relevance, so it must not silently hide un-indexed photos."""
    rows = [{"id": 1}, {"id": 2}, {"id": 99}]
    ids = np.array([1, 2], dtype=np.int64)
    mat = np.array([[0.9, 0], [0.01, 0]], dtype=np.float16)
    ranked = rank(rows, ids, mat, np.array([1, 0], dtype=np.float16),
                  limit=None, ratio=0.5, min_keep=1)
    assert [r["id"] for r in ranked] == [1, 99]
    assert ranked[-1]["score"] is None


def test_no_positive_signal_falls_back_to_the_closest_few():
    """All-negative scores give no meaningful top hit to take a fraction of —
    `top × ratio` would sit *above* the top score and cut everything. Neither
    "no matches" nor "the whole library matches" is right; the closest few are.
    """
    rows, ids, mat, tvec = _vecs([-0.1] * 40)
    ranked = rank(rows, ids, mat, tvec, limit=None, ratio=RELEVANCE_RATIO)
    assert len(ranked) == MIN_KEEP
    assert all(r["score"] < 0 for r in ranked)

    # ...and a library smaller than MIN_KEEP still comes back whole
    rows, ids, mat, tvec = _vecs([-0.5, -0.9])
    ranked = rank(rows, ids, mat, tvec, limit=None, ratio=RELEVANCE_RATIO)
    assert [r["id"] for r in ranked] == [1, 2]


def test_region_and_date_combine():
    places = ["Bali", "ID"]
    pq = parse("bali july 2025", places, CAMS, now=NOW)
    assert pq.places == ["Bali"]
    assert pq.date_from == "2025-07-01T00:00:00"
    assert pq.date_to == "2025-07-31T23:59:59.999999"
    assert pq.residual == ""


# ── the sentence the text tower actually sees ──────────────────────────────
def test_text_prompt_wraps_the_residual_in_a_caption():
    """SigLIP was trained on captions, so a bare noun is off-distribution.
    Measured on the reference library: "beach" 0.1027 → 0.1211 against an
    unmoved noise floor."""
    assert text_prompt("beach") == "a photo of a beach"
    assert text_prompt("golden retriever") == "a photo of a golden retriever"


# ── the confidence horizon ─────────────────────────────────────────────────
def test_confidence_horizon_finds_where_the_answers_stop():
    """The shape of the user's complaint: three real matches, then MIN_KEEP
    padding rendered identically to them. The split has to be nameable."""
    rows, ids, mat, tvec = _vecs([0.103, 0.056, 0.054] + [0.03] * 20)
    ranked = rank(rows, ids, mat, tvec, limit=None, ratio=RELEVANCE_RATIO)
    strong, cutoff = confidence_horizon(ranked)

    assert strong == 3
    assert cutoff == ranked[0]["score"] * RELEVANCE_RATIO
    # everything above the line clears it, everything below it does not
    assert all(r["score"] >= cutoff for r in ranked[:strong])
    assert all(r["score"] < cutoff for r in ranked[strong:])
    # ...and the padding is still *there*, just no longer disguised as an answer
    assert len(ranked) == MIN_KEEP


def test_confidence_horizon_is_zero_when_nothing_is_a_match():
    """No positive top score is no signal to take a fraction of. Saying "0
    strong" is the honest answer; the closest rows still come back so the user
    can judge for themselves."""
    rows, ids, mat, tvec = _vecs([-0.1] * 30)
    ranked = rank(rows, ids, mat, tvec, limit=None, ratio=RELEVANCE_RATIO)
    assert confidence_horizon(ranked) == (0, None)
    assert len(ranked) == MIN_KEEP           # returned, not hidden


def test_confidence_horizon_ignores_unembedded_rows():
    """A row with no vector is "not indexed", not "not relevant" — it can
    neither be strong nor set the boundary."""
    rows = [{"id": 1, "score": 0.9}, {"id": 2, "score": 0.2},
            {"id": 99, "score": None}]
    assert confidence_horizon(rows, ratio=0.5) == (1, 0.45)
    assert confidence_horizon([{"id": 1, "score": None}]) == (0, None)
    assert confidence_horizon([]) == (0, None)


def test_month_range_covers_an_mtime_derived_timestamp():
    """A photo with no EXIF capture date must still be findable by its month.

    `taken_at` falls back to the file's mtime (metadata.extract), and
    isoformat() writes that with microseconds. The bound is compared as a
    *string* in SQL, so a plain "T23:59:59" end-of-month sorted BELOW
    "…T23:59:59.500000" and a photo taken in the final second of a month was
    unreachable by a search for that month — while being perfectly reachable by
    a search for the year, which made it look like a parser bug rather than a
    boundary one.
    """
    pq = parse("july 2025", PLACES, CAMS, now=NOW)
    for taken in ("2025-07-31T23:59:59.500000", "2025-07-31T23:59:59",
                  "2025-07-01T00:00:00", "2025-07-15T12:00:00.000001"):
        assert pq.date_from <= taken <= pq.date_to, taken
    # ...and the bound still stops at the month: the next month's first instant
    # must not sort inside it.
    assert not ("2025-08-01T00:00:00" <= pq.date_to)


def test_year_range_covers_an_mtime_derived_timestamp():
    """Same boundary, one level up — 31 December's final second."""
    pq = parse("2025", PLACES, CAMS, now=NOW)
    assert pq.date_from <= "2025-12-31T23:59:59.999999" <= pq.date_to
    assert pq.date_from <= "2025-12-31T23:59:59.500000" <= pq.date_to
    assert not ("2026-01-01T00:00:00" <= pq.date_to)


# ── people ─────────────────────────────────────────────────────────────────
def test_a_named_person_is_read_out_of_the_words():
    """"photos of Ana" is the whole point of letting the user type a name: a name
    joins the vocabulary the way an album name does, and the rest of the query
    still parses around it."""
    pq = parse("photos of ana costa in ubud last year", PLACES, CAMS, now=NOW,
               known_people=["Ana Costa", "Ben"])
    assert pq.people == ["Ana Costa"]
    assert pq.places == ["Ubud"] and pq.date_from == "2025-01-01T00:00:00"
    assert pq.residual == ""


def test_a_person_wins_over_an_album_of_the_same_name():
    """A person's name is the most specific thing here — the user typed it
    against a face — and an album called "Ana" is the same intention anyway."""
    pq = parse("ana", PLACES, CAMS, now=NOW, known_albums=["Ana"],
               known_people=["Ana"])
    assert pq.people == ["Ana"] and pq.albums == []


def test_a_person_called_2025_does_not_swallow_every_year():
    """Same exemption albums get: nobody is called "July 2025", and swallowing
    every bare year in every query would be far too high a price if they were."""
    pq = parse("july 2025", PLACES, CAMS, now=NOW, known_people=["July 2025"])
    assert pq.people == [] and pq.date_from == "2025-07-01T00:00:00"


def test_two_people_narrow_rather_than_widen():
    """Each clause here narrows, and two faces asked for together means "both of
    them in one photograph" — which is also what makes typing a second name
    inside somebody's grid useful."""
    pq = parse("", PLACES, CAMS, now=NOW)
    where, params = build_where(pq, "photos", None, [3, 7])
    assert where.count("SELECT photo_id FROM faces") == 2
    assert " AND ".join(where.split(" AND ")[-2:]) == (
        "id IN (SELECT photo_id FROM faces WHERE cluster_id = ?) AND "
        "id IN (SELECT photo_id FROM faces WHERE cluster_id = ?)")
    assert params == [3, 7]


def test_a_person_filter_composes_with_everything_else():
    pq = parse("ubud july 2025", PLACES, CAMS, now=NOW)
    where, params = build_where(pq, "photos", 4, [9])
    assert "trip_id = ?" in where and "taken_at >= ?" in where
    assert "cluster_id = ?" in where
    assert params[0] == 4 and params[-1] == 9


def test_no_people_adds_no_clause():
    pq = parse("beach", PLACES, CAMS, now=NOW)
    for people in (None, []):
        where, params = build_where(pq, "photos", None, people)
        assert "faces" not in where and params == []
