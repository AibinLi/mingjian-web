import hashlib
import json
import struct
import time
import wave
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.audio_detector import AstAudioDetector
from backend.app.main import MAX_UPLOAD_BYTES, MockReporter, PhaseThreeReporter, create_app
from backend.app.preprocess import CommandResult, MediaPreprocessor, image_dimensions, sniff_magic
from backend.app.visual_detector import Meso4VisualDetector


def wait_for_status(client: TestClient, job_id: str, expected: str) -> dict:
    # The first real Meso4 inference includes a one-time local checkpoint load.
    for _ in range(240):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] == expected:
            return job
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not reach {expected}: {job}")


class FixtureMediaRunner:
    """Deterministic command-result substitute; it does not decode media."""

    def __init__(self, *, damaged_probe: bool = False, low_resolution_video: bool = False, probe_override=None, keyframe_pts=None, keyframe_showinfo_pts=None, scene_pts=None, derived_bytes: int = 0) -> None:
        self.damaged_probe = damaged_probe
        self.low_resolution_video = low_resolution_video
        self.probe_override = probe_override
        self.keyframe_pts = keyframe_pts
        self.keyframe_showinfo_pts = keyframe_showinfo_pts
        self.scene_pts = scene_pts
        self.derived_bytes = derived_bytes

    def run(self, command, timeout_seconds, cancelled):
        if cancelled():
            return CommandResult(-1, "fixture cancelled", cancelled=True)
        if command[0] == "ffprobe":
            if self.damaged_probe:
                return CommandResult(1, "fixture: corrupt media container")
            if self.probe_override is not None:
                return CommandResult(0, "", json.dumps(self.probe_override))
            magic = sniff_magic(Path(command[-1]).read_bytes()[:4096])
            if magic == "png":
                probe = {"format": {"format_name": "png_pipe", "duration": "0"}, "streams": [{"codec_type": "video", "codec_name": "png", "width": 640, "height": 480, "avg_frame_rate": "0/0"}]}
            elif magic == "jpeg":
                probe = {"format": {"format_name": "jpeg_pipe", "duration": "0"}, "streams": [{"codec_type": "video", "codec_name": "mjpeg", "width": 640, "height": 480, "avg_frame_rate": "0/0"}]}
            elif magic == "webp":
                probe = {"format": {"format_name": "webp_pipe", "duration": "0"}, "streams": [{"codec_type": "video", "codec_name": "webp", "width": 640, "height": 480, "avg_frame_rate": "0/0"}]}
            elif magic == "wav":
                probe = {"format": {"format_name": "wav", "duration": "1.0"}, "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le", "sample_rate": "16000", "channels": 1}]}
            else:
                dimensions = (160, 90) if self.low_resolution_video else (640, 480)
                probe = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2.0"}, "streams": [{"codec_type": "video", "codec_name": "h264", "width": dimensions[0], "height": dimensions[1], "avg_frame_rate": "24/1"}]}
            return CommandResult(0, "", json.dumps(probe))
        target = Path(command[-1])
        filter_value = command[command.index("-vf") + 1] if "-vf" in command else ""
        if "fps=1/2,showinfo" in filter_value:
            points = self.keyframe_pts if self.keyframe_pts is not None else [0.0]
            for index, _ in enumerate(points, 1):
                self._write_target(target, index)
            showinfo_points = self.keyframe_showinfo_pts if self.keyframe_showinfo_pts is not None else points
            stderr = "\n".join(f"[Parsed_showinfo] n:{index} pts_time:{point}" for index, point in enumerate(showinfo_points))
            return CommandResult(0, stderr)
        if "select='gt(scene,0.30)'" in filter_value:
            points = self.scene_pts if self.scene_pts is not None else []
            stderr = "\n".join(f"[Parsed_showinfo] n:{index} pts_time:{point}" for index, point in enumerate(points))
            return CommandResult(0, stderr)
        self._write_target(target, 1)
        return CommandResult(0, "")

    def _write_target(self, target: Path, index: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if "%" in target.name:
            target = target.with_name(target.name.replace("%06d", f"{index:06d}"))
        target.write_bytes(b"x" * self.derived_bytes if self.derived_bytes else b"fixture-derived-media")


def png_fixture() -> bytes:
    # A complete, 1x1 RGB PNG with a valid signature and IHDR.
    import zlib
    def chunk(kind: bytes, value: bytes) -> bytes:
        return len(value).to_bytes(4, "big") + kind + value + zlib.crc32(kind + value).to_bytes(4, "big")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 640, 480, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b"")


