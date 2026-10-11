"""Explicitly provision the pinned local Whisper ASR artifacts.

This command is never called at web-service runtime.  It requires an explicit
flag because model downloads are a user-authorised administrative operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


MODEL_ID = "openai/whisper-base"
MODEL_REVISION = "e37978b90ca9030d5170a5c07aadb050351a65bb"
FILES = [
    "added_tokens.json", "config.json", "generation_config.json", "merges.txt",
    "model.safetensors", "normalizer.json", "preprocessor_config.json",
    "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json", "vocab.json",
]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parents[1] / "models" / "cache" / "whisper-base")
    args = parser.parse_args()
    if not args.allow_download:
        parser.error("Model download requires --allow-download.")
    from huggingface_hub import snapshot_download

    root = args.directory.resolve()
    snapshot_download(repo_id=MODEL_ID, revision=MODEL_REVISION, local_dir=root, allow_patterns=FILES)
    missing = [name for name in FILES if not (root / name).is_file()]
    if missing:
        raise RuntimeError(f"Pinned model snapshot is incomplete: {missing}")
    manifest = {
        "schema_version": "mingjian-whisper-artifacts-v1",
        "model_id": MODEL_ID,
        "revision": MODEL_REVISION,
        "source": f"https://huggingface.co/{MODEL_ID}/tree/{MODEL_REVISION}",
        "license": "Apache-2.0",
        "provisioned_at": datetime.now(timezone.utc).isoformat(),
        "files": [{"name": name, "size_bytes": (root / name).stat().st_size, "sha256": digest(root / name)} for name in FILES],
    }
    (root / "mingjian-artifacts.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

