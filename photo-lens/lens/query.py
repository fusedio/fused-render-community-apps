import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_STOP = {"photos", "photo", "pictures", "picture", "pics", "pic", "images",
         "image", "from", "my", "of", "the", "a", "in", "at", "on", "show",
         "me", "all", "with", "taken", "shot",
         # "photos in album Bali" — the word names the vocabulary the next word
         # comes from, so it is scaffolding, not something to search for
         "album", "albums"}

# How relevant a semantic hit must be, as a *fraction* of the top score, to
# count as a match at all.
#
# This used to be an absolute margin behind the top hit (0.10). That is the
# wrong scale for these scores: SigLIP's image/text similarities are small
# positive numbers, so on a real library "beach" topped out at 0.103 and a
# 0.10 margin let everything down to 0.003 through — 681 of 1923 files,
# almost all of them junk. Relative to the top hit the same corpus separates
# cleanly: the real beach shot at 0.103, related water at 0.055, and the
# plateau of unrelated frames at 0.039 (38% of the top).
RELEVANCE_RATIO = 0.5

# ...but a query whose best hit is itself weak has no strong top to measure
# against, and a purely proportional cut can then return one or two rows out of
# a library that does hold plausible answers. Always offer this many, ranked,
# so the user can judge instead of being told "no matches".
MIN_KEEP = 12

# What the text tower is actually asked to encode.
#
# SigLIP was trained on captions, not on bare nouns, so a one-word query sits
# off the distribution its text encoder knows. Wrapping the residual in a
# caption-shaped sentence measurably widens the gap between the real answers
# and the plateau of unrelated frames: on the reference library "beach" went
# from a top score of 0.1027 to 0.1211 while the noise floor stayed put, which
# is exactly the separation the relevance cut is measured against.
#
# Only the *embedding* uses this. Everything the view echoes back — the chip,
# the parsed payload — keeps the user's own words.
TEXT_PROMPT = "a photo of a {}"


def text_prompt(residual: str) -> str:
    """The sentence to embed for `residual` (see TEXT_PROMPT)."""
    return TEXT_PROMPT.format(residual)


def confidence_horizon(rows, ratio: float = RELEVANCE_RATIO):
    """`(strong, cutoff)` for an already-ranked `rows`.

    `strong` is how many leading rows are real matches rather than the
    `MIN_KEEP` padding behind them, and `cutoff` is the score that separates
    them — the same `top × ratio` boundary `rank` cuts on, handed to the view
    so it can *draw* the boundary instead of rendering noise identically to
    answers.

    `(0, None)` means nothing here is strong: either no row was scored at all,
    or the best score is non-positive, which is no signal to take a fraction
    of. That is the honest "no strong matches" case, and it is reachable —
    the rows are still returned, they are just all below the horizon."""
    scored = [r for r in rows if r.get("score") is not None]
    if not scored:
        return 0, None
    top = scored[0]["score"]
    if top <= 0:
        return 0, None
    cut = top * ratio
    return sum(1 for r in scored if r["score"] >= cut), cut


@dataclass
class ParsedQuery:
    date_from: str | None = None
    date_to: str | None = None
    places: list = field(default_factory=list)
    cameras: list = field(default_factory=list)
    # Apple Photos album names and titles the query named (see
    # store.apple_phrases). Called `albums` throughout because that is what one
    # of these is in practice — "album Bali", "Best of 2025".
    albums: list = field(default_factory=list)
    # Names of people the query asked for, matched against the ones the user has
    # actually named in the People view (see store.person_names). Names, not ids:
    # the parser's job is to read the words, and turning a name into the person
    # id a WHERE clause needs is the daemon's (see daemon.run_query).
    people: list = field(default_factory=list)
    trip_mode: bool = False
    residual: str = ""


# The last instant of a day, to the precision `taken_at` can actually carry.
#
# It used to be plain "T23:59:59", and that silently lost photos. `taken_at` is
# compared as a *string* in SQL, and a file with no EXIF capture date falls back
# to its mtime (metadata.extract), which isoformat() writes with microseconds —
# so "2025-07-31T23:59:59.500000" <= "2025-07-31T23:59:59" is false, and a photo
# taken in the last second of a month was unreachable by a search for that
# month. Found by validate.retrieval_sanity, which is what it is for.
_END_OF_DAY = "T23:59:59.999999"


def _month_range(year: int, month: int):
    last = calendar.monthrange(year, month)[1]
    return (f"{year:04d}-{month:02d}-01T00:00:00",
            f"{year:04d}-{month:02d}-{last:02d}{_END_OF_DAY}")


def _year_range(year: int):
    return f"{year:04d}-01-01T00:00:00", f"{year:04d}-12-31{_END_OF_DAY}"


