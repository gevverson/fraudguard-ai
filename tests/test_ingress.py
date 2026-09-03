import asyncio
import json

from src.ingress import StreamedBodyLimitMiddleware


def run_asgi(body_chunks, *, limit):
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(body_chunks) - 1,
        }
        for index, chunk in enumerate(body_chunks)
    ]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    async def downstream(_scope, receive_call, send_call):
        while True:
            message = await receive_call()
            if not message.get("more_body"):
                break
        await send_call({"type": "http.response.start", "status": 204, "headers": []})
        await send_call({"type": "http.response.body", "body": b""})

    middleware = StreamedBodyLimitMiddleware(downstream, max_bytes=limit)
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/v1/decisions"},
            receive,
            send,
        )
    )
    return sent


def test_streamed_limit_rejects_chunked_body_without_content_length():
    sent = run_asgi([b"abc", b"def"], limit=5)
    assert sent[0]["status"] == 413
    body = json.loads(sent[1]["body"])
    assert body["detail"]["code"] == "request_too_large"


def test_streamed_limit_accepts_body_exactly_at_limit():
    sent = run_asgi([b"abc", b"de"], limit=5)
    assert sent[0]["status"] == 204
