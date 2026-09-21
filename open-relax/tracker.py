"""Open Relax — listening tracker.

Stores everything the app accumulates under <app>/.fused/data/:
  sessions.jsonl  append-only heartbeats  {"t": epoch, "d": "YYYY-MM-DD", "s": secs, "m": [ids]}
  exercises.jsonl one row per finished breathing practice
                  {"t": epoch, "d": "YYYY-MM-DD", "s": secs, "x": id, "c": cycles, "f": completed}
  settings.json   {"goal": minutes}

Both files feed the same daily totals — a minute of breathing counts toward the
goal exactly like a minute of listening. The page stops the listening heartbeat
while an exercise runs, so the same wall-clock minute is never counted twice.
"""

import json
import os
import time

APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(APP, ".fused", "data")
SESSIONS = os.path.join(DATA, "sessions.jsonl")
EXERCISES = os.path.join(DATA, "exercises.jsonl")
SETTINGS = os.path.join(DATA, "settings.json")

SESSION_GAP = 300          # a gap this long starts a new "session"
EXPLORER_MIN = 25   # distinct sounds tried; the library holds about three hundred
ALL_EXERCISES = ["sigh", "box", "478", "coherent"]


# ---------- tiny io helpers ----------
def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("goal", 10)
    return s


def _events(path=None):
    out = []
    try:
        with open(path or SESSIONS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass                      # a torn line never poisons the stats
    except OSError:
        pass
    out.sort(key=lambda e: e.get("t", 0))
    return out


def _daystr(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def _midnight(epoch):
    lt = time.localtime(epoch)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _add_days(epoch, n):
    lt = time.localtime(epoch)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + n, lt.tm_hour, lt.tm_min, lt.tm_sec, 0, 0, -1))


