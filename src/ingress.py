"""ASGI ingress controls that operate before request-body parsing."""

from __future__ import annotations

import json
from typing import Any


class StreamedBodyLimitMiddleware:
    """Count streamed bytes before parsing and buffer at most the allowed body."""

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        consumed = 0
        buffered: list[dict[str, Any]] = []
        while True:
            message = await receive()
            buffered.append(message)
            if message.get("type") != "http.request":
                break
            consumed += len(message.get("body", b""))
            if consumed > self._max_bytes:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break

        cursor = 0

        async def replay_receive() -> dict[str, Any]:
            nonlocal cursor
            if cursor < len(buffered):
                message = buffered[cursor]
                cursor += 1
                return message
            return await receive()

        await self._app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = json.dumps(
            {
                "detail": {
                    "code": "request_too_large",
                    "message": "request body exceeds the configured limit",
                }
            },
            separators=(",", ":"),
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
