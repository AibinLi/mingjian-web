"""Opt-in real FFmpeg smoke test; never uses the deterministic command substitute."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.audio_detector import AstAudioDetector
from backend.app.main import create_app


pytestmark = pytest.mark.skipif(
    os.environ.get("MINGJIAN_REAL_MEDIA_SMOKE") != "1" or not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Real FFmpeg/FFprobe smoke test requires explicit local-tool authorization.",
)


def wait_for_status(client: TestClient, job_id: str, expected: str) -> dict:
    # First local Meso4/AST CPU initialization can exceed the worker's normal
    # polling window on a cold process.  This is a test wait budget only; it
    # does not relax the bounded media-tool command timeout.
    for _ in range(300):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] == expected:
            return job
        time.sleep(0.1)
    raise AssertionError(f"Job {job_id} did not reach {expected}: {job}")


def make_media(command: list[str]) -> None:
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_real_ffmpeg_media_preprocessing_and_cleanup(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    png = fixtures / "source.png"
    webp_vp8 = fixtures / "source-vp8.webp"
    webp_vp8l = fixtures / "source-vp8l.webp"
    wav = fixtures / "source.wav"
    mp4 = fixtures / "source.mp4"

    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=0.1", "-frames:v", "1", str(png)])
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=320x240:d=0.1", "-frames:v", "1", "-c:v", "libwebp", "-lossless", "0", str(webp_vp8)])
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=0.1", "-frames:v", "1", "-c:v", "libwebp", "-lossless", "1", str(webp_vp8l)])
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.5", "-c:a", "pcm_s16le", str(wav)])
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=12:d=1.2", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=16000:duration=1.2", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(mp4)])
    audio_weight_available = AstAudioDetector(tmp_path).describe()["weight_available"]
    assert webp_vp8.read_bytes()[12:16] == b"VP8 "
    assert webp_vp8l.read_bytes()[12:16] == b"VP8L"

    cases = [
        (png, "image/png", True),
        (webp_vp8, "image/webp", True),
        (webp_vp8l, "image/webp", True),
        (wav, "audio/wav", False),
        (mp4, "video/mp4", True),
    ]
    with TestClient(create_app(tmp_path)) as client:
        for source, media_type, expects_visual_frames in cases:
            created = client.post("/api/v1/jobs/upload", files={"file": (source.name, source.read_bytes(), media_type)})
            assert created.status_code == 201, created.text
            job = wait_for_status(client, created.json()["id"], "completed")
            results = job["results"]
            assert results["status"] == "completed"
            paths = {item["path"] for item in results["artifacts"]}
            required = {"normalized_media", "metadata", "shot_list", "frame_index", "faces", "vad", "asr", "ocr", "visual_detection", "audio_detection", "audio_visual_sync"}
            assert required.issubset({item["name"] for item in results["artifacts"]})
            assert any(path.endswith(".stderr.log") for path in paths)
            for path in paths:
                assert (tmp_path / path).is_file(), path

            frames = json.loads((tmp_path / next(path for path in paths if path.endswith("frame_index.json"))).read_text(encoding="utf-8"))
            duration = float(results["input_features"].get("duration_seconds") or 0.0)
            timestamps = [frame["timestamp_seconds"] for frame in frames]
            assert timestamps == sorted(timestamps)
            assert all(0.0 <= timestamp <= duration for timestamp in timestamps)

            visual_path = tmp_path / next(path for path in paths if path.endswith("visual_detection.json"))
            visual = json.loads(visual_path.read_text(encoding="utf-8"))
            assert visual["schema_version"] == "visual-detector-result-v1"
            if expects_visual_frames:
                assert visual["status"] == "completed"
                assert visual["frame_risks"]
                assert visual["evidence"]["highest_risk_frame"]["path"].startswith("derived/")
            else:
                assert visual["status"] == "unavailable"
                assert visual["video_risk"] is None

            report = client.get(f"/api/v1/jobs/{job['id']}/report").json()
            assert report["mock"] is False
            assert report["axes"]["authenticity"]["mock"] is False
            assert report["axes"]["content"]["mock"] is True
            assert report["axes"]["provenance"]["mock"] is True
            expected_authenticity_status = "completed" if expects_visual_frames or (source == wav and audio_weight_available) else "unavailable"
            assert report["axes"]["authenticity"]["status"] == expected_authenticity_status
            assert report["audio_model"]["status"] in {"completed", "unavailable", "not_applicable"}
            if (source == wav or source == mp4) and audio_weight_available:
                assert report["audio_model"]["status"] == "completed"
            if report["audio_model"]["status"] == "completed":
                assert report["audio_model"]["segment_risks"]
                assert report["audio_model"]["whole_audio_risk"] is not None
            assert report["audio_visual_sync"]["mock"] is False
            assert report["audio_visual_sync"]["status"] in {"completed", "unavailable", "not_applicable"}

            object_path = tmp_path / job["source"]["object_key"]
            derived = tmp_path / "derived" / job["id"]
            assert client.delete(f"/api/v1/jobs/{job['id']}").status_code == 204
            assert not object_path.exists()
            assert not derived.exists()


def test_real_ffmpeg_audio_degradation_cases_are_recorded_without_low_risk_claims(tmp_path: Path) -> None:
    fixtures = tmp_path / "audio-degradation"
    fixtures.mkdir()
    source = fixtures / "source.wav"
    compressed = fixtures / "compressed.mp3"
    noisy = fixtures / "noisy.wav"
    speed = fixtures / "speed.wav"
    music = fixtures / "music.wav"
    short = fixtures / "short.wav"
    video_without_audio = fixtures / "without-audio.mp4"
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=1.2", "-c:a", "pcm_s16le", str(source)])
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(source), "-c:a", "libmp3lame", "-b:a", "48k", str(compressed)])
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(source), "-f", "lavfi", "-i", "anoisesrc=color=white:sample_rate=16000:duration=1.2:amplitude=0.03", "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0", "-c:a", "pcm_s16le", str(noisy)])
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(source), "-filter:a", "atempo=1.25", "-c:a", "pcm_s16le", str(speed)])
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(source), "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=16000:duration=1.2", "-filter_complex", "[0:a][1:a]amix=inputs=2:weights='1 0.4':normalize=0", "-c:a", "pcm_s16le", str(music)])
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.12", "-c:a", "pcm_s16le", str(short)])
    make_media(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=12:d=1.2", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video_without_audio)])
    audio_weight_available = AstAudioDetector(tmp_path).describe()["weight_available"]

    cases = [(source, "audio/wav", False), (compressed, "audio/mpeg", False), (noisy, "audio/wav", False), (speed, "audio/wav", False), (music, "audio/wav", False), (short, "audio/wav", False), (video_without_audio, "video/mp4", True)]
    with TestClient(create_app(tmp_path)) as client:
        for file_path, media_type, is_video_without_audio in cases:
            created = client.post("/api/v1/jobs/upload", files={"file": (file_path.name, file_path.read_bytes(), media_type)})
            assert created.status_code == 201, created.text
            job = wait_for_status(client, created.json()["id"], "completed")
            report = client.get(f"/api/v1/jobs/{job['id']}/report").json()
            audio = report["audio_model"]
            vad_path = tmp_path / next(item["path"] for item in job["results"]["artifacts"] if item["name"] == "vad")
            vad = json.loads(vad_path.read_text(encoding="utf-8"))
            if is_video_without_audio:
                assert audio["status"] == "not_applicable"
                assert vad["status"] == "not_applicable"
                assert report["audio_visual_sync"]["status"] == "not_applicable"
            else:
                assert audio["status"] in {"completed", "unavailable"}
                if audio_weight_available:
                    assert audio["status"] == "completed"
                assert vad["status"] in {"available", "degraded"}
                if audio["status"] == "completed":
                    assert audio["segment_risks"]
                    assert audio["whole_audio_risk"] is not None
                else:
                    assert audio["whole_audio_risk"] is None
                    assert "低风险" in "".join(audio["limitations"])
            assert report["audio_visual_sync"]["risk_score"] is None or 0.0 <= report["audio_visual_sync"]["risk_score"] <= 1.0
