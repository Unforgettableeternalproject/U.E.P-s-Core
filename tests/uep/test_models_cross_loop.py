"""模型層不得跨 event loop 重用 async client：``ModelPortRuntime.stream`` 每次都開新 loop 並在結束時關閉，
共用的 AsyncOpenAI／httpx 連線池若沿用上一個 loop 的 keep-alive 連線，下一輪關閉舊連線時會拋 ``Event loop is closed``。

用本機 loopback 的 keep-alive HTTP 伺服器（不打外網）搭配真實的 AsyncOpenAI 連線池重現。
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from uep.models import ChatMessage, ModelRequest
from uep.models.pydantic_ai_port import PydanticAIModelPort


def _chunk(delta: dict, finish: str | None = None) -> bytes:
    body = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body)}\n\n".encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive：連線會留在 client 的連線池

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = _chunk({"role": "assistant", "content": "你好"}) + _chunk({}, "stop") + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture
def base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    server.server_close()


def _run_on_fresh_loop(port: PydanticAIModelPort, attempt: str) -> list:
    """與 ``ModelPortRuntime.stream`` 相同：新 loop、跑完整段串流、關閉 loop。"""
    req = ModelRequest(attempt_id=attempt, messages=[ChatMessage("user", "hi")], capability_set={"tools": []})

    async def collect() -> list:
        return [e async for e in port.stream(req)]

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(collect())
    finally:
        loop.close()


def test_stream_on_two_sequential_loops_does_not_fail(base_url):
    port = PydanticAIModelPort("openai_chat", "m", api_key="k", base_url=base_url)
    for attempt in ("att_a", "att_b", "att_c"):
        events = _run_on_fresh_loop(port, attempt)
        assert events[-1].kind == "finish" and events[-1].finish_reason == "stop", (attempt, events)
        assert "".join(e.text for e in events if e.kind == "text_delta") == "你好"
