"""Live "am I in a meeting?" signals from this Mac. Everything here is
read-only and permission-light:

* `pmset -g assertions`  — which app holds a WebRTC wake lock ("WebRTC has
  active PeerConnections": Chrome/Slack/Teams/Discord in a live call) and
  which PID coreaudiod is running the microphone for (`audio-in` resources).
  No permission needed. This is the primary "in a call" evidence.
* CoreAudio (ctypes) — `kAudioDevicePropertyDeviceIsRunningSomewhere` on
  input devices: mic live, even when pmset attribution is missing.
* AVFoundation (PyObjC, isolated subprocess) — camera in use by any app.
* Zoom — the `CptHost` helper exists only while a meeting is joined.
* Browser tabs — which meeting link is actually open. Chromium browsers and
  Safari via AppleScript (one-time Automation permission prompt), Chrome also
  via its on-disk SNSS session files (no permission), Firefox via its
  sessionstore (`recovery.jsonlz4`, no permission). Only URLs of known meeting
  providers are kept; nothing else about browsing is stored.
* Idle time / screen lock via `ioreg`.

`collect()` returns one JSON-friendly snapshot; `presence_for()` turns a
snapshot + an event's links into in_call / lobby / absent.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import datetime as dt
import glob
import json
import os
import plistlib
import re
import struct
import subprocess
import sys
import time

import ics_feed

UTC = dt.timezone.utc

CHROMIUM_APPS = {
    "Google Chrome": "~/Library/Application Support/Google/Chrome",
    "Google Chrome Canary": "~/Library/Application Support/Google/Chrome Canary",
    "Brave Browser": "~/Library/Application Support/BraveSoftware/Brave-Browser",
    "Microsoft Edge": "~/Library/Application Support/Microsoft Edge",
    "Arc": "~/Library/Application Support/Arc/User Data",
    "Vivaldi": "~/Library/Application Support/Vivaldi",
    "Chromium": "~/Library/Application Support/Chromium",
}
MEETING_APPS = {  # process/app name -> provider it serves natively
    "zoom.us": "zoom",
    "Microsoft Teams": "teams",
    "Microsoft Teams (work or school)": "teams",
    "Webex": "webex",
    "Cisco Webex Meetings": "webex",
    "Slack": "slack",
    "Discord": "discord",
    "FaceTime": "facetime",
    "Google Meet": "meet",  # the Meet PWA
}
BROWSER_APPS = set(CHROMIUM_APPS) | {"Safari", "Firefox", "Firefox Developer Edition"}
WEBRTC_LOCK = "WebRTC has active PeerConnections"


def _run(cmd: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except OSError as e:
        return -1, "", str(e)


# ------------------------------------------------------------ processes ----

_APP_RX = re.compile(r"/([^/]+)\.app/")


def app_from_path(path: str) -> str:
    """'/Applications/Google Chrome.app/Contents/Frameworks/.../Google Chrome Helper'
    -> 'Google Chrome'. Non-bundle binaries -> their basename."""
    m = _APP_RX.search(path)
    if m:
        return m.group(1)
    return os.path.basename(path.strip()) or path


def process_table() -> dict[int, dict]:
    code, out, _ = _run(["ps", "-axo", "pid=,ppid=,comm="], timeout=6)
    table: dict[int, dict] = {}
    if code != 0:
        return table
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        comm = parts[2]
        table[pid] = {"ppid": ppid, "comm": comm, "app": app_from_path(comm),
                      "name": os.path.basename(comm), "main": is_main_binary(comm)}
    return table


_MAIN_RX = re.compile(r"^(?:.*/)?([^/]+)\.app/Contents/MacOS/[^/]+$")


def is_main_binary(path: str) -> bool:
    """True for an app bundle's own executable (…/X.app/Contents/MacOS/x), not
    a helper living under Frameworks/ or a nested helper bundle."""
    return path.count(".app/") == 1 and bool(_MAIN_RX.match(path))


def running_apps(procs: dict[int, dict]) -> set[str]:
    return {p["app"] for p in procs.values() if p.get("main")}


def process_running(procs: dict[int, dict], name: str) -> bool:
    return any(p.get("name") == name for p in procs.values())


# ------------------------------------------------------------ assertions ---

_ASSERT_RX = re.compile(r"^\s*pid\s+(\d+)\(([^)]*)\):\s*\[[^\]]*\]\s*(\S+)\s+(\S+)\s+named:\s*\"(.*)\"\s*$")
_CREATED_RX = re.compile(r"Created for PID:\s*(\d+)")
_RESOURCES_RX = re.compile(r"Resources:\s*(.*)$")


def parse_assertions(text: str) -> list[dict]:
    """Records from `pmset -g assertions` 'Listed by owning process' block."""
    records: list[dict] = []
    for line in text.splitlines():
        m = _ASSERT_RX.match(line)
        if m:
            records.append({"pid": int(m.group(1)), "proc": m.group(2), "age": m.group(3),
                            "type": m.group(4), "name": m.group(5), "created_for": None, "resources": ""})
            continue
        if not records:
            continue
        c = _CREATED_RX.search(line)
        if c:
            records[-1]["created_for"] = int(c.group(1))
            continue
        r = _RESOURCES_RX.search(line)
        if r:
            records[-1]["resources"] = r.group(1).strip()
    return records


def summarize_assertions(records: list[dict], procs: dict[int, dict]) -> dict:
    def app_of(pid: int | None, fallback: str = "") -> str:
        if pid is not None and pid in procs:
            return procs[pid]["app"]
        return fallback

    webrtc: set[str] = set()
    mic: dict[str, set[str]] = {}
    audio_out: set[str] = set()
    display: dict[str, set[str]] = {}
    for rec in records:
        app = app_of(rec["pid"], rec["proc"])
        if WEBRTC_LOCK in rec["name"]:
            webrtc.add(app)
        if rec["proc"] == "coreaudiod" and rec["resources"]:
            owner = app_of(rec["created_for"], f"pid {rec['created_for']}")
            if "audio-in" in rec["resources"]:
                dev = rec["resources"].split("audio-in", 1)[1].strip().split()[0] if "audio-in" in rec["resources"] else ""
                mic.setdefault(owner, set()).add(dev)
            elif "audio-out" in rec["resources"]:
                audio_out.add(owner)
        if rec["type"] in ("NoDisplaySleepAssertion", "PreventUserIdleDisplaySleep") and app not in ("powerd",):
            display.setdefault(app, set()).add(rec["name"])
    return {
        "webrtc_apps": sorted(webrtc),
        "mic_apps": {k: sorted(v) for k, v in sorted(mic.items())},
        "audio_out_apps": sorted(audio_out),
        "display_lock_apps": {k: sorted(v) for k, v in sorted(display.items())},
    }


# -------------------------------------------------------------- CoreAudio --

def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


class _AOPA(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


_ca = None


def _coreaudio():
    global _ca
    if _ca is None:
        lib = ctypes.util.find_library("CoreAudio")
        _ca = ctypes.cdll.LoadLibrary(lib) if lib else False
    return _ca or None


def _ca_get(obj: int, sel: str, scope: str, ctype):
    ca = _coreaudio()
    if ca is None:
        return None
    addr = _AOPA(_fourcc(sel), _fourcc(scope), 0)
    size = ctypes.c_uint32(0)
    if ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0:
        return None
    n = max(size.value // ctypes.sizeof(ctype), 1)
    buf = (ctype * n)()
    if ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf) != 0:
        return None
    return list(buf)[: size.value // ctypes.sizeof(ctype)]


def _ca_cfstring(obj: int, sel: str) -> str:
    ca = _coreaudio()
    if ca is None:
        return ""
    cf_lib = ctypes.util.find_library("CoreFoundation")
    cf = ctypes.cdll.LoadLibrary(cf_lib)
    addr = _AOPA(_fourcc(sel), _fourcc("glob"), 0)
    ref = ctypes.c_void_p(0)
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    if ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(ref)) != 0 or not ref.value:
        return ""
    cf.CFStringGetLength.restype = ctypes.c_long
    n = cf.CFStringGetLength(ref)
    buf = ctypes.create_string_buffer(n * 4 + 1)
    cf.CFStringGetCString(ref, buf, len(buf), 0x08000100)
    cf.CFRelease(ref)
    return buf.value.decode("utf-8", "replace")


def microphone_devices() -> list[dict]:
    """[{name, running}] for every audio device with input streams."""
    out: list[dict] = []
    devs = _ca_get(1, "dev#", "glob", ctypes.c_uint32) or []
    for d in devs:
        streams = _ca_get(d, "stm#", "inpt", ctypes.c_uint32) or []
        if not streams:
            continue
        running = _ca_get(d, "gone", "glob", ctypes.c_uint32) or [0]
        out.append({"name": _ca_cfstring(d, "lnam"), "running": bool(running[0])})
    return out


# ----------------------------------------------------------------- camera --

_CAMERA_PROBE = r"""
import json
try:
    import AVFoundation as AV
    devs = AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeVideo) or []
    out = [{"name": str(d.localizedName()), "in_use": bool(d.isInUseByAnotherApplication())} for d in devs]
    print(json.dumps({"ok": True, "devices": out}))
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)[:200]}))
"""


def camera_state(timeout: float = 6.0) -> dict:
    code, out, err = _run([sys.executable, "-c", _CAMERA_PROBE], timeout=timeout)
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (err or "no output")[:200], "devices": [], "in_use": None}
    if not data.get("ok"):
        data.setdefault("devices", [])
        data["in_use"] = None
        return data
    data["in_use"] = any(d["in_use"] for d in data["devices"])
    return data


# ------------------------------------------------------------- idle/lock ----

def idle_seconds() -> float | None:
    code, out, _ = _run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], timeout=6)
    if code != 0:
        return None
    m = re.search(r"HIDIdleTime\"\s*=\s*(\d+)", out)
    return int(m.group(1)) / 1e9 if m else None


def screen_locked() -> bool | None:
    code, out, _ = _run(["ioreg", "-n", "Root", "-d1", "-a"], timeout=6)
    if code != 0 or not out.strip():
        return None
    try:
        data = plistlib.loads(out.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    users = data.get("IOConsoleUsers") or []
    for u in users:
        if u.get("kCGSSessionOnConsoleKey"):
            return bool(u.get("CGSSessionScreenIsLocked", False))
    return None


# ------------------------------------------------------- browser tabs ------

def meeting_tabs_from_urls(app: str, urls: list[str], source: str) -> list[dict]:
    tabs: list[dict] = []
    for url in urls:
        if not url:
            continue
        for link in ics_feed.extract_links(url):
            if link["provider"] == "link":
                continue
            tabs.append({"app": app, "provider": link["provider"], "code": link["code"], "url": link["url"], "source": source})
    return tabs


_AS_CHROMIUM = '''
set out to ""
tell application "%s"
  repeat with w in windows
    repeat with t in tabs of w
      try
        set out to out & (URL of t) & linefeed
      end try
    end repeat
  end repeat
end tell
return out
'''
_AS_SAFARI = '''
set out to ""
tell application "Safari"
  repeat with w in windows
    repeat with t in tabs of w
      try
        set out to out & (URL of t) & linefeed
      end try
    end repeat
  end repeat
end tell
return out
'''


def applescript_tabs(app: str, timeout: float = 8.0) -> tuple[list[str], str]:
    """(urls, status) — status: ok | denied | error | timeout."""
    script = _AS_SAFARI if app == "Safari" else _AS_CHROMIUM % app
    code, out, err = _run(["osascript", "-e", script], timeout=timeout)
    if code == -1 and err == "timeout":
        return [], "timeout"
    if code != 0:
        if "-1743" in err or "Not authorized" in err:
            return [], "denied"
        if "-600" in err or "isn't running" in err:
            return [], "off"
        return [], "error: " + err.strip()[:160]
    return [u.strip() for u in out.splitlines() if u.strip()], "ok"


def open_permissions_pane() -> None:
    _run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"], timeout=5)


# --- Chrome SNSS session files (permission-free) -----------------------------

class _Pickle:
    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.d, self.i)[0]
        self.i += 4
        return v

    def _pad(self, n: int) -> int:
        return n + ((4 - n % 4) % 4)

    def string(self) -> str:
        n = self.u32()
        s = self.d[self.i:self.i + n]
        self.i += self._pad(n)
        return s.decode("utf-8", "replace")

    def string16(self) -> str:
        n = self.u32()
        s = self.d[self.i:self.i + 2 * n]
        self.i += self._pad(2 * n)
        return s.decode("utf-16-le", "replace")


def parse_snss(data: bytes) -> list[dict]:
    """Open tabs [{url, title}] from one SNSS Session_/Apps_ file."""
    if data[:4] != b"SNSS" or len(data) < 8:
        return []
    tabs: dict[int, dict] = {}
    windows_closed: set[int] = set()
    i = 8
    n = len(data)
    while i + 3 <= n:
        size = struct.unpack_from("<H", data, i)[0]
        cmd = data[i + 2]
        payload = data[i + 3:i + 2 + size]
        i += 2 + size
        if size == 0:
            break
        try:
            p = _Pickle(payload)
            if cmd == 6:  # kCommandUpdateTabNavigation
                p.u32()  # pickle payload size
                tab_id = p.u32()
                idx = p.u32()
                url = p.string()
                title = p.string16()
                t = tabs.setdefault(tab_id, {"nav": {}, "current": None, "closed": False, "window": None})
                t["nav"][idx] = (url, title)
                if t["current"] is None:
                    t["current"] = idx
            elif cmd == 7:  # kCommandSetSelectedNavigationIndex
                tab_id = p.u32()
                idx = p.u32()
                tabs.setdefault(tab_id, {"nav": {}, "current": None, "closed": False, "window": None})["current"] = idx
            elif cmd == 0:  # kCommandSetTabWindow
                win = p.u32()
                tab_id = p.u32()
                tabs.setdefault(tab_id, {"nav": {}, "current": None, "closed": False, "window": None})["window"] = win
            elif cmd == 16:  # kCommandTabClosed
                tab_id = p.u32()
                tabs.setdefault(tab_id, {"nav": {}, "current": None, "closed": False, "window": None})["closed"] = True
            elif cmd == 17:  # kCommandWindowClosed
                windows_closed.add(p.u32())
        except (struct.error, IndexError):
            continue
    out: list[dict] = []
    for t in tabs.values():
        if t["closed"] or (t["window"] in windows_closed) or not t["nav"]:
            continue
        cur = t["current"] if t["current"] in t["nav"] else max(t["nav"])
        url, title = t["nav"][cur]
        out.append({"url": url, "title": title})
    return out


_snss_cache: dict[str, tuple[float, int, list[dict]]] = {}


def chromium_session_tabs(user_data_dir: str) -> tuple[list[str], int]:
    """URLs of open tabs across the profiles currently open in a Chromium
    browser, read from its Session_/Apps_ files. Returns (urls, n_files)."""
    base = os.path.expanduser(user_data_dir)
    if not os.path.isdir(base):
        return [], 0
    profiles: list[str] = []
    try:
        with open(os.path.join(base, "Local State"), "rb") as f:
            state = json.load(f)
        profiles = list(state.get("profile", {}).get("last_active_profiles") or [])
    except (OSError, ValueError):
        profiles = []
    if not profiles:
        profiles = [os.path.basename(p) for p in glob.glob(os.path.join(base, "*")) if os.path.isdir(os.path.join(p, "Sessions"))]
    urls: list[str] = []
    n_files = 0
    for prof in profiles:
        sess_dir = os.path.join(base, prof, "Sessions")
        for prefix in ("Session_", "Apps_"):
            files = sorted(glob.glob(os.path.join(sess_dir, prefix + "*")), key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
            if not files:
                continue
            path = files[-1]
            try:
                st = os.stat(path)
                key = path
                cached = _snss_cache.get(key)
                if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                    tabs = cached[2]
                else:
                    with open(path, "rb") as f:
                        tabs = parse_snss(f.read())
                    _snss_cache[key] = (st.st_mtime, st.st_size, tabs)
                n_files += 1
                urls.extend(t["url"] for t in tabs)
            except OSError:
                continue
    return urls, n_files


# --- Firefox sessionstore (permission-free) ----------------------------------

def lz4_block_decompress(src: bytes) -> bytes:
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 0xF
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off == 0 or start < 0:
            raise ValueError("bad lz4 offset")
        for k in range(ml):
            dst.append(dst[start + k])
    return bytes(dst)


def read_mozlz4(path: str) -> dict:
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:8] != b"mozLz40\0":
        raise ValueError("not a mozlz4 file")
    return json.loads(lz4_block_decompress(raw[12:]).decode("utf-8", "replace"))


def _process_start_epoch(procs: dict[int, dict], name_lower: str) -> float | None:
    pids = [pid for pid, p in procs.items() if p.get("main") and p["app"].lower() == name_lower]
    if not pids:
        return None
    code, out, _ = _run(["ps", "-o", "lstart=", "-p", str(min(pids))], timeout=5)
    if code != 0 or not out.strip():
        return None
    try:
        return dt.datetime.strptime(out.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError:
        return None


def firefox_tabs(procs: dict[int, dict] | None = None) -> tuple[list[str], int]:
    """Open-tab URLs from the sessionstore of the Firefox profile(s) in use.
    Firefox rewrites recovery.jsonlz4 only when something changes, so age
    alone is no freshness test: take profiles written since Firefox started,
    else the most recently written one."""
    paths = glob.glob(os.path.expanduser("~/Library/Application Support/Firefox/Profiles/*/sessionstore-backups/recovery.jsonlz4"))
    if not paths:
        return [], 0
    started = _process_start_epoch(procs, "firefox") if procs else None
    stamped = []
    for path in paths:
        try:
            stamped.append((os.path.getmtime(path), path))
        except OSError:
            continue
    if not stamped:
        return [], 0
    stamped.sort(reverse=True)
    chosen = [p for m, p in stamped if started is not None and m >= started - 120] or [stamped[0][1]]
    urls: list[str] = []
    n = 0
    for path in chosen:
        try:
            sess = read_mozlz4(path)
        except (OSError, ValueError):
            continue
        n += 1
        for w in sess.get("windows", []):
            for t in w.get("tabs", []):
                entries = t.get("entries") or []
                idx = int(t.get("index", len(entries))) - 1
                if 0 <= idx < len(entries):
                    urls.append(entries[idx].get("url") or "")
    return urls, n


# ------------------------------------------------------------- snapshot -----

_as_state: dict[str, tuple[float, str]] = {}  # app -> (last try monotonic, status)


def browser_tabs(procs: dict[int, dict], allow_applescript: bool = True, first_prompt: bool = False) -> tuple[list[dict], dict]:
    running = running_apps(procs)
    tabs: list[dict] = []
    report: dict[str, dict] = {}
    now = time.monotonic()
    for app, user_dir in CHROMIUM_APPS.items():
        if app not in running:
            continue
        rep = {"running": True, "applescript": "skipped", "session_files": 0}
        if allow_applescript:
            last = _as_state.get(app)
            retry_after = 300 if (last and last[1] == "denied") else 0
            if not last or now - last[0] >= retry_after:
                urls, status = applescript_tabs(app, timeout=45.0 if first_prompt else 8.0)
                _as_state[app] = (now, status)
                rep["applescript"] = status
                if status == "ok":
                    tabs.extend(meeting_tabs_from_urls(app, urls, "applescript"))
            else:
                rep["applescript"] = last[1]
        urls, n_files = chromium_session_tabs(user_dir)
        rep["session_files"] = n_files
        tabs.extend(meeting_tabs_from_urls(app, urls, "session"))
        report[app] = rep
    if "Safari" in running:
        rep = {"running": True, "applescript": "skipped", "session_files": 0}
        if allow_applescript:
            last = _as_state.get("Safari")
            retry_after = 300 if (last and last[1] == "denied") else 0
            if not last or now - last[0] >= retry_after:
                urls, status = applescript_tabs("Safari", timeout=45.0 if first_prompt else 8.0)
                _as_state["Safari"] = (now, status)
                rep["applescript"] = status
                if status == "ok":
                    tabs.extend(meeting_tabs_from_urls("Safari", urls, "applescript"))
            else:
                rep["applescript"] = last[1]
        report["Safari"] = rep
    for ff in ("Firefox", "Firefox Developer Edition"):
        if ff in running:
            urls, n = firefox_tabs(procs)
            tabs.extend(meeting_tabs_from_urls(ff, urls, "session"))
            report[ff] = {"running": True, "applescript": "n/a", "session_files": n}
    # dedupe by (app, provider, code)
    seen = set()
    uniq = []
    for t in tabs:
        k = (t["app"], t["provider"], t["code"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    return uniq, report


def collect(allow_applescript: bool = True, first_prompt: bool = False, with_camera: bool = True) -> dict:
    t0 = time.monotonic()
    errors: list[str] = []
    procs = process_table()
    apps_up = running_apps(procs)
    code, out, err = _run(["pmset", "-g", "assertions"], timeout=8)
    records = parse_assertions(out) if code == 0 else []
    if code != 0:
        errors.append("pmset: " + (err or "failed")[:120])
    asum = summarize_assertions(records, procs)
    try:
        mics = microphone_devices()
    except Exception as e:  # noqa: BLE001
        mics = []
        errors.append(f"coreaudio: {e}"[:120])
    cam = camera_state() if with_camera else {"in_use": None, "devices": [], "ok": False}
    if with_camera and not cam.get("ok"):
        errors.append("camera: " + str(cam.get("error", ""))[:120])
    tabs, browsers = browser_tabs(procs, allow_applescript=allow_applescript, first_prompt=first_prompt)
    zoom_meeting = process_running(procs, "CptHost")
    calls: list[dict] = []
    call_apps: dict[str, set[str]] = {}
    for app in asum["webrtc_apps"]:
        call_apps.setdefault(app, set()).add("webrtc")
    for app in asum["mic_apps"]:
        call_apps.setdefault(app, set()).add("mic")
    if zoom_meeting:
        call_apps.setdefault("zoom.us", set()).add("zoom-meeting")
    for app, kinds in sorted(call_apps.items()):
        calls.append({"app": app, "signals": sorted(kinds),
                      "provider": MEETING_APPS.get(app, "browser" if app in BROWSER_APPS else "other")})
    mic_running = [m["name"] for m in mics if m["running"]]
    return {
        "ts": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "took_ms": int((time.monotonic() - t0) * 1000),
        "mic_in_use": bool(mic_running) or bool(asum["mic_apps"]),
        "mic_devices_running": mic_running,
        "mic_apps": asum["mic_apps"],
        "webrtc_apps": asum["webrtc_apps"],
        "display_lock_apps": asum["display_lock_apps"],
        "camera_in_use": cam.get("in_use"),
        "camera_devices": cam.get("devices", []),
        "zoom_in_meeting": zoom_meeting,
        "meeting_apps_running": sorted(a for a in MEETING_APPS if a in apps_up),
        "calls": calls,
        "tabs": tabs,
        "browsers": browsers,
        "idle_s": idle_seconds(),
        "screen_locked": screen_locked(),
        "errors": errors,
    }


# ------------------------------------------------------------- presence -----

def _apps_in_call(sig: dict) -> set[str]:
    return {c["app"] for c in sig.get("calls", [])}


def presence_for(links: list[dict], sig: dict) -> dict:
    """Classify attendance of one event from a snapshot.

    Returns {state: in_call|lobby|absent, confidence: high|medium|low,
             via: str, app: str, other_calls: [apps]}.
    - in_call/high: the event's own meeting code is open in a browser that is
      holding a WebRTC lock or the microphone (Meet/Zoom-web/Teams-web), or
      Zoom's CptHost is up for a Zoom link.
    - in_call/medium: the provider's native app is in a call (Teams/Webex/
      Slack) but the specific meeting cannot be verified.
    - lobby: the meeting tab is open but no media is flowing (pre-join screen).
    - in_call/low: event has no recognizable link but some call is active.
    """
    in_call_apps = _apps_in_call(sig)
    webrtc_apps = set(sig.get("webrtc_apps") or [])
    tabs = sig.get("tabs", [])
    best = {"state": "absent", "confidence": "high", "via": "no meeting activity", "app": "", "other_calls": sorted(in_call_apps)}
    rank = {"absent": 0, "lobby": 1, "in_call": 2}

    def consider(cand: dict):
        nonlocal best
        if rank[cand["state"]] > rank[best["state"]]:
            cand["other_calls"] = sorted(in_call_apps - ({cand["app"]} if cand["app"] else set()))
            best = cand

    def browser_in_call(app: str) -> bool:
        # Chromium/WebKit publish a "WebRTC has active PeerConnections" lock
        # only once media flows; a live mic alone is the pre-join preview (or
        # another tab). Firefox publishes no such lock, so the mic decides.
        if app in CHROMIUM_APPS or app == "Safari":
            return app in webrtc_apps
        return app in in_call_apps

    for link in links or []:
        prov, code = link.get("provider"), link.get("code")
        matching = [t for t in tabs if t["provider"] == prov and (t["code"] == code or prov in ("teams", "webex"))]
        for t in matching:
            if browser_in_call(t["app"]):
                consider({"state": "in_call", "confidence": "high", "via": f"{prov} tab with this meeting + live call media in {t['app']}", "app": t["app"]})
            elif t["app"] in in_call_apps:
                consider({"state": "lobby", "confidence": "medium", "via": f"{prov} tab open in {t['app']}: mic on but no call media yet (pre-join screen?)", "app": t["app"]})
            else:
                consider({"state": "lobby", "confidence": "medium", "via": f"{prov} tab open in {t['app']}, no call media yet", "app": t["app"]})
        if prov == "zoom" and sig.get("zoom_in_meeting"):
            consider({"state": "in_call", "confidence": "high", "via": "Zoom meeting in progress (CptHost)", "app": "zoom.us"})
        native = [app for app, p in MEETING_APPS.items() if p == prov and app in in_call_apps]
        if native and prov in ("teams", "webex", "slack", "discord", "meet"):
            consider({"state": "in_call", "confidence": "medium" if prov != "meet" else "high", "via": f"{native[0]} is in a call", "app": native[0]})
    if not links and in_call_apps:
        app = sorted(in_call_apps)[0]
        consider({"state": "in_call", "confidence": "low", "via": f"no link on this event; a call is active in {app}", "app": app})
    return best
