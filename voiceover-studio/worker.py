import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import wave

import fused_ai
import numpy as np

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, ".fused", "data")
CACHE_DIR = os.path.join(APP_DIR, ".fused", "cache")
VOICES_DIR = os.path.join(DATA_DIR, "voices")
DESIGNS_DIR = os.path.join(DATA_DIR, "designs")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
SAMPLES_DIR = os.path.join(CACHE_DIR, "samples")
VOICES_JSON = os.path.join(DATA_DIR, "voices.json")
HISTORY_JSON = os.path.join(DATA_DIR, "history.json")
PREFS_JSON = os.path.join(DATA_DIR, "prefs.json")
DRAFT_JSON = os.path.join(DATA_DIR, "draft.json")

SR = 24000
MAX_REF_SECONDS = 30.0

DEMO_VOICES = [
    ("Narrator Sam", "en_man", "Neutral American male, conversational", "english"),
    ("Clara", "en_woman", "Clear American female, friendly and even", "english"),
]
DEMO_BASE = "https://raw.githubusercontent.com/Blaizzy/mlx-audio/main/examples/voice_prompts/"

SAMPLE_LINES = {
    "chinese": "你好，很高兴认识你。这是我的声音样本。",
    "japanese": "こんにちは。これは私の声のサンプルです。",
    "korean": "안녕하세요. 이것은 제 목소리 샘플입니다.",
}
DEFAULT_SAMPLE_LINE = "Hello there. This is a short sample of my voice, so you can hear how I sound."
DRAFT_MAX_AGE_S = 3600

RUNS = {}
DATA_LOCK = threading.RLock()


def _urlopen(url):
    import ssl

    req = urllib.request.Request(url, headers={"User-Agent": "voiceover-studio"})
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=60, context=ctx)


def _server_origin():
    origin = os.environ.get("FUSED_RENDER_ORIGIN")
    if origin:
        return origin.rstrip("/")
    home = os.environ.get("FUSED_RENDER_HOME_DIR") or os.path.expanduser("~/.fused-render")
    return (_read_json(os.path.join(home, "server.json"), {}).get("origin") or "").rstrip("/")


def _job_id(s):
    return re.sub(r"[^A-Za-z0-9._:-]", "_", str(s))[:120]