def jpeg_fixture() -> bytes:
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\xff\xc0\x00\x11\x08\x01\xe0\x02\x80\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00\xff\xd9"


def webp_chunk(kind: bytes, payload: bytes) -> bytes:
    body = b"WEBP" + kind + len(payload).to_bytes(4, "little") + payload
    if len(payload) % 2:
        body += b"\x00"
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def webp_vp8_fixture(width: int = 640, height: int = 480) -> bytes:
    return webp_chunk(b"VP8 ", b"\x00\x00\x00\x9d\x01\x2a" + width.to_bytes(2, "little") + height.to_bytes(2, "little"))


def webp_vp8l_fixture(width: int = 640, height: int = 480) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    return webp_chunk(b"VP8L", b"\x2f" + bits.to_bytes(4, "little"))


def webp_vp8x_fixture(width: int = 640, height: int = 480) -> bytes:
    return webp_chunk(b"VP8X", b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))


def wav_fixture() -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(16000); writer.writeframes(b"\x00\x00" * 16000)
    return buffer.getvalue()


@pytest.mark.parametrize("filename,media_type,payload", [
    pytest.param("normal.png", "image/png", png_fixture(), id="png"),
    pytest.param("normal.jpg", "image/jpeg", jpeg_fixture(), id="jpeg"),
    pytest.param("normal.webp", "image/webp", webp_vp8x_fixture(), id="webp"),
    pytest.param("normal.wav", "audio/wav", wav_fixture(), id="wav"),
])
def test_four_normal_media_types_preprocess_and_preserve_three_axes(tmp_path, filename, media_type, payload) -> None:
    preprocessor = MediaPreprocessor(tmp_path, runner=FixtureMediaRunner())
    # This is a pre-processing contract test.  It must not depend on a local
    # AST/Meso4 cold start or on user-installed local model caches.
    unavailable_audio = AstAudioDetector(tmp_path, model_dir=tmp_path / "missing-audio-model")
    unavailable_visual = Meso4VisualDetector(tmp_path, weights_path=tmp_path / "missing-visual-model")
    with TestClient(create_app(tmp_path, preprocessor=preprocessor, detector=unavailable_visual, audio_detector=unavailable_audio)) as client:
        created = client.post(
            "/api/v1/jobs/upload",
            files={"file": (filename, payload, media_type)},
        )
        assert created.status_code == 201
        job = created.json()
        assert job["status"] == "queued"
        assert job["source"]["object_key"] == f"objects/{hashlib.sha256(payload).hexdigest()[:2]}/{hashlib.sha256(payload).hexdigest()}"
        assert "normal" not in job["source"]["object_key"]

        assert wait_for_status(client, job["id"], "completed")["error"] is None
        report = client.get(f"/api/v1/jobs/{job['id']}/report")
        # These report-only endpoints are intentionally exercised for all four
        # upload demo media types.  They only expose persisted, allow-listed
        # derivatives and are what the phase-7 UI uses for playback, frames
        # and the ASR/OCR audit view.
        assert client.get(f"/api/v1/jobs/{job['id']}/artifacts/asr").status_code == 200
        assert client.get(f"/api/v1/jobs/{job['id']}/artifacts/ocr").status_code == 200
        assert client.get(f"/api/v1/jobs/{job['id']}/artifacts/internet_verification").status_code == 200
        assert client.get(f"/api/v1/jobs/{job['id']}/artifacts/aggregate_assessment").status_code == 200
        assert client.get(f"/api/v1/jobs/{job['id']}/media").status_code == 200
        assert client.get(f"/api/v1/jobs/{job['id']}/artifacts/ffprobe_stderr").status_code == 404
        if filename != "normal.wav":
            assert client.get(f"/api/v1/jobs/{job['id']}/frames/1").status_code == 200

    assert report.status_code == 200
    assert report.json()["mock"] is False
    assert set(report.json()["axes"]) == {"authenticity", "content", "provenance"}
    assert report.json()["axes"]["authenticity"]["mock"] is False
    assert report.json()["axes"]["authenticity"]["conclusion"] == "无法判断"
    assert report.json()["axes"]["content"]["mock"] is True
    assert report.json()["axes"]["provenance"]["mock"] is True
    assert report.json()["aggregate_assessment"]["routing_label"] == "需人工干预"
    assert report.json()["aggregate_assessment"]["truth_label"] == "none"
    results = report.json()["preprocess"]
    assert results["status"] == "completed"
    assert {"metadata", "shot_list", "frame_index", "faces", "ocr", "vad", "asr", "visual_detection"}.issubset({item["name"] for item in results["artifacts"]})


