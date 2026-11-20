---
name: clay-lookalike
description: Lookalike search over Clay v1.5 NAIP embeddings for San Francisco. Fetch the scene cache, then rank chips by similarity to chosen chip ids.
---
# Clay Lookalike

Data lives in .fused/cache/v1/: one .npz per NAIP scene, then emb.npy, bounds.bin and meta.json built from them. Chip ids are row indexes into those arrays.

## lookalike.py
Cache status, per-scene fetch, index build, or a similarity query. `fetch` and `build` write .fused/cache/v1/; `status` and `query` write nothing. Call `status` first: `build` raises RuntimeError until all 19 scenes are fetched, and `query` raises FileNotFoundError until `build` has run.
- Args: `action` (str, one of `status`, `fetch`, `build`, `query`, default `status`), `scene` (str, NAIP scene name from the hard-coded list, required for `fetch`), `pos` (str, comma-separated chip ids, required for `query`), `neg` (str, comma-separated chip ids, default ""), `k` (int, default 1500)
- Returns: `status` → `{"ready": bool, "cached": int, "total": int, "missing": [str], "n": int|null}`; `fetch` → `{"scene": str, "n": int, "downloaded": bool}`; `build` → `{"n": int, "scenes": int}`; `query` → `{"top": [[id, score], ...]}` best first, selected ids excluded
- Example: `{"action":"py","app":"clay-lookalike","file":"lookalike.py","args":{"action":"query","pos":"1204,1205","neg":"88","k":50}}`