def _post(path, body):
    origin = _server_origin()
    if not origin:
        return {}
    try:
        req = urllib.request.Request(
            origin + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Fused": "1"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            rep = json.loads(r.read() or b"{}")
        return rep if isinstance(rep, dict) else {}
    except Exception:
        return {}


def _report_job(body):
    return bool(_post("/api/jobs", body).get("cancel_requested"))


def _cancel_job(job_id):
    _post("/api/jobs/" + urllib.parse.quote(job_id, safe="") + "/cancel", {})


class Cancelled(Exception):
    pass


def _speak(model_id, text, out, voice=None, instruct=None, ref_audio=None, ref_text=None,
           language="auto", cancelled=None, progress=None):
    asked = []

    def watch(job):
        if progress:
            progress(job)
        if cancelled and not asked and cancelled():
            asked.append(job["id"])
            _cancel_job(job["id"])

    try:
        r = fused_ai.speech(text, model=model_id, voice=voice, instruct=instruct,
                            ref_audio=ref_audio, ref_text=ref_text,
                            language=language or "auto", on_progress=watch)
    except fused_ai.ServerNotRunning as e:
        raise ValueError("FusedRender is not running: " + str(e)) from e
    except fused_ai.AiError as e:
        if e.type == "cancelled":
            raise Cancelled() from e
        raise ValueError(str(e)) from e
    os.makedirs(os.path.dirname(out), exist_ok=True)
    shutil.move(r["audio"][0]["path"], out)
    pcm, sr = _read_wav(out)
    return pcm, sr


def _ensure_dirs():
    for d in (DATA_DIR, VOICES_DIR, DESIGNS_DIR, HISTORY_DIR, SAMPLES_DIR):
        os.makedirs(d, exist_ok=True)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    tmp = path + ".%s.tmp" % uuid.uuid4().hex[:6]
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _write_wav(path, audio, sr=SR):
    a = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    pcm = (a * 32767.0).astype("<i2")
    tmp = path + ".%s.tmp" % uuid.uuid4().hex[:6]
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    os.replace(tmp, path)


def _read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        data = w.readframes(w.getnframes())
    a = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32767.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def _decode_any(raw=None, path=None):
    if raw is None:
        with open(path, "rb") as f:
            raw = f.read()
    try:
        import miniaudio

        dec = miniaudio.decode(raw, nchannels=1, sample_rate=SR)
        return np.asarray(dec.samples, dtype=np.int16).astype(np.float32) / 32767.0
    except Exception:
        pass
    with tempfile.TemporaryDirectory() as td:
        src = path
        if src is None:
            src = os.path.join(td, "in.bin")
            with open(src, "wb") as f:
                f.write(raw)
        out = os.path.join(td, "out.wav")
        r = subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@%d" % SR, "-c", "1", src, out],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0 or not os.path.exists(out):
            raise ValueError("Cannot read this audio. Use WAV, MP3, M4A, FLAC or OGG.")
        a, _ = _read_wav(out)
        return a


def _frame_db(pcm, frame=480):
    n = len(pcm) // frame
    if n == 0:
        return np.array([-120.0])
    fr = pcm[: n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(fr * fr, axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9)


def _trim_silence(pcm):
    db = _frame_db(pcm)
    if len(db) < 3:
        return pcm
    noise = float(np.percentile(db, 10))
    thr = max(noise + 8.0, -55.0)
    idx = np.where(db > thr)[0]
    if len(idx) == 0:
        return pcm
    pad = 8
    a = max(0, idx[0] - pad) * 480
    b = min(len(db), idx[-1] + pad + 1) * 480
    return pcm[a:b]


def _normalize(pcm):
    peak = float(np.max(np.abs(pcm))) if len(pcm) else 0.0
    if peak <= 1e-4:
        return pcm
    gain = min(0.89 / peak, 4.0)
    return pcm * gain


def _pitch(pcm):
    frame, hop = 1200, 600
    if len(pcm) < frame * 4:
        return None
    db = _frame_db(pcm, hop)
    gate = float(np.percentile(db, 60))
    lo, hi = SR // 400, SR // 70
    starts = [i for i in range(0, len(pcm) - frame, hop) if db[min(i // hop, len(db) - 1)] >= gate]
    if len(starts) < 8:
        return None
    x = np.stack([pcm[i : i + frame] for i in starts])
    x = (x - x.mean(axis=1, keepdims=True)) * np.hanning(frame)
    spec = np.fft.rfft(x, n=frame * 2, axis=1)
    ac = np.fft.irfft(spec * np.conj(spec), axis=1)[:, :frame]
    e = ac[:, 0] + 1e-9
    seg = ac[:, lo:hi]
    k = np.argmax(seg, axis=1)
    strength = seg[np.arange(len(k)), k] / e
    f0 = SR / (lo + k[strength > 0.4])
    if len(f0) < 8:
        return None
    return round(float(np.median(f0)), 1)


def _silent_reason(pcm):
    peak = float(np.max(np.abs(pcm))) if len(pcm) else 0.0
    if peak == 0.0:
        return "SILENT: The recording is fully silent. The microphone sent no signal."
    if peak < 0.003 or float(np.percentile(_frame_db(pcm), 95)) < -58:
        return "SILENT: The recording is almost silent (peak %.0f dBFS)." % (20 * np.log10(peak + 1e-9))
    return None


def _analyze(pcm, trimmed_from=None):
    dur = len(pcm) / SR
    peak = float(np.max(np.abs(pcm))) if len(pcm) else 0.0
    clip_pct = float(np.mean(np.abs(pcm) >= 0.985) * 100) if len(pcm) else 0.0
    db = _frame_db(pcm)
    speech_db = float(np.percentile(db, 90))
    noise_db = float(np.percentile(db, 10))
    snr = speech_db - noise_db
    checks = []

    def add(key, label, status, detail):
        checks.append({"key": key, "label": label, "status": status, "detail": detail})

    if dur < 5:
        add("length", "Length", "fail", "%.1f s. Record a minimum of 10 s of speech." % dur)
    elif dur < 10:
        add("length", "Length", "warn", "%.1f s. You can use it. 10–30 s gives a better match." % dur)
    elif trimmed_from and trimmed_from > MAX_REF_SECONDS + 0.5:
        add("length", "Length", "ok", "Cut to the first %d s" % MAX_REF_SECONDS)
    else:
        add("length", "Length", "ok", "%.1f s of speech" % dur)

    if speech_db < -38:
        add("level", "Loudness", "fail", "Too quiet. Move nearer to the microphone.")
    elif speech_db < -30:
        add("level", "Loudness", "warn", "Slightly quiet. The app increased the level.")
    else:
        add("level", "Loudness", "ok", "Good level")

    if clip_pct > 0.5:
        add("clip", "Clipping", "fail", "Distorted peaks. Move away from the microphone, or decrease the input gain.")
    elif clip_pct > 0.05:
        add("clip", "Clipping", "warn", "A few clipped peaks")
    else:
        add("clip", "Clipping", "ok", "No distortion")

    if snr < 15:
        add("noise", "Background", "fail", "Noisy room (%.0f dB SNR). Go to a quieter location." % snr)
    elif snr < 25:
        add("noise", "Background", "warn", "Some background noise (%.0f dB SNR)" % snr)
    else:
        add("noise", "Background", "ok", "Clean (%.0f dB SNR)" % snr)

    statuses = [c["status"] for c in checks]
    verdict = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "ok")
    return {
        "pitch_hz": _pitch(pcm),
        "duration": round(dur, 2),
        "peak": round(peak, 3),
        "speech_db": round(speech_db, 1),
        "noise_db": round(noise_db, 1),
        "snr_db": round(snr, 1),
        "clip_pct": round(clip_pct, 3),
        "checks": checks,
        "verdict": verdict,
    }


DEMO_META = {slug: (name, desc, lang) for name, slug, desc, lang in DEMO_VOICES}


def _voice_defaults(v):
    v.setdefault("kind", "demo" if v.get("slug") else "clone")
    v.setdefault("description", "")
    v.setdefault("language", "auto")
    if not v.get("engine_set"):
        v["engine"] = None
    v.setdefault("draft", False)
    v.setdefault("quality", None)
    meta = DEMO_META.get(v.get("slug"))
    if meta and str(v.get("name", "")).startswith("Demo —"):
        v["name"], v["description"], v["language"] = meta
    return v


def _voices(include_drafts=False):
    vs = [_voice_defaults(v) for v in _read_json(VOICES_JSON, [])]
    return vs if include_drafts else _listed(vs)


def _save_voices(vs):
    _write_json(VOICES_JSON, vs)


def _listed(vs):
    return [v for v in vs if not v.get("draft")]


def _find_voice(voice_id):
    return next((v for v in _voices(True) if v["id"] == voice_id), None)


def _history():
    return _read_json(HISTORY_JSON, [])


def _prefs():
    return _read_json(PREFS_JSON, {})


def _gdir(gen_id):
    if not gen_id or "/" in gen_id or ".." in gen_id:
        raise ValueError("Bad generation id")
    return os.path.join(HISTORY_DIR, gen_id)


def _manifest_path(gen_id):
    return os.path.join(_gdir(gen_id), "manifest.json")


def _load_manifest(gen_id):
    man = _read_json(_manifest_path(gen_id), None)
    if man is None:
        raise ValueError("Unknown generation: " + gen_id)
    for i, s in enumerate(man["segments"]):
        s.setdefault("sid", "%04d" % i)
        s.setdefault("rev", 0)
    return man


def _save_manifest(man):
    _write_json(_manifest_path(man["id"]), man)


def _seg_at(man, idx):
    idx = int(idx)
    if not 0 <= idx < len(man["segments"]):
        raise ValueError("This line does not exist. Load the page again.")
    return man["segments"][idx]


def _seg_path(gen_id, seg):
    return os.path.join(_gdir(gen_id), "segs", seg["sid"] + ".wav")


def _spec_key(spec):
    if not spec:
        return ""
    return ("c:" + str(spec.get("voice_id"))) if spec.get("type") == "clone" else ("p:" + str(spec.get("speaker") or ""))


def _spec_label(spec):
    if not spec:
        return ""
    if spec.get("type") == "clone":
        v = _find_voice(spec.get("voice_id"))
        return v["name"] if v else ""
    return str(spec.get("speaker") or "").replace("_", " ").title()


def _history_row(man):
    labels = []
    for s in man["segments"]:
        lab = s.get("voice_label") or man.get("voice_label") or _spec_label(s.get("voice") or man.get("voice"))
        if lab and lab not in labels:
            labels.append(lab)
    first = man["segments"][0] if man["segments"] else {}
    if man.get("duration") is None and man.get("status") == "done":
        try:
            with wave.open(os.path.join(_gdir(man["id"]), "output.wav"), "rb") as w:
                man["duration"] = round(w.getnframes() / float(w.getframerate()), 2)
        except Exception:
            pass
    return {
        "voice_key": _spec_key(first.get("voice") or man.get("voice")),
        "id": man["id"],
        "title": man["title"],
        "mode": man["mode"],
        "status": man["status"],
        "created_at": man["created_at"],
        "duration": man.get("duration"),
        "model_id": man.get("model_id"),
        "voice_label": ", ".join(labels[:3]) + ("…" if len(labels) > 3 else ""),
        "num_segments": len(man["segments"]),
        "snippet": (man.get("source_text") or "")[:140],
        "take": man.get("take", 1),
        "root_id": man.get("root_id", man["id"]),
        "wer": (man.get("eval") or {}).get("wer"),
        "stale": man.get("stale", False),
        "rev": man.get("rev", 0),
    }


def _sync_history(man):
    hist = _history()
    row = _history_row(man)
    for i, h in enumerate(hist):
        if h["id"] == man["id"]:
            hist[i] = row
            break
    else:
        hist.insert(0, row)
    _write_json(HISTORY_JSON, hist)
    return hist


def _old_draft(v):
    try:
        made = time.mktime(time.strptime(v.get("created_at") or "", "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return True
    return time.time() - made > DRAFT_MAX_AGE_S


def a_bootstrap(**_):
    with DATA_LOCK:
        vs = _voices(True)
        keep = []
        for v in vs:
            retired_demo = v.get("kind") == "demo" and v.get("slug") not in DEMO_META
            if (v.get("draft") and _old_draft(v)) or retired_demo:
                try:
                    os.remove(os.path.join(APP_DIR, v["path"]))
                except OSError:
                    pass
            else:
                keep.append(v)
        if keep != _read_json(VOICES_JSON, []):
            _save_voices(keep)
        hist = _history()
        stale = lambda h: "snippet" not in h or "voice_key" not in h or not h.get("voice_label") or (h.get("status") == "done" and not h.get("duration"))
        if any(stale(h) for h in hist):
            rows = []
            for h in hist:
                try:
                    rows.append(_history_row(_load_manifest(h["id"])) if stale(h) else h)
                except Exception:
                    rows.append(h)
            _write_json(HISTORY_JSON, rows)
            hist = rows
        active = {g for g, r in RUNS.items() if r.get("phase") not in ("done", "error", "cancelled")}
        for h in hist:
            if h.get("status") == "pending" and h["id"] not in active:
                try:
                    cur = _load_manifest(h["id"])
                    cur["status"] = "interrupted"
                    _save_manifest(cur)
                    hist = _sync_history(cur)
                except Exception:
                    pass
    return {
        "voices": _listed(keep),
        "history": hist,
        "runs": _runs_status(),
        "prefs": _prefs(),
        "export": _export_info(),
    }


def a_status(**_):
    return {"runs": _runs_status()}


def _runs_status():
    out = {}
    for gid, ru in list(RUNS.items()):
        out[gid] = {k: v for k, v in ru.items() if k != "cancel"}
    return out


def _run_cancelled(gen_id):
    ru = RUNS.get(gen_id) or {}
    if ru.get("cancel"):
        return True
    try:
        return _load_manifest(gen_id).get("status") != "pending"
    except ValueError:
        return True


def a_run_generation(gen_id="", **_):
    man = _load_manifest(gen_id)
    if man.get("status") != "pending":
        return {"phase": man.get("status")}
    if gen_id in RUNS and RUNS[gen_id].get("phase") not in (None, "done", "error", "cancelled"):
        return {"phase": RUNS[gen_id]["phase"]}
    n = len(man["segments"])
    run = RUNS[gen_id] = {"phase": "starting", "idx": 0, "n": n, "message": None, "cancel": False, "title": man["title"]}
    jid = _job_id(gen_id)
    title = "Voiceover: " + man["title"]

    def job(state, **more):
        body = {"id": jid, "title": title, "kind": "task", "cancellable": True, "state": state}
        body.update(more)
        if _report_job(body):
            run["cancel"] = True

    try:
        job("running", detail="The engine starts")
        for i in range(n):
            if _run_cancelled(gen_id):
                break
            line = "Line %d/%d" % (i + 1, n)
            run.update({"phase": "generate", "idx": i, "detail": None})
            job("running", done=i, total=n + 1, detail=line + ": " + man["segments"][i]["text"][:60])

            def progress(rec, line=line, i=i):
                run["detail"] = rec.get("detail")
                if rec.get("detail"):
                    job("running", done=i, total=n + 1, detail=line + " · " + rec["detail"])

            try:
                _generate_segment(gen_id, i, False, lambda: _run_cancelled(gen_id), progress)
            except Cancelled:
                run["cancel"] = True
                break
        if _run_cancelled(gen_id):
            with DATA_LOCK:
                cur = _load_manifest(gen_id)
                if cur.get("status") == "pending":
                    cur["status"] = "cancelled"
                    _save_manifest(cur)
                _sync_history(cur)
            run["phase"] = "cancelled"
            job("cancelled", detail="Cancelled")
            return {"phase": "cancelled"}
        run["phase"] = "mix"
        job("running", done=n, total=n + 1, detail="Mixing")
        a_assemble(gen_id=gen_id)
        run["phase"] = "done"
        job("done", detail="The voiceover is ready")
        return {"phase": "done"}
    except Exception as e:
        run["phase"] = "error"
        run["message"] = str(e)
        try:
            with DATA_LOCK:
                cur = _load_manifest(gen_id)
                if cur.get("status") == "pending":
                    cur["status"] = "error"
                    cur["error"] = str(e)
                    _save_manifest(cur)
                _sync_history(cur)
        except Exception:
            pass
        job("error", detail=str(e))
        return {"phase": "error", "message": str(e)}


def a_save_draft(text="", **_):
    _write_json(DRAFT_JSON, {"text": text})
    return {"ok": True}


def a_set_prefs(prefs_json="", **_):
    p = _prefs()
    p.update(json.loads(prefs_json or "{}"))
    _write_json(PREFS_JSON, p)
    return {"prefs": p}


def a_fetch_demo_voices(**_):
    have = {v.get("slug") for v in _voices(True)}
    added = []
    for name, slug, desc, lang in DEMO_VOICES:
        if slug in have:
            continue
        with _urlopen(DEMO_BASE + slug + ".wav") as r:
            raw = r.read()
        pcm = _decode_any(raw=raw)
        _write_wav(os.path.join(VOICES_DIR, slug + ".wav"), pcm)
        with _urlopen(DEMO_BASE + slug + ".txt") as r:
            ref_text = r.read().decode("utf-8").strip()
        added.append(
            _voice_defaults(
                {
                    "id": "v" + uuid.uuid4().hex[:10],
                    "slug": slug,
                    "kind": "demo",
                    "name": name,
                    "description": desc,
                    "language": lang,
                    "path": ".fused/data/voices/" + slug + ".wav",
                    "ref_text": ref_text,
                    "duration": round(len(pcm) / SR, 2),
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "quality": _analyze(pcm),
                }
            )
        )
    with DATA_LOCK:
        voices = _voices(True)
        have = {v.get("slug") for v in voices}
        voices.extend(v for v in added if v["slug"] not in have)
        _save_voices(voices)
    return {"voices": _listed(voices)}


def a_import_voice(path="", audio_b64="", filename="", name="", **_):
    if audio_b64:
        pcm = _decode_any(raw=base64.b64decode(audio_b64))
    elif path:
        if not os.path.isfile(path):
            raise ValueError("The app cannot find the recording: " + path)
        pcm = _decode_any(path=path)
    else:
        raise ValueError("The app received no audio")
    reason = _silent_reason(pcm)
    if reason:
        raise ValueError(reason)
    trimmed = _trim_silence(pcm)
    full = len(trimmed) / SR
    if full > MAX_REF_SECONDS:
        trimmed = trimmed[: int(MAX_REF_SECONDS * SR)]
    quality = _analyze(trimmed, trimmed_from=full)
    clean = _normalize(trimmed)
    vid = "v" + uuid.uuid4().hex[:10]
    fname = vid + ".wav"
    _write_wav(os.path.join(VOICES_DIR, fname), clean)
    entry = _voice_defaults(
        {
            "id": vid,
            "slug": None,
            "kind": "clone",
            "name": name or os.path.splitext(os.path.basename(filename or ""))[0] or "My voice",
            "path": ".fused/data/voices/" + fname,
            "ref_text": "",
            "duration": round(len(clean) / SR, 2),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "draft": True,
            "quality": quality,
        }
    )
    with DATA_LOCK:
        vs = _voices(True)
        vs.append(entry)
        _save_voices(vs)
    return {"voice": entry, "quality": quality}


def a_update_voice(voice_id="", fields_json="", **_):
    fields = json.loads(fields_json or "{}")
    allowed = {"name", "description", "language", "engine", "ref_text", "draft"}
    vs = _voices(True)
    found = None
    for v in vs:
        if v["id"] == voice_id:
            for k, val in fields.items():
                if k in allowed:
                    v[k] = val
            if "engine" in fields:
                v["engine_set"] = bool(fields["engine"])
            found = v
            break
    if found is None:
        raise ValueError("Voice not found")
    _save_voices(vs)
    return {"voice": found, "voices": _listed(vs)}


def a_delete_voice(voice_id="", **_):
    keep = []
    for v in _voices(True):
        if v["id"] == voice_id:
            try:
                os.remove(os.path.join(APP_DIR, v["path"]))
            except OSError:
                pass
        else:
            keep.append(v)
    _save_voices(keep)
    return {"voices": _listed(keep)}


def a_design_preview(description="", text="", language="auto", model_id="", **_):
    if not description.strip():
        raise ValueError("Describe the voice first")
    if not model_id:
        raise ValueError("No VoiceDesign model is available")
    text = text.strip() or DEFAULT_SAMPLE_LINE
    t0 = time.time()
    did = "d" + uuid.uuid4().hex[:10]
    audio, sr = _speak(model_id, text, os.path.join(DESIGNS_DIR, did + ".wav"),
                       instruct=description.strip(), language=language)
    return {
        "design_id": did,
        "path": ".fused/data/designs/" + did + ".wav",
        "duration": round(len(audio) / sr, 2),
        "seconds": round(time.time() - t0, 1),
        "text": text,
    }


def a_save_design(design_id="", name="", description="", language="auto", text="", engine="17", **_):
    if not design_id or "/" in design_id or ".." in design_id:
        raise ValueError("Bad design id")
    src = os.path.join(DESIGNS_DIR, design_id + ".wav")
    if not os.path.exists(src):
        raise ValueError("The app cannot find the design preview. Generate it again.")
    vid = "v" + uuid.uuid4().hex[:10]
    dst = os.path.join(VOICES_DIR, vid + ".wav")
    pcm, sr = _read_wav(src)
    _write_wav(dst, pcm, sr)
    entry = _voice_defaults(
        {
            "id": vid,
            "slug": None,
            "kind": "designed",
            "name": name or "Designed voice",
            "description": description,
            "language": language or "auto",
            "engine": engine or None,
            "path": ".fused/data/voices/" + vid + ".wav",
            "ref_text": text or DEFAULT_SAMPLE_LINE,
            "duration": round(len(pcm) / sr, 2),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "quality": _analyze(pcm),
        }
    )
    with DATA_LOCK:
        vs = _voices(True)
        vs.append(entry)
        _save_voices(vs)
    for f in os.listdir(DESIGNS_DIR):
        p = os.path.join(DESIGNS_DIR, f)
        try:
            if p == src or time.time() - os.path.getmtime(p) > DRAFT_MAX_AGE_S:
                os.remove(p)
        except OSError:
            pass
    return {"voice": entry, "voices": _listed(vs)}


def a_preset_sample(speaker="", model_id="", language="auto", **_):
    language = language or "auto"
    for name, value in (("speaker", speaker), ("language", language)):
        if not str(value).replace("_", "").isalnum():
            raise ValueError("Unknown %s: %s" % (name, value))
    key = "%s_%s_%s.wav" % (model_id.split("/")[-1], speaker.lower(), language)
    path = os.path.join(SAMPLES_DIR, key)
    if not os.path.exists(path):
        _speak(model_id, SAMPLE_LINES.get(language, DEFAULT_SAMPLE_LINE), path,
               voice=speaker, language=language)
    return {"path": ".fused/cache/samples/" + key}


def _speech_options(voice):
    if voice["type"] == "clone":
        entry = _find_voice(voice.get("voice_id"))
        if entry is None:
            raise ValueError("That voice is deleted. Select a different voice.")
        if not (entry.get("ref_text") or "").strip():
            raise ValueError("Voice '%s' has no reference transcript. Add it on the Voices page." % entry["name"])
        if (entry.get("quality") or {}).get("peak") == 0:
            raise ValueError("Voice '%s' has a silent sample. Delete the voice and record it again." % entry["name"])
        return {
            "ref_audio": os.path.join(APP_DIR, entry["path"]),
            "ref_text": entry["ref_text"],
            "language": voice.get("language") or entry.get("language") or "auto",
        }
    return {
        "voice": voice.get("speaker"),
        "instruct": (voice.get("instruct") or "").strip() or None,
        "language": voice.get("language") or "auto",
    }


def _check_voice(voice, model_id):
    if not model_id or not voice:
        raise ValueError("The line has no voice. Select a voice.")
    if not isinstance(model_id, str) or not isinstance(voice, dict) or voice.get("type") not in ("clone", "preset"):
        raise ValueError("The voice data is not correct. Select a voice again.")
    if not isinstance(voice.get("language") or "auto", str):
        raise ValueError("The language must be text.")
    if voice["type"] == "clone":
        if not isinstance(voice.get("voice_id"), str) or not voice["voice_id"]:
            raise ValueError("The voice data has no voice id. Select a voice again.")
        return
    if not isinstance(voice.get("speaker"), str) or not voice["speaker"]:
        raise ValueError("The preset voice has no speaker. Select the voice again.")
    if not isinstance(voice.get("instruct") or "", str):
        raise ValueError("The style instruction must be text.")


def _check_time(name, value):
    if value is None:
        return None
    try:
        t = float(value)
    except (TypeError, ValueError):
        raise ValueError("The %s time must be a number." % name)
    if not np.isfinite(t):
        raise ValueError("The %s time must be a number." % name)
    if t < 0:
        raise ValueError("The %s time must be 0 or more." % name)
    return round(t, 3)


def _clean_seg(s, man_voice=None, man_model=None, man_label=""):
    return {
        "sid": uuid.uuid4().hex[:8],
        "rev": 0,
        "text": s["text"],
        "start": s.get("start"),
        "end": s.get("end"),
        "speaker": s.get("speaker"),
        "voice": s.get("voice") or man_voice,
        "model_id": s.get("model_id") or man_model,
        "voice_label": s.get("voice_label") or man_label,
        "duration": None,
    }


def a_create_generation(payload="", **_):
    spec = json.loads(payload)
    gen_id = spec["id"]
    gdir = _gdir(gen_id)
    os.makedirs(os.path.join(gdir, "segs"), exist_ok=True)
    root_id = spec.get("root_id") or gen_id
    take = 1
    if root_id != gen_id:
        take = 1 + sum(1 for h in _history() if h.get("root_id", h["id"]) == root_id)
    segs = [_clean_seg(s, spec.get("voice"), spec.get("model_id"), spec.get("voice_label", "")) for s in spec["segments"]]
    for s in segs:
        _check_voice(s["voice"], s["model_id"])
        s["start"] = _check_time("start", s["start"])
        s["end"] = _check_time("end", s["end"])
    man = {
        "id": gen_id,
        "title": (spec.get("title") or "Untitled")[:120],
        "mode": spec.get("mode", "text"),
        "model_id": segs[0]["model_id"] if segs else None,
        "voice_label": spec.get("voice_label", ""),
        "source_text": spec.get("source_text", ""),
        "segments": segs,
        "status": spec.get("status", "pending"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root_id": root_id,
        "take": take,
        "eval": None,
        "rev": 0,
        "stale": False,
    }
    _save_manifest(man)
    hist = _sync_history(man)
    return {"gen_id": gen_id, "manifest": man, "history": hist}


def _source_text(man):
    return " ".join(s["text"] for s in man["segments"])


def a_update_segment(gen_id="", idx=0, fields_json="", **_):
    man = _load_manifest(gen_id)
    seg = _seg_at(man, idx)
    fields = json.loads(fields_json or "{}")
    if "voice" in fields or "model_id" in fields:
        _check_voice(fields.get("voice", seg.get("voice")), fields.get("model_id", seg.get("model_id")))
    if "text" in fields and not isinstance(fields["text"], str):
        raise ValueError("The line text must be text.")
    times = {k: _check_time(k, fields[k]) for k in ("start", "end") if k in fields}
    start, end = times.get("start", seg.get("start")), times.get("end", seg.get("end"))
    if times and start is not None and end is not None and end < start:
        raise ValueError("The end time must be after the start time.")
    invalidate = False
    for k in ("text", "voice", "model_id", "voice_label", "speaker"):
        if k in fields and fields[k] != seg.get(k):
            if k in ("text", "voice", "model_id"):
                invalidate = True
            seg[k] = fields[k]
    seg.update(times)
    if invalidate:
        seg["duration"] = None
        try:
            os.remove(_seg_path(gen_id, seg))
        except OSError:
            pass
    if man["mode"] == "srt":
        man["segments"].sort(key=lambda s: (s.get("start") or 0))
    man["source_text"] = _source_text(man)
    if man["status"] == "done":
        man["stale"] = True
    _save_manifest(man)
    _sync_history(man)
    return {"manifest": man}


def a_assign_speaker(gen_id="", speaker="", fields_json="", **_):
    man = _load_manifest(gen_id)
    fields = json.loads(fields_json or "{}")
    _check_voice(fields.get("voice"), fields.get("model_id"))
    for seg in man["segments"]:
        if (seg.get("speaker") or "") != (speaker or ""):
            continue
        if seg.get("voice") == fields.get("voice") and seg.get("model_id") == fields.get("model_id"):
            continue
        seg["voice"] = fields.get("voice")
        seg["model_id"] = fields.get("model_id")
        seg["voice_label"] = fields.get("voice_label", "")
        seg["duration"] = None
        try:
            os.remove(_seg_path(gen_id, seg))
        except OSError:
            pass
    if man["status"] == "done":
        man["stale"] = True
    _save_manifest(man)
    _sync_history(man)
    return {"manifest": man}


def a_insert_segment(gen_id="", after=-1, fields_json="", **_):
    after = int(after)
    man = _load_manifest(gen_id)
    segs = man["segments"]
    ref = segs[after] if 0 <= after < len(segs) else (segs[-1] if segs else None)
    start = 0.0
    if ref is not None:
        start = float(ref.get("end") or ref.get("start") or 0) + 0.5
    eff = lambda s: {
        "voice": s.get("voice") or man.get("voice"),
        "model_id": s.get("model_id") or man.get("model_id"),
        "voice_label": s.get("voice_label") or man.get("voice_label"),
    }
    cands = [eff(s) for s in ([ref] if ref else []) + segs + [{}]]
    src = next((c for c in cands if c["voice"] and c["model_id"]), None)
    if src is None:
        src = json.loads(fields_json or "{}")
        _check_voice(src.get("voice"), src.get("model_id"))
    new = _clean_seg(
        {
            "text": "New line",
            "start": round(start, 3),
            "end": round(start + 3.0, 3),
            "voice": src.get("voice"),
            "model_id": src.get("model_id"),
            "voice_label": src.get("voice_label") or "",
            "speaker": ref.get("speaker") if ref else None,
        }
    )
    segs.insert(after + 1 if after >= 0 else len(segs), new)
    if man["mode"] == "srt":
        segs.sort(key=lambda s: (s.get("start") or 0))
    man["source_text"] = _source_text(man)
    if man["status"] == "done":
        man["stale"] = True
    _save_manifest(man)
    _sync_history(man)
    return {"manifest": man}


def a_delete_segment(gen_id="", idx=0, **_):
    man = _load_manifest(gen_id)
    seg = _seg_at(man, idx)
    man["segments"].pop(int(idx))
    try:
        os.remove(_seg_path(gen_id, seg))
    except OSError:
        pass
    man["source_text"] = _source_text(man)
    if man["status"] == "done":
        man["stale"] = True
    _save_manifest(man)
    _sync_history(man)
    return {"manifest": man}


def a_generate_segment(gen_id="", idx=0, force=False, **_):
    return _generate_segment(gen_id, int(idx), str(force).lower() in ("1", "true", "yes", "on"))


def _generate_segment(gen_id, idx, force, cancelled=None, progress=None):
    man = _load_manifest(gen_id)
    seg = _seg_at(man, idx)
    path = _seg_path(gen_id, seg)
    if not force and seg.get("duration") and os.path.exists(path):
        return {"duration": seg["duration"], "cached": True, "manifest": man}
    voice = seg.get("voice") or man.get("voice")
    model_id = seg.get("model_id") or man.get("model_id")
    if not voice or not model_id:
        raise ValueError("Line %d has no voice. Select a voice." % (idx + 1))
    t0 = time.time()
    tmp = path + ".new.wav"
    audio, sr = _speak(model_id, seg["text"], tmp, cancelled=cancelled, progress=progress,
                       **_speech_options(voice))
    with DATA_LOCK:
        man = _load_manifest(gen_id)
        cur = next((s for s in man["segments"] if s["sid"] == seg["sid"]), None)
        if (
            cur is None
            or cur.get("text") != seg.get("text")
            or (cur.get("voice") or man.get("voice")) != voice
            or (cur.get("model_id") or man.get("model_id")) != model_id
        ):
            os.remove(tmp)
            return {"duration": None, "cached": False, "changed": True, "manifest": man}
        os.replace(tmp, path)
        cur["duration"] = round(len(audio) / sr, 3)
        cur["rev"] = cur.get("rev", 0) + 1
        if man["status"] == "done":
            man["stale"] = True
        _save_manifest(man)
    dur = cur["duration"]
    return {"duration": dur, "rtf": round((time.time() - t0) / max(dur, 0.01), 2), "cached": False, "manifest": man}


def _mix_key(man):
    return [man["mode"]] + [(s["sid"], s.get("rev", 0), s.get("duration"), s.get("start"), s.get("end")) for s in man["segments"]]


def a_assemble(gen_id="", **_):
    for _attempt in range(3):
        man = _load_manifest(gen_id)
        key = _mix_key(man)
        mix, sr, overruns = _mix(gen_id, man)
        with DATA_LOCK:
            cur = _load_manifest(gen_id)
            if _mix_key(cur) != key:
                continue
            for s, m in zip(cur["segments"], man["segments"]):
                s["offset"] = m.get("offset")
            _write_wav(os.path.join(_gdir(gen_id), "output.wav"), mix, sr)
            cur["duration"] = round(len(mix) / sr, 2)
            cur["status"] = "done"
            cur["stale"] = False
            cur["overruns"] = overruns
            cur["rev"] = cur.get("rev", 0) + 1
            cur["source_text"] = _source_text(cur)
            _save_manifest(cur)
            hist = _sync_history(cur)
        return {"duration": cur["duration"], "overruns": overruns, "manifest": cur, "history": hist}
    raise ValueError("The lines changed during the mix. Click Mix again.")


def _mix(gen_id, man):
    parts = []
    sr = SR
    for i, seg in enumerate(man["segments"]):
        p = _seg_path(gen_id, seg)
        if not os.path.exists(p):
            raise ValueError("Line %d has no audio. Generate it first." % (i + 1))
        a, sr = _read_wav(p)
        parts.append(a)
    overruns = []
    if man["mode"] == "srt":
        ends = [int(float(s.get("start") or 0) * sr) + len(a) for s, a in zip(man["segments"], parts)]
        total = (max(ends) if ends else 0) + int(0.25 * sr)
        mix = np.zeros(total, dtype=np.float32)
        for i, (seg, a) in enumerate(zip(man["segments"], parts)):
            start = float(seg.get("start") or 0)
            off = int(start * sr)
            mix[off : off + len(a)] += a
            seg["offset"] = round(start, 3)
            if seg.get("end") is not None and seg.get("start") is not None:
                slot = float(seg["end"]) - float(seg["start"])
                actual = len(a) / sr
                if slot > 0 and actual > slot + 0.15:
                    overruns.append({"idx": i, "slot": round(slot, 2), "actual": round(actual, 2)})
        mix = np.clip(mix, -1.0, 1.0)
    else:
        gap = int(0.35 * sr)
        chunks = []
        pos = 0
        for i, (seg, a) in enumerate(zip(man["segments"], parts)):
            if i:
                chunks.append(np.zeros(gap, dtype=np.float32))
                pos += gap
            seg["offset"] = round(pos / sr, 3)
            chunks.append(a)
            pos += len(a)
        mix = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
    return mix, sr, overruns


def a_cancel_generation(gen_id="", **_):
    if gen_id in RUNS:
        RUNS[gen_id]["cancel"] = True
    try:
        man = _load_manifest(gen_id)
    except ValueError:
        return {"history": _history()}
    if man["status"] == "pending":
        man["status"] = "cancelled"
        _save_manifest(man)
    return {"history": _sync_history(man)}


def a_get_generation(gen_id="", **_):
    return _load_manifest(gen_id)


def a_history(**_):
    return {"history": _history()}


def a_rename_generation(gen_id="", title="", **_):
    man = _load_manifest(gen_id)
    man["title"] = (title or man["title"])[:120]
    _save_manifest(man)
    return {"history": _sync_history(man), "manifest": man}


def a_delete_generation(gen_id="", **_):
    shutil.rmtree(_gdir(gen_id), ignore_errors=True)
    hist = [h for h in _history() if h["id"] != gen_id]
    _write_json(HISTORY_JSON, hist)
    return {"history": hist}


def a_save_eval(gen_id="", eval_json="", **_):
    man = _load_manifest(gen_id)
    man["eval"] = json.loads(eval_json)
    _save_manifest(man)
    return {"history": _sync_history(man), "manifest": man}


def _export_dir():
    return os.path.expanduser(_prefs().get("export_dir") or os.path.join("~", "Downloads", "OpenVoiceover"))


def _pretty_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def _export_info():
    d = _export_dir()
    return {"dir": d, "display": _pretty_path(d), "exists": os.path.isdir(d)}


def a_export_info(**_):
    return _export_info()


def a_export_audio(path="", name="", **_):
    root = os.path.realpath(APP_DIR)
    src = os.path.realpath(os.path.join(APP_DIR, path))
    if not src.startswith(root + os.sep) or not os.path.isfile(src):
        raise ValueError("The audio file is not available.")
    stem = re.sub(r"[^\w\- ]+", "", name or "").strip() or "audio"
    ext = os.path.splitext(src)[1] or ".wav"
    d = _export_dir()
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, stem + ext)
    n = 2
    while os.path.exists(dst):
        dst = os.path.join(d, "%s (%d)%s" % (stem, n, ext))
        n += 1
    shutil.copyfile(src, dst)
    return {"path": dst, "name": os.path.basename(dst), "dir_display": _pretty_path(d)}


def a_reveal(path="", **_):
    d = os.path.realpath(_export_dir())
    if path:
        target = os.path.realpath(path)
        if not target.startswith(d + os.sep) or not os.path.exists(target):
            raise ValueError("The file is not in the download folder.")
        subprocess.run(["open", "-R", target], check=False)
    else:
        os.makedirs(d, exist_ok=True)
        subprocess.run(["open", d], check=False)
    return {"ok": True}


def a_pick_export_dir(**_):
    cur = _export_dir()
    start = cur if os.path.isdir(cur) else os.path.expanduser("~")
    script = 'activate\nPOSIX path of (choose folder with prompt "Select the folder for downloads" default location (POSIX file "%s"))' % start.replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        if "-128" in (r.stderr or ""):
            return dict(_export_info(), cancelled=True)
        raise RuntimeError("The folder picker did not open: " + (r.stderr or "").strip())
    picked = r.stdout.strip().rstrip("/") or "/"
    with DATA_LOCK:
        p = _prefs()
        p["export_dir"] = picked
        _write_json(PREFS_JSON, p)
    return _export_info()


ACTIONS = {
    "bootstrap": a_bootstrap,
    "status": a_status,
    "set_prefs": a_set_prefs,
    "save_draft": a_save_draft,
    "fetch_demo_voices": a_fetch_demo_voices,
    "import_voice": a_import_voice,
    "update_voice": a_update_voice,
    "delete_voice": a_delete_voice,
    "design_preview": a_design_preview,
    "save_design": a_save_design,
    "preset_sample": a_preset_sample,
    "create_generation": a_create_generation,
    "update_segment": a_update_segment,
    "assign_speaker": a_assign_speaker,
    "insert_segment": a_insert_segment,
    "delete_segment": a_delete_segment,
    "generate_segment": a_generate_segment,
    "run_generation": a_run_generation,
    "assemble": a_assemble,
    "cancel_generation": a_cancel_generation,
    "get_generation": a_get_generation,
    "history": a_history,
    "rename_generation": a_rename_generation,
    "delete_generation": a_delete_generation,
    "save_eval": a_save_eval,
    "export_info": a_export_info,
    "export_audio": a_export_audio,
    "reveal": a_reveal,
    "pick_export_dir": a_pick_export_dir,
}


UNLOCKED = {"bootstrap", "status", "fetch_demo_voices", "import_voice", "design_preview", "save_design", "preset_sample", "generate_segment", "run_generation", "assemble", "export_info", "export_audio", "reveal", "pick_export_dir"}


def main(action="status", **params):
    _ensure_dirs()
    fn = ACTIONS.get(action)
    if fn is None:
        raise ValueError("Unknown action: " + str(action))
    if action in UNLOCKED:
        return fn(**params)
    with DATA_LOCK:
        return fn(**params)
