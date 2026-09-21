"""Alert delivery on macOS: Notification Center banner, system sound, spoken
text, a modal dialog with Open / Snooze / Skip buttons, and opening the
meeting link. All via stock binaries (osascript, afplay, say, open) so the
daemon needs no extra permissions beyond what a notification takes."""
from __future__ import annotations

import os
import subprocess
import threading

SOUNDS_DIR = "/System/Library/Sounds"


def _q(s: str) -> str:
    """Quote for embedding inside an AppleScript string literal."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _osascript(script: str, timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip() or (p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return -1, str(e)


def notify(title: str, message: str, subtitle: str = "", sound: str | None = None) -> bool:
    script = f'display notification "{_q(message)}" with title "{_q(title)}"'
    if subtitle:
        script += f' subtitle "{_q(subtitle)}"'
    if sound:
        script += f' sound name "{_q(sound)}"'
    code, _ = _osascript(script, timeout=10)
    return code == 0


def available_sounds() -> list[str]:
    try:
        return sorted(n[:-5] for n in os.listdir(SOUNDS_DIR) if n.endswith(".aiff"))
    except OSError:
        return []


def play_sound(name: str, volume: float = 1.5) -> None:
    if not name:
        return
    path = os.path.join(SOUNDS_DIR, f"{name}.aiff")
    if not os.path.exists(path):
        return
    try:
        subprocess.Popen(["afplay", "-v", str(volume), path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def speak(text: str) -> None:
    try:
        subprocess.Popen(["say", text[:200]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def open_url(url: str) -> bool:
    if not url or not (url.startswith("http://") or url.startswith("https://") or url.startswith("zoommtg://") or url.startswith("msteams://")):
        return False
    try:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


BTN_OPEN, BTN_DISMISS, BTN_SKIP = "Open meeting", "Dismiss", "Skip this one"
#: Returned by `dialog()` when a newer dialog took its place: not an answer,
#: not a miss — the caller must neither ack nor escalate.
REPLACED = "__replaced__"
DIALOG_TIMEOUT_S = 20

#: Set by the daemon to `<cache>/dialog.pid`. Records the osascript process
#: showing the current dialog so that *any* process (a newer daemon instance,
#: a restart) can dismiss it before showing its own: one popup at a time,
#: machine-wide, and each new one replaces the last instead of stacking.
DIALOG_PID_PATH: str | None = None
_dialog_lock = threading.Lock()
_dialog_proc: subprocess.Popen | None = None


def _process_command(pid: int) -> str:
    try:
        p = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return (p.stdout or "").strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _kill_osascript(pid: int) -> bool:
    """Terminate `pid` only if it really is an osascript (a dialog)."""
    if pid <= 0 or pid == os.getpid() or "osascript" not in _process_command(pid):
        return False
    try:
        os.kill(pid, 15)
        return True
    except OSError:
        return False


def _read_dialog_pid() -> int:
    if not DIALOG_PID_PATH:
        return 0
    try:
        with open(DIALOG_PID_PATH, encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def _record_dialog_pid(pid: int | None, only_if: int | None = None) -> None:
    """Write the dialog's pid, or (pid=None) forget it. `only_if` guards the
    forget: a newer dialog from another process may already own the file."""
    if not DIALOG_PID_PATH:
        return
    try:
        if pid is None:
            if only_if is not None and _read_dialog_pid() != only_if:
                return
            if os.path.exists(DIALOG_PID_PATH):
                os.remove(DIALOG_PID_PATH)
            return
        os.makedirs(os.path.dirname(DIALOG_PID_PATH), exist_ok=True)
        tmp = f"{DIALOG_PID_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(pid))
        os.replace(tmp, DIALOG_PID_PATH)
    except OSError:
        pass


def dismiss_open_dialog() -> bool:
    """Close whatever alert dialog is on screen, whichever process opened it
    (ours or an earlier daemon instance). Returns True if one was closed."""
    global _dialog_proc
    closed = False
    with _dialog_lock:
        proc = _dialog_proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            closed = True
        except OSError:
            pass
    if DIALOG_PID_PATH:
        try:
            with open(DIALOG_PID_PATH, encoding="utf-8") as f:
                pid = int((f.read() or "0").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and (proc is None or pid != proc.pid):
            closed = _kill_osascript(pid) or closed
        _record_dialog_pid(None)
    return closed


def dialog(title: str, message: str, has_link: bool, timeout_s: int = DIALOG_TIMEOUT_S) -> str | None:
    """Blocking modal. Returns the button name, None when nobody touched it
    before it gave up (= the warning was missed, so the caller escalates), or
    REPLACED when a newer dialog dismissed it. Any dialog already on screen
    is dismissed first: popups replace each other, they never pile up.
    Escape / Cmd-. is the Dismiss button: an interaction, never a skip —
    only an explicit click on Skip silences a meeting for good."""
    global _dialog_proc
    buttons = [BTN_SKIP, BTN_DISMISS] + ([BTN_OPEN] if has_link else [])
    default = BTN_OPEN if has_link else BTN_DISMISS
    btn_list = ", ".join(f'"{b}"' for b in buttons)
    script = (f'display dialog "{_q(message)}" with title "{_q(title)}" buttons {{{btn_list}}} '
              f'default button "{default}" cancel button "{BTN_DISMISS}" giving up after {int(timeout_s)} with icon caution')
    dismiss_open_dialog()
    try:
        proc = subprocess.Popen(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:
        return None
    with _dialog_lock:
        _dialog_proc = proc
    _record_dialog_pid(proc.pid)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s + 15)
        code, out = proc.returncode, (stdout or "").strip() or (stderr or "").strip()
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        code, out = -1, "timeout"
    finally:
        with _dialog_lock:
            if _dialog_proc is proc:
                _dialog_proc = None
        _record_dialog_pid(None, only_if=proc.pid)
    if code != 0:
        if "-128" in out:  # user canceled (Escape) = the cancel button = Dismiss
            return BTN_DISMISS
        if code == -15 or code == 143:  # SIGTERM from dismiss_open_dialog(): superseded, not missed
            return REPLACED
        return None
    if "gave up:true" in out:
        return None
    for b in buttons:
        if f"button returned:{b}" in out:
            return b
    return None


def deliver(alert: dict, settings: dict, on_button=None, dialog_allowed: bool = True, level: int = 0) -> dict:
    """Fire one alert according to settings. Non-blocking: the dialog runs in
    a thread and reports its button (or None on give-up) through
    `on_button(alert, button)`. `level` = unanswered warnings so far in this
    chain: the sound gets louder with it. Returns what was done."""
    done = {"notification": False, "sound": False, "dialog": False, "voice": False, "opened": False}
    soft = alert.get("soft", False)
    sound = settings.get("alert_sound") or ""
    if settings.get("alert_notification", True):
        done["notification"] = notify(alert["title"], alert["message"], subtitle="NoShow", sound=sound or None)
    if sound and not soft:
        play_sound(sound, volume=min(3.0, 1.5 + 0.5 * max(0, level)))
        done["sound"] = True
    if settings.get("alert_voice") and not soft:
        speak(alert["title"])
        done["voice"] = True
    if settings.get("auto_open_link") and not soft and alert.get("url") and alert.get("kind") in ("missing",) and alert.get("first_missing"):
        done["opened"] = open_url(alert["url"])
    if settings.get("alert_dialog", True) and not soft and dialog_allowed:
        def run():
            btn = dialog(alert["title"], alert["message"] + ("\n\n" + alert["url"] if alert.get("url") else ""), bool(alert.get("url")))
            if on_button:
                try:
                    on_button(alert, btn)
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=run, name="alert-dialog", daemon=True).start()
        done["dialog"] = True
    return done
