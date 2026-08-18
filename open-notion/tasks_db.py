"""runPython target for the task tracker: dispatches actions to lake.py."""

# lake.py (imported below) uses duckdb; it must be in this entry's venv.
import json

import lake


def main(action: str, table: str = "", payload: str = "{}") -> dict:
    p = json.loads(payload) if payload else {}

    if action == "list_tables":
        return {"tables": lake.list_tables()}
    if action == "create_table":
        lake.create_table(p.get("table") or table)
        return {"ok": True}
    if action == "rename_table":
        lake.rename_table(table, p["new_name"])
        return {"ok": True}
    if action == "list_rows":
        return {"rows": lake.latest(table)}
    if action == "create_row":
        return {"row": lake.create_row(table, p.get("properties", {}))}
    if action == "update_row":
        return {"row": lake.update_row(table, p["id"], p.get("properties", {}))}
    if action == "delete_row":
        lake.delete_row(table, p["id"])
        return {"ok": True}
    if action == "reorder_rows":
        return {"rows": lake.reorder_rows(table, p["ids"])}
    if action == "history":
        return {"snapshots": lake.history(table)}
    if action == "snapshot_at":
        return {"rows": lake.snapshot_at(table, p["filename"])}
    if action == "get_lake_dir":
        return {"path": lake.get_lake_dir()}
    if action == "set_lake_dir":
        return {"path": lake.set_lake_dir(p["path"])}

    raise ValueError(f"unknown action: {action!r}")


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