def test_upload_can_record_a_public_original_url_without_fetching_it(tmp_path) -> None:
    app = create_app(tmp_path, start_worker=False, test_mode=True)
    with TestClient(app) as client:
        created = client.post("/api/v1/jobs/upload", files={"file": ("source.png", png_fixture(), "image/png")}, data={"source_url": "https://example.org/original-post"})
        assert created.status_code == 201
        assert created.json()["source"]["declared_source_url"] == "https://example.org/original-post"


def test_mock_reporter_returns_complete_phase_two_report_without_affecting_phase_three(tmp_path) -> None:
    mock_report = MockReporter().create({"id": "mock-job"}, {"status": "completed"})

    assert isinstance(mock_report, dict)
    assert mock_report["mock"] is True
    assert set(mock_report["axes"]) == {"authenticity", "content", "provenance"}
    assert all(axis["mock"] is True for axis in mock_report["axes"].values())

    class CompletedDetector:
        def detect(self, job, preprocess, cancelled):
            return {
                "schema_version": "visual-detector-result-v1", "status": "completed", "mock": False,
                "model": {}, "configuration": {}, "input_quality": {}, "frame_risks": [{"risk_threshold": 0.5}],
                "track_risks": [], "video_risk": {"risk_score": 0.8},
                "evidence": {"highest_risk_frame": {"frame_index": 1}, "suspicious_segments": []},
                "limitations": [], "elapsed_seconds": 0.0,
            }

    phase_three = PhaseThreeReporter(tmp_path, CompletedDetector()).create({"id": "phase-three-job"})
    assert phase_three["mock"] is False
    assert phase_three["axes"]["authenticity"]["mock"] is False
    assert phase_three["axes"]["content"]["mock"] is True
    assert phase_three["axes"]["provenance"]["mock"] is True


def test_rejects_illegal_extension_and_oversized_upload(tmp_path) -> None:
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        illegal = client.post("/api/v1/jobs/upload", files={"file": ("suspect.exe", b"x", "application/octet-stream")})
        oversized = client.post(
            "/api/v1/jobs/upload",
            files={"file": ("large.png", png_fixture() + b"x" * (MAX_UPLOAD_BYTES + 1), "image/png")},
        )

    assert illegal.status_code == 415
    assert oversized.status_code == 413


def test_rejects_magic_mismatch_and_unknown_corrupt_input(tmp_path) -> None:
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        mismatch = client.post("/api/v1/jobs/upload", files={"file": ("wrong.png", jpeg_fixture(), "image/png")})
        webp_mismatch = client.post("/api/v1/jobs/upload", files={"file": ("wrong.png", webp_vp8_fixture(), "image/png")})
        corrupt = client.post("/api/v1/jobs/upload", files={"file": ("corrupt.png", b"not a media container", "image/png")})
    assert mismatch.status_code == 415
    assert webp_mismatch.status_code == 415
    assert corrupt.status_code == 415


