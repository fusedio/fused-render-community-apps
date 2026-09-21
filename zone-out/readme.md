# ZoneOut

A fused-render app that watches your calendar and this Mac, and tells you —
loudly — when a meeting you should be in is running without you, before you
become the no-show.

## How it works

1. **Calendar** — you paste the *private iCal link* of your calendar (Google
   Calendar → Settings → your calendar → *Integrate calendar* → *Secret
   address in iCal format*; Outlook → *Publish calendar* → ICS). A resident
   background daemon (`daemon.py`) downloads it every 5 minutes, expands
   recurring events and pulls the meeting link out of each event
   (`X-GOOGLE-CONFERENCE`, location, description: Meet, Zoom, Teams, Webex,
   Slack huddles, or any URL).
2. **Detection** (`signals.py`) — every 20 s while a meeting is near, the
   daemon reads:
   - `pmset -g assertions`: which app holds a **WebRTC wake lock** ("WebRTC
     has active PeerConnections" = a live call in Chrome / Slack / Teams /
     Discord) and which PID coreaudiod is running the **microphone** for.
   - CoreAudio: whether any input device is live. AVFoundation: camera in use.
   - Zoom's `CptHost` helper — exists only while a Zoom meeting is joined.
   - **Browser tabs**: Chrome (session files, no permission; AppleScript with
     a one-time Automation permission), Safari (AppleScript), Firefox
     (sessionstore, no permission). Only URLs of meeting providers are kept.
   - Idle time and screen lock.

   For each live event: the event's own Meet/Zoom code open in a browser
   that is holding the WebRTC call lock → **in the call** (high confidence);
   tab open with only the mic live (pre-join preview) or with no media →
   **lobby**; Zoom `CptHost` for a Zoom link → in the call;
   Teams/Webex/Slack app in a call → in the call (medium); no link on the
   event but some call active → in the call (low). Otherwise **absent**.
3. **Attendance** (`attendance.py`) — presence samples become segments per
   meeting instance in `.fused/data/attendance/<day>.json`, with a verdict
   (attended / partial / missed / skipped) and lateness.
4. **Alerts** (`alerts.py`) — after a grace period (default 2 min) a tracked
   meeting without you triggers a macOS notification + sound + a modal
   dialog (Open meeting / Dismiss / Skip). Unanswered warnings escalate:
   the next one comes after 1 minute, then 30 seconds, then every 15
   seconds, until you touch a warning (any button, Escape, or an action in
   the app), you are seen in the call, or the meeting ends. An interaction
   resets the ladder to one reminder a minute while you are still absent. One call credits one meeting: a link-less event is
   only credited with "some call is active" when no other live meeting has
   claimed that call by its meeting code. Also: heads-up 2 min before start, and a "you dropped out" alert.
   Optional: spoken alert, auto-open the meeting link.

Track everything (minus the ones you ignore) or switch to *selected* mode and
tick only the meetings that matter. Declined invitations are skipped.

Not every calendar entry is a video call. Entries with a location but no
link are treated as in-person (heads-up only, nothing to verify). Entries
with other invitees but neither link nor location get one clear "add a
meeting link" notification instead of alarms. Entries with nobody else
invited are personal and stay silent.

## Permissions

- Notifications and dialogs use `osascript`; no setup needed.
- Reading Chrome/Safari tabs via AppleScript prompts once: *"FusedRender
  wants to control Google Chrome"*. Say OK, or later enable it under System
  Settings → Privacy & Security → Automation → FusedRender. Without it, Chrome
  tabs are still read from Chrome's session files (a few seconds of lag).

## Files

- `index.html` — the page (live banner, signals, day list, settings, history)
- `daemon.py` — resident daemon (`daemon =` in `pyproject.toml`), HTTP API
- `ics_feed.py`, `signals.py`, `attendance.py`, `alerts.py`
- `tests/` — `python -m unittest discover -s tests`
- `.fused/data/settings.json` — settings (contains your private calendar link)
- `.fused/data/attendance/*.json`, `.fused/data/alerts.jsonl` — history
- `.fused/cache/` — downloaded feeds, daemon log, last snapshot
