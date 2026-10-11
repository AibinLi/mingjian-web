import hashlib
import json
import math
import wave
from pathlib import Path

from backend.app import audio_detector
from backend.app.audio_detector import AstAudioDetector
from backend.app.audio_visual_sync import HaarMotionAudioVisualSyncAdapter, MockAudioVisualSyncAdapter
from backend.app.main import PhaseFourReporter
from backend.app.preprocess import EnergyVadAdapter


def make_tone(path: Path, duration: float = 0.6) -> None:
    rate = 16000
    samples = [int(9000 * math.sin(2 * math.pi * 440 * index / rate)) for index in range(int(rate * duration))]
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples))


def test_energy_vad_produces_locatable_speech_segments(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    make_tone(audio)

    result = EnergyVadAdapter().analyse(audio)

    assert result["mock"] is False
    assert result["status"] == "available"
    assert result["segments"]
    assert result["segments"][0]["start_seconds"] == 0.0
    assert result["segments"][0]["end_seconds"] > 0.5


def test_audio_detector_returns_not_applicable_for_video_without_audio(tmp_path: Path) -> None:
    detector = AstAudioDetector(tmp_path, model_dir=tmp_path / "missing")
    result = detector.detect({"id": "no-audio"}, {"status": "completed", "input_features": {"has_audio": False}, "quality_gates": []}, lambda: False)

    assert result["status"] == "not_applicable"
    assert result["whole_audio_risk"] is None
    assert result["segment_risks"] == []
    assert "低风险" in "".join(result["limitations"])


def test_audio_detector_missing_weight_is_explicitly_unavailable(tmp_path: Path) -> None:
    derived = tmp_path / "derived" / "audio-test"
    derived.mkdir(parents=True)
    audio = derived / "audio.wav"
    make_tone(audio)
    vad = EnergyVadAdapter().analyse(audio)
    (derived / "vad.json").write_text(json.dumps(vad), encoding="utf-8")
    preprocess = {"status": "completed", "input_features": {"has_audio": True}, "quality_gates": [], "artifacts": [{"name": "normalized_audio", "path": "derived/audio-test/audio.wav"}, {"name": "vad", "path": "derived/audio-test/vad.json"}]}

    result = AstAudioDetector(tmp_path, model_dir=tmp_path / "missing").detect({"id": "audio-test"}, preprocess, lambda: False)

    assert result["status"] == "unavailable"
    assert result["whole_audio_risk"] is None
    assert result["segment_risks"] == []


def test_audio_detector_rejects_each_invalid_frozen_artifact_before_loading(tmp_path: Path, monkeypatch) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    expected = {"model.safetensors": b"weights", "config.json": b"config", "preprocessor_config.json": b"processor"}
    monkeypatch.setattr(audio_detector, "FROZEN_ARTIFACTS", {name: {"size_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()} for name, value in expected.items()})
    derived = tmp_path / "derived" / "frozen"
    derived.mkdir(parents=True)
    audio = derived / "audio.wav"
    make_tone(audio, 0.8)
    vad = EnergyVadAdapter().analyse(audio)
    (derived / "vad.json").write_text(json.dumps(vad), encoding="utf-8")
    preprocess = {"status": "completed", "input_features": {"has_audio": True}, "quality_gates": [], "artifacts": [{"name": "normalized_audio", "path": "derived/frozen/audio.wav"}, {"name": "vad", "path": "derived/frozen/vad.json"}]}

    for invalid_name, invalid_value in (("model.safetensors", b"bad-weights"), ("config.json", b"bad-config"), ("preprocessor_config.json", None)):
        for name, value in expected.items():
            path = model_dir / name
            if name == invalid_name and invalid_value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(invalid_value if name == invalid_name else value)
        detector = AstAudioDetector(tmp_path, model_dir=model_dir)
        result = detector.detect({"id": "frozen"}, preprocess, lambda: False)

        assert result["status"] == "unavailable"
        assert invalid_name in "".join(result["limitations"])
        assert result["model"]["frozen_artifacts"][invalid_name]["valid"] is False


def test_audio_detector_caches_processor_and_cpu_model_once(tmp_path: Path) -> None:
    calls = {"processor": 0, "model": 0, "eval": 0, "to": 0}

    class Processor:
        @staticmethod
        def from_pretrained(*_args, **kwargs):
            assert kwargs["local_files_only"] is True
            calls["processor"] += 1
            return object()

    class Model:
        @staticmethod
        def from_pretrained(*_args, **kwargs):
            assert kwargs["local_files_only"] is True
            calls["model"] += 1
            return Model()

        def eval(self):
            calls["eval"] += 1
            return self

        def to(self, device):
            assert device == "cpu"
            calls["to"] += 1
            return self

    detector = AstAudioDetector(tmp_path, model_dir=tmp_path / "model")
    first = detector._load_runtime(Processor, Model)
    second = detector._load_runtime(Processor, Model)

    assert first is second
    assert calls == {"processor": 1, "model": 1, "eval": 1, "to": 1}


def test_sync_adapter_is_mock_and_never_claims_a_risk_score() -> None:
    adapter = MockAudioVisualSyncAdapter()
    vad = {"status": "available", "segments": [{"kind": "speech", "start_seconds": 0.0, "end_seconds": 1.0}]}

    result = adapter.analyse({"id": "sync"}, {"input_features": {"has_audio": True, "has_video": True}, "quality_gates": []}, vad, lambda: False)

    assert result["mock"] is True
    assert result["status"] == "unavailable"
    assert result["risk_score"] is None
    assert result["anomalous_intervals"] == []


def test_haar_motion_sync_returns_not_applicable_without_audio_or_video(tmp_path: Path) -> None:
    adapter = HaarMotionAudioVisualSyncAdapter(tmp_path, opencv_dir=tmp_path / "missing-opencv")
    vad = {"status": "available", "segments": [{"kind": "speech", "start_seconds": 0.0, "end_seconds": 2.0}]}

    no_audio = adapter.analyse({"id": "sync"}, {"input_features": {"has_audio": False, "has_video": True}, "quality_gates": []}, vad, lambda: False)
    no_video = adapter.analyse({"id": "sync"}, {"input_features": {"has_audio": True, "has_video": False}, "quality_gates": []}, vad, lambda: False)

    assert no_audio["mock"] is False
    assert no_audio["status"] == "not_applicable"
    assert no_audio["risk_score"] is None
    assert no_video["status"] == "not_applicable"


def test_haar_motion_sync_emits_locatable_anomaly_for_low_correlation(tmp_path: Path) -> None:
    adapter = HaarMotionAudioVisualSyncAdapter(tmp_path, opencv_dir=tmp_path / "missing-opencv")
    times = [index / 6 for index in range(13)]
    motion = [float(index) for index in range(13)]
    audio = list(reversed(motion))
    preprocess = {"input_features": {"has_audio": True, "has_video": True}, "quality_gates": []}

    result = adapter._score_samples(list(zip(times, motion)), audio, [(0.0, 2.0)], preprocess, 2.0, {"status": "available", "motion_sample_count": 13})

    assert result["status"] == "completed"
    assert result["mock"] is False
    assert result["risk_score"] >= result["risk_threshold"]
    assert result["anomalous_intervals"]
    assert result["anomalous_intervals"][0]["start_seconds"] == 0.0
    assert result["anomalous_intervals"][0]["end_seconds"] == 2.0


def test_report_persists_segment_level_audio_evidence(tmp_path: Path) -> None:
    class Visual:
        def detect(self, *_args):
            return {"status": "unavailable", "limitations": ["no frames"], "video_risk": None, "frame_risks": [], "evidence": {"highest_risk_frame": None, "suspicious_segments": []}}

    class Audio:
        def describe(self):
            return {"name": "test"}

        def detect(self, *_args):
            segment = {"segment_index": 0, "start_seconds": 0.2, "end_seconds": 0.8, "risk_score": 0.9, "risk_threshold": 0.5}
            return {"status": "completed", "mock": False, "limitations": [], "whole_audio_risk": {"risk_score": 0.9}, "segment_risks": [segment], "evidence": {"highest_risk_segment": segment, "suspicious_segments": [{"start_seconds": 0.2, "end_seconds": 0.8, "peak_risk_score": 0.9}]}}

    preprocess = {"status": "completed", "input_features": {"has_audio": True, "has_video": False}, "quality_gates": [], "artifacts": []}
    report = PhaseFourReporter(tmp_path, Visual(), Audio()).create({"id": "report-audio"}, preprocess)
    artifact = tmp_path / "derived" / "report-audio" / "audio_detection.json"

    assert report["audio_model"]["segment_risks"][0]["start_seconds"] == 0.2
    assert artifact.is_file()
    assert json.loads(artifact.read_text(encoding="utf-8"))["evidence"]["suspicious_segments"][0]["end_seconds"] == 0.8