@pytest.mark.parametrize("payload,expected", [
    pytest.param(webp_vp8_fixture(321, 123), (321, 123), id="vp8"),
    pytest.param(webp_vp8l_fixture(322, 124), (322, 124), id="vp8l"),
    pytest.param(webp_vp8x_fixture(323, 125), (323, 125), id="vp8x"),
])
def test_webp_common_chunk_headers_have_safe_dimension_parsing(tmp_path, payload, expected) -> None:
    source = tmp_path / "variant.webp"
    source.write_bytes(payload)
    assert image_dimensions(source, "webp") == expected


@pytest.mark.parametrize("payload", [
    pytest.param(webp_vp8_fixture(), id="vp8"),
    pytest.param(webp_vp8l_fixture(), id="vp8l"),
    pytest.param(webp_vp8x_fixture(), id="vp8x"),
])
def test_webp_variants_reach_container_stage_with_command_substitute(tmp_path, payload) -> None:
    # The substitute supplies only a deterministic command result.  It is not a
    # real WebP decoder and this is not a real decoder-success claim.
    preprocessor = MediaPreprocessor(tmp_path, runner=FixtureMediaRunner())
    with TestClient(create_app(tmp_path, preprocessor=preprocessor)) as client:
        job_id = client.post("/api/v1/jobs/upload", files={"file": ("variant.webp", payload, "image/webp")}).json()["id"]
        completed = wait_for_status(client, job_id, "completed")
    assert completed["results"]["input_features"]["magic"] == "webp"


def test_damaged_webp_is_deferred_to_container_validation_and_fails_with_trace(tmp_path) -> None:
    # This is intentionally only a malformed RIFF/WebP-shaped fixture.  The
    # command substitute does not claim it can decode WebP.
    damaged = webp_chunk(b"VP8 ", b"\x00\x00")
    preprocessor = MediaPreprocessor(tmp_path, runner=FixtureMediaRunner(damaged_probe=True))
    with TestClient(create_app(tmp_path, preprocessor=preprocessor)) as client:
        created = client.post("/api/v1/jobs/upload", files={"file": ("damaged.webp", damaged, "image/webp")})
        assert created.status_code == 201
        failed = wait_for_status(client, created.json()["id"], "failed")
    assert failed["error"]["code"] == "ffprobe_failed"
    assert failed["results"]["input_features"]["magic"] == "webp"


def test_damaged_container_retains_stderr_command_and_input_features(tmp_path) -> None:
    preprocessor = MediaPreprocessor(tmp_path, runner=FixtureMediaRunner(damaged_probe=True))
    with TestClient(create_app(tmp_path, preprocessor=preprocessor)) as client:
        job_id = client.post("/api/v1/jobs/upload", files={"file": ("damaged.png", png_fixture(), "image/png")}).json()["id"]
        failed = wait_for_status(client, job_id, "failed")
    assert failed["error"]["code"] == "ffprobe_failed"
    assert failed["results"]["input_features"]["magic"] == "png"
    stderr = next(item for item in failed["results"]["artifacts"] if item["name"] == "ffprobe_stderr")
    assert (tmp_path / stderr["path"]).read_text(encoding="utf-8") == "fixture: corrupt media container"


def test_derived_budget_exceeded_removes_media_but_keeps_trace_and_source(tmp_path) -> None:
    budget = 8 * 1024
    preprocessor = MediaPreprocessor(tmp_path, runner=FixtureMediaRunner(derived_bytes=20 * 1024), max_derived_bytes=budget)
    payload = png_fixture()
    digest = hashlib.sha256(payload).hexdigest()
    with TestClient(create_app(tmp_path, preprocessor=preprocessor)) as client:
        job_id = client.post("/api/v1/jobs/upload", files={"file": ("large-derived.png", payload, "image/png")}).json()["id"]
        failed = wait_for_status(client, job_id, "failed")
    derived = tmp_path / "derived" / job_id
    assert failed["error"]["code"] == "derived_size_exceeded"
    assert (tmp_path / "objects" / digest[:2] / digest).is_file()
    assert (derived / "metadata.json").is_file()
    assert list(derived.glob("normalized.*")) == []
    assert not (derived / "frames").exists()
    assert sum(path.stat().st_size for path in derived.rglob("*") if path.is_file()) <= budget
    assert any(item["name"] == "ffmpeg_normalize_stderr" for item in failed["results"]["artifacts"])


