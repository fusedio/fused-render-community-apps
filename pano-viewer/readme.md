# Pano Viewer

A panoramic-image workbench for fused-render: import panoramas in any
Pillow-readable format (JPEG/PNG/WebP/TIFF/BMP/…), validate that they really
are panoramic, browse them in a library sidebar, and reproject them on the
fly with py360convert.

Open `index.html` in fused-render.

## Features

- **Import ▾ menu**: *Upload files…* (file picker; drag & drop anywhere also
  works), *From URL…* (server downloads the image, 128 MB cap), and *Browse
  computer…* — a popover file explorer over the server filesystem with
  path bar and name/size/modified sorting; clicking an image imports it.
  Uploads go browser → Python in 6 MB base64 chunks; originals are kept
  verbatim in `library/`, plus a browser-displayable copy (TIFF → JPEG,
  capped at 8192 px) and a thumbnail in `display/<id>/`.
- **Validation**: aspect-ratio + metadata classification — 2:1 equirect,
  6:1 cube strip, 4:3 cube cross (with a blank-corner pixel check so normal
  4:3 photos aren't misdetected), 1:1 VR180, GPano/photo-sphere XMP sniffing.
  Non-panoramic images stay in the library but are flagged.
- **Processing ▾ menu** (keys 1–8): interactive 360° sphere (Pannellum,
  WebGL) · original pixels · cube cross (`e2c` dice) · cube strip (`e2c`
  horizon) · six faces with per-face downloads · perspective (`e2p`, drag to
  look, scroll to zoom FOV) · little planet (stereographic) · 180° fisheye.
- **Cube inputs**: cross/strip layouts are auto-reprojected via `c2e` for
  the 360° view, and can be materialized as new equirect assets
  ("→ equirect" button on the card).
- **Keyboard**: `↑/↓` or `k/j` browse images, `1`–`8` switch projection,
  `i` import, `d` download the current view, `?` help.
- **Persistence**: the library lives in `pano.db` (SQLite, plus an event
  log of imports/conversions/deletes); reloading restores everything, and
  selection/projection state lives in URL params.

Conversions are cached on disk (`display/<id>/derived/<hash>.jpg`) keyed by
all conversion parameters, so repeated views are instant.

First launch generates sample panoramas (JPEG scene, WebP, TIFF, a cube
cross, and a deliberately non-panoramic image) so every feature is
exercisable out of the box. Requires `numpy`, `pillow`, `py360convert`
(in the project deps). UI: shadcn-style light theme.

Vendored third-party assets: [Pannellum](https://pannellum.org/) (MIT
license) for the WebGL 360° viewer, and the
[Inter](https://rsms.me/inter/) font (SIL Open Font License 1.1).
