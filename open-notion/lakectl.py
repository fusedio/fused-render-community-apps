#!/usr/bin/env python3
"""lakectl — drive the parquet docdb from the command line (or from Claude).

The UI (tasks.html) and this CLI share the same lake/ directory and the same
lake.py engine, so anything done here shows up in the UI on reload — with
full snapshot history, since every command is one new snapshot.

Examples:
  lakectl.py tables
  lakectl.py create-table brain
  lakectl.py rename-table brain second-brain
  lakectl.py rows tasks
  lakectl.py rows tasks --json
  lakectl.py add tasks --set title="Ship the demo" --set status=prog --set priority=high
  lakectl.py add brain --json-props '{"title": "An idea", "body": "# Notes\\nfree text"}'
  lakectl.py bulk-add brain ideas.json            # a JSON array of property maps
  lakectl.py update tasks <id> --set status=done
  lakectl.py delete tasks <id> [<id> ...]
  lakectl.py add-column tasks effort --default med
  lakectl.py rename-column tasks effort size
  lakectl.py drop-column tasks size
  lakectl.py history tasks
  lakectl.py snapshot tasks <filename> --json
  lakectl.py export tasks > backup.json
  lakectl.py import brain backup.json             # bulk-add rows from an export

Task-tracker row conventions (what tasks.html renders):
  title: str, status: none|prog|done, priority: ""|low|med|high,
  due: YYYY-MM-DD, body: markdown-ish plain text (#, ##, -, 1., [ ], >).
Other tables are free-form: any properties become columns.
"""
import argparse
import json
import sys

import lake


def _print_rows(rows, as_json):
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("(no rows)")
        return
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    cols.sort(key=lambda c: (c != "id", c != "title"))
    widths = {c: max(len(c), *(len(str(r.get(c, "") or "")) for r in rows)) for c in cols}
    widths = {c: min(w, 40) for c, w in widths.items()}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(
            str(r.get(c, "") or "").replace("\n", "⏎")[: widths[c]].ljust(widths[c])
            for c in cols
        ))


def _parse_sets(pairs):
    props = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--set expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        props[k] = v
    return props


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="lakectl.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tables", help="list tables")

    p = sub.add_parser("create-table", help="create an empty table")
    p.add_argument("table")

    p = sub.add_parser("rename-table", help="rename a table (history moves with it)")
    p.add_argument("table")
    p.add_argument("new_name")

    p = sub.add_parser("rows", help="print the current rows of a table")
    p.add_argument("table")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("add", help="add one row")
    p.add_argument("table")
    p.add_argument("--set", action="append", metavar="key=value")
    p.add_argument("--json-props", metavar="JSON", help="properties as a JSON object")

    p = sub.add_parser("bulk-add", help="add many rows (one snapshot) from a JSON array file, or - for stdin")
    p.add_argument("table")
    p.add_argument("file")

    p = sub.add_parser("update", help="update one row")
    p.add_argument("table")
    p.add_argument("id")
    p.add_argument("--set", action="append", metavar="key=value")
    p.add_argument("--json-props", metavar="JSON")

    p = sub.add_parser("delete", help="delete rows by id (one snapshot)")
    p.add_argument("table")
    p.add_argument("ids", nargs="+")

    p = sub.add_parser("add-column", help="add a column with the same value on every row")
    p.add_argument("table")
    p.add_argument("column")
    p.add_argument("--default", default="")

    p = sub.add_parser("rename-column", help="rename a column on every row")
    p.add_argument("table")
    p.add_argument("old")
    p.add_argument("new")

    p = sub.add_parser("drop-column", help="remove a column from every row")
    p.add_argument("table")
    p.add_argument("column")

    p = sub.add_parser("history", help="list snapshots, newest first")
    p.add_argument("table")

    p = sub.add_parser("snapshot", help="print the rows of one historical snapshot")
    p.add_argument("table")
    p.add_argument("filename")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("export", help="dump current rows as JSON to stdout")
    p.add_argument("table")

    p = sub.add_parser("import", help="bulk-add rows from a JSON export (ids reassigned)")
    p.add_argument("table")
    p.add_argument("file")

    args = ap.parse_args(argv)

    if args.cmd == "tables":
        for t in lake.list_tables():
            print(t)
    elif args.cmd == "create-table":
        lake.create_table(args.table)
        print(f"created {args.table}")
    elif args.cmd == "rename-table":
        lake.rename_table(args.table, args.new_name)
        print(f"renamed {args.table} -> {args.new_name}")
    elif args.cmd == "rows":
        _print_rows(lake.latest(args.table), args.json)
    elif args.cmd == "add":
        props = _parse_sets(getattr(args, "set"))
        if args.json_props:
            props.update(json.loads(args.json_props))
        row = lake.create_row(args.table, props)
        print(json.dumps(row))
    elif args.cmd == "bulk-add":
        data = json.load(sys.stdin if args.file == "-" else open(args.file))
        if not isinstance(data, list):
            raise SystemExit("expected a JSON array of property maps")
        created = lake.bulk_create(args.table, data)
        print(f"added {len(created)} rows")
    elif args.cmd == "update":
        props = _parse_sets(getattr(args, "set"))
        if args.json_props:
            props.update(json.loads(args.json_props))
        row = lake.update_row(args.table, args.id, props)
        print(json.dumps(row))
    elif args.cmd == "delete":
        n = lake.bulk_delete(args.table, args.ids)
        print(f"deleted {n} rows")
    elif args.cmd == "add-column":
        lake.set_column(args.table, args.column, args.default)
        print(f"added column {args.column}")
    elif args.cmd == "rename-column":
        lake.rename_column(args.table, args.old, args.new)
        print(f"renamed column {args.old} -> {args.new}")
    elif args.cmd == "drop-column":
        lake.drop_column(args.table, args.column)
        print(f"dropped column {args.column}")
    elif args.cmd == "history":
        for s in lake.history(args.table):
            print(f"{s['filename']}")
    elif args.cmd == "snapshot":
        _print_rows(lake.snapshot_at(args.table, args.filename), args.json)
    elif args.cmd == "export":
        print(json.dumps(lake.latest(args.table), indent=2))
    elif args.cmd == "import":
        data = json.load(sys.stdin if args.file == "-" else open(args.file))
        rows = [{k: v for k, v in r.items() if k != "id"} for r in data]
        created = lake.bulk_create(args.table, rows)
        print(f"imported {len(created)} rows")


if __name__ == "__main__":
    main()
