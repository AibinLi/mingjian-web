"""Read-only Celeb-DF v2 official-test manifest helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


DATASET_NAME = "Celeb-DF"
DATASET_VERSION = "v2"
OFFICIAL_LIST_NAME = "List_of_testing_videos.txt"
EXPECTED_TEST_VIDEOS = 518
LABELS = {
    "YouTube-real": ("1", "real", 0),
    "Celeb-real": ("1", "real", 0),
    "Celeb-synthesis": ("0", "fake", 1),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_official_manifest(
    dataset_root: Path,
    output: Path,
    *,
    expected_count: int | None = EXPECTED_TEST_VIDEOS,
) -> dict[str, Any]:
    """Hash and freeze the official test list without writing under dataset_root."""
    root = dataset_root.resolve()
    list_path = root / OFFICIAL_LIST_NAME
    if not list_path.is_file():
        raise ValueError(f"Official test list is missing: {list_path}")
    lines = [line.strip() for line in list_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if expected_count is not None and len(lines) != expected_count:
        raise ValueError(f"Official test list has {len(lines)} entries, expected {expected_count}.")
    list_hash = sha256_file(list_path)
    entries: list[dict[str, Any]] = []
    paths: set[str] = set()
    for position, line in enumerate(lines, start=1):
        pieces = line.split(maxsplit=1)
        if len(pieces) != 2:
            raise ValueError(f"Official list line {position} is malformed.")
        official_label, relative = pieces
        normalised = relative.replace("\\", "/")
        if normalised in paths:
            raise ValueError(f"Official list repeats a video path: {normalised}")
        parts = normalised.split("/", 1)
        if len(parts) != 2 or parts[0] not in LABELS:
            raise ValueError(f"Official list line {position} has an unsupported class path: {normalised}")
        expected_official, label, binary_label = LABELS[parts[0]]
        if official_label != expected_official:
            raise ValueError(f"Official label/path mismatch on line {position}: {line}")
        source = (root / Path(normalised)).resolve()
        try:
            source.relative_to(root)
        except ValueError as error:
            raise ValueError(f"Official list line {position} escapes dataset root.") from error
        if not source.is_file():
            raise ValueError(f"Official test video is missing: {normalised}")
        paths.add(normalised)
        entries.append({
            "sample_id": f"celeb-df-v2-test-{position:04d}",
            "official_list_index": position,
            "relative_path": normalised,
            "label": label,
            "binary_label_fake": binary_label,
            "official_label": int(official_label),
            "source_sha256": sha256_file(source),
            "dataset_name": DATASET_NAME,
            "dataset_version": DATASET_VERSION,
            "official_testing_list_sha256": list_hash,
        })
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for entry in entries:
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "dataset_root": str(root),
        "official_list_path": str(list_path),
        "official_testing_list_sha256": list_hash,
        "manifest_path": str(output.resolve()),
        "manifest_sha256": sha256_file(output),
        "total": len(entries),
        "real": sum(entry["label"] == "real" for entry in entries),
        "fake": sum(entry["label"] == "fake" for entry in entries),
    }


def load_manifest(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Manifest line {line_number} is invalid JSON.") from error
        required = {"sample_id", "relative_path", "label", "binary_label_fake", "source_sha256", "dataset_name", "dataset_version", "official_testing_list_sha256"}
        if not isinstance(row, dict) or not required.issubset(row):
            raise ValueError(f"Manifest line {line_number} omits required provenance fields.")
        if row["label"] not in {"real", "fake"} or row["binary_label_fake"] not in {0, 1}:
            raise ValueError(f"Manifest line {line_number} has an invalid label.")
        rows.append(row)
    if not rows:
        raise ValueError("Manifest contains no samples.")
    return rows
