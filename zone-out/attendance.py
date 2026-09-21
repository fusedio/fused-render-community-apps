"""Attendance bookkeeping: per meeting-instance records (segments of
in_call / lobby / absent), verdicts, and the alert decisions ("you are
missing X", "starting in 2 min", "you dropped out of X").

Pure logic + file persistence; no system calls (alerts are *decided* here and
*delivered* by alerts.py). Records live in .fused/data/attendance/<day>.json,
one file per local day, keyed by the instance key (uid::original start).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile

UTC = dt.timezone.utc

DEFAULT_SETTINGS = {
    "ics_urls": [],
    "my_email": "",
    "track_mode": "all",          # all (minus ignored) | selected (tracked_uids only)
    "tracked_uids": [],
    "ignored_uids": [],
    "track_declined": False,
    "require_link": False,
    "grace_min": 2,
    "repeat_min": 1,
    "heads_up_min": 2,
    "dropout_min": 2,
    "alert_notification": True,
    "alert_sound": "Glass",
    "alert_dialog": True,
    "alert_voice": False,
    "auto_open_link": False,
    "alert_when_in_other_meeting": False,
    "escalate_unacknowledged": True,
    "allow_applescript": True,
    "camera_probe": True,
    "poll_s": 60,
    "poll_live_s": 20,
    "calendar_refresh_min": 5,
    "autostart_hint_dismissed": False,
}


def merge_settings(stored: dict | None) -> dict:
    s = dict(DEFAULT_SETTINGS)
    for k, v in (stored or {}).items():
        if k in s:
            s[k] = v
    # normalise
    if isinstance(s["ics_urls"], str):
        s["ics_urls"] = [u.strip() for u in s["ics_urls"].splitlines() if u.strip()]
    for k in ("grace_min", "repeat_min", "heads_up_min", "dropout_min", "poll_s", "poll_live_s", "calendar_refresh_min"):
        try:
            s[k] = max(0, float(s[k]))
        except (TypeError, ValueError):
            s[k] = DEFAULT_SETTINGS[k]
    s["poll_s"] = max(10, s["poll_s"])
    s["poll_live_s"] = max(5, s["poll_live_s"])
    s["calendar_refresh_min"] = max(1, s["calendar_refresh_min"])
    s["repeat_min"] = max(1, s["repeat_min"])
    return s


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def fmt_ts(d: dt.datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def my_partstat(event: dict, my_email: str) -> str:
    email = (my_email or "").strip().lower()
    if not email:
        return ""
    for a in event.get("attendees", []):
        if a.get("email") == email:
            return a.get("partstat", "")
    return ""


def classify_kind(event: dict, my_email: str) -> tuple[str, int]:
    """What kind of calendar entry this is, and how many *other* people are
    invited:
      call       — has a Meet/Zoom/Teams/… link: attendance is verified, alarms on
      in_person  — a location but no link: heads-up only, nothing to verify
      needs_link — other invitees, no link, no location: one clear
                   "add a meeting link" heads-up, no alarms
      personal   — nobody else invited, no link, no location: silent
    """
    me = (my_email or "").strip().lower()
    others = [a for a in event.get("attendees", []) if a.get("email") and a["email"] != me]
    if event.get("links"):
        return "call", len(others)
    if (event.get("location") or "").strip():
        return "in_person", len(others)
    if others:
        return "needs_link", len(others)
    return "personal", 0


def is_tracked(event: dict, settings: dict) -> tuple[bool, str]:
    """(tracked, reason)."""
    uid = event["uid"]
    if uid in settings["ignored_uids"]:
        return False, "ignored by you"
    if settings["track_mode"] == "selected":
        if uid in settings["tracked_uids"]:
            return True, "selected"
        return False, "not selected"
    if uid in settings["tracked_uids"]:
        return True, "selected"
    ps = my_partstat(event, settings.get("my_email", ""))
    if ps == "DECLINED" and not settings.get("track_declined"):
        return False, "you declined"
    if settings.get("require_link") and not event.get("links"):
        return False, "no meeting link"
    return True, "all meetings"


# ---------------------------------------------------------------- records ---

class Store:
    """Day files under data_dir/attendance. Keeps today's (and touched days')
    records in memory; writes atomically after every change."""

    def __init__(self, data_dir: str, local_tz: dt.tzinfo | None = None):
        self.dir = os.path.join(data_dir, "attendance")
        os.makedirs(self.dir, exist_ok=True)
        self.tz = local_tz or dt.datetime.now().astimezone().tzinfo
        self._days: dict[str, dict] = {}

    def day_of(self, start_iso: str) -> str:
        return parse_ts(start_iso).astimezone(self.tz).strftime("%Y-%m-%d")

    def _path(self, day: str) -> str:
        return os.path.join(self.dir, f"{day}.json")

    def load_day(self, day: str) -> dict:
        if day in self._days:
            return self._days[day]
        try:
            with open(self._path(day), encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "events" not in data:
                raise ValueError
        except (OSError, ValueError):
            data = {"date": day, "events": {}}
        self._days[day] = data
        return data

    def save_day(self, day: str) -> None:
        data = self._days.get(day)
        if data is None:
            return
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".day-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self._path(day))

    def days(self) -> list[str]:
        try:
            names = [n[:-5] for n in os.listdir(self.dir) if n.endswith(".json") and not n.startswith(".")]
        except OSError:
            return []
        return sorted(names)

    def record(self, event: dict, create: bool = True) -> dict | None:
        day = self.day_of(event["start"])
        data = self.load_day(day)
        rec = data["events"].get(event["key"])
        if rec is None and create:
            rec = {
                "key": event["key"], "uid": event["uid"], "title": event["title"],
                "start": event["start"], "end": event["end"], "links": event.get("links", []),
                "tracked": True, "segments": [], "in_s": 0.0, "lobby_s": 0.0,
                "first_in": None, "last_in": None, "last_state": None, "last_seen": None,
                "verdict": None, "late_s": None, "alerts": [], "snoozed_until": None,
                "skipped": False, "opened": False, "final": False,
                "nag_level": 0, "acked_at": None,
            }
            data["events"][event["key"]] = rec
        if rec is not None:
            rec["title"] = event["title"]
            rec["end"] = event["end"]
            rec["links"] = event.get("links", rec.get("links", []))
        return rec

    def records_for_day(self, day: str) -> dict:
        return self.load_day(day)["events"]

    # --- updates -----------------------------------------------------------

    def observe(self, event: dict, state: str, now: dt.datetime, tracked: bool) -> dict:
        """Fold one poll's presence state into the instance record."""
        rec = self.record(event)
        rec["tracked"] = tracked
        ts = fmt_ts(now)
        segs = rec["segments"]
        if segs and segs[-1][0] == state:
            segs[-1][2] = ts
        else:
            segs.append([state, ts, ts])
        if state == "in_call":
            rec["first_in"] = rec["first_in"] or ts
            rec["last_in"] = ts
        rec["last_state"] = state
        rec["last_seen"] = ts
        rec["in_s"], rec["lobby_s"] = self._durations(rec, now)
        if rec["first_in"]:
            rec["late_s"] = max(0.0, (parse_ts(rec["first_in"]) - parse_ts(rec["start"])).total_seconds())
        if now >= parse_ts(rec["end"]) and not rec["final"]:
            rec["verdict"] = verdict_for(rec)
            rec["final"] = True
        else:
            rec["verdict"] = verdict_for(rec, provisional=True)
        self.save_day(self.day_of(event["start"]))
        return rec

    @staticmethod
    def _durations(rec: dict, now: dt.datetime) -> tuple[float, float]:
        """Seconds in_call / lobby. A segment's span is from its first to its
        last sample; each sample is assumed to cover the poll gap before it,
        so a lone sample still counts the poll interval (capped at 60 s)."""
        in_s = lobby_s = 0.0
        segs = rec["segments"]
        for i, (state, a, b) in enumerate(segs):
            span = (parse_ts(b) - parse_ts(a)).total_seconds()
            # lead-in: time since the previous segment's last sample (or the
            # meeting start for the first one), capped so a long gap (sleep)
            # is not credited.
            prev_end = parse_ts(segs[i - 1][2]) if i else parse_ts(rec["start"])
            lead = min(60.0, max(0.0, (parse_ts(a) - prev_end).total_seconds()))
            if state == "in_call":
                in_s += span + lead
            elif state == "lobby":
                lobby_s += span + lead
        return in_s, lobby_s

    def finalize_ended(self, now: dt.datetime) -> list[dict]:
        """Close records whose meeting ended while we were not watching
        (daemon down, Mac asleep). Returns the records finalized."""
        done = []
        for day in list(self._days):
            for rec in self._days[day]["events"].values():
                if not rec["final"] and now >= parse_ts(rec["end"]) + dt.timedelta(minutes=2):
                    rec["verdict"] = verdict_for(rec)
                    rec["final"] = True
                    done.append(rec)
            if done:
                self.save_day(day)
        return done

    def add_alert(self, rec: dict, kind: str, message: str, now: dt.datetime) -> None:
        rec["alerts"].append({"ts": fmt_ts(now), "kind": kind, "message": message})
        rec["alerts"] = rec["alerts"][-50:]
        if kind in NAG_KINDS:
            rec["nag_level"] = int(rec.get("nag_level") or 0) + 1
        self.save_day(self.day_of(rec["start"]))

    def ack(self, rec: dict, now: dt.datetime) -> None:
        """The user interacted with a warning (dialog button, Escape, or an
        action in the app): the escalation ladder restarts from the base
        interval. Reminders continue while they are still absent."""
        rec["nag_level"] = 0
        rec["acked_at"] = fmt_ts(now)
        self.save_day(self.day_of(rec["start"]))

    def set_flags(self, rec: dict, **flags) -> None:
        for k, v in flags.items():
            rec[k] = v
        self.save_day(self.day_of(rec["start"]))


