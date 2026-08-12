# COG Range Viewer

**One photo, 316 MB, zero downloads.** An interactive explainer of why
cloud-optimized GeoTIFFs work: pan and zoom a 316 MB public Sentinel-2
satellite image (straight from the `sentinel-cogs` S3 bucket) while the app
visualizes, live, the exact HTTP byte-range requests the browser makes —
which tiles, which overview levels, how few bytes it actually took.

Works on local files too: pass `?file=<absolute path>` and a tiny stdlib
range server (`range_server.py`, started via `fused.runPython`) serves your
own GeoTIFF with the same Range + CORS semantics as S3, so you can compare.

No Python dependencies — the backend is stdlib only.
