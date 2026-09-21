"""Calendar feed: fetch private iCalendar (.ics) links, parse VEVENTs, expand
recurrences into concrete instances for a window, and pull out meeting links.

Stdlib + python-dateutil (bundled with fused-render's interpreter). No
icalendar package: Google's feed uses a small, regular subset of RFC 5545 and
the hand parser below covers it — line unfolding, params, TZID/UTC/floating/
DATE values, DTEND or DURATION, RRULE/EXDATE/RDATE, RECURRENCE-ID overrides,
STATUS:CANCELLED, ATTENDEE PARTSTAT and the X-GOOGLE-CONFERENCE property.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # bundled with fused-render's interpreter (pandas dependency)
    from dateutil import rrule as _rrule
except ImportError:  # pragma: no cover - degraded: recurring events not expanded
    _rrule = None

UTC = dt.timezone.utc

# ---------------------------------------------------------------- links ----

LINK_PATTERNS = [
    # provider, regex, group -> code
    ("meet", re.compile(r"https?://meet\.google\.com/(?:lookup/)?([a-z]{3}-[a-z]{4}-[a-z]{3}|[A-Za-z0-9_-]{6,})"), 1),
    ("zoom", re.compile(r"https?://[\w.-]*zoom\.(?:us|com)/(?:j|my|w|s|wc)/([A-Za-z0-9._-]+)"), 1),
    ("teams", re.compile(r"https?://teams\.(?:microsoft\.com|live\.com)/(?:l/meetup-join|meet)/([^\s\"'<>]+)"), 1),
    ("webex", re.compile(r"https?://[\w.-]*webex\.com/[^\s\"'<>]+"), 0),
    ("slack", re.compile(r"https?://app\.slack\.com/huddle/([^\s\"'<>]+)"), 1),
    ("whereby", re.compile(r"https?://[\w.-]*whereby\.com/([^\s\"'<>?]+)"), 1),
    ("around", re.compile(r"https?://[\w.-]*around\.co/([^\s\"'<>?]+)"), 1),
]
_ANY_URL = re.compile(r"https?://[^\s\"'<>]+")


def extract_links(*texts: str | None) -> list[dict]:
    """Meeting links found in the given texts, deduplicated, known providers
    first. Each: {provider, url, code} — `code` is the provider-specific
    identifier used to match an open browser tab (Meet code, Zoom id ...)."""
    found: list[dict] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        text = _unescape(text)
        for provider, rx, group in LINK_PATTERNS:
            for m in rx.finditer(text):
                url = m.group(0).rstrip(".,;)")
                code = (m.group(group) if group else url).rstrip(".,;)")
                if provider == "meet" and "lookup/" not in url:
                    code = code.lower()
                if provider == "zoom":
                    code = code.split("?")[0]
                key = f"{provider}:{code}"
                if key in seen:
                    continue
                seen.add(key)
                found.append({"provider": provider, "url": url, "code": code})
    if not found:
        for text in texts:
            if not text:
                continue
            for m in _ANY_URL.finditer(_unescape(text)):
                url = m.group(0).rstrip(".,;)")
                if url in seen or "google.com/calendar" in url or "calendar.google" in url:
                    continue
                seen.add(url)
                found.append({"provider": "link", "url": url, "code": url})
                break
    return found


def _unescape(value: str) -> str:
    """RFC 5545 TEXT unescaping plus a light HTML strip (Google puts anchors
    in DESCRIPTION)."""
    value = value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    value = re.sub(r"<[^>]+>", " ", value)
    return value.replace("&amp;", "&")


# -------------------------------------------------------------- parsing ----

def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


_PROP = re.compile(r"^([A-Za-z0-9-]+)((?:;[^:]*)?):(.*)$")


def parse_property(line: str) -> tuple[str, dict, str] | None:
    m = _PROP.match(line)
    if not m:
        return None
    name, raw_params, value = m.group(1).upper(), m.group(2), m.group(3)
    params: dict[str, str] = {}
    if raw_params:
        for chunk in re.findall(r';([^=;]+)=("(?:[^"]*)"|[^;]*)', raw_params):
            params[chunk[0].upper()] = chunk[1].strip('"')
    return name, params, value


def parse_components(text: str) -> tuple[list[dict], dict]:
    """Return (vevents, calendar-level props). Each vevent is a dict of
    UPPERCASE property -> list of (params, value) so repeated properties
    (ATTENDEE, EXDATE) are kept."""
    events: list[dict] = []
    cal: dict = {}
    stack: list[tuple[str, dict]] = []
    for line in unfold(text):
        prop = parse_property(line)
        if prop is None:
            continue
        name, params, value = prop
        if name == "BEGIN":
            stack.append((value.upper(), {}))
            continue
        if name == "END":
            if not stack:
                continue
            kind, comp = stack.pop()
            if kind == "VEVENT":
                events.append(comp)
            continue
        if not stack:
            continue
        kind, comp = stack[-1]
        if kind == "VEVENT":
            comp.setdefault(name, []).append((params, value))
        elif kind == "VCALENDAR":
            cal[name] = value
    return events, cal


def _tz(name: str | None) -> dt.tzinfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    # Windows / Outlook style names seen in the wild → nearest IANA zone
    aliases = {
        "eastern standard time": "America/New_York", "central standard time": "America/Chicago",
        "mountain standard time": "America/Denver", "pacific standard time": "America/Los_Angeles",
        "gmt standard time": "Europe/London", "w. europe standard time": "Europe/Berlin",
        "central europe standard time": "Europe/Warsaw", "romance standard time": "Europe/Paris",
        "india standard time": "Asia/Kolkata", "tokyo standard time": "Asia/Tokyo",
        "utc": "UTC", "gmt": "UTC", "z": "UTC",
    }
    iana = aliases.get(name.strip().strip('"').lower())
    if iana:
        try:
            return ZoneInfo(iana)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return None
    return None


def parse_datetime(value: str, params: dict, local_tz: dt.tzinfo) -> tuple[dt.datetime | dt.date, bool]:
    """Return (value, is_date). DATE-TIMEs come back timezone-aware."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        return dt.date(int(value[:4]), int(value[4:6]), int(value[6:8])), True
    utc = value.endswith("Z")
    if utc:
        value = value[:-1]
    base = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if utc:
        return base.replace(tzinfo=UTC), False
    tz = _tz(params.get("TZID")) or local_tz
    return base.replace(tzinfo=tz), False


