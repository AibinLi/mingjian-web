"""Internal-only, bounded Tavily retrieval worker.

The public API never reads ``TAVILY_API_KEY`` and never sends requests to
Tavily. It talks only to this local worker during the explicit refresh path.
"""
from __future__ import annotations

import asyncio
import os
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .security import safe_error_trace


app = FastAPI(title="MingJian Tavily retrieval worker")

TAVILY_URL = "https://api.tavily.com/search"
NETWORK_VERDICT = "联网核验不可用，请人工核查"

REQUEST_TIMEOUT = httpx.Timeout(
    connect=5.0,
    read=15.0,
    write=5.0,
    pool=5.0,
)

OPERATION_TIMEOUT_SECONDS = 10.0

TRANSIENT_STATUS_CODES = {
    429,
    500,
    502,
    503,
    504,
}


class SearchRequest(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=320,
    )


def _enabled(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _retryable(error: BaseException) -> bool:
    if isinstance(
        error,
        (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.ProxyError,
            socket.gaierror,
        ),
    ):
        return True

    return (
        isinstance(error, httpx.HTTPStatusError)
        and error.response.status_code in TRANSIENT_STATUS_CODES
    )


async def _resolve_provider() -> None:
    host = urlsplit(TAVILY_URL).hostname

    if not host:
        raise socket.gaierror(
            "Tavily provider hostname is missing"
        )

    loop = asyncio.get_running_loop()

    await asyncio.wait_for(
        loop.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
        ),
        timeout=5.0,
    )


async def _search_provider(
    query: str,
    key: str,
) -> list[dict[str, Any]]:

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    payload = {
        "query": query,
        "search_depth": "basic",
        "max_results": 5,
        "include_raw_content": False,
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
    ) as client:

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(
                multiplier=0.5,
                min=0.5,
                max=2.0,
            ),
            retry=retry_if_exception(_retryable),
            reraise=True,
        ):
            with attempt:
                await _resolve_provider()

                response = await client.post(
                    TAVILY_URL,
                    headers=headers,
                    json=payload,
                )

                response.raise_for_status()

                body = response.json()

                if not isinstance(body, dict):
                    raise TypeError(
                        "Tavily response must be a JSON object."
                    )

                values = body.get("results", [])

                if not isinstance(values, list):
                    raise TypeError(
                        "Tavily results must be a list."
                    )

                return values

    raise RuntimeError(
        "Tavily request did not run."
    )


def _network_error(
    error: BaseException,
) -> dict[str, Any]:

    return {
        "status": "network_error",
        "results": [],
        "verdict": NETWORK_VERDICT,
        "error_trace": safe_error_trace(error),
        "limitations": [
            "DNS、连接、代理或网络超时导致联网检索未完成；"
            "该维度不得参与加权。"
        ],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "mingjian-tavily-worker",
        "retrieval_enabled": _enabled(
            "ENABLE_RETRIEVAL"
        ),
        "tavily_configured": bool(
            os.environ.get("TAVILY_API_KEY")
        ),
    }


@app.post("/search")
async def search(
    request: SearchRequest,
    http_request: Request,
) -> dict[str, Any]:

    # ENABLE_RETRIEVAL 在 API 启动时确定。
    # 主 API 会通过内部请求头传递该状态，
    # 同时保留 Worker 自己的环境变量开关，
    # 方便本地独立 smoke test。
    enabled_by_api = (
        http_request.headers
        .get(
            "X-MingJian-Retrieval-Enabled",
            "",
        )
        .strip()
        .lower()
        == "true"
    )

    if not (
        _enabled("ENABLE_RETRIEVAL")
        or enabled_by_api
    ):
        return {
            "status": "not_configured",
            "results": [],
            "error_code": "retrieval_disabled",
            "message": (
                "ENABLE_RETRIEVAL is false and "
                "the API did not authorize retrieval."
            ),
        }

    key = os.environ.get("TAVILY_API_KEY")

    if not key:
        return {
            "status": "not_configured",
            "results": [],
            "error_code": "tavily_api_key_missing",
            "message": (
                "TAVILY_API_KEY is not configured "
                "in the Tavily worker."
            ),
        }

    try:
        async with asyncio.timeout(
            OPERATION_TIMEOUT_SECONDS
        ):
            results = await _search_provider(
                request.query,
                key,
            )

            return {
                "status": "completed",
                "results": results,
            }

    except (
        socket.gaierror,
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.ProxyError,
        TimeoutError,
    ) as error:

        return _network_error(error)

    except httpx.HTTPStatusError as error:
        return {
            "status": "unavailable",
            "results": [],
            "error_trace": safe_error_trace(error),
            "limitations": [
                "Tavily 返回 HTTP 错误；"
                "该维度不得参与加权。"
            ],
        }

    except (
        httpx.HTTPError,
        TypeError,
        ValueError,
    ) as error:

        return {
            "status": "unavailable",
            "results": [],
            "error_trace": safe_error_trace(error),
            "limitations": [
                "Tavily 在重试后仍未返回可用检索结果；"
                "该维度不得参与加权。"
            ],
        }


if __name__ == "__main__":
    uvicorn.run(
        "backend.app.tavily_worker:app",
        host="0.0.0.0",
        port=8005,
        log_level="info",
    )