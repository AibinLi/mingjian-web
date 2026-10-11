"""Build a fixed, non-test Celeb-DF v2 calibration manifest without media writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.celeb_df_v2 import DATASET_NAME, DATASET_VERSION, LABELS, OFFICIAL_LIST_NAME, sha256_file  # noqa: E402


DEFAULT_SEED = "mingjian-phase6-calibration-selection-v1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--real-count", type=int, default=200)
    parser.add_argument("--fake-count", type=int, default=200)
    parser.add_argument("--selection-seed", default=DEFAULT_SEED)
    args = parser.parse_args()
    print(json.dumps(build_manifest(args.dataset_root, args.output, args.real_count, args.fake_count, args.selection_seed), ensure_ascii=False, indent=2, sort_keys=True))


def build_manifest(dataset_root: Path, output: Path, real_count: int = 200, fake_count: int = 200, selection_seed: str = DEFAULT_SEED) -> dict[str, Any]:
    if real_count <= 0 or fake_count <= 0:
        raise ValueError("Class counts must be positive.")
    root = dataset_root.resolve()
    official_path = root / OFFICIAL_LIST_NAME
    if not official_path.is_file():
        raise ValueError("Official testing list is missing.")
    official_hash = sha256_file(official_path)
    official_paths = _official_paths(official_path)
    candidates = {"real": [], "fake": []}
    for directory, (_, label, binary_label) in LABELS.items():
        for source in sorted((root / directory).glob("*.mp4")):
            relative = source.resolve().relative_to(root).as_posix()
            if relative not in official_paths:
                candidates[label].append({"relative_path": relative, "label": label, "binary_label_fake": binary_label, "source_sha256": sha256_file(source), "group_id": _group_id(relative)})
    selected = _balanced_select(candidates["real"], real_count, selection_seed, "real") + _balanced_select(candidates["fake"], fake_count, selection_seed, "fake")
    if {item["relative_path"] for item in selected}.intersection(official_paths):
        raise AssertionError("Calibration selection overlaps the official test list.")
    rows: list[dict[str, Any]] = []
    counters = {"real": 0, "fake": 0}
    for item in sorted(selected, key=lambda value: (value["label"], value["source_sha256"])):
        counters[item["label"]] += 1
        rows.append({"schema_version": "phase6-calibration-manifest-v1", "dataset_role": "calibration", "sample_id": f"phase6-celeb-df-v2-{item['label']}-{counters[item['label']]:04d}", **item, "dataset_name": DATASET_NAME, "dataset_version": DATASET_VERSION, "official_testing_list_sha256": official_hash, "official_test_overlap_count": 0, "selection_rule": "Per-class deterministic round-robin across source groups, ordered by SHA-256(selection_seed|label|group_id|source_sha256).", "selection_seed": selection_seed})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {"manifest_path": str(output.resolve()), "manifest_sha256": sha256_file(output), "dataset_role": "calibration", "official_testing_list_sha256": official_hash, "official_test_overlap_count": 0, "selection_seed": selection_seed, "total": len(rows), "real": counters["real"], "fake": counters["fake"], "groups": len({row["group_id"] for row in rows})}


def _official_paths(path: Path) -> set[str]:
    values: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            pieces = line.split(maxsplit=1)
            if len(pieces) != 2:
                raise ValueError(f"Official list line {line_number} is malformed.")
            values.add(pieces[1].replace("\\", "/"))
    return values


def _group_id(relative_path: str) -> str:
    directory, filename = relative_path.split("/", 1)
    stem = Path(filename).stem
    if directory in {"Celeb-synthesis", "Celeb-real"}:
        # Celeb-real/idX_* and Celeb-synthesis/idX_idY_* show the same source
        # identity idX.  Directory prefixes must not split that identity.
        return f"celeb/{stem.split('_', 1)[0]}"
    return f"youtube/{stem}"


def _balanced_select(candidates: list[dict[str, Any]], target: int, seed: str, label: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        groups.setdefault(item["group_id"], []).append(item)
    if not groups:
        raise ValueError(f"No non-test {label} candidates exist.")
    ordered_groups = sorted(groups, key=lambda group: hashlib.sha256(f"{seed}|{label}|{group}".encode("utf-8")).hexdigest())
    for group in ordered_groups:
        groups[group].sort(key=lambda item: hashlib.sha256(f"{seed}|{label}|{group}|{item['source_sha256']}".encode("utf-8")).hexdigest())
    selected, index = [], 0
    while len(selected) < target:
        group = ordered_groups[index % len(ordered_groups)]
        if groups[group]:
            selected.append(groups[group].pop(0))
        elif not any(groups.values()):
            break
        index += 1
    if len(selected) != target:
        raise ValueError(f"Only {len(selected)} non-test {label} videos are available; requested {target}.")
    return selected


if __name__ == "__main__":
    main()
