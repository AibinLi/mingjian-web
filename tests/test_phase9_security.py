"""Safety regressions for Stage 9. Fixtures exercise defenses only."""

from __future__ import annotations

import hashlib
import json
import time
import asyncio
import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import MAX_UPLOAD_BODY_BYTES, JobRepository, StoredSource, create_app, validate_url
from backend.app.local_boundary import LocalRequestBoundaryMiddleware
from backend.app.ffmpeg_sandbox import MAX_FFMPEG_ADDRESS_SPACE_BYTES, apply_posix_limits
from backend.app.preprocess import CommandResult, SAFE_PROTOCOL_WHITELIST, MediaPreprocessor, SubprocessCommandRunner
from backend.app.security import redact_sensitive, safe_error_trace, validate_public_http_url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://public.example/file",
        "http://127.0.0.1/admin",
        "http://2130706433/admin",
        "http://[::1]/admin",
        "http://10.0.0.1/private",
        "http://192.168.1.1/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata.google.internal./computeMetadata/v1/",
        "http://localhost./",
        "http://example.local./",
        "http://public.example:8080/",
        "http://user:secret@public.example/",
    ],
)
def test_url_submission_blocks_non_public_or_non_http_inputs(tmp_path, url: str) -> None:
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        response = client.post("/api/v1/jobs", json={"url": url})
    assert response.status_code == 422


def test_url_reference_is_normalized_but_no_network_fetcher_exists() -> None:
    assert validate_url("HTTPS://BÜCHER.Example./report#ignored") == "https://xn--bcher-kva.example/report"
    assert validate_url("https://[2606:4700:4700::1111]/a") == "https://[2606:4700:4700::1111]/a"
    with pytest.raises(ValueError):
        validate_public_http_url("http://[::1]/")


def test_filename_is_not_a_path_and_server_key_is_hash_only(tmp_path) -> None:
    # A minimal valid PNG header is sufficient because this submission test does
    # not claim decoder success.
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 48
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        response = client.post("/api/v1/jobs/upload", files={"file": ("../../outside.png", payload, "image/png")})
    assert response.status_code == 201
    key = response.json()["source"]["object_key"]
    digest = hashlib.sha256(payload).hexdigest()
    assert key == f"objects/{digest[:2]}/{digest}"
    assert ".." not in key and "outside" not in key


def test_expired_final_job_proactively_deletes_source_and_derived_artifacts(tmp_path) -> None:
    repository = JobRepository(tmp_path)
    source_bytes = b"retention-fixture"
    digest = hashlib.sha256(source_bytes).hexdigest()
    object_key = f"objects/{digest[:2]}/{digest}"
    source = tmp_path / object_key
    source.parent.mkdir(parents=True)
    source.write_bytes(source_bytes)
    job = repository.create(StoredSource("file", object_key=object_key, sha256=digest, size_bytes=len(source_bytes), media_extension=".png", inferred_mime="image/png"))
    repository.claim_next()
    repository.fail(job["id"], "fixture", "fixture failure")
    derived = tmp_path / "derived" / job["id"]
    derived.mkdir(parents=True)
    (derived / "audit.json").write_text(json.dumps({"fixture": True}), encoding="utf-8")
    assert repository.purge_expired(60, reference_time=time.time() + 61) == 1
    assert repository.get(job["id"]) is None
    assert not source.exists()
    assert not derived.exists()


def test_error_and_log_values_redact_credentials_and_tokens() -> None:
    value = "https://alice:super-secret@example.org/path?token=top-secret Authorization: Bearer private-token"
    redacted = redact_sensitive(value)
    assert "super-secret" not in redacted
    assert "private-token" not in redacted
    assert "[REDACTED]" in redacted
    assert "https://alice:[REDACTED]@example.org" in redacted


def test_safe_error_trace_removes_secrets_paths_and_stack_details() -> None:
    trace = safe_error_trace(RuntimeError(
        r"API_KEY=private-token failed at C:\Users\WHY\private\model.bin and /srv/private/cache/model.bin"
    ))

    assert trace.startswith("RuntimeError:")
    assert "private-token" not in trace
    assert r"C:\Users\WHY" not in trace
    assert "/srv/private" not in trace
    assert "[REDACTED]" in trace
    assert "[PATH]" in trace