# ---------- stats ----------
def _stats():
    goal = _settings()["goal"]
    goal_secs = goal * 60
    events = _events()
    now = time.time()
    today = _daystr(now)

    by_day, by_sound = {}, {}
    sessions = 0
    prev_t = None
    mix_max = 0
    night_owl = early_bird = False

    for e in events:
        t = e.get("t", 0)
        secs = float(e.get("s", 0) or 0)
        by_day[e.get("d") or _daystr(t)] = by_day.get(e.get("d") or _daystr(t), 0) + secs
        mix = [m for m in (e.get("m") or []) if m]
        mix_max = max(mix_max, len(mix))
        for m in mix:
            by_sound[m] = by_sound.get(m, 0) + secs
        hour = time.localtime(t).tm_hour
        if hour >= 22 or hour < 4:
            night_owl = True
        if 4 <= hour < 7:
            early_bird = True
        if prev_t is None or t - prev_t > SESSION_GAP:
            sessions += 1
        prev_t = t

    # breathing practices land in the same daily totals as listening
    ex_events = _events(EXERCISES)
    ex_by_id, ex_by_day = {}, {}
    ex_total = ex_cycles = ex_done = 0
    for e in ex_events:
        t = e.get("t", 0)
        secs = float(e.get("s", 0) or 0)
        d = e.get("d") or _daystr(t)
        by_day[d] = by_day.get(d, 0) + secs
        ex_by_day[d] = ex_by_day.get(d, 0) + secs
        xid = e.get("x") or "?"
        slot = ex_by_id.setdefault(xid, {"s": 0, "n": 0})
        slot["s"] += secs
        slot["n"] += 1
        ex_total += secs
        ex_cycles += int(e.get("c", 0) or 0)
        if e.get("f"):
            ex_done += 1
        hour = time.localtime(t).tm_hour
        if hour >= 22 or hour < 4:
            night_owl = True
        if 4 <= hour < 7:
            early_bird = True

    # streak: consecutive days with at least a minute. Today not counting yet
    # doesn't break it — you still have the rest of the day.
    def _streak_from(anchor):
        n, cur = 0, anchor
        while by_day.get(_daystr(cur), 0) >= 60:
            n += 1
            cur = _add_days(cur, -1)
        return n

    midnight = _midnight(now)
    streak = _streak_from(midnight) or _streak_from(_add_days(midnight, -1))

    best = run = 0
    for d in sorted(by_day):
        secs = by_day[d]
        if secs >= 60:
            run += 1
            best = max(best, run)
        else:
            run = 0
    best = max(best, streak)

    days = []
    for i in range(34, -1, -1):
        ts = _add_days(midnight, -i)
        d = _daystr(ts)
        secs = by_day.get(d, 0)
        days.append({"d": d, "s": round(secs), "goal": secs >= goal_secs})

    total = sum(by_day.values())
    goal_days = sum(1 for s in by_day.values() if s >= goal_secs)
    week = sum(by_day.get(_daystr(_add_days(midnight, -i)), 0) for i in range(7))
    today_secs = by_day.get(today, 0)

    fav = max(by_sound.items(), key=lambda kv: kv[1])[0] if by_sound else None

    earned = {
        "first":      total > 0,
        "ten":        total >= 600,
        "hour":       total >= 3600,
        "five":       total >= 18000,
        "goal1":      goal_days >= 1,
        "goal7":      goal_days >= 7,
        "streak3":    best >= 3,
        "streak7":    best >= 7,
        "streak30":   best >= 30,
        "mix":        mix_max >= 4,
        "ex1":        ex_done >= 1,
        "ex10":       ex_done >= 10,
        "exAll":      all(x in ex_by_id for x in ALL_EXERCISES),
        "ex60":       ex_total >= 3600,
        "owl":        night_owl,
        "bird":       early_bird,
        "explorer":   len(by_sound) >= EXPLORER_MIN,
    }

    return {
        "goal": goal,
        "today": round(today_secs),
        "todayPct": min(1.0, today_secs / goal_secs) if goal_secs else 0,
        "week": round(week),
        "total": round(total),
        "sessions": sessions,
        "streak": streak,
        "best": best,
        "goalDays": goal_days,
        "activeDays": sum(1 for s in by_day.values() if s >= 60),
        "days": days,
        "bySound": {k: round(v) for k, v in by_sound.items()},
        "favorite": fav,
        "earned": earned,
        "ex": {
            "today": round(ex_by_day.get(today, 0)),
            "total": round(ex_total),
            "sessions": len(ex_events),
            "done": ex_done,
            "cycles": ex_cycles,
            "byEx": {k: {"s": round(v["s"]), "n": v["n"]} for k, v in ex_by_id.items()},
        },
    }


# ---------- entry point ----------
def main(action: str = "stats", seconds: float = 0.0, sounds: str = "", goal: int = 0,
         exercise: str = "", cycles: int = 0, done: bool = False):
    os.makedirs(DATA, exist_ok=True)

    if action == "goal":
        s = _settings()
        s["goal"] = max(1, min(240, int(goal or 10)))
        _write_atomic(SETTINGS, json.dumps(s))

    elif action == "heartbeat" and seconds > 0:
        now = time.time()
        row = {
            "t": round(now, 1),
            "d": _daystr(now),
            "s": round(min(float(seconds), 3600), 1),
            "m": [x for x in sounds.split(",") if x],
        }
        with open(SESSIONS, "a", encoding="utf-8") as f:      # append is atomic enough
            f.write(json.dumps(row) + "\n")

    elif action == "exercise" and seconds > 0 and exercise:
        now = time.time()
        row = {
            "t": round(now, 1),
            "d": _daystr(now),
            "s": round(min(float(seconds), 7200), 1),
            "x": exercise,
            "c": max(0, int(cycles)),
            "f": bool(done),
        }
        with open(EXERCISES, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    elif action == "reset":
        for p in (SESSIONS, EXERCISES):
            try:
                os.remove(p)
            except OSError:
                pass

    return _stats()