_DUR = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(value: str) -> dt.timedelta:
    m = _DUR.match(value.strip())
    if not m:
        return dt.timedelta(0)
    sign, w, d, h, mi, s = m.groups()
    td = dt.timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == "-" else td


def _first(comp: dict, name: str) -> tuple[dict, str] | None:
    vals = comp.get(name)
    return vals[0] if vals else None


def _attendees(comp: dict) -> list[dict]:
    out = []
    for params, value in comp.get("ATTENDEE", []):
        email = value.split(":", 1)[-1].strip().lower() if ":" in value else value.strip().lower()
        out.append({"email": email, "partstat": params.get("PARTSTAT", "NEEDS-ACTION").upper(),
                    "name": params.get("CN", "")})
    return out


def _to_utc_iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fix_until(rule: str, dtstart: dt.datetime) -> str:
    """dateutil insists UNTIL be UTC when DTSTART is aware; Google always
    writes it with Z, other producers do not."""
    def repl(m):
        raw = m.group(1)
        if raw.endswith("Z"):
            return m.group(0)
        if len(raw) == 8:
            until = dt.datetime(int(raw[:4]), int(raw[4:6]), int(raw[6:8]), 23, 59, 59, tzinfo=dtstart.tzinfo)
        else:
            until = dt.datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").replace(tzinfo=dtstart.tzinfo)
        return "UNTIL=" + until.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return re.sub(r"UNTIL=([0-9TZ]+)", repl, rule)


