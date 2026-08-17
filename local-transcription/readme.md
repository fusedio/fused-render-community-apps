# Local Transcription

![Local Transcription — a record panel, a list of media files found on this machine, and a diarized transcript](preview.png)

**Speech to text on your own machine.** Record something, or point it at audio
and video you already have, and get a transcript back — no upload, no API key,
no per-minute billing.

## Getting audio in

- **Record** straight from the page via `MediaRecorder`: microphone, desktop
  audio, or a browser tab. Pick the input device from the dropdown (Refresh
  devices re-enumerates after you plug something in). Capturing system audio on
  macOS needs a loopback device such as BlackHole or VoiceMeeter routed through
  Audio MIDI Setup — the page says so inline when you choose that source.
- **Found on this machine** — `audio_scan.py` walks a few sensible directories
  and lists the audio and video files it finds, with size and date, so you can
  pick one instead of typing an absolute path. Rescan picks up new files.

Recordings land in `./recordings/` via `save_recording.py`, which decodes the
browser's blob from base64 in chunks so long sessions don't have to fit in one
message.

## Transcribing

- **Model** — choose from the local Whisper and Parakeet models fused-render
  knows about. Preload warms one up before you need it; Unload frees the
  memory.
- **Language** — a dozen-plus options, or let the model detect it.
- **Task** — transcribe in the source language, or translate to English.
- **Identify speakers** — optional diarization, labelling each segment with the
  speaker it belongs to.
- Progress is reported while it runs, and Stop cancels mid-job.

## Getting text out

The transcript renders three ways: **plain text**, **segments** with
timestamps, and **SRT** subtitles. Copy takes whichever view you're looking at.

---

Transcription runs through `fused.ai.transcribe(...)`, so the model is loaded
and held resident by fused-render's shared runtime rather than by this app.
