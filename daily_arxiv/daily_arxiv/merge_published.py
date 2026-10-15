"""Keep earlier results when publishing another batch on the same date."""

import argparse
import json
import subprocess
from pathlib import Path

from .check_stats import paper_version_id


def merge_file(path: Path) -> None:
    history = subprocess.run(
        ["git", "show", f"origin/data:data/{path.name}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if history.returncode != 0:
        return  # The data branch or this date's file does not exist yet.

    merged = {}
    for text in (history.stdout, path.read_text(encoding="utf-8")):
        for line in text.splitlines():
            if line.strip():
                paper = json.loads(line)
                merged[paper_version_id(paper)] = paper

    path.write_text(
        "".join(json.dumps(paper, ensure_ascii=False) + "\n" for paper in merged.values()),
        encoding="utf-8",
    )
    print(f"Preserved and merged {len(merged)} paper versions in {path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.files:
        merge_file(path)


if __name__ == "__main__":
    main()
