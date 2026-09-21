"""Resident background daemon for NoShow (meeting attendance tracker).

Spawned by fused-render (`daemon = "daemon.py"` in pyproject.toml) as
    python daemon.py --status <file> --cache <dir> --version <str>
Binds 127.0.0.1:0, publishes {port, token, pid, version} atomically, then:

* a monitor thread refreshes the calendar feeds every few minutes, polls this
  Mac's meeting signals every 20-60 s, folds presence into attendance records
  and fires alerts (notification / sound / dialog / voice) when a tracked
  meeting is running without you;
* a small token-gated HTTP API serves the page (`fused.daemon.call(route)`).

Routes (POST, JSON body, token in `?t=`):
  state {day}            full snapshot for one local day (default today)
  settings {…}           merge + save settings, returns state
  track {uid, mode}      mode: track | ignore | default
  refresh                refetch calendar + poll now
  snooze {key, minutes}  / skip {key} / unskip {key}
  open {key}             open the meeting link
  test_alert             fire a sample alert
  permissions {action}   probe (AppleScript tab access, waits for the macOS
                         prompt) | open_pane
  history {days}         per-day attendance summaries + records
GET /ping → {ok, version}; GET /quit → shutdown.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import alerts  # noqa: E402
import attendance  # noqa: E402
import ics_feed  # noqa: E402
import signals  # noqa: E402

APP_DIR = os.environ.get("FUSED_RENDER_APP_DIR") or HERE
DATA_DIR = os.path.join(APP_DIR, ".fused", "data")
CACHE_DIR = os.path.join(APP_DIR, ".fused", "cache")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
ALERT_LOG = os.path.join(DATA_DIR, "alerts.jsonl")
LOG_PATH = os.path.join(CACHE_DIR, "daemon.log")
SNAPSHOT_PATH = os.path.join(CACHE_DIR, "state.json")
#: Single-instance guard. fused-render respawns this daemon on every code
#: change / server restart and cannot always reap the previous copy (it is
#: setsid-detached), so a folder can accumulate orphaned daemons that all
#: poll, all alert and all open their own dialog. The newest instance owns
#: this file; everyone else exits.
PID_PATH = os.path.join(CACHE_DIR, "daemon.pid")
alerts.DIALOG_PID_PATH = os.path.join(CACHE_DIR, "dialog.pid")
SIBLINGS = ["ics_feed.py", "signals.py", "attendance.py", "alerts.py"]
UTC = dt.timezone.utc
WINDOW_BACK_DAYS = 1
WINDOW_AHEAD_DAYS = 14


def local_zone() -> dt.tzinfo:
    try:
        target = os.readlink("/etc/localtime")
        name = target.split("zoneinfo/", 1)[1]
        return ZoneInfo(name)
    except (OSError, IndexError, ValueError, KeyError):
        return dt.datetime.now().astimezone().tzinfo or UTC


TZ = local_zone()


def log(msg: str) -> None:
    line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def write_json_atomic(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    os.replace(tmp, path)


def mask_url(url: str) -> str:
    u = urlparse(url)
    if not u.netloc:
        return url[:40]
    path = u.path
    if len(path) > 28:
        path = path[:18] + "…" + path[-8:]
    return f"{u.netloc}{path}"


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


# ------------------------------------------------------------------ monitor -

class Monitor:
    def __init__(self, version: str):
        self.version = version
        self.started = now_utc()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.settings = self._load_settings()
        self.store = attendance.Store(DATA_DIR, local_tz=TZ)
        self.instances: list[dict] = []
        self.calendar: dict = {"ok": False, "error": "", "fetched_at": None, "sources": [], "n_instances": 0, "detected_email": ""}
        self.calendar_due = 0.0
        self.signals: dict = {}
        self.annotated: list[dict] = []
        self.live: dict = {"status": "free"}
        self.alert_log: collections.deque = collections.deque(maxlen=200)
        self.polls = 0
        self.last_poll_ms = 0
        self.last_poll_at: str | None = None
        self.next_poll_at: str | None = None
        self.last_error = ""
        self._dialog_open = False
        self._nagging = False
        self._sibling_mtimes = self._stat_siblings()
        self._load_alert_log()

    # --- settings ----------------------------------------------------------

    def _load_settings(self) -> dict:
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            stored = {}
        return attendance.merge_settings(stored)

    def save_settings(self, patch: dict) -> dict:
        with self.lock:
            merged = dict(self.settings)
            merged.update({k: v for k, v in patch.items() if k in attendance.DEFAULT_SETTINGS})
            self.settings = attendance.merge_settings(merged)
            write_json_atomic(SETTINGS_PATH, self.settings)
            if "ics_urls" in patch or "my_email" in patch:
                self.calendar_due = 0.0
            self.wake.set()
            return self.settings

    def _load_alert_log(self) -> None:
        try:
            with open(ALERT_LOG, encoding="utf-8") as f:
                lines = f.readlines()[-200:]
            for line in lines:
                try:
                    self.alert_log.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass

    # --- calendar ----------------------------------------------------------

    def refresh_calendar(self) -> None:
        settings = self.settings
        urls = settings["ics_urls"]
        today = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        ws = today - dt.timedelta(days=WINDOW_BACK_DAYS)
        we = today + dt.timedelta(days=WINDOW_AHEAD_DAYS)
        sources = []
        instances: dict[str, dict] = {}
        detected_email = ""
        any_ok = False
        for i, url in enumerate(urls):
            text, info = ics_feed.fetch_ics(url, CACHE_DIR)
            src = {"label": mask_url(url), "index": i, **info, "n_events": 0, "calname": ""}
            if text:
                try:
                    inst, cal = ics_feed.expand_events(text, ws, we, local_tz=TZ, source=str(i))
                    src["n_events"] = len(inst)
                    src["calname"] = cal.get("X-WR-CALNAME", "")
                    if "@" in src["calname"] and not detected_email:
                        detected_email = src["calname"].strip().lower()
                    for e in inst:
                        instances.setdefault(e["key"], e)
                    any_ok = any_ok or info["ok"] or info["from_cache"]
                except Exception as e:  # noqa: BLE001
                    src["error"] = f"parse: {type(e).__name__}: {e}"[:300]
                    log(f"calendar parse failed for {src['label']}: {traceback.format_exc()[-600:]}")
            sources.append(src)
        with self.lock:
            self.instances = sorted(instances.values(), key=lambda e: e["start"])
            errors = [s["error"] for s in sources if s.get("error")]
            self.calendar = {
                "ok": any_ok and not errors,
                "error": "; ".join(errors)[:400],
                "fetched_at": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sources": sources,
                "n_instances": len(self.instances),
                "detected_email": detected_email,
                "configured": bool(urls),
            }
            self.calendar_due = time.monotonic() + self.settings["calendar_refresh_min"] * 60
        log(f"calendar: {len(self.instances)} instances from {len(urls)} feed(s)" + (f"; errors: {errors}" if errors else ""))

    # --- polling -----------------------------------------------------------

    def my_email(self) -> str:
        return self.settings.get("my_email") or self.calendar.get("detected_email", "")

    def tracked(self, event: dict) -> tuple[bool, str]:
        settings = dict(self.settings)
        settings["my_email"] = self.my_email()
        return attendance.is_tracked(event, settings)

    def poll(self) -> None:
        t0 = time.monotonic()
        settings = self.settings
        sig = signals.collect(allow_applescript=settings["allow_applescript"], with_camera=settings["camera_probe"])
        now = now_utc()
        heads_up = dt.timedelta(minutes=settings["heads_up_min"])
        with self.lock:
            instances = list(self.instances)
        live_window = [e for e in instances
                       if attendance.parse_ts(e["start"]) - heads_up <= now < attendance.parse_ts(e["end"]) + dt.timedelta(minutes=2)]
        kinds = {e["key"]: attendance.classify_kind(e, self.my_email()) for e in live_window}
        presences: dict[str, dict] = {}
        for e in live_window:
            kind, _n = kinds[e["key"]]
            if kind == "call":
                presences[e["key"]] = signals.presence_for(e.get("links", []), sig)
            else:
                presences[e["key"]] = {"state": "unknown", "confidence": "none", "app": "", "other_calls": [],
                                       "via": {"in_person": "in person — attendance is not checked",
                                               "needs_link": "no meeting link — attendance cannot be checked",
                                               "personal": "personal entry"}[kind]}
        # one call credits one meeting: a verified match beats a link-less guess
        in_meeting_keys = attendance.attribute_shared_calls(presences, {e["key"]: e["title"] for e in live_window})
        for e in live_window:
            start, end = attendance.parse_ts(e["start"]), attendance.parse_ts(e["end"])
            pres = presences[e["key"]]
            if kinds[e["key"]][0] != "call":
                continue  # nothing to observe: no link to match against
            if start <= now < end:
                tracked, _ = self.tracked(e)
                rec = self.store.observe(e, pres["state"], now, tracked)
                if pres["state"] == "in_call" and rec.get("nag_level"):
                    rec["nag_level"] = 0  # seen in the call: the ladder starts over if you drop out
            elif now >= end:
                rec = self.store.record(e, create=False)
                if rec is not None and not rec["final"]:
                    self.store.observe(e, pres["state"], now, rec.get("tracked", True))
        # alerts: only a *verified* call in another meeting silences the nagging
        in_other = bool(in_meeting_keys)
        nagging = False
        for e in live_window:
            start, end = attendance.parse_ts(e["start"]), attendance.parse_ts(e["end"])
            if now >= end:
                continue
            tracked, _ = self.tracked(e)
            if not tracked:
                continue
            rec = self.store.record(e)
            pres = presences[e["key"]]
            other = in_other and e["key"] not in in_meeting_keys
            kind, n_others = kinds[e["key"]]
            decision = attendance.alert_decision(rec, e, pres, now, settings, other, kind=kind, n_others=n_others)
            if decision:
                decision["key"] = e["key"]
                decision["first_missing"] = attendance.last_alert(rec, attendance.NAG_KINDS) is None
                self.fire(decision, rec, now)
            if kind == "call" and start <= now < end and pres["state"] != "in_call" and rec.get("nag_level") and not rec.get("skipped"):
                nagging = True
        self._nagging = nagging
        self.store.finalize_ended(now)
        with self.lock:
            self.signals = sig
            self.polls += 1
            self.last_poll_ms = int((time.monotonic() - t0) * 1000)
            self.last_poll_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            self._presences = presences
            self.annotated = self.annotate(self.today(), now)
            self.live = attendance.live_summary(self.annotated, now, settings)
        try:
            write_json_atomic(SNAPSHOT_PATH, {"ts": self.last_poll_at, "live": self.live, "signals": sig})
        except OSError:
            pass

    def deliver(self, decision: dict, level: int = 0, on_button=None) -> dict:
        """One place that hands an alert to `alerts.deliver` while keeping at
        most one dialog per process (the on-disk pid file in `alerts` keeps
        it to one per machine). Used by real alerts and the test button."""
        settings = self.settings
        on_button = on_button or self.on_dialog_button

        def done_with_dialog(alert, btn):
            with self.lock:
                self._dialog_open = False
            on_button(alert, btn)

        with self.lock:
            dialog_allowed = not self._dialog_open
            wants_dialog = dialog_allowed and settings.get("alert_dialog", True) and not decision.get("soft")
            if wants_dialog:
                self._dialog_open = True
        done = alerts.deliver(decision, settings, on_button=done_with_dialog, dialog_allowed=dialog_allowed, level=level)
        if wants_dialog and not done.get("dialog"):
            with self.lock:
                self._dialog_open = False
        return done

    def fire(self, decision: dict, rec: dict, now: dt.datetime) -> None:
        level = int(rec.get("nag_level") or 0) if decision["kind"] in attendance.NAG_KINDS else 0
        done = self.deliver(decision, level=level)
        self.store.add_alert(rec, decision["kind"], decision["title"], now)
        if done.get("opened"):
            self.store.set_flags(rec, opened=True)
        entry = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": decision["kind"], "title": decision["title"],
                 "message": decision["message"], "key": decision.get("key", ""), "delivered": done}
        with self.lock:
            self.alert_log.append(entry)
        try:
            with open(ALERT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
        log(f"alert {decision['kind']}: {decision['title']} -> {done}")

    def on_dialog_button(self, alert: dict, button: str | None) -> None:
        key = alert.get("key")
        rec = self.record_by_key(key) if key else None
        if button == alerts.REPLACED:
            log(f"dialog for {key} replaced by a newer one")
            return
        if button is None:
            log(f"dialog unanswered for {key} — escalating")
            self.wake.set()
            return
        if rec is not None:
            self.store.ack(rec, now_utc())  # any interaction restarts the ladder
        if button == alerts.BTN_OPEN and alert.get("url"):
            alerts.open_url(alert["url"])
            if rec is not None:
                self.store.set_flags(rec, opened=True)
        elif button == alerts.BTN_SKIP and rec is not None:
            self.store.set_flags(rec, skipped=True)
        log(f"dialog button {button!r} for {key}")
        self.wake.set()

    def record_by_key(self, key: str) -> dict | None:
        for e in self.instances:
            if e["key"] == key:
                return self.store.record(e)
        # not in current instances: search loaded days
        for day in self.store.days():
            rec = self.store.records_for_day(day).get(key)
            if rec is not None:
                return rec
        return None

    def event_by_key(self, key: str) -> dict | None:
        for e in self.instances:
            if e["key"] == key:
                return e
        return None

    # --- views -------------------------------------------------------------

    def today(self) -> str:
        return dt.datetime.now(TZ).strftime("%Y-%m-%d")

    def annotate(self, day: str, now: dt.datetime) -> list[dict]:
        out = []
        presences = getattr(self, "_presences", {})
        records = self.store.records_for_day(day)
        seen = set()
        for e in self.instances:
            if self.store.day_of(e["start"]) != day:
                continue
            start, end = attendance.parse_ts(e["start"]), attendance.parse_ts(e["end"])
            phase = "past" if now >= end else ("live" if now >= start else "upcoming")
            tracked, reason = self.tracked(e)
            rec = records.get(e["key"])
            pres = presences.get(e["key"])
            if pres is None and rec is not None and rec.get("last_state"):
                pres = {"state": rec["last_state"], "confidence": "", "via": "last observation", "app": "", "other_calls": []}
            kind, n_others = attendance.classify_kind(e, self.my_email())
            if pres is None:
                pres = {"state": "unknown" if kind != "call" else "absent", "confidence": "", "via": "", "app": "", "other_calls": []}
            seen.add(e["key"])
            out.append({
                "kind": kind, "n_others": n_others,
                "key": e["key"], "uid": e["uid"], "title": e["title"], "start": e["start"], "end": e["end"],
                "links": e.get("links", []), "location": e.get("location", ""), "recurring": e.get("recurring", False),
                "organizer": e.get("organizer", {}), "my_partstat": attendance.my_partstat(e, self.my_email()),
                "n_attendees": len(e.get("attendees", [])), "tracked": tracked, "track_reason": reason,
                "phase": phase, "presence": pres, "record": self._public_record(rec),
                "description": (e.get("description") or "")[:240],
            })
        # records for events no longer in the feed (deleted/moved) — history stays honest
        for key, rec in records.items():
            if key in seen:
                continue
            out.append({
                "key": key, "uid": rec["uid"], "title": rec["title"], "start": rec["start"], "end": rec["end"],
                "links": rec.get("links", []), "location": "", "recurring": False, "organizer": {}, "my_partstat": "",
                "n_attendees": 0, "tracked": rec.get("tracked", True), "track_reason": "no longer on calendar",
                "kind": "call" if rec.get("links") else "personal", "n_others": 0,
                "phase": "past" if now >= attendance.parse_ts(rec["end"]) else "live",
                "presence": {"state": rec.get("last_state") or "absent", "confidence": "", "via": "", "app": "", "other_calls": []},
                "record": self._public_record(rec), "description": "", "gone": True,
            })
        out.sort(key=lambda x: (x["start"], x["title"]))
        return out

    @staticmethod
    def _public_record(rec: dict | None) -> dict | None:
        if rec is None:
            return None
        return {k: rec.get(k) for k in ("in_s", "lobby_s", "first_in", "last_in", "last_state", "last_seen", "verdict",
                                        "late_s", "snoozed_until", "skipped", "opened", "final", "segments",
                                        "nag_level", "acked_at")} | {
            "alerts": rec.get("alerts", [])[-10:]}

    def state(self, day: str | None = None) -> dict:
        now = now_utc()
        with self.lock:
            day = day or self.today()
            events = self.annotated if day == self.today() and self.annotated else self.annotate(day, now)
            if day == self.today():
                live = self.live if self.annotated else attendance.live_summary(events, now, self.settings)
            else:
                live = self.live
            settings = dict(self.settings)
            browsers = self.signals.get("browsers", {}) if self.signals else {}
            return {
                "ok": True,
                "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tz": str(TZ),
                "today": self.today(),
                "day": day,
                "engine": {"pid": os.getpid(), "version": self.version, "started": self.started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "uptime_s": int((now - self.started).total_seconds()), "polls": self.polls,
                           "last_poll_ms": self.last_poll_ms, "last_poll": self.last_poll_at, "next_poll": self.next_poll_at,
                           "last_error": self.last_error, "python": sys.version.split()[0]},
                "settings": settings,
                "calendar": self.calendar,
                "signals": self.signals,
                "permissions": {
                    "browsers": browsers,
                    "camera": (self.signals.get("camera_in_use") is not None) if self.signals else None,
                    "any_denied": any(b.get("applescript") == "denied" for b in browsers.values()),
                },
                "live": live,
                "events": events,
                "alerts": list(self.alert_log)[-30:][::-1],
                "days": self.store.days(),
                "sounds": alerts.available_sounds(),
            }

    def history(self, days: int = 30) -> dict:
        out = []
        for day in self.store.days()[-days:]:
            recs = self.store.records_for_day(day)
            counts = collections.Counter()
            items = []
            for rec in sorted(recs.values(), key=lambda r: r["start"]):
                v = rec.get("verdict") or ("attending" if rec.get("last_state") == "in_call" else "pending")
                if rec.get("tracked", True):
                    counts[v] += 1
                items.append({"key": rec["key"], "title": rec["title"], "start": rec["start"], "end": rec["end"],
                              "verdict": v, "in_s": rec.get("in_s", 0), "late_s": rec.get("late_s"), "tracked": rec.get("tracked", True),
                              "skipped": rec.get("skipped", False), "n_alerts": len(rec.get("alerts", [])), "final": rec.get("final", False)})
            out.append({"day": day, "counts": dict(counts), "events": items})
        return {"ok": True, "days": out[::-1]}

    # --- loop --------------------------------------------------------------

    def _stat_siblings(self) -> dict:
        out = {}
        for name in SIBLINGS + ["daemon.py"]:
            try:
                out[name] = os.path.getmtime(os.path.join(HERE, name))
            except OSError:
                out[name] = None
        return out

    def _code_changed(self) -> bool:
        return self._stat_siblings() != self._sibling_mtimes

    def run(self) -> None:
        log(f"monitor up: pid {os.getpid()} version {self.version} tz {TZ} data {DATA_DIR}")
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= self.calendar_due:
                    self.refresh_calendar()
                self.poll()
                self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                log("poll failed: " + traceback.format_exc()[-1200:])
            if superseded():
                log(f"pid {os.getpid()} superseded by a newer daemon instance — exiting")
                alerts.dismiss_open_dialog()
                os._exit(0)
            if self._code_changed():
                log("code changed on disk — asking fused-render to restart this daemon")
                self._sibling_mtimes = self._stat_siblings()
                threading.Thread(target=self_restart, daemon=True).start()
            interval = self.settings["poll_live_s"] if self._something_live() else self.settings["poll_s"]
            if self._nagging:
                interval = min(interval, 7.5)  # a 15 s ladder needs sub-15 s checks
            if not self.settings["ics_urls"]:
                interval = max(interval, 60)
            self.next_poll_at = (now_utc() + dt.timedelta(seconds=interval)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.wake.wait(timeout=interval)
            self.wake.clear()
        log("monitor stopped")

    def _something_live(self) -> bool:
        now = now_utc()
        soon = dt.timedelta(minutes=max(5, self.settings["heads_up_min"] + 1))
        for e in self.instances:
            if attendance.parse_ts(e["start"]) - soon <= now < attendance.parse_ts(e["end"]) + dt.timedelta(minutes=2):
                return True
        return False


def self_restart() -> None:
    """Ask the fused-render server to respawn us (fresh code). Falls back to
    exiting; heal-on-proxy revives the daemon on the page's next call."""
    time.sleep(1.0)
    try:
        home = os.environ.get("FUSED_RENDER_HOME_DIR") or os.path.expanduser("~/.fused-render")
        with open(os.path.join(home, "server.json"), encoding="utf-8") as f:
            info = json.load(f)
        if info["shared"] not in sys.path:
            sys.path.insert(0, info["shared"])
        import background_app  # noqa: E402
        background_app.restart()
    except Exception as e:  # noqa: BLE001
        log(f"self-restart via server failed ({e}); exiting so the next call respawns me")
        os._exit(0)


