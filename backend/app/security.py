"""Shared Stage-9 input, network, logging, and retention safety boundaries.

This module intentionally has no dependency on the media or claim pipeline.
Untrusted media text, URLs, redirect locations, and error values remain data;
they never select tools or alter system instructions.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse, urlunparse


MAX_URL_LENGTH = 2048

_SENSITIVE = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+|"
    r"(api[_-]?key\s*[:=]\s*)[^\s,;]+|"
    r"(token\s*[:=]\s*)[^\s,;]+|"
    r"(password\s*[:=]\s*)[^\s,;]+|"
    r"(https?://[^\s/@:]+:)[^\s@/]+@"
)
_WINDOWS_PATH = re.compile(r"(?i)\b[a-z]:\\(?:[^\\\s]+\\)*[^\\\s]*")
_POSIX_PATH = re.compile(r"(?<![:\w])/(?:[^/\s]+/)*[^/\s]*")


def redact_sensitive(value: object) -> str:
    """Return an audit-safe diagnostic string without credentials or tokens."""
    text = str(value).replace("\x00", "")
    def replace(match: re.Match[str]) -> str:
        prefix = next(group for group in match.groups() if group is not None)
        # Preserve a syntactically recognisable URL while removing only its
        # password, so diagnostics remain useful without leaking credentials.
        return f"{prefix}[REDACTED]@" if prefix.startswith(("http://", "https://")) else f"{prefix}[REDACTED]"
    return _SENSITIVE.sub(replace, text)[:512]


def safe_error_trace(error: BaseException) -> str:
    """Return a bounded diagnostic without credentials, full paths or a stack."""
    detail = redact_sensitive(error)
    detail = _WINDOWS_PATH.sub("[PATH]", detail)
    detail = _POSIX_PATH.sub("[PATH]", detail)
    return f"{type(error).__name__}: {detail}"[:240]


class RedactingFilter(logging.Filter):
    """Protect application/access logs even if a dependency logs raw input."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive(record.getMessage())
        record.args = ()
        return True


def configure_redacting_logging() -> None:
    """Install one idempotent filter on existing root and uvicorn handlers."""
    for name in (None, "uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if not any(isinstance(item, RedactingFilter) for item in handler.filters):
                handler.addFilter(RedactingFilter())


def _blocked_ip(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_private does not include every non-public use range on all Python
    # versions, so explicitly include shared and benchmarking ranges too.
    shared = ipaddress.ip_network("100.64.0.0/10")
    benchmark = ipaddress.ip_network("198.18.0.0/15")
    return bool(
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_multicast
        or value.is_unspecified
        or value.is_reserved
        or value in shared
        or value in benchmark
    )


def validate_public_http_url(value: str) -> str:
    """Normalize and validate an offline HTTP(S) URL reference.

    URL download is intentionally not implemented: a resolver followed by a
    stock HTTP client cannot bind the inspected DNS answer to the connected
    peer and therefore cannot safely claim DNS-rebinding protection.  This
    validator only stores a normalized, non-local reference for the existing
    offline URL-job workflow.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH or "\x00" in value:
        raise ValueError("URL is empty or exceeds the allowed length.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Only credential-free absolute http(s) URLs are allowed.")
    try:
        raw_host = (parsed.hostname or "").rstrip(".")
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL host or port is invalid.") from error
    if not raw_host:
        raise ValueError("URL host is invalid.")
    try:
        literal = ipaddress.ip_address(raw_host)
        host = literal.compressed.lower()
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise ValueError("URL hostname cannot be normalized with IDNA.") from error
        # Some system resolvers accept legacy integer/abbreviated IPv4 syntax
        # (for example 2130706433 for 127.0.0.1). Treat such syntax as an IP
        # literal here so the offline submission boundary cannot be bypassed.
        try:
            literal = ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
        except OSError:
            literal = None
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        raise ValueError("Local hostnames are not allowed.")
    if host in {"metadata.google.internal", "metadata", "instance-data"}:
        raise ValueError("Cloud metadata hostnames are not allowed.")
    if port is not None and port not in {80, 443}:
        raise ValueError("Only HTTP(S) default ports are allowed.")
    if literal is not None:
        if _blocked_ip(literal):
            raise ValueError("Non-public IP addresses are not allowed.")
    display_host = f"[{host}]" if isinstance(literal, ipaddress.IPv6Address) else host
    netloc = display_host + (f":{port}" if port is not None else "")
    return urlunparse((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.params, parsed.query, ""))
