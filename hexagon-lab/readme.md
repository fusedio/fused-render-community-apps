# Hexagon Lab

![The six-step H3 lab: globe, neighbors, hierarchy, building counts, raster stats, change detection](preview.png)

Six steps from the whole world to one join — an interactive explainer of
H3 hexagons where every step is a live sandbox, not a slide.

1. **the world** — the whole Earth cut into H3 cells on a draggable globe,
   with a resolution slider that *animates* between res 0 → 1 → 2 (a
   stochastic dissolve, one level at a time). Toggle the 12 pentagons or
   color every cell by how far its area drifts from the resolution mean.
2. **neighbors** — a hexagon has 6 neighbors all at the same distance; a
   square's 8 come at two. Rings, distance and why that matters for
   convolution-style analysis.
3. **hierarchy** — every cell splits into 7 twisted children. Descend from
   res 0 down to real Amsterdam footprints, watch the ±19° rotation
   alternate (even resolutions align with even, odd with odd), or draw your
   own shape and polyfill it (overlap mode, not centroid).
4. **count buildings** — real Overture footprints for five cities. Click a
   hexagon and it becomes one 20-byte row; ⇧-sweep to paint, ⚡ count them
   all. A live byte meter compares WKB outline bytes against hex rows.
5. **raster statistics** — the same pipeline on a raster: Grand Canyon
   elevation pixels summarized per cell (avg / min / max dropdown). At res
   10 the meter flips red: more rows than pixels, resolution too fine.
6. **spot the change** — change detection as ONE JOIN: the same neighborhood
   in two Overture releases, matched row-by-row on the hex id, SQL on screen.

Deep links: `?tab=world|nbr|hier|bld|ras|cmp` · `?place=` ·
`?cmpplace=japan|ams|assam` · `?metric=avg|min|max` · `?res=8..11`.

Every number on the page is computed for real, on your machine: `h3_ingest.py`
(steps 4–6) and `basics/hierarchy_h3.py` (step 3) run the actual H3 math —
DuckDB's community `h3` extension locally, the pure-python `h3` package when
hosted — over real Overture building extracts and a Grand Canyon elevation
raster that ship in `data/` and `basics/data/`, no external services or
downloads needed.

Python dependencies (installed on first run into the app's venv): `duckdb`,
`pyarrow`, `pandas`.
