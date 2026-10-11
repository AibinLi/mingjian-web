"""Build a frozen manifest from Celeb-DF v2's official testing list."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.celeb_df_v2 import build_official_manifest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Manifest path outside the original dataset directory.")
    args = parser.parse_args()
    root = args.dataset_root.resolve()
    output = args.output.resolve()
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise SystemExit("--output must be outside --dataset-root; original data is never modified.")
    if output.exists():
        raise SystemExit("Manifest output already exists; choose a fresh path to preserve the previous run.")
    print(json.dumps(build_official_manifest(root, output), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