def test_low_resolution_video_without_audio_is_degraded_not_low_risk(tmp_path) -> None:
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    preprocessor = MediaPreprocessor(tmp_path, runner=FixtureMediaRunner(low_resolution_video=True))
    with TestClient(create_app(tmp_path, preprocessor=preprocessor)) as client:
        job_id = client.post("/api/v1/jobs/upload", files={"file": ("small.mp4", mp4, "video/mp4")}).json()["id"]
        completed = wait_for_status(client, job_id, "completed")
    gates = completed["results"]["quality_gates"]
    assert {gate["name"]: gate["status"] for gate in gates}["resolution"] == "degraded"
    assert {gate["name"]: gate["status"] for gate in gates}["audio"] == "not_applicable"
    assert all("低风险" not in str(gate.get("reason", "")) or "不构成" in gate["reason"] or "不能" in gate["reason"] for gate in gates)


def video_probe(duration: str = "2.0") -> dict:
    return {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": duration}, "streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 480, "avg_frame_rate": "24000/1001"}]}


def test_vfr_keyframe_pts_and_scene_mapping_are_exact_and_bounded(tmp_path) -> None:
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    preprocessor = MediaPreprocessor(tmp_path, runner=FixtureMediaRunner(probe_override=video_probe(), keyframe_pts=[0.0, 0.333, 1.94], scene_pts=[0.40, 1.80]))
    with TestClient(create_app(tmp_path, preprocessor=preprocessor)) as client:
        job_id = client.post("/api/v1/jobs/upload", files={"file": ("vfr.mp4", mp4, "video/mp4")}).json()["id"]
        completed = wait_for_status(client, job_id, "completed")
    results = completed["results"]
    frame_index = json.loads((tmp_path / next(item["path"] for item in results["artifacts"] if item["name"] == "frame_index")).read_text(encoding="utf-8"))
    shot_list = json.loads((tmp_path / next(item["path"] for item in results["artifacts"] if item["name"] == "shot_list")).read_text(encoding="utf-8"))
    assert [frame["timestamp_seconds"] for frame in frame_index] == [0.0, 0.333, 1.94]
    assert all(frame["timestamp_quality"] == "exact" for frame in frame_index)
    assert all(0.0 <= frame["timestamp_seconds"] <= 2.0 for frame in frame_index)
    assert [shot["keyframe_index"] for shot in shot_list] == [1, 2, 3]


def test_short_video_last_keyframe_timestamp_is_not_past_duration(tmp_path) -> None:
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    preprocessor = MediaPreprocessor(tmp_path, runner=FixtureMediaRunner(probe_override=video_probe("0.25"), keyframe_pts=[0.0, 0.25]))
    with TestClient(create_app(tmp_path, preprocessor=preprocessor)) as client:
        job_id = client.post("/api/v1/jobs/upload", files={"file": ("short.mp4", mp4, "video/mp4")}).json()["id"]
        completed = wait_for_status(client, job_id, "completed")
    index_path = tmp_path / next(item["path"] for item in completed["results"]["artifacts"] if item["name"] == "frame_index")
    frames = json.loads(index_path.read_text(encoding="utf-8"))
    assert [frame["timestamp_seconds"] for frame in frames] == [0.0, 0.25]
    assert all(frame["timestamp_seconds"] <= 0.25 for frame in frames)


def test_missing_showinfo_pts_is_explicitly_estimated(tmp_path) -> None:
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    preprocessor = MediaPreprocessor(tmp_path, runner=FixtureMediaRunner(probe_override=video_probe(), keyframe_pts=[0.0, 1.0], keyframe_showinfo_pts=[]))
    with TestClient(create_app(tmp_path, preprocessor=preprocessor)) as client:
        job_id = client.post("/api/v1/jobs/upload", files={"file": ("missing-pts.mp4", mp4, "video/mp4")}).json()["id"]
        completed = wait_for_status(client, job_id, "completed")
    index_path = tmp_path / next(item["path"] for item in completed["results"]["artifacts"] if item["name"] == "frame_index")
    frames = json.loads(index_path.read_text(encoding="utf-8"))
    assert all(frame["timestamp_quality"] == "estimated" for frame in frames)
    assert all(0.0 <= frame["timestamp_seconds"] <= 2.0 for frame in frames)


