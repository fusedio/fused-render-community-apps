"""Lightweight parquet-snapshot docdb ("duckLake").

Each table is a folder under the lake dir holding full-state parquet
snapshots. Every write produces a new timestamp-named snapshot; nothing
is mutated or deleted on disk. The current state is the
lexicographically-largest filename.

Nothing is ever written inside the app folder. The lake lives under
~/.fused-render/cache/open-notion/lake by default (override the parent
with the OPEN_NOTION_CACHE_DIR env var), and the user can relocate it
anywhere via set_lake_dir().

Parquet I/O prefers pyarrow and falls back to duckdb: the FusedRender
runtime that serves the UI bundles pyarrow but a broken duckdb (its
native module fails to import), while a typical local python has duckdb
but not pyarrow. Both backends produce plain parquet files, so the UI
and local tools (lakectl.py, Claude, scripts) can share one lake. Since
every read targets a single snapshot file, no cross-file schema
reconciliation (union_by_name) is needed — each snapshot already
carries the union of columns at its write time.
"""
import datetime
import json
import math
import os
import shutil
import uuid

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    _BACKEND = "pyarrow"
except ImportError:
    import duckdb
    _BACKEND = "duckdb"

_HERE = os.path.dirname(os.path.abspath(__file__))
_SEED_DIR = os.path.join(_HERE, "seed")