def test_prompt_like_asr_text_cannot_be_a_url_or_tool_instruction() -> None:
    # Validation is applied as data: no LLM/tool exists at this boundary, and
    # injected schemes cannot become URL work items.
    with pytest.raises(Exception):
        validate_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        validate_public_http_url("http://localhost/ignore-previous-instructions")


def test_ffmpeg_runner_refuses_any_non_allowlisted_executable() -> None:
    result = SubprocessCommandRunner().run(["not-a-media-tool", "--version"], 1.0, lambda: False)
    assert result.returncode == 126
    assert "allow-list" in result.stderr


def test_ffmpeg_windows_launches_directly_while_posix_uses_rlimit_helper() -> None:
    command = ["ffprobe", "-protocol_whitelist", SAFE_PROTOCOL_WHITELIST, "-version"]
    runner = SubprocessCommandRunner()
    assert runner._spawn_command(command, platform_name="nt") == command
    posix = runner._spawn_command(command, platform_name="posix")
    assert posix[:3] != command[:3]
    assert posix[-len(command):] == command
    assert posix[2] == "--"


def test_posix_helper_does_not_set_process_or_thread_count_limit() -> None:
    class ResourceRecorder:
        RLIMIT_CPU = "cpu"
        RLIMIT_AS = "as"
        RLIMIT_FSIZE = "fsize"
        RLIMIT_NOFILE = "nofile"
        RLIMIT_NPROC = "nproc"
        calls: list[tuple[str, tuple[int, int]]] = []
        @classmethod
        def setrlimit(cls, resource_name, values):  # type: ignore[no-untyped-def]
            cls.calls.append((resource_name, values))

    apply_posix_limits(ResourceRecorder)
    assert {name for name, _ in ResourceRecorder.calls} == {"cpu", "as", "fsize", "nofile"}
    assert "nproc" not in {name for name, _ in ResourceRecorder.calls}
    assert ("as", (MAX_FFMPEG_ADDRESS_SPACE_BYTES, MAX_FFMPEG_ADDRESS_SPACE_BYTES)) in ResourceRecorder.calls


