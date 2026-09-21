from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import engine  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def score(extracted: str, ground_truth: str) -> float:
    return difflib.SequenceMatcher(None, normalize(extracted), normalize(ground_truth)).ratio()


def run_one(mode: str, image_path: Path) -> dict:
    started = time.perf_counter()
    result = engine.run_sync(mode, str(image_path))
    result["elapsed"] = round(time.perf_counter() - started, 1)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["fast", "accurate", "apple", "both"], default="both")
    parser.add_argument("--fixture", default="")
    args = parser.parse_args()

    modes = ["fast", "accurate"] if args.mode == "both" else [args.mode]
    fixtures = sorted(FIXTURES_DIR.glob("*.png")) + sorted(FIXTURES_DIR.glob("*.jpg"))
    if args.fixture:
        fixtures = [path for path in fixtures if path.stem == args.fixture]
    if not fixtures:
        print(f"No fixtures found in {FIXTURES_DIR} -- run generate_fixtures.py first")
        return

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary = []

    for mode in modes:
        active = engine.catalog()["active"].get(mode, "Apple Vision (on-device)")
        print(f"\n=== loading {mode} model ({active}) ===")
        for image_path in fixtures:
            print(f"-- {image_path.name} [{mode}] --")
            try:
                result = run_one(mode, image_path)
            except Exception as error:
                print(f"   FAILED: {error}")
                summary.append((image_path.stem, mode, None, str(error)))
                continue

            stem = f"{image_path.stem}__{mode}"
            (RESULTS_DIR / f"{stem}.md").write_text(result["markdown"], encoding="utf-8")
            (RESULTS_DIR / f"{stem}.raw.txt").write_text(result["raw"], encoding="utf-8")
            if result["structured"] is not None:
                (RESULTS_DIR / f"{stem}.json").write_text(json.dumps(result["structured"], indent=2), encoding="utf-8")

            gt_path = FIXTURES_DIR / f"{image_path.stem}.gt.txt"
            similarity = None
            if gt_path.exists():
                similarity = score(result["markdown"], gt_path.read_text(encoding="utf-8"))
            summary.append((image_path.stem, mode, similarity, f"{result['elapsed']}s"))
            note = f"similarity={similarity:.2f}" if similarity is not None else "no ground truth"
            print(f"   done in {result['elapsed']}s, {note}")

    print("\n=== summary ===")
    for fixture, mode, similarity, note in summary:
        similarity_text = f"{similarity:.2f}" if similarity is not None else "  -  "
        print(f"{fixture:<14} {mode:<10} similarity={similarity_text}   {note}")


if __name__ == "__main__":
    main()