def _cache_dir() -> str:
    """Per-app global state dir. Never inside the app folder, so a checkout
    (or a marketplace install) stays clean when the app runs."""
    override = os.environ.get("OPEN_NOTION_CACHE_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".fused-render", "cache", "open-notion")


CACHE_DIR = _cache_dir()
_DEFAULT_LAKE_DIR = os.path.join(CACHE_DIR, "lake")
# Lives in the cache dir, not inside the lake dir itself, since that dir is
# exactly what this config lets the user relocate.
_LOCATION_FILE = os.path.join(CACHE_DIR, "lake_location.json")


def _load_lake_dir() -> str:
    if os.path.isfile(_LOCATION_FILE):
        try:
            with open(_LOCATION_FILE) as fh:
                path = json.load(fh).get("path")
        except (OSError, ValueError):
            path = None
        if path:
            path = os.path.expanduser(path)
            if os.path.isdir(path):
                return path
    return _DEFAULT_LAKE_DIR


LAKE_DIR = _load_lake_dir()

RESERVED_COLUMNS = ("id", "body")


def get_lake_dir() -> str:
    return LAKE_DIR


def bootstrap() -> str:
    """First run: create the lake dir and copy in the demo tables shipped in
    seed/ (one <table>.json file per table, a JSON array of property maps).
    A no-op once the lake dir exists, so deleting a seeded table is final."""
    global LAKE_DIR
    if os.path.isdir(LAKE_DIR):
        return LAKE_DIR
    os.makedirs(LAKE_DIR, exist_ok=True)
    if os.path.isdir(_SEED_DIR):
        for name in sorted(os.listdir(_SEED_DIR)):
            if not name.endswith(".json"):
                continue
            table = name[: -len(".json")]
            try:
                with open(os.path.join(_SEED_DIR, name)) as fh:
                    rows = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(rows, list):
                continue
            create_table(table)
            bulk_create(table, [r for r in rows if isinstance(r, dict)])
    return LAKE_DIR


def set_lake_dir(new_dir: str) -> str:
    """Move the whole lake (every table, every snapshot) to `new_dir` and
    remember the new location for future calls."""
    global LAKE_DIR
    new_dir = os.path.abspath(os.path.expanduser(new_dir))
    if new_dir == LAKE_DIR:
        return LAKE_DIR
    if os.path.exists(new_dir):
        raise FileExistsError(f"path already exists: {new_dir}")
    parent = os.path.dirname(new_dir)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.isdir(LAKE_DIR):
        shutil.move(LAKE_DIR, new_dir)
    else:
        os.makedirs(new_dir)
    home = os.path.expanduser("~")
    stored = new_dir
    if stored == home or stored.startswith(home + os.sep):
        stored = "~" + stored[len(home):]
    os.makedirs(os.path.dirname(_LOCATION_FILE), exist_ok=True)
    with open(_LOCATION_FILE, "w") as fh:
        json.dump({"path": stored}, fh)
    LAKE_DIR = new_dir
    return new_dir


def _table_dir(table: str) -> str:
    if not table or "/" in table or table.startswith("."):
        raise ValueError(f"invalid table name: {table!r}")
    return os.path.join(LAKE_DIR, table)


def _timestamp() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S%fZ")


def _snapshot_files(table: str) -> list[str]:
    d = _table_dir(table)
    if not os.path.isdir(d):
        raise FileNotFoundError(f"no such table: {table}")
    return sorted(f for f in os.listdir(d) if f.endswith(".parquet"))


def _jsonable(v):
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return None if math.isnan(v) else v
    return str(v)  # UUID, datetime, etc.


def _clean(rows: list[dict]) -> list[dict]:
    return [{k: _jsonable(v) for k, v in row.items()} for row in rows]


def _read_parquet(path: str) -> list[dict]:
    if _BACKEND == "pyarrow":
        rows = pq.read_table(path).to_pylist()
    else:
        rel = duckdb.sql(f"SELECT * FROM read_parquet('{path}')")
        cols = rel.columns
        rows = [dict(zip(cols, r)) for r in rel.fetchall()]
    return _clean(rows)


def _write_parquet(path: str, rows: list[dict]) -> None:
    if not rows:
        if _BACKEND == "pyarrow":
            pq.write_table(pa.table({"id": pa.array([], type=pa.string())}), path)
        else:
            duckdb.sql(f"COPY (SELECT CAST(NULL AS VARCHAR) AS id WHERE false) TO '{path}' (FORMAT parquet)")
        return
    # Give every row the union of keys so the writer sees a consistent schema.
    cols = []
    for row in rows:
        for k in row:
            if k not in cols:
                cols.append(k)
    normalized = [{c: row.get(c) for c in cols} for row in rows]
    if _BACKEND == "pyarrow":
        pq.write_table(pa.Table.from_pylist(normalized), path)
    else:
        tmp = path + ".json"
        with open(tmp, "w") as fh:
            json.dump(normalized, fh)
        try:
            duckdb.sql(f"COPY (SELECT * FROM read_json('{tmp}', format='array')) TO '{path}' (FORMAT parquet)")
        finally:
            os.remove(tmp)


def list_tables() -> list[str]:
    if not os.path.isdir(LAKE_DIR):
        return []
    return sorted(
        name for name in os.listdir(LAKE_DIR)
        if os.path.isdir(os.path.join(LAKE_DIR, name))
    )


def create_table(table: str) -> None:
    d = _table_dir(table)
    if os.path.isdir(d):
        raise FileExistsError(f"table already exists: {table}")
    os.makedirs(d)
    _write_parquet(os.path.join(d, f"{_timestamp()}.parquet"), [])


def rename_table(table: str, new_name: str) -> None:
    src = _table_dir(table)
    dst = _table_dir(new_name)
    if not os.path.isdir(src):
        raise FileNotFoundError(f"no such table: {table}")
    if os.path.isdir(dst):
        raise FileExistsError(f"table already exists: {new_name}")
    os.rename(src, dst)


def latest(table: str) -> list[dict]:
    files = _snapshot_files(table)
    if not files:
        return []
    return _read_parquet(os.path.join(_table_dir(table), files[-1]))


def write_snapshot(table: str, rows: list[dict]) -> str:
    d = _table_dir(table)
    if not os.path.isdir(d):
        raise FileNotFoundError(f"no such table: {table}")
    filename = f"{_timestamp()}.parquet"
    _write_parquet(os.path.join(d, filename), rows)
    return filename


def create_row(table: str, properties: dict) -> dict:
    rows = latest(table)
    row = dict(properties)
    row["id"] = str(uuid.uuid4())
    rows.append(row)
    write_snapshot(table, rows)
    return row


def update_row(table: str, row_id: str, properties: dict) -> dict:
    rows = latest(table)
    target = None
    for row in rows:
        if row.get("id") == row_id:
            row.update(properties)
            target = row
            break
    if target is None:
        raise KeyError(f"no row {row_id!r} in table {table!r}")
    write_snapshot(table, rows)
    return target


def delete_row(table: str, row_id: str) -> None:
    rows = latest(table)
    remaining = [r for r in rows if r.get("id") != row_id]
    if len(remaining) == len(rows):
        raise KeyError(f"no row {row_id!r} in table {table!r}")
    write_snapshot(table, remaining)


def bulk_create(table: str, rows: list[dict]) -> list[dict]:
    """Insert many rows in ONE snapshot. Each dict is a property map; ids are
    assigned. Returns the created rows."""
    current = latest(table)
    created = []
    for props in rows:
        row = dict(props)
        row["id"] = str(uuid.uuid4())
        created.append(row)
    write_snapshot(table, current + created)
    return created


def bulk_delete(table: str, ids: list[str]) -> int:
    """Remove many rows in ONE snapshot. Returns how many were removed."""
    rows = latest(table)
    drop = set(ids)
    remaining = [r for r in rows if r.get("id") not in drop]
    write_snapshot(table, remaining)
    return len(rows) - len(remaining)


def rename_column(table: str, old: str, new: str) -> None:
    if old in RESERVED_COLUMNS:
        raise ValueError(f"cannot rename reserved column {old!r}")
    rows = latest(table)
    for r in rows:
        if old in r:
            r[new] = r.pop(old)
    write_snapshot(table, rows)


def drop_column(table: str, column: str) -> None:
    if column in RESERVED_COLUMNS:
        raise ValueError(f"cannot drop reserved column {column!r}")
    rows = latest(table)
    for r in rows:
        r.pop(column, None)
    write_snapshot(table, rows)


def set_column(table: str, column: str, value) -> None:
    """Add a column (or overwrite it) with the same value on every row."""
    rows = latest(table)
    for r in rows:
        r[column] = value
    write_snapshot(table, rows)


def reorder_rows(table: str, ids: list[str]) -> list[dict]:
    """Rewrite the snapshot with rows ordered per `ids`; row order in the
    parquet file is the persisted order. Unlisted rows keep their relative
    order at the end."""
    rows = latest(table)
    pos = {row_id: i for i, row_id in enumerate(ids)}
    rows.sort(key=lambda r: pos.get(r.get("id"), len(pos)))
    write_snapshot(table, rows)
    return rows


def history(table: str) -> list[dict]:
    entries = []
    for f in reversed(_snapshot_files(table)):
        ts = f[: -len(".parquet")]
        entries.append({"filename": f, "timestamp": ts})
    return entries


def snapshot_at(table: str, filename: str) -> list[dict]:
    if "/" in filename or not filename.endswith(".parquet"):
        raise ValueError(f"invalid snapshot filename: {filename!r}")
    path = os.path.join(_table_dir(table), filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no snapshot {filename!r} in table {table!r}")
    return _read_parquet(path)
