"""Apple Photos as a source of photos to index — read-only, in place.

The walker deliberately refuses to descend into a `.photoslibrary` bundle
(indexer.SKIP_SUFFIXES): it holds every original *and* every derivative render
side by side, so walking one catalogues each photograph several times over,
under paths nobody chose. But that bundle is where most people's photographs
actually live, and the library's own database knows which file is the original,
when the picture was taken, where, which albums it belongs to and who is in it —
facts that are either absent from the files or better than what they carry.

So lens reads the library through `osxphotos` instead, and indexes the
**originals in place**: no export, no copy, one catalog row per photograph
pointing at the file inside the bundle. Nothing here writes to the library, or
to anything in it — every call below is a read.

Two facts about a real library shape the whole module:

  * **iCloud offloading.** With "Optimise Mac Storage" on, the original is not
    on this machine; `PhotoInfo.path` is then None. Those photos are counted and
    reported, never indexed — downloading them would mean touching the network
    and the user's iCloud quota, which lens does not do.
  * **macOS privacy (TCC).** Reading the library database needs Full Disk Access
    (or Photos access) for whatever launched the daemon. Without it the very
    first call raises, and an index run must survive that with a message the
    user can act on rather than a traceback (see `enumerate_library`).

Movies are skipped: lens embeds still images, and a video is a later wave.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime

from lens import metadata

# What the user has to do when macOS refuses us the library. Worth spelling out
# in full — "operation not permitted" on a path inside ~/Pictures is one of the
# least actionable errors macOS produces, and the fix is two clicks in a place
# nobody visits.
TCC_HINT = ("macOS blocked access to your Photos library. Grant Full Disk "
            "Access to the terminal (or app) running the lens daemon in "
            "System Settings → Privacy & Security → Full Disk Access, then "
            "reindex.")

# Album names and titles go into one text column, one phrase per line, so the
# query parser can match them as a vocabulary (see store.apple_phrases). A
# newline cannot occur inside an album name, which is what makes it a safe
# separator for splitting the column back into phrases.
PHRASE_SEP = "\n"


@dataclass
class ApplePhoto:
    """One photograph in the library, as lens needs it.

    `path` is the **original** on this machine, and it is None when there isn't
    one — an iCloud-offloaded photo is still in the library, so it is still one of
    these; it is simply not a file anything can read this run. Callers index the
    ones with a path and count the rest.

    `sig` is that file's (mtime, size) — the same signature the folder walker
    records, so an Apple row goes through the indexer's skip-if-unchanged path
    unmodified — and is None alongside a None path."""

    uuid: str
    path: str | None
    sig: tuple | None
    taken_at: str | None = None
    lat: float | None = None
    lon: float | None = None
    camera: str | None = None
    title: str | None = None
    description: str | None = None
    albums: list = field(default_factory=list)
    persons: list = field(default_factory=list)
    favorite: bool = False


def _report(**kw):
    """The shape every caller can rely on, whatever happened.

    `found` counts the photographs in the library lens would index; `local` the
    ones whose original is actually on this machine; `offloaded` the rest. A
    status line reads these, so they must be present even for a run that never
    got as far as opening the database."""
    out = {"found": 0, "local": 0, "offloaded": 0, "movies": 0, "error": None}
    out.update(kw)
    return out


def _photos_db(library=None):
    """The library, or a raised exception. Imported here rather than at module
    scope: `osxphotos` pulls in a stack of pyobjc frameworks, and lens must
    start (and its tests must run) on a machine where the feature is off, the
    dependency is missing, or the OS is not macOS at all."""
    import osxphotos
    return (osxphotos.PhotosDB(library) if library
            else osxphotos.PhotosDB())


def _naive(dt):
    """Photos stores a capture instant with the offset it was taken at; the
    catalog's `taken_at` is the tz-naive local wall clock everywhere else (see
    metadata.extract), and mixing the two would make string comparison — which
    is how every date filter works — meaningless. The wall clock is what the
    photograph was taken at, so the offset is dropped rather than converted."""
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=None).isoformat()


def _clean(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _names(v):
    """A list of names from Photos, minus the placeholder it uses for a face it
    has detected but nobody has named."""
    return [n for n in (_clean(x) for x in (v or []))
            if n and n != "_UNKNOWN_"]


def _camera(photo):
    """Make + model as Photos itself read them, or None.

    `exif_info` is the library's own copy of what the file said, and it is
    absent for photographs Photos never scanned that far. A None here is not a
    problem: metadata.extract reads the file's own EXIF anyway, and the merge
    below only lets this value fill a gap (see `merge`)."""
    info = getattr(photo, "exif_info", None)
    if info is None:
        return None
    parts = [_clean(getattr(info, "camera_make", None)),
             _clean(getattr(info, "camera_model", None))]
    return " ".join(p for p in parts if p) or None


def _hidden(photo):
    # `hidden` today, `ishidden` in older osxphotos — a hidden photo is hidden
    # on purpose and must not surface in a search.
    return bool(getattr(photo, "hidden", None)
                or getattr(photo, "ishidden", False))


def enumerate_library(library=None):
    """`(items, report)` for every photograph in the library.

    Every one, offloaded originals included (`ApplePhoto.path` is then None):
    the caller indexes the ones it can read, but it also has to know the full set
    of uuids the library holds, because that is what an Apple row is pruned
    against. Leaving the offloaded ones out of `items` meant a photo iCloud took
    off the disk between two runs looked exactly like a photo deleted from
    Photos, and lost its row.

    Never raises. A library that cannot be opened — no osxphotos, no macOS, no
    Full Disk Access, a database an OS upgrade moved — comes back as an empty
    list and a report carrying the message, because an index run has folders to
    scan whether or not Photos co-operated, and "Photos is unreadable" is a
    status line, not a failed run."""
    try:
        db = _photos_db(library)
    except ImportError as exc:
        return [], _report(error=f"osxphotos is not installed ({exc})")
    except (PermissionError, OSError) as exc:
        # The TCC refusal arrives as one of these, and so does a genuinely
        # missing library. Both need the hint: the second is usually the first
        # in disguise (macOS reports a path it will not let us see as absent).
        return [], _report(error=f"{TCC_HINT} ({exc})")
    except Exception as exc:                  # osxphotos' own failure types
        return [], _report(error=f"Could not read the Photos library: {exc}")

    try:
        # movies=False: lens embeds still images. intrash=False is the default
        # and is stated anyway — a deleted photo must not come back as a row.
        raw = db.photos(images=True, movies=False, intrash=False)
    except Exception as exc:
        return [], _report(error=f"Could not list the Photos library: {exc}")

    items, found, offloaded, movies = [], 0, 0, 0
    for p in raw:
        try:
            if getattr(p, "ismovie", False):
                movies += 1
                continue
            if _hidden(p) or getattr(p, "intrash", False):
                continue
            found += 1
            path, sig = getattr(p, "path", None), None
            if path:
                try:
                    st = os.stat(path)
                    sig = (st.st_mtime, st.st_size)
                except OSError:
                    # Photos believes the original is local and it is not — an
                    # offload that raced this run, or a library the user moved.
                    # Same consequence as an offload, so: same treatment.
                    path = None
            if not path:
                offloaded += 1        # the original is not on this machine
            items.append(ApplePhoto(
                uuid=str(getattr(p, "uuid", "") or ""),
                path=str(path) if path else None, sig=sig,
                taken_at=_naive(getattr(p, "date", None)),
                lat=getattr(p, "latitude", None),
                lon=getattr(p, "longitude", None),
                camera=_camera(p),
                title=_clean(getattr(p, "title", None)),
                description=_clean(getattr(p, "description", None)),
                albums=[a for a in (_clean(x) for x in (getattr(p, "albums", None) or []))
                        if a],
                persons=_names(getattr(p, "persons", None)),
                favorite=bool(getattr(p, "favorite", False)),
            ))
        except Exception:
            # One unreadable row must not cost the other twenty thousand.
            continue
    # A row with no uuid cannot be pruned when it leaves the library, and a
    # uuid is the one thing Photos always has, so its absence means a row we
    # do not understand.
    items = [it for it in items if it.uuid]
    return items, _report(found=found, offloaded=offloaded, movies=movies,
                          local=sum(1 for it in items if it.path))


def phrases(item: ApplePhoto) -> str:
    """The searchable text for one photo: its title and its album names.

    Not the description — the vocabulary is matched as whole phrases, and a
    sentence of prose is not a phrase anyone types. It is kept in `_apple`
    below, where the details panel can still show it."""
    seen, out = set(), []
    for v in [item.title, *item.albums]:
        v = _clean(v)
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return PHRASE_SEP.join(out)


def merge(rec: dict, item: ApplePhoto) -> dict:
    """Fold what Photos knows into a record `metadata.extract` produced.

    Photos wins on **when** and **where**, and nothing else. Those two are the
    ones it genuinely knows better: a date the user corrected in Photos, or a
    location it holds for a photograph whose GPS tags a sharing pipeline
    stripped, is the truth about the picture — while the file's own EXIF is the
    truth about the file. Everything else (dimensions, format, exposure, the raw
    tag dump) stays exactly as the file said, and the camera is only *filled in*
    where the file had none.

    The Photos-side facts that have nowhere else to go — uuid, albums, favorite,
    persons, title, description — go under `_apple` in `raw_exif`, so one /meta
    request still answers everything the details panel shows.
    """
    rec["source"] = "apple"
    rec["apple_uuid"] = item.uuid
    rec["apple_text"] = phrases(item) or None

    if item.taken_at:
        rec["taken_at"] = item.taken_at
    if item.lat is not None and item.lon is not None:
        # The place names must come from the coordinates that won, not be left
        # describing the ones that lost — so the geocode is re-run whenever
        # Photos supplies a location. A failed lookup loses the names, not the
        # coordinates (same rule as metadata.extract).
        rec["lat"], rec["lon"] = float(item.lat), float(item.lon)
        try:
            city, region, country = metadata.geocode(rec["lat"], rec["lon"])
        except Exception:
            pass
        else:
            rec["place_city"] = city or None
            rec["place_region"] = region or None
            rec["place_country"] = country or None
    if not rec.get("camera") and item.camera:
        rec["camera"] = item.camera

    try:
        raw = json.loads(rec.get("raw_exif") or "{}")
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw["_apple"] = {"uuid": item.uuid, "albums": list(item.albums),
                     "favorite": bool(item.favorite),
                     "persons": list(item.persons),
                     "title": item.title, "description": item.description}
    try:
        rec["raw_exif"] = json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        rec["raw_exif"] = json.dumps({"_apple": raw["_apple"]})

    # `is_photo` is re-derived rather than kept, because the reason a library
    # photo could fail it is now gone: a HEIC whose capture tags were stripped
    # has no camera and no date in the file, but Photos holds the date — and a
    # photograph in someone's Photos library, taken at a known instant, is a
    # photograph. The real rule is reused (not restated) by handing it the
    # Photos date as the capture timestamp it is.
    if item.taken_at:
        raw.setdefault("DateTimeOriginal", item.taken_at)
    rec["is_photo"] = 1 if metadata.is_photo(rec, raw) else 0
    return rec
