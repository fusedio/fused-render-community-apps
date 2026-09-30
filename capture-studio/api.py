"""runPython dispatcher for capture_studio.

Single entry point: main(action, session, payload). Every action returns a
JSON-native dict with at least {"ok": true}, or raises (the page shows the
error). Imports are lazy inside functions.

Transcription runs entirely in the browser via fused.ai.transcribe() against
a local speech-to-text model — there is no Python-side ASR here, so this
file only manages session folders on disk.
"""


def _app_dir() -> str:
    import os

    return os.path.dirname(os.path.abspath(__file__))


def _sessions_dir() -> str:
    import os

    d = os.path.join(_app_dir(), "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def _validate_session_id(session: str) -> None:
    if not session:
        raise ValueError("session id required")
    for bad in ("/", "\\", "..", ":"):
        if bad in session:
            raise ValueError(f"invalid session id: {session}")


def _new_session() -> dict:
    import datetime
    import os

    sessions_dir = _sessions_dir()
    session_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = session_id
    suffix = 0
    while os.path.isdir(os.path.join(sessions_dir, candidate)):
        suffix += 1
        candidate = f"{session_id}-{suffix}"

    os.makedirs(os.path.join(sessions_dir, candidate))
    return {"ok": True, "session": candidate}


def _finalize_session(session: str) -> dict:
    import base64
    import os

    _validate_session_id(session)
    session_dir = os.path.join(_sessions_dir(), session)
    if not os.path.isdir(session_dir):
        raise ValueError(f"no such session: {session}")

    for name in sorted(os.listdir(session_dir)):
        if not name.endswith(".b64"):
            continue
        b64_path = os.path.join(session_dir, name)
        with open(b64_path, "r", encoding="utf-8") as f:
            data = base64.b64decode(f.read())
        bin_path = os.path.join(session_dir, name[: -len(".b64")])
        with open(bin_path, "wb") as f:
            f.write(data)
        os.remove(b64_path)

    return {"ok": True}


def _list_sessions() -> dict:
    import json
    import os

    sessions_dir = _sessions_dir()
    sessions = []
    for name in os.listdir(sessions_dir):
        session_dir = os.path.join(sessions_dir, name)
        if not os.path.isdir(session_dir):
            continue

        started_at = None
        ended_at = None
        shots = 0
        upload_path = os.path.join(session_dir, "upload.json")
        if os.path.isfile(upload_path):
            with open(upload_path, "r", encoding="utf-8") as f:
                upload = json.load(f)
            started_at = upload.get("started_at")
            ended_at = upload.get("ended_at")
            shots = len(upload.get("shots", []))

        state = "new"
        job_path = os.path.join(session_dir, "job.json")
        if os.path.isfile(job_path):
            with open(job_path, "r", encoding="utf-8") as f:
                state = json.load(f).get("state", "unknown")

        text_preview = ""
        transcript_path = os.path.join(session_dir, "transcript.json")
        if os.path.isfile(transcript_path):
            with open(transcript_path, "r", encoding="utf-8") as f:
                text_preview = json.load(f).get("text", "")[:120]

        sessions.append(
            {
                "id": name,
                "started_at": started_at,
                "ended_at": ended_at,
                "state": state,
                "shots": shots,
                "text_preview": text_preview,
            }
        )

    sessions.sort(key=lambda s: s["id"], reverse=True)
    return {"ok": True, "sessions": sessions}


def _delete_session(session: str) -> dict:
    import os
    import shutil

    _validate_session_id(session)
    session_dir = os.path.join(_sessions_dir(), session)
    if os.path.isdir(session_dir):
        shutil.rmtree(session_dir)
    return {"ok": True}


def _check_setup() -> dict:
    return {
        "ok": True,
        # The page needs absolute paths for fused.readFile/writeFile/rawUrl;
        # this is the one authoritative source for where the app lives.
        "app_dir": _app_dir().replace("\\", "/"),
    }


def main(action: str = "", session: str = "", payload: str = "") -> dict:
    if action == "new_session":
        return _new_session()
    if action == "finalize_session":
        return _finalize_session(session)
    if action == "list_sessions":
        return _list_sessions()
    if action == "delete_session":
        return _delete_session(session)
    if action == "check_setup":
        return _check_setup()
    raise ValueError(f"unknown action: {action}")