def expand_events(text: str, window_start: dt.datetime, window_end: dt.datetime,
                  local_tz: dt.tzinfo | None = None, source: str = "") -> tuple[list[dict], dict]:
    """Concrete timed meeting instances overlapping [window_start, window_end)
    plus calendar metadata. All-day events are skipped (nothing to attend).

    Instance: {key, uid, title, start, end, all_day:false, location,
    description(trimmed), links[], attendees[], status, organizer, source,
    recurring, url}. `start`/`end` are ISO-8601 UTC strings; `key` is stable
    per instance (uid + original start), so overrides keep their identity.
    """
    local_tz = local_tz or dt.datetime.now().astimezone().tzinfo
    events, cal = parse_components(text)
    by_uid: dict[str, dict] = {}
    overrides: dict[str, dict[str, dict]] = {}
    for comp in events:
        uid = (_first(comp, "UID") or ({}, ""))[1]
        if not uid:
            uid = hashlib.sha1(json.dumps(sorted(comp.keys())).encode()).hexdigest()
        rid = _first(comp, "RECURRENCE-ID")
        if rid:
            rid_val, _is_date = parse_datetime(rid[1], rid[0], local_tz)
            rkey = rid_val.isoformat() if isinstance(rid_val, dt.date) and not isinstance(rid_val, dt.datetime) else _to_utc_iso(rid_val)
            overrides.setdefault(uid, {})[rkey] = comp
        else:
            by_uid[uid] = comp

    instances: list[dict] = []
    for uid, comp in by_uid.items():
        instances.extend(_instances_for(uid, comp, overrides.get(uid, {}), window_start, window_end, local_tz, source))
    # Overrides whose master is missing (feed only carries the exception)
    for uid, ovs in overrides.items():
        if uid in by_uid:
            continue
        for comp in ovs.values():
            inst = _single(uid, comp, None, local_tz, source)
            if inst and _overlaps(inst, window_start, window_end):
                instances.append(inst)
    instances.sort(key=lambda e: (e["start"], e["title"]))
    return instances, cal