def _consume_phrases(text: str, vocab: list[str]) -> tuple[str, list[str]]:
    """Match known multi-word values case-insensitively; remove from text.

    Vocabulary entries that are themselves stop words are skipped. The place
    vocabulary includes two-letter country codes, and "IN" (India) would
    otherwise swallow the "in" in "photos in bali" and pull in every Indian
    photo alongside the Balinese ones."""
    hits = []
    vocab = [v for v in vocab if v and v.lower() not in _STOP]
    for v in sorted(vocab, key=len, reverse=True):
        pat = re.compile(r"\b" + re.escape(v.lower()) + r"\b")
        if pat.search(text):
            hits.append(v)
            text = pat.sub(" ", text)
    return text, hits


_DATE_WORD = re.compile(r"19\d{2}|20\d{2}|" + "|".join(_MONTHS))


def _datelike(phrase: str) -> bool:
    """Does this phrase say nothing but a date? ("2025", "July 2025".)

    Such a phrase is not offered to the vocabulary matcher, whatever it names:
    the words belong to the date parser, and a query for a month must keep
    meaning the month even for someone who has an album called "July"."""
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    return bool(words) and all(_DATE_WORD.fullmatch(w) for w in words)


def parse(q: str, known_places: list[str], known_cameras: list[str],
          now: datetime | None = None,
          known_albums: list[str] | None = None,
          known_people: list[str] | None = None) -> ParsedQuery:
    now = now or datetime.now()
    pq = ParsedQuery()
    text = " " + re.sub(r"\s+", " ", q.lower().strip()) + " "

    if re.search(r"\btrips?\b", text):
        pq.trip_mode = True
        text = re.sub(r"\btrips?\b", " ", text)

    # Albums go first of every vocabulary, and that order is the answer to a name
    # that is in two of them. An album is a label the *user* wrote on a set of
    # photographs; a place is what an offline geocode guessed from a coordinate,
    # and a date is a pattern. So "Bali 2025" is one phrase the user typed on
    # purpose rather than a place plus a year — which is exactly what it became
    # when dates were consumed first, leaving no album to match at all.
    #
    # `_datelike` is the exemption that makes this safe: an album called "2025"
    # or "July 2025" says nothing but a date, and swallowing every bare year in
    # every query would be a high price for one folder's name.
    # People go before albums, and for the sharper version of the same argument:
    # a person's name is the most specific thing a user can type, because they
    # typed it themselves against a face. An album called "Ana" and a person
    # called Ana are the same intention anyway, and matching the person first is
    # what makes "photos of Ana" mean her face rather than a folder's name.
    #
    # Same `_datelike` exemption as albums: nobody is called "July 2025", and if
    # they were, swallowing every bare year in every query would be far too high
    # a price for it.
    text, pq.people = _consume_phrases(
        text, [p for p in (known_people or []) if not _datelike(p)])

    text, pq.albums = _consume_phrases(
        text, [a for a in (known_albums or []) if not _datelike(a)])

    if re.search(r"\blast year\b", text):
        pq.date_from, pq.date_to = _year_range(now.year - 1)
        text = re.sub(r"\blast year\b", " ", text)
    elif re.search(r"\bthis year\b", text):
        pq.date_from, pq.date_to = _year_range(now.year)
        text = re.sub(r"\bthis year\b", " ", text)
    elif re.search(r"\blast month\b", text):
        y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        pq.date_from, pq.date_to = _month_range(y, m)
        text = re.sub(r"\blast month\b", " ", text)

    m = re.search(r"\b(" + "|".join(_MONTHS) + r")\b(?:\s+(\d{4}))?", text)
    if m and pq.date_from is None:
        month = _MONTHS[m.group(1)]
        year = int(m.group(2)) if m.group(2) else (
            now.year if month <= now.month else now.year - 1)
        pq.date_from, pq.date_to = _month_range(year, month)
        text = text.replace(m.group(0), " ")

    y = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if y and pq.date_from is None:
        pq.date_from, pq.date_to = _year_range(int(y.group(1)))
        text = text.replace(y.group(0), " ")

    text, pq.places = _consume_phrases(text, known_places)
    text, pq.cameras = _consume_phrases(text, known_cameras)
    # loose camera brand match ("iphone" → any camera containing it)
    for word in list(re.findall(r"[a-z0-9]+", text)):
        brand_hits = [c for c in known_cameras if word in c.lower()]
        if brand_hits and word not in _STOP and len(word) > 3:
            pq.cameras.extend(h for h in brand_hits if h not in pq.cameras)
            text = re.sub(r"\b" + re.escape(word) + r"\b", " ", text)

    words = [w for w in re.findall(r"[a-z0-9]+", text) if w not in _STOP]
    pq.residual = " ".join(words)
    return pq


SCOPES = ("photos", "videos", "all")


