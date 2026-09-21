# Open Relax ✿

A cozy break-time companion for fused-render. Mix up to fifteen sounds from a
library of about three hundred (or play a curated YouTube relaxation video),
follow guided breathing exercises (cyclic sighing, box, 4-7-8, coherent), and
watch your daily minutes fill a goal ring, a five-week heatmap and a set of
badges.

The sound library borrows BetterSleep's catalogue — the same names, folded
into five rows (Nature, City & Noise, Melodies, ASMR, Brainwaves). Each row
opens with a short shelf — ten or a dozen of its most distinctive sounds plus
a few generated "gen" stand-ins — rather than the whole family. A wooden "+"
tile ends every row: it opens a picker over the full catalogue (280 sounds)
with a small search line, so typing "fire" finds Fireplace, Campfire and Fire
Crackles. Tapping a sound shows its page — a name to keep or change, a glyph
from the set the tiles already carve, and the recordings found for it, each
with a listen button — and Add puts a card of your own at the front of that
row, pinned to the exact clip you chose. Cards live in localStorage, newest
first, and carry a small ✕.

The recordings are Creative Commons clips from Openverse (Freesound, Jamendo,
Wikimedia), and every one is under 1 MB: `research.py` searched the catalogue
once, kept up to six verified clips per sound in `catalog.json`, and that file
is what the picker reads. The first time a tile or card plays, `sounds.py`
downloads its clip into `.fused/cache/audio/`; later plays read the cached
file, and credit and licence are shown under the sound in the mixer. Ordinary
use never spends an Openverse search. Most tiles also have a small Web Audio
recipe (coloured noise, filtered hums and drones, soft chord pads, chirps and
calls, binaural and isochronic tones). The synth never imitates an instrument
— a generated piano or harp line sounds like MIDI — so instrument tiles and
cards stay quiet until their recording arrives. Brainwaves stay pure tones. As
in BetterSleep, a mix holds fifteen sounds and only one brainwave at a time.

To refresh the catalogue, run `uv run research.py` (resumes; `--force` starts
over). Openverse allows an anonymous client 20 searches a minute, and the
script paces itself accordingly.

Reminders ring as real macOS notifications: `remind.py` spawns a detached
worker that waits for its moment. `tracker.py` stores sessions, exercises and
settings under `.fused/data/`, and `openurl.py` hands YouTube links to your
default browser when the embedded player is blocked.

Open `index.html` in fused-render to use it.
