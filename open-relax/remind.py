"""Open Relax — native reminders.

Scheduling spawns a detached worker (a new session, so the 60 s executor kill
never reaches it) that sleeps until its moment and then rings the OS's own
notification centre.

  .fused/data/reminders.json        the schedule — only ever written by main()
  .fused/data/reminder_events.jsonl append-only, only ever written by workers
"""

import json
import os
import platform
import subprocess
import sys
import time

APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(APP, ".fused", "data")
FILE = os.path.join(DATA, "reminders.json")
EVENTS = os.path.join(DATA, "reminder_events.jsonl")

TITLE = "Open Relax ✿"
DEFAULT_MSG = "Time for a cozy break — breathe for a few minutes ♡"
DAY = 86400


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _load():
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _fired():
    """id -> latest epoch a worker reported ringing."""
    out = {}
    try:
        with open(EVENTS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                rid = e.get("id")
                if rid:
                    out[rid] = max(out.get(rid, 0), e.get("at", 0))
    except OSError:
        pass
    return out


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


# ---------- the actual notification ----------
def _aps(text):
    """An AppleScript string literal. json.dumps would \\u-escape the emoji and
    osascript has no idea what that means — it must stay literal UTF-8."""
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", " ").replace("\r", " ")
    return '"' + text + '"'


def notify(message=DEFAULT_MSG, title=TITLE):
    system = platform.system()
    try:
        if system == "Darwin":
            script = (
                f"display notification {_aps(message)} "
                f"with title {_aps(title)} sound name \"Glass\""
            )
            r = subprocess.run(["osascript", "-e", script], check=False,
                               capture_output=True, timeout=15)
            return r.returncode == 0
        if system == "Linux":
            r = subprocess.run(["notify-send", title, message], check=False,
                               capture_output=True, timeout=15)
            return r.returncode == 0
        if system == "Windows":
            ps = (
                "[reflection.assembly]::loadwithpartialname('System.Windows.Forms');"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                f"$n.ShowBalloonTip(10000,{json.dumps(title)},{json.dumps(message)},'Info')"
            )
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               check=False, capture_output=True, timeout=20)
            return r.returncode == 0
    except Exception:
        pass
    return False


# ---------- detached worker ----------
def _worker(rid, fire, repeat, message):
    while True:
        delay = fire - time.time()
        if delay > 0:
            time.sleep(delay)
        notify(message)
        os.makedirs(DATA, exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": rid, "at": time.time(), "type": "fired"}) + "\n")
        if not repeat:
            return
        fire = max(fire + DAY, time.time() + 60)


def _spawn(rid, fire, repeat, message):
    devnull = open(os.devnull, "wb")
    p = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--worker",
         rid, repr(fire), "1" if repeat else "0", message],
        stdin=subprocess.DEVNULL, stdout=devnull, stderr=devnull,
        start_new_session=True, cwd=APP,
    )
    return p.pid


# ---------- reconciliation ----------
def _view():
    fired = _fired()
    now = time.time()
    rows = []
    for r in _load():
        if r.get("status") == "cancelled":
            continue
        rid, fire = r.get("id"), float(r.get("fire", 0))
        last = fired.get(rid, 0)
        alive = _alive(r.get("pid"))
        if r.get("repeat"):
            nxt = fire
            while nxt <= now:
                nxt += DAY
            status = "pending" if alive else "stopped"
            rows.append({**r, "next": nxt, "status": status, "lastFired": last})
        else:
            if last or (fire <= now and not alive):
                continue                    # rung, or missed while the mac slept
            rows.append({**r, "next": fire,
                         "status": "pending" if alive else "stopped"})
    rows.sort(key=lambda r: r["next"])
    return rows


def _next_at(hhmm):
    """Next local occurrence of 'HH:MM'."""
    hh, _, mm = hhmm.partition(":")
    hh, mm = int(hh), int(mm or 0)
    lt = time.localtime()
    fire = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if fire <= time.time() + 5:
        fire += DAY
    return fire


def main(action: str = "list", minutes: float = 0.0, at: str = "",
         message: str = "", repeat: bool = False, rid: str = ""):
    os.makedirs(DATA, exist_ok=True)
    msg = (message or DEFAULT_MSG).strip()[:200]

    if action == "test":
        return {"ok": notify(msg), "platform": platform.system(), "reminders": _view()}

    if action == "schedule":
        fire = _next_at(at) if at else time.time() + max(1.0, float(minutes)) * 60
        new = {
            "id": f"r{int(time.time() * 1000)}",
            "fire": fire,
            "repeat": bool(repeat),
            "message": msg,
            "at": at,
            "created": time.time(),
            "status": "pending",
        }
        new["pid"] = _spawn(new["id"], fire, new["repeat"], msg)
        rows = [r for r in _load() if r.get("status") != "cancelled"]
        # a daily reminder is a singleton — a second one replaces the first
        if new["repeat"]:
            for r in rows:
                if r.get("repeat") and _alive(r.get("pid")):
                    try:
                        os.kill(int(r["pid"]), 15)
                    except OSError:
                        pass
                    r["status"] = "cancelled"
        rows.append(new)
        _write_atomic(FILE, json.dumps(rows[-50:], indent=1))

    elif action == "cancel":
        rows = _load()
        for r in rows:
            if r.get("id") == rid:
                try:
                    os.kill(int(r.get("pid")), 15)
                except (OSError, TypeError, ValueError):
                    pass
                r["status"] = "cancelled"
        _write_atomic(FILE, json.dumps(rows, indent=1))

    return {"reminders": _view(), "platform": platform.system(), "now": time.time()}


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--worker":
    _worker(sys.argv[2], float(sys.argv[3]), sys.argv[4] == "1", sys.argv[5])
