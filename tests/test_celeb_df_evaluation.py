import json
from pathlib import Path

from evaluation.celeb_df_v2 import build_official_manifest, sha256_file
from evaluation.run_celeb_df_v2 import extraction_command


def test_manifest_uses_official_labels_paths_and_source_hashes_without_writing_dataset(tmp_path) -> None:
    root = tmp_path / "Celeb-DF-v2"
    (root / "YouTube-real").mkdir(parents=True)
    (root / "Celeb-synthesis").mkdir()
    real = root / "YouTube-real" / "00001.mp4"
    fake = root / "Celeb-synthesis" / "id0_id1_0000.mp4"
    real.write_bytes(b"real-video")
    fake.write_bytes(b"fake-video")
    official = root / "List_of_testing_videos.txt"
    official.write_text("1 YouTube-real/00001.mp4\n0 Celeb-synthesis/id0_id1_0000.mp4\n", encoding="utf-8")
    output = tmp_path / "evaluation" / "manifest.jsonl"

    summary = build_official_manifest(root, output, expected_count=None)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

    assert summary["total"] == 2
    assert summary["real"] == 1
    assert summary["fake"] == 1
    assert summary["official_testing_list_sha256"] == sha256_file(official)
    assert rows[0]["relative_path"] == "YouTube-real/00001.mp4"
    assert rows[0]["label"] == "real"
    assert rows[0]["binary_label_fake"] == 0
    assert rows[1]["label"] == "fake"
    assert rows[1]["source_sha256"] == sha256_file(fake)
    assert not list(root.rglob("manifest.jsonl"))


def test_extraction_rule_is_fixed_and_uses_no_shell() -> None:
    command = extraction_command(Path("source.mp4"), Path("frames/frame-%06d.jpg"))

    assert command[:8] == ["ffmpeg", "-nostdin", "-y", "-v", "info", "-threads", "1", "-i"]
    assert "fps=1/2,scale=256:256:flags=bilinear,showinfo" in command
    assert command[command.index("-frames:v") + 1] == "150"
