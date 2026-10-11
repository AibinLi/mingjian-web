"""ASGI-level boundary for the localhost-only single-user service."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse


STATE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _response(send: Callable[..., Awaitable[None]], status: int, detail: str) -> Awaitable[None]:
    body = json.dumps({"detail": detail}).encode("utf-8")
    async def emit() -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})
    return emit()


class LocalRequestBoundaryMiddleware:
    """Reject cross-origin writes and constrain raw uploads before parsing.

    CORS is not an authorization boundary.  This middleware makes a deliberate
    localhost trust decision before FastAPI/Starlette consumes a multipart body.
    Requests without Origin/Referer remain available to local CLI clients.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        max_upload_body_bytes: int,
        max_concurrent_uploads: int = 2,
        max_state_requests_per_minute: int = 30,
        test_mode: bool = False,
        trusted_gateway_ips: set[str] | None = None,
    ) -> None:
        self.app = app
        self.max_upload_body_bytes = max_upload_body_bytes
        self.max_concurrent_uploads = max_concurrent_uploads
        self.max_state_requests_per_minute = max_state_requests_per_minute
        self.test_mode = test_mode
        self.trusted_gateway_ips = trusted_gateway_ips or set()
        self._lock = asyncio.Lock()
        self._active_uploads = 0
        self._state_requests: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope: dict[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]], send: Callable[..., Awaitable[None]]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        method = str(scope.get("method", "GET")).upper()
        client = (scope.get("client") or ("", 0))[0]
        if not self._local_client(client):
            await _response(send, 403, "This local service accepts loopback clients only.")
            return
        if method in STATE_METHODS:
            if not self._same_origin_or_local_cli(scope, headers):
                await _response(send, 403, "Cross-origin state-changing requests are not allowed.")
                return
            if not await self._allow_rate(client):
                await _response(send, 429, "Too many state-changing requests; retry later.")
                return

        is_upload = scope.get("path") == "/api/v1/jobs/upload" and method == "POST"
        if not is_upload:
            await self.app(scope, receive, send)
            return
        async with self._lock:
            if self._active_uploads >= self.max_concurrent_uploads:
                await _response(send, 429, "Too many concurrent uploads; retry later.")
                return
            self._active_uploads += 1
        received = 0
        buffered: list[dict[str, Any]] = []
        try:
            # Read the bounded ASGI body before Starlette's multipart parser.
            # This protects chunked/no-Content-Length requests as well as the
            # common Content-Length fast path and never feeds an oversize body
            # into the parser or temporary-file spooler.
            while True:
                message = await receive()
                buffered.append(message)
                if message.get("type") == "http.request":
                    received += len(message.get("body", b""))
                    if received > self.max_upload_body_bytes:
                        await _response(send, 413, "Upload exceeds the allowed body size.")
                        return
                    if not message.get("more_body", False):
                        break
                elif message.get("type") == "http.disconnect":
                    break
            iterator = iter(buffered)

            async def limited_receive() -> dict[str, Any]:
                try:
                    return next(iterator)
                except StopIteration:
                    return {"type": "http.disconnect"}

            await self.app(scope, limited_receive, send)
        finally:
            async with self._lock:
                self._active_uploads -= 1

    def _local_client(self, value: object) -> bool:
        if self.test_mode and value == "testclient":
            return True
        if str(value) in self.trusted_gateway_ips:
            return True
        try:
            return ipaddress.ip_address(str(value)).is_loopback
        except ValueError:
            return False

    async def _allow_rate(self, client: object) -> bool:
        now = time.monotonic()
        key = str(client)
        async with self._lock:
            values = self._state_requests[key]
            while values and values[0] <= now - 60.0:
                values.popleft()
            if len(values) >= self.max_state_requests_per_minute:
                return False
            values.append(now)
            return True

    @staticmethod
    def _same_origin_or_local_cli(scope: dict[str, Any], headers: dict[str, str]) -> bool:
        origin = headers.get("origin")
        referer = headers.get("referer")
        if not origin and not referer:
            return True
        expected = f"{scope.get('scheme', 'http')}://{headers.get('host', '')}".lower()
        if origin:
            parsed = urlparse(origin)
            return bool(parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password and not parsed.path.rstrip("/") and not parsed.query and not parsed.fragment and origin.rstrip("/").lower() == expected)
        parsed = urlparse(referer or "")
        return bool(parsed.scheme in {"http", "https"} and parsed.netloc and f"{parsed.scheme}://{parsed.netloc}".lower() == expected)