@pytest.mark.parametrize("probe,error_code", [
    pytest.param({"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "301"}, "streams": [{"codec_type": "video", "width": 640, "height": 480, "avg_frame_rate": "24/1"}]}, "duration_exceeded", id="duration"),
    pytest.param({"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2"}, "streams": [{"codec_type": "video", "width": 4097, "height": 480, "avg_frame_rate": "24/1"}]}, "resolution_exceeded", id="resolution"),
    pytest.param({"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2"}, "streams": [{"codec_type": "video", "width": 640, "height": 480, "avg_frame_rate": "61/1"}]}, "frame_rate_exceeded", id="frame-rate"),
])
def test_duration_resolution_and_frame_rate_limits_are_enforced(tmp_path, probe, error_code) -> None:
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    preprocessor = MediaPreprocessor(tmp_path, runner=FixtureMediaRunner(probe_override=probe))
    with TestClient(create_app(tmp_path, preprocessor=preprocessor)) as client:
        job_id = client.post("/api/v1/jobs/upload", files={"file": ("limited.mp4", mp4, "video/mp4")}).json()["id"]
        failed = wait_for_status(client, job_id, "failed")
    assert failed["error"]["code"] == error_code


def test_preprocess_cancellation_keeps_its_stderr_trace(tmp_path) -> None:
    digest = hashlib.sha256(png_fixture()).hexdigest()
    source = tmp_path / "objects" / digest[:2] / digest
    source.parent.mkdir(parents=True)
    source.write_bytes(png_fixture())
    job = {"id": "cancellation-test", "source": {"type": "file", "object_key": f"objects/{digest[:2]}/{digest}", "media_extension": ".png"}}
    with pytest.raises(Exception) as caught:
        MediaPreprocessor(tmp_path, runner=FixtureMediaRunner()).process(job, lambda: True)
    error = caught.value
    assert getattr(error, "code") == "preprocess_cancelled"
    result = getattr(error, "result")
    stderr = next(item for item in result["artifacts"] if item["name"] == "ffprobe_stderr")
    assert (tmp_path / stderr["path"]).read_text(encoding="utf-8") == "fixture cancelled"


def test_cancelled_job_can_be_retried_and_deleted(tmp_path) -> None:
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        created = client.post("/api/v1/jobs", json={"url": "https://example.org/allowed-media"})
        assert created.status_code == 201
        job_id = created.json()["id"]
        cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel")
        assert cancelled.json()["status"] == "cancelled"
        retried = client.post(f"/api/v1/jobs/{job_id}/retry")
        assert retried.status_code == 200
        assert retried.json()["status"] == "queued"
        assert client.delete(f"/api/v1/jobs/{job_id}").status_code == 204
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404


def test_job_metadata_survives_a_new_application_instance(tmp_path) -> None:
    with TestClient(create_app(tmp_path, start_worker=False)) as first_client:
        created = first_client.post("/api/v1/jobs", json={"url": "https://example.org/persisted"})
        job_id = created.json()["id"]

    # A page reload queries this same persistent API state; a process restart does too.
    with TestClient(create_app(tmp_path, start_worker=False)) as second_client:
        restored = second_client.get(f"/api/v1/jobs/{job_id}")

    assert restored.status_code == 200
    assert restored.json()["status"] == "queued"


def test_worker_failure_is_persisted_and_retry_completes(tmp_path) -> None:
    # The dependency-injected reporter fails once; production reporter has no failure switch.
    with TestClient(create_app(tmp_path, reporter=MockReporter(failures_remaining=1))) as client:
        created = client.post("/api/v1/jobs", json={"url": "https://example.org/failure-coverage"})
        job_id = created.json()["id"]
        failed = wait_for_status(client, job_id, "failed")
        assert failed["error"]["code"] == "mock_worker_error"
        assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 200
        completed = wait_for_status(client, job_id, "completed")
        assert completed["error"] is None