def verdict_for(rec: dict, provisional: bool = False) -> str:
    """Final: attended | partial | missed | skipped.
    Provisional (meeting still running): attending | partial | missing."""
    duration = max(60.0, (parse_ts(rec["end"]) - parse_ts(rec["start"])).total_seconds())
    in_s = rec.get("in_s", 0.0)
    if provisional:
        if rec.get("last_state") == "in_call":
            return "attending"
        return "partial" if in_s >= 60 else "missing"
    if rec.get("skipped") and in_s < 60:
        return "skipped"
    if in_s >= min(0.5 * duration, 15 * 60):
        return "attended"
    if in_s >= 60:
        return "partial"
    return "missed"


# ------------------------------------------------------------------ alerts --

NAG_KINDS = ("missing", "lobby", "dropped")
#: Seconds to wait before the next warning when the previous ones went
#: unanswered: after the 1st → 60 s (repeat_min), after the 2nd → 30 s,
#: from the 3rd on → every 15 s, until an interaction, the call, or the end.
ESCALATION_S = (30.0, 15.0)


def next_interval_s(rec: dict, settings: dict) -> float:
    base = settings["repeat_min"] * 60
    level = int(rec.get("nag_level") or 0)
    if level <= 1 or not settings.get("escalate_unacknowledged", True):
        return base
    return ESCALATION_S[min(level - 2, len(ESCALATION_S) - 1)]


