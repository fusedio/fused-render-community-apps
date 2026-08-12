#!/usr/bin/env python3
"""Validate every app folder and (re)generate index.json.

Stdlib only. Two modes:

    python3 scripts/build_index.py --check    # validate only (PR CI)
    python3 scripts/build_index.py            # validate + write index.json (merge CI)

Exit code 1 with per-app error lines on any validation failure.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
APP_SIZE_CAP = 20 * 1024 * 1024
FILE_SIZE_CAP = 10 * 1024 * 1024
SKIP_DIRS = {".git", ".github", "scripts"}

REQUIRED_STR = ("name", "description")
LIMITS = {"name": 60, "description": 200}


def fail(errors: list[str], app: str, msg: str) -> None:
    errors.append(f"{app}: {msg}")


def validate_app(folder: Path, errors: list[str]) -> dict | None:
    slug = folder.name
    if not SLUG_RE.match(slug):
        fail(errors, slug, "slug must match ^[a-z0-9][a-z0-9-]{1,63}$")
        return None

    htmls = [p for p in folder.iterdir() if p.suffix == ".html" and p.is_file()]
    if [p.name for p in htmls] != ["index.html"]:
        fail(errors, slug, "must contain exactly one top-level .html, named index.html")
        return None

    for name in ("readme.md", "preview.png", "metadata.json"):
        if not (folder / name).is_file():
            fail(errors, slug, f"missing required file {name}")
            return None

    total = 0
    for p in folder.rglob("*"):
        if p.is_symlink():
            fail(errors, slug, f"symlink not allowed: {p.relative_to(folder)}")
            return None
        if p.is_file():
            size = p.stat().st_size
            total += size
            if size > FILE_SIZE_CAP:
                fail(errors, slug, f"file over 10 MB: {p.relative_to(folder)}")
                return None
    if total > APP_SIZE_CAP:
        fail(errors, slug, f"app over 20 MB ({total // (1024 * 1024)} MB)")
        return None

    try:
        meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(errors, slug, f"metadata.json unreadable: {exc}")
        return None

    if meta.get("schema") != 1:
        fail(errors, slug, 'metadata.json: "schema" must be 1')
        return None
    for key in REQUIRED_STR:
        val = meta.get(key)
        if not isinstance(val, str) or not val.strip():
            fail(errors, slug, f'metadata.json: "{key}" is required (non-empty string)')
            return None
        if len(val) > LIMITS[key]:
            fail(errors, slug, f'metadata.json: "{key}" over {LIMITS[key]} chars')
            return None
    author = meta.get("author")
    if not isinstance(author, dict) or not isinstance(author.get("name"), str) or not author["name"].strip():
        fail(errors, slug, 'metadata.json: "author.name" is required')
        return None
    if not isinstance(meta.get("version"), str) or not meta["version"].strip():
        fail(errors, slug, 'metadata.json: "version" is required (semver string)')
        return None
    if not isinstance(meta.get("requires_python"), bool):
        fail(errors, slug, 'metadata.json: "requires_python" is required (boolean)')
        return None
    tags = meta.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 5 or any(
        not isinstance(t, str) or t != t.lower() for t in tags
    ):
        fail(errors, slug, 'metadata.json: "tags" must be ≤ 5 lowercase strings')
        return None

    return {"slug": slug, "size_bytes": total, **meta}


def app_commit(slug: str) -> str:
    out = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", slug],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    folders = sorted(
        p for p in ROOT.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in SKIP_DIRS
    )
    errors: list[str] = []
    apps = []
    for folder in folders:
        entry = validate_app(folder, errors)
        if entry is not None:
            apps.append(entry)

    if errors:
        for line in errors:
            print(f"FAIL {line}", file=sys.stderr)
        return 1

    print(f"validated {len(apps)} app(s)")
    if check_only:
        return 0

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    for entry in apps:
        entry_commit = app_commit(entry["slug"])
        entry["commit"] = entry_commit or head
    index = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": head,
        "apps": apps,
    }
    (ROOT / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("wrote index.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