# ------------------------------------------------------------------- server -

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    token = ""
    version = ""
    monitor: Monitor


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"
    timeout = 70

    def log_message(self, _format, *_args):
        return

    def _authorized(self) -> bool:
        supplied = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.server.token)

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._json(403, {"error": "forbidden"})
        if path == "/ping":
            return self._json(200, {"ok": True, "version": self.server.version, "pid": os.getpid()})
        if path == "/quit":
            self.server.monitor.stop_event.set()
            self.server.monitor.wake.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.strip("/")
        if not self._authorized():
            return self._json(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}") if length > 0 else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"ok": False, "error": f"invalid body: {e}"})
        try:
            result = self.route(path, body)
        except Exception as e:  # noqa: BLE001
            log(f"route {path} failed: {traceback.format_exc()[-1200:]}")
            result = {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-2000:]}
        return self._json(200, result)

    def route(self, path: str, body: dict) -> dict:
        m = self.server.monitor
        if path == "state":
            return m.state(body.get("day") or None)
        if path == "settings":
            m.save_settings(body.get("settings") or body)
            return m.state()
        if path == "track":
            uid, mode = body.get("uid", ""), body.get("mode", "default")
            with m.lock:
                tracked = [u for u in m.settings["tracked_uids"] if u != uid]
                ignored = [u for u in m.settings["ignored_uids"] if u != uid]
                if mode == "track":
                    tracked.append(uid)
                elif mode == "ignore":
                    ignored.append(uid)
            m.save_settings({"tracked_uids": tracked, "ignored_uids": ignored})
            with m.lock:
                m.annotated = m.annotate(m.today(), now_utc())
                m.live = attendance.live_summary(m.annotated, now_utc(), m.settings)
            return m.state()
        if path == "refresh":
            m.refresh_calendar()
            m.poll()
            return m.state()
        if path in ("snooze", "skip", "unskip", "open", "ack"):
            rec = m.record_by_key(body.get("key", ""))
            if rec is None:
                return {"ok": False, "error": "unknown meeting"}
            m.store.ack(rec, now_utc())
            if path == "ack":
                pass
            elif path == "snooze":
                minutes = float(body.get("minutes") or 5)
                m.store.set_flags(rec, snoozed_until=(now_utc() + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            elif path == "skip":
                m.store.set_flags(rec, skipped=True)
            elif path == "unskip":
                m.store.set_flags(rec, skipped=False, snoozed_until=None)
            else:
                links = rec.get("links") or []
                if not links or not alerts.open_url(links[0]["url"]):
                    return {"ok": False, "error": "this meeting has no link to open"}
                m.store.set_flags(rec, opened=True)
            with m.lock:
                m.annotated = m.annotate(m.today(), now_utc())
            return m.state()
        if path == "test_alert":
            demo = {"kind": "missing", "title": "Missing meeting: Example sync (test)", "key": "",
                    "message": "This is what an alert looks like. Started 3 min ago, ends 10:30.",
                    "url": "https://meet.google.com/", "soft": False, "first_missing": False}
            done = m.deliver(demo, on_button=lambda a, b: log(f"test dialog -> {b!r}"))
            return {"ok": True, "delivered": done}
        if path == "permissions":
            action = body.get("action")
            if action == "open_pane":
                signals.open_permissions_pane()
                return {"ok": True}
            if action == "probe":
                procs = signals.process_table()
                signals._as_state.clear()
                tabs, report = signals.browser_tabs(procs, allow_applescript=True, first_prompt=True)
                with m.lock:
                    if m.signals:
                        m.signals["browsers"] = report
                        m.signals["tabs"] = tabs
                m.wake.set()
                return {"ok": True, "browsers": report, "n_meeting_tabs": len(tabs)}
            return {"ok": False, "error": "unknown permissions action"}
        if path == "history":
            return m.history(int(body.get("days") or 30))
        if path == "signals":
            return {"ok": True, "signals": m.signals}
        return {"ok": False, "error": f"unknown route {path!r}"}


def publish_status(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


# ------------------------------------------------------- single instance --

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def other_instances() -> list[int]:
    """Pids of every other daemon.py running for THIS app folder."""
    me = os.path.abspath(__file__)
    found: list[int] = []
    try:
        p = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return found
    for line in p.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, cmd = int(parts[0]), parts[1]
        if pid != os.getpid() and me in cmd and "python" in cmd.lower():
            found.append(pid)
    return found


def retire_other_instances() -> list[int]:
    """Newest instance wins: stop every earlier copy (orphans of previous
    fused-render servers included) and close any dialog one of them left on
    screen, so alerts come from exactly one process."""
    victims = other_instances()
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and any(_pid_alive(p) for p in victims):
        time.sleep(0.1)
    for pid in victims:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    alerts.dismiss_open_dialog()
    if victims:
        log(f"retired {len(victims)} earlier daemon instance(s): {victims}")
    return victims


def claim_instance() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = f"{PID_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    os.replace(tmp, PID_PATH)


def superseded() -> bool:
    """True when another, live daemon has claimed the pid file since we did."""
    try:
        with open(PID_PATH, encoding="utf-8") as f:
            owner = int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return False
    return owner != 0 and owner != os.getpid() and _pid_alive(owner)


def _on_terminate(signum, _frame) -> None:
    alerts.dismiss_open_dialog()  # never leave our popup behind
    log(f"pid {os.getpid()} got signal {signum} — exiting")
    os._exit(0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _on_terminate)
    retire_other_instances()
    claim_instance()
    monitor = Monitor(args.version)
    server = Server(("127.0.0.1", 0), Handler)
    server.token = secrets.token_urlsafe(32)
    server.version = args.version
    server.monitor = monitor
    port = int(server.server_address[1])
    publish_status(args.status, {"port": port, "token": server.token, "pid": os.getpid(), "version": args.version})
    threading.Thread(target=monitor.run, name="monitor", daemon=True).start()
    log(f"serving on 127.0.0.1:{port}")
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        monitor.stop_event.set()
        server.server_close()
        log("server closed")


if __name__ == "__main__":
    main()