def test_all_constructed_media_commands_declare_small_thread_limits(tmp_path) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []
        def run(self, command, timeout_seconds, cancelled):  # type: ignore[no-untyped-def]
            self.commands.append(command)
            if command[0] == "ffprobe":
                probe = {"format": {"format_name": "png_pipe", "duration": "0"}, "streams": [{"codec_type": "video", "codec_name": "png", "width": 1, "height": 1, "avg_frame_rate": "0/0"}]}
                return CommandResult(0, "", json.dumps(probe))
            if command[-1] == "-":
                return CommandResult(0, "")
            target = Path(command[-1])
            if "%06d" in target.name:
                target = target.with_name(target.name.replace("%06d", "000001"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fixture")
            return CommandResult(0, "")

    def chunk(kind: bytes, value: bytes) -> bytes:
        return len(value).to_bytes(4, "big") + kind + value + zlib.crc32(kind + value).to_bytes(4, "big")
    payload = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b"")
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "objects" / digest[:2] / digest
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    runner = RecordingRunner()
    MediaPreprocessor(tmp_path, runner=runner).process({"id": "thread-limits", "source": {"type": "file", "object_key": f"objects/{digest[:2]}/{digest}", "media_extension": ".png"}}, lambda: False)
    for command in runner.commands:
        assert command[command.index("-threads") + 1] == "2"
        if command[0] == "ffmpeg":
            assert command[command.index("-filter_threads") + 1] == "2"
            assert command[command.index("-filter_complex_threads") + 1] == "2"


@pytest.mark.parametrize("network_input", ["http://127.0.0.1/private", "https://example.org/x", "tcp://127.0.0.1:9", "udp://127.0.0.1:9", "ftp://example.org/a", "rtmp://example.org/a"])
def test_ffmpeg_runner_rejects_network_protocols(network_input: str) -> None:
    result = SubprocessCommandRunner().run(["ffprobe", "-protocol_whitelist", SAFE_PROTOCOL_WHITELIST, network_input], 1.0, lambda: False)
    assert result.returncode == 126
    assert "Network protocol" in result.stderr


def test_compose_served_ui_is_available_from_the_api_origin(tmp_path) -> None:
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        page = client.get("/")
        health = client.get("/health")
    assert page.status_code == 200
    assert "window.location.origin" in page.text
    assert health.json()["status"] == "ok"


def test_explicit_docker_gateway_policy_allows_only_configured_gateway(tmp_path) -> None:
    app = create_app(tmp_path, start_worker=False, test_mode=True, trusted_gateway_ips={"192.168.65.1"})
    with TestClient(app, base_url="http://localhost", client=("192.168.65.1", 45000)) as gateway:
        assert gateway.get("/health").status_code == 200
        assert gateway.get("/").status_code == 200
        upload = gateway.post("/api/v1/jobs/upload", files={"file": ("safe.png", b"\x89PNG\r\n\x1a\n" + b"x" * 32, "image/png")}, headers={"Origin": "http://localhost"})
    assert upload.status_code == 201
    with TestClient(app, base_url="http://localhost", client=("192.168.65.2", 45000)) as untrusted:
        assert untrusted.get("/health").status_code == 403


def test_host_and_origin_boundary_rejects_before_multipart_job_creation(tmp_path) -> None:
    with TestClient(create_app(tmp_path, start_worker=False, test_mode=True)) as client:
        evil_host = client.post("/api/v1/jobs", json={"url": "https://example.org"}, headers={"Host": "evil.example"})
        evil_origin = client.post("/api/v1/jobs/upload", files={"file": ("x.png", b"\x89PNG\r\n\x1a\n" + b"x" * 32, "image/png")}, headers={"Origin": "https://evil.example"})
        cli = client.post("/api/v1/jobs", json={"url": "https://example.org"})
        same_origin = client.post("/api/v1/jobs", json={"url": "https://example.net"}, headers={"Origin": "http://testserver"})
    assert evil_host.status_code == 400
    assert evil_origin.status_code == 403
    assert cli.status_code == 201
    assert same_origin.status_code == 201
    assert (tmp_path / "objects").exists()
    assert not list((tmp_path / "objects").rglob("*.part"))


def test_active_job_and_state_rate_limits(tmp_path) -> None:
    with TestClient(create_app(tmp_path, start_worker=False, test_mode=True)) as client:
        for index in range(8):
            assert client.post("/api/v1/jobs", json={"url": f"https://example{index}.org"}).status_code == 201
        assert client.post("/api/v1/jobs", json={"url": "https://limit.example"}).status_code == 429
    with TestClient(create_app(tmp_path / "rate", start_worker=False, test_mode=True)) as client:
        for _ in range(30):
            assert client.post("/api/v1/jobs/missing/cancel").status_code == 404
        assert client.post("/api/v1/jobs/missing/cancel").status_code == 429


def test_raw_chunked_upload_is_rejected_without_content_length(tmp_path) -> None:
    app = create_app(tmp_path, start_worker=False, test_mode=True)
    sent: list[dict] = []
    chunks = iter([{"type": "http.request", "body": b"x" * (MAX_UPLOAD_BODY_BYTES + 1), "more_body": False}])

    async def receive() -> dict:
        return next(chunks)

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/v1/jobs/upload", "raw_path": b"/api/v1/jobs/upload", "query_string": b"", "headers": [(b"host", b"testserver"), (b"content-type", b"multipart/form-data; boundary=x")], "client": ("testclient", 50000), "server": ("testserver", 80)}
    asyncio.run(app(scope, receive, send))
    assert next(item for item in sent if item["type"] == "http.response.start")["status"] == 413


def test_upload_admission_limits_concurrent_bodies_before_parsing() -> None:
    responses: list[list[dict]] = [[], []]

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
            await receive()
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        middleware = LocalRequestBoundaryMiddleware(inner, max_upload_body_bytes=1024, max_concurrent_uploads=1, test_mode=True)
        scope = {"type": "http", "method": "POST", "scheme": "http", "path": "/api/v1/jobs/upload", "headers": [(b"host", b"testserver")], "client": ("testclient", 1)}
        async def request(target: list[dict]) -> None:
            sent_once = False
            async def receive() -> dict:
                nonlocal sent_once
                if sent_once:
                    return {"type": "http.disconnect"}
                sent_once = True
                return {"type": "http.request", "body": b"x", "more_body": False}
            async def send(message: dict) -> None:
                target.append(message)
            await middleware(scope, receive, send)
        first = asyncio.create_task(request(responses[0]))
        await entered.wait()
        await request(responses[1])
        release.set()
        await first

    asyncio.run(scenario())
    assert next(item for item in responses[1] if item["type"] == "http.response.start")["status"] == 429