def _overlaps(inst: dict, ws: dt.datetime, we: dt.datetime) -> bool:
    s = dt.datetime.strptime(inst["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    e = dt.datetime.strptime(inst["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return s < we and e > ws


def _single(uid: str, comp: dict, start_override: dt.datetime | None, local_tz, source: str,
            original_start: dt.datetime | None = None) -> dict | None:
    ds = _first(comp, "DTSTART")
    if not ds:
        return None
    start, is_date = parse_datetime(ds[1], ds[0], local_tz)
    if is_date:
        return None  # all-day: nothing to attend
    if start_override is not None:
        start = start_override
    de = _first(comp, "DTEND")
    du = _first(comp, "DURATION")
    if de:
        end, end_is_date = parse_datetime(de[1], de[0], local_tz)
        if end_is_date:
            return None
        if start_override is not None:
            # keep the master's duration for generated instances
            ms, _ = parse_datetime(ds[1], ds[0], local_tz)
            end = start + (end - ms)
    elif du:
        end = start + parse_duration(du[1])
    else:
        end = start + dt.timedelta(hours=1)
    if end <= start:
        end = start + dt.timedelta(minutes=30)
    status = (_first(comp, "STATUS") or ({}, "CONFIRMED"))[1].upper()
    if status == "CANCELLED":
        return None
    title = _unescape((_first(comp, "SUMMARY") or ({}, "(no title)"))[1]).strip() or "(no title)"
    location = _unescape((_first(comp, "LOCATION") or ({}, ""))[1]).strip()
    description = _unescape((_first(comp, "DESCRIPTION") or ({}, ""))[1]).strip()
    conf = (_first(comp, "X-GOOGLE-CONFERENCE") or ({}, ""))[1]
    url = (_first(comp, "URL") or ({}, ""))[1]
    x_alt = (_first(comp, "X-ALT-DESC") or ({}, ""))[1]
    links = extract_links(conf, location, description, url, x_alt)
    organizer = (_first(comp, "ORGANIZER") or ({}, ""))
    org_email = organizer[1].split(":", 1)[-1].lower() if organizer[1] else ""
    key_start = original_start or start
    return {
        "key": f"{uid}::{_to_utc_iso(key_start)}",
        "uid": uid,
        "title": title,
        "start": _to_utc_iso(start),
        "end": _to_utc_iso(end),
        "all_day": False,
        "location": location[:300],
        "description": description[:600],
        "links": links,
        "attendees": _attendees(comp),
        "organizer": {"email": org_email, "name": organizer[0].get("CN", "")},
        "status": status,
        "recurring": bool(comp.get("RRULE")) or original_start is not None,
        "source": source,
    }


def _instances_for(uid: str, comp: dict, overrides: dict[str, dict], ws: dt.datetime, we: dt.datetime,
                   local_tz, source: str) -> list[dict]:
    ds = _first(comp, "DTSTART")
    if not ds:
        return []
    start, is_date = parse_datetime(ds[1], ds[0], local_tz)
    if is_date:
        return []
    rrules = comp.get("RRULE", [])
    out: list[dict] = []
    if not rrules:
        inst = _single(uid, comp, None, local_tz, source)
        if inst and _overlaps(inst, ws, we):
            out.append(inst)
        # explicit RDATEs on a non-recurring event
        starts = [start]
    else:
        starts = []
    if rrules and _rrule is not None:
        rule_text = _fix_until(rrules[0][1], start)
        try:
            rule = _rrule.rrulestr(rule_text, dtstart=start, forceset=True)
        except (ValueError, TypeError):
            rule = None
        if rule is not None:
            for params, value in comp.get("EXDATE", []):
                for piece in value.split(","):
                    try:
                        ex, exd = parse_datetime(piece, params, local_tz)
                    except ValueError:
                        continue
                    if not exd:
                        rule.exdate(ex)
            for params, value in comp.get("RDATE", []):
                for piece in value.split(","):
                    try:
                        rd, rdd = parse_datetime(piece.split("/")[0], params, local_tz)
                    except ValueError:
                        continue
                    if not rdd:
                        rule.rdate(rd)
            # Look back a day so an instance that started before the window
            # but overlaps it is kept.
            lo = ws - dt.timedelta(days=1)
            try:
                starts = list(rule.between(lo, we, inc=True))
            except (ValueError, TypeError, OverflowError):
                starts = []
    elif rrules:
        starts = [start]
    if rrules:
        for s in starts:
            if s.tzinfo is None:
                s = s.replace(tzinfo=start.tzinfo)
            key = _to_utc_iso(s)
            ov = overrides.pop(key, None)
            if ov is not None:
                inst = _single(uid, ov, None, local_tz, source, original_start=s)
            else:
                inst = _single(uid, comp, s, local_tz, source, original_start=s)
            if inst and _overlaps(inst, ws, we):
                out.append(inst)
    # Overrides that moved an instance from outside the window into it (or
    # whose original start dateutil did not generate).
    for key, ov in list(overrides.items()):
        inst = _single(uid, ov, None, local_tz, source, original_start=dt.datetime.strptime(key, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) if key.endswith("Z") else None)
        if inst and _overlaps(inst, ws, we) and not any(i["key"] == inst["key"] for i in out):
            out.append(inst)
    return out


# ------------------------------------------------------------- fetching ----

def normalize_url(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def fetch_ics(url: str, cache_dir: str, timeout: float = 25.0) -> tuple[str, dict]:
    """Download one feed (or read a local .ics path). Returns (text, info)
    where info = {ok, error, from_cache, fetched_at, bytes}. On failure the
    last good copy in cache_dir is returned with from_cache=True."""
    url = normalize_url(url)
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "feed-" + hashlib.sha1(url.encode()).hexdigest()[:16] + ".ics")
    info = {"ok": False, "error": "", "from_cache": False, "fetched_at": None, "bytes": 0, "url_hash": os.path.basename(cache_path)}
    text = ""
    try:
        if url.startswith("http://") or url.startswith("https://"):
            req = urllib.request.Request(url, headers={"User-Agent": "meeting-attendance-tracker/1.0 (+fused-render)",
                                                       "Accept": "text/calendar, text/plain;q=0.8, */*;q=0.5"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
        else:
            path = os.path.expanduser(url[len("file://"):] if url.startswith("file://") else url)
            with open(path, "rb") as f:
                data = f.read()
        text = data.decode("utf-8", "replace")
        if "BEGIN:VCALENDAR" not in text[:4000]:
            raise ValueError("response is not an iCalendar feed (no BEGIN:VCALENDAR) — is this the *secret address in iCal format*?")
        fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".feed-", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, cache_path)
        info.update(ok=True, fetched_at=dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), bytes=len(data))
    except Exception as e:  # noqa: BLE001 - surfaced to the UI
        info["error"] = f"{type(e).__name__}: {e}"[:300]
        try:
            with open(cache_path, "rb") as f:
                text = f.read().decode("utf-8", "replace")
            info["from_cache"] = True
            info["bytes"] = len(text)
        except OSError:
            text = ""
    return text, info
