# Open Awake

Keep your Mac awake. Pick a duration from the dropdown (15 minutes to 8 hours, or until you stop it), press **Stay awake**, and the Mac won't idle-sleep until the time is up or you press **Stop · allow sleep**. An [Amphetamine](https://apps.apple.com/app/amphetamine/id937984704)-style tool built on macOS's own `caffeinate`.

- **A vintage wall clock** shows the time of day; a blue band on its bezel runs from now to when the Mac may sleep again, and a brass plate counts down (or up, for open-ended sessions). The pendulum swings while the Mac is held awake.
- **Timed or open-ended** sessions.
- **Keep the display on** is a switch; off lets the screen dim while the Mac stays awake.
- **History** lists recent sessions and how long the Mac stayed awake today. Picking another length while a session runs restarts the timer from now.
- Sessions **survive closing the tab**: a small background daemon owns the `caffeinate` process. It is tied to the daemon (`caffeinate -w`), so quitting or stopping the app can never leave an orphaned process.

## How it works

`awake.py` is a fused-render background app (`[tool.fused-render.app] daemon`). Pressing the button starts `caffeinate -i -m -s [-d] -w <daemon pid>`. The daemon owns the deadline and stops `caffeinate` once the wall clock passes the end time, so a timed session ends on time even if the Mac slept in between (`caffeinate -t` would pause its timer during sleep). Nothing starts when the page merely opens or previews.

## Limits

- macOS only (`caffeinate` ships with it).
- With the lid closed, a MacBook on battery still sleeps; macOS only honours "stay awake" with the lid shut when on power with an external display. This is an OS rule, not something the app can override.
- Standard (stdlib-only) Python; no dependencies.

Design: Railway Ticket theme, ticket-stub surface, ticket component set and spring-press motion from the Vintage Dashboard Foundations kit, with a hand-built wall clock (wooden case, brass bezel, paper dial). The whole app fits one screen with no page scroll.