def build_where(pq: ParsedQuery, scope: str = "all", trip: int | None = None,
                people: list | None = None):
    """`scope` is one of SCOPES; anything else searches everything catalogued.

      * "photos" — camera captures, stills only (see metadata.is_photo). A video
        is never one of these: it is not what "photograph" means, and mixing the
        two would make the default view of a library into a mix of things you
        look at and things you press play on.
      * "videos" — every video, and only videos. Not gated on is_photo, which is
        a claim about stills and is 0 for every video row: a screen recording is
        as much a video as a clip from a phone is, and the toggle promises all of
        them.
      * "all" — everything catalogued, graphics included.

    `trip` narrows to one trip. It is not something the parser can find in the
    words — the trips view holds a row id, not a name — so it arrives beside
    the parsed query rather than inside it, and composes with everything else:
    a search typed while a trip is open searches within that trip.

    `people` is a list of person ids, and arrives the same way for the same
    reason: it comes either from the People view's own link or from names the
    parser matched and the caller resolved. Several ids mean *all* of them —
    "photos of Ana and Ben" — because that is what every other filter here does
    (each clause narrows) and it is the only reading that makes typing a second
    name inside somebody's grid useful. Photos of either are one press away: the
    two names are two searches."""
    clauses, params = ["error IS NULL"], []
    if scope == "photos":
        clauses.append("is_photo = 1")
        # …and stated separately rather than relied upon: is_photo is derived at
        # index time and a future rule could set it on something that is not a
        # still, whereas `kind` is read off the extension and cannot drift.
        clauses.append("kind = 'image'")
    elif scope == "videos":
        clauses.append("kind = 'video'")
    if trip is not None:
        clauses.append("trip_id = ?")
        params.append(trip)
    if pq.date_from:
        clauses.append("taken_at >= ?")
        params.append(pq.date_from)
    if pq.date_to:
        clauses.append("taken_at <= ?")
        params.append(pq.date_to)
    if pq.places:
        # a matched place name may be a city, an admin1 region or a country
        # code — we don't know which, so test all three
        marks = ", ".join("?" * len(pq.places))
        clauses.append(f"(place_city IN ({marks}) OR place_region IN ({marks}) "
                       f"OR place_country IN ({marks}))")
        params.extend(pq.places * 3)
    if pq.cameras:
        clauses.append(f"camera IN ({', '.join('?' * len(pq.cameras))})")
        params.extend(pq.cameras)
    if pq.albums:
        # `instr`, not LIKE: the phrase is a user's album name and may contain %
        # or _, which LIKE would read as wildcards — "100%" would match every
        # row. apple_text holds several phrases separated by newlines, so a
        # substring test is what "belongs to this album" means here, and the
        # phrases come from the column itself so there is nothing else to hit.
        marks = " OR ".join(
            ["instr(lower(apple_text), lower(?)) > 0"] * len(pq.albums))
        clauses.append(f"({marks})")
        params.extend(pq.albums)
    # One clause per person, because they compose as AND (see above). A subquery
    # rather than a JOIN: a photograph with three faces of the same person would
    # come back three times from a join, and everything above this counts rows
    # (`total`, the ranking, the paging offsets). IN (SELECT …) is a set test,
    # which is exactly what "this person is in this photo" means.
    for pid in (people or []):
        clauses.append(
            "id IN (SELECT photo_id FROM faces WHERE cluster_id = ?)")
        params.append(int(pid))
    if pq.trip_mode:
        clauses.append("trip_id IS NOT NULL")
    return " AND ".join(clauses), params


def rank(rows, ids, mat, text_vec, limit: int | None = 200,
         ratio: float | None = None, min_keep: int = MIN_KEEP):
    """Cosine-rank `rows` against `text_vec`; `limit=None` means no cut.

    `ratio` cuts weak matches: a row is kept when it scores at least
    `ratio × top_score`. Cosine similarity is not calibrated — its absolute
    range depends on the model — so neither a fixed threshold nor a fixed
    margin behind the top hit transfers between models or corpora, but the
    *proportion* of the best score does. `min_keep` top rows survive the cut
    regardless, so a query with only weak signal degrades to "here are the
    closest ones" rather than to nothing.

    Rows with no embedding yet are never scored and never cut: they are
    "not indexed", not "not relevant", so they stay at the tail."""
    if len(rows) == 0:
        return []
    pos = {int(pid): i for i, pid in enumerate(ids)} if len(ids) else {}
    embedded = [r for r in rows if r["id"] in pos]
    unembedded = [r for r in rows if r["id"] not in pos]

    ranked = []
    if embedded:
        sub = mat[[pos[r["id"]] for r in embedded]].astype(np.float32)
        scores = sub @ text_vec.astype(np.float32)
        order = np.argsort(-scores)
        for i in order:
            r = dict(embedded[i])
            r["score"] = float(scores[i])
            ranked.append(r)

    if ratio is not None and ranked:
        # A non-positive best score is no signal at all, and `top × ratio` is
        # then *above* the top score — so rather than let the multiplication
        # decide (it would cut everything) or skip the cut (it would return the
        # whole library as "matches"), fall through to the min_keep closest.
        top = ranked[0]["score"]
        cut = top * ratio if top > 0 else float("inf")
        keep = [r for r in ranked if r["score"] >= cut]
        ranked = keep if len(keep) >= min_keep else ranked[:min_keep]

    tail = [dict(r, score=None) for r in unembedded]
    return (ranked + tail)[:limit]
