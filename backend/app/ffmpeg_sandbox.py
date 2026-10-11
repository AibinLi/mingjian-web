"""Exec-only helper which applies POSIX resource limits outside worker threads."""

from __future__ import annotations

import os
import sys


MAX_DERIVED_BYTES = 30 * 1024 * 1024
# H.264 decoding/encoding reserves substantial virtual address space even for
# short 720p inputs.  768 MiB rejects otherwise valid MP4 files during x264
# buffer allocation.  Keep a strict per-tool ceiling below the Compose 2 GiB
# container limit while allowing the verified 720x1280 H.264/AAC case.
MAX_FFMPEG_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024


def apply_posix_limits(resource_module) -> None:  # type: ignore[no-untyped-def]
    """Apply only per-process limits; Compose owns the PID/thread ceiling."""
    resource_module.setrlimit(resource_module.RLIMIT_CPU, (35, 35))
    resource_module.setrlimit(
        resource_module.RLIMIT_AS,
        (MAX_FFMPEG_ADDRESS_SPACE_BYTES, MAX_FFMPEG_ADDRESS_SPACE_BYTES),
    )
    resource_module.setrlimit(resource_module.RLIMIT_FSIZE, (MAX_DERIVED_BYTES, MAX_DERIVED_BYTES))
    resource_module.setrlimit(resource_module.RLIMIT_NOFILE, (64, 64))


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--" or argv[2] not in {"ffmpeg", "ffprobe"}:
        return 126
    if os.name != "nt":
        import resource

        apply_posix_limits(resource)
    os.execvpe(argv[2], argv[2:], os.environ.copy())
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