def last_alert(rec: dict, kinds: tuple[str, ...]) -> dt.datetime | None:
    for a in reversed(rec.get("alerts", [])):
        if a["kind"] in kinds:
            return parse_ts(a["ts"])
    return None


def alert_decision(rec: dict, event: dict, presence: dict, now: dt.datetime, settings: dict,
                   in_other_meeting: bool, kind: str = "call", n_others: int = 0) -> dict | None:
    """Decide whether an alert is due for this tracked, live-or-imminent
    instance. Returns {kind, title, message, url} or None.

    Alert kinds: heads_up (before start) · needs_link (no link to verify) ·
    missing (absent after grace) · lobby (tab open, not joined) · dropped
    (was in, now out) — the last three repeat on the escalation ladder while
    the condition holds, unless snoozed/skipped. Only `call` events get them;
    in-person / needs-link / personal entries never alarm (see classify_kind).
    """
    start, end = parse_ts(rec["start"]), parse_ts(rec["end"])
    if rec.get("skipped") or now >= end:
        return None
    if rec.get("snoozed_until") and now < parse_ts(rec["snoozed_until"]):
        return None
    url = (rec.get("links") or [{}])[0].get("url", "") if rec.get("links") else ""
    title = rec["title"]
    heads_up = settings["heads_up_min"] * 60
    if kind != "call":
        return _non_call_decision(rec, event, now, settings, kind, n_others, start, end, title, heads_up)
    if now < start:
        if heads_up > 0 and (start - now).total_seconds() <= heads_up and last_alert(rec, ("heads_up",)) is None:
            mins = max(1, int(round((start - now).total_seconds() / 60)))
            return {"kind": "heads_up", "title": f"Starting in {mins} min: {title}",
                    "message": f"{start.astimezone().strftime('%H:%M')} – {end.astimezone().strftime('%H:%M')}" + (f" · {presence_hint(rec)}" if url else ""), "url": url, "soft": True}
        return None
    state = presence["state"]
    if state == "in_call":
        return None
    if in_other_meeting and not settings.get("alert_when_in_other_meeting"):
        return None
    grace = settings["grace_min"] * 60
    since_start = (now - start).total_seconds()
    was_in = bool(rec.get("first_in"))
    if was_in:
        # dropped out: absent for dropout_min since last in_call sample
        gap = (now - parse_ts(rec["last_in"])).total_seconds()
        if gap < settings["dropout_min"] * 60:
            return None
        kind = "dropped"
    else:
        if since_start < grace:
            return None
        kind = "lobby" if state == "lobby" else "missing"
    prev = last_alert(rec, NAG_KINDS)
    if prev is not None:
        ref = prev
        if rec.get("acked_at"):
            ref = max(ref, parse_ts(rec["acked_at"]))
        if (now - ref).total_seconds() < next_interval_s(rec, settings):
            return None
    late_min = int(since_start // 60)
    ends = end.astimezone().strftime("%H:%M")
    if kind == "dropped":
        msg = f"You left the call {int(gap // 60)} min ago — it runs until {ends}."
        head = f"You dropped out of: {title}"
    elif kind == "lobby":
        msg = f"The meeting tab is open but you haven't joined. Started {late_min} min ago, ends {ends}."
        head = f"Not joined yet: {title}"
    else:
        other = presence.get("other_calls") or []
        extra = f" You are on a call in {other[0]}." if other else ""
        msg = f"Started {late_min} min ago, ends {ends}. No sign of you in this meeting.{extra}"
        head = f"Missing meeting: {title}"
    return {"kind": kind, "title": head, "message": msg, "url": url, "soft": False}


def _non_call_decision(rec, event, now, settings, kind, n_others, start, end, title, heads_up):
    if kind == "personal" or last_alert(rec, ("needs_link", "heads_up")) is not None:
        return None
    when = start.astimezone().strftime("%H:%M")
    if kind == "needs_link":
        lead = max(heads_up, 60.0)
        if now < start - dt.timedelta(seconds=lead):
            return None
        who = f"{n_others} other invitee" + ("s" if n_others != 1 else "")
        verb = "Starts" if now < start else "Started"
        return {"kind": "needs_link", "title": f"Add a meeting link: {title}",
                "message": f"{verb} at {when} with {who} but has no Meet, Zoom or Teams link. "
                           f"Add one to the calendar event so I can check that you are in it.",
                "url": "", "soft": True}
    # in person: an ordinary heads-up, nothing to verify afterwards
    if heads_up <= 0 or now >= start or (start - now).total_seconds() > heads_up:
        return None
    mins = max(1, int(round((start - now).total_seconds() / 60)))
    location = (event.get("location") or "").strip()
    return {"kind": "heads_up", "title": f"Starting in {mins} min: {title}",
            "message": f"In person at {location}. Attendance is not checked for in-person meetings.",
            "url": "", "soft": True}


def presence_hint(rec: dict) -> str:
    links = rec.get("links") or []
    if not links:
        return ""
    return {"meet": "Google Meet", "zoom": "Zoom", "teams": "Teams", "webex": "Webex", "slack": "Slack huddle"}.get(links[0]["provider"], "link")


CONFIDENT = ("high", "medium")


def attribute_shared_calls(presences: dict[str, dict], titles: dict[str, str]) -> set[str]:
    """One call, one meeting. When an event with a real link is verifiably
    in a call in some app, a link-less event credited with "some call is
    active in that app" loses that credit (downgraded to absent, in place).
    Returns the keys of events confidently in a call."""
    confident = {k for k, p in presences.items() if p["state"] == "in_call" and p.get("confidence") in CONFIDENT}
    claimed = {presences[k].get("app"): k for k in confident if presences[k].get("app")}
    for key, p in presences.items():
        if p["state"] == "in_call" and p.get("confidence") == "low" and p.get("app") in claimed:
            owner = claimed[p["app"]]
            p["state"] = "absent"
            p["via"] = f"the call in {p['app']} belongs to '{titles.get(owner, 'another meeting')}'"
            p["other_calls"] = sorted(set(p.get("other_calls", [])) | {p["app"]})
            p["app"] = ""
    return confident


def live_summary(events: list[dict], now: dt.datetime, settings: dict) -> dict:
    """Banner state from the day's annotated events (each carries phase,
    tracked, presence, record). A verified call wins; an unverified one
    (link-less event, some call active) ranks below a meeting you are
    demonstrably missing."""
    live = [e for e in events if e["phase"] == "live"]

    def in_meeting(e):
        since = e["record"].get("first_in") or e["start"]
        return {"status": "in_meeting", "key": e["key"], "title": e["title"], "since": since, "end": e["end"],
                "detail": e["presence"]["via"], "confidence": e["presence"]["confidence"]}

    for e in live:
        if e["presence"]["state"] == "in_call" and e["presence"].get("confidence") in CONFIDENT:
            return in_meeting(e)
    grace = settings["grace_min"] * 60
    for e in live:
        if e["tracked"] and not e["record"].get("skipped") and e["presence"]["state"] != "in_call" and e.get("kind", "call") == "call":
            late = (now - parse_ts(e["start"])).total_seconds()
            if e["presence"]["state"] == "lobby":
                return {"status": "lobby", "key": e["key"], "title": e["title"], "since": e["start"], "end": e["end"], "detail": e["presence"]["via"], "late_s": late}
            if late >= grace:
                return {"status": "missing", "key": e["key"], "title": e["title"], "since": e["start"], "end": e["end"], "detail": e["presence"]["via"], "late_s": late}
            return {"status": "starting", "key": e["key"], "title": e["title"], "since": e["start"], "end": e["end"], "detail": "grace period", "late_s": late}
    for e in live:
        if e["presence"]["state"] == "in_call":
            return in_meeting(e)
    for e in live:
        kind = e.get("kind", "call")
        if kind != "call":
            detail = {"in_person": "in person" + (f" at {e['location']}" if e.get("location") else "") + " · attendance is not checked",
                      "needs_link": f"{e.get('n_others', 0)} other invitee(s), no meeting link · add one so attendance can be checked",
                      "personal": "personal entry · no attendance check"}[kind]
            return {"status": "unverifiable", "kind": kind, "key": e["key"], "title": e["title"], "since": e["start"], "end": e["end"], "detail": detail}
    if live:
        e = live[0]
        return {"status": "untracked", "key": e["key"], "title": e["title"], "since": e["start"], "end": e["end"], "detail": e.get("track_reason", "")}
    upcoming = [e for e in events if e["phase"] == "upcoming"]
    if upcoming:
        e = upcoming[0]
        return {"status": "free", "next_key": e["key"], "next_title": e["title"], "next_start": e["start"], "next_end": e["end"], "next_tracked": e["tracked"]}
    return {"status": "free"}
