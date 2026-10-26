"""全域停止的本機 HTTP API（D36：本機 API＋token）。

- 只綁 loopback（127.0.0.1／::1／localhost）；其他位址拒絕建立。
- 每個請求都需 ``Authorization: Bearer <token>``，token 由 ``GlobalStop`` 以常數時間比對；
  伺服端沒有設定 token 時回 503（fail-closed），不使用預設值。
- 路徑：``POST /v1/global-stop``（停止）、``POST /v1/global-stop/release``（解除）、``GET /v1/global-stop``（狀態）。
- 不記錄 token、不回顯 token；請求本文上限 4KB。
- 伺服器由呼叫端（組裝層）明確 ``start``，本模組 import 時不開任何 socket。
"""
from __future__ import annotations

import json
import logging
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from uep.stop.service import GlobalStop, StopAuthError

logger = logging.getLogger("uep.stop.api")

PATH_STOP = "/v1/global-stop"
PATH_RELEASE = "/v1/global-stop/release"
_MAX_BODY = 4096
_CONN_TIMEOUT = 5.0
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def _make_handler(service: GlobalStop) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "UepStop/1"
        timeout = _CONN_TIMEOUT  # 每條連線的 socket timeout：慢速／停滯的連線不會佔住執行緒

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _reply(self, status: HTTPStatus, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > _MAX_BODY:
                raise ValueError("請求本文過大")
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(parsed, dict):
                raise TypeError("請求本文必須是 JSON 物件")
            return parsed

        def _auth(self) -> bool:
            try:
                service.authenticate(_bearer(self.headers.get("Authorization")))
                return True
            except StopAuthError as exc:
                status = HTTPStatus.SERVICE_UNAVAILABLE if "未設定" in str(exc) else HTTPStatus.UNAUTHORIZED
                self._reply(status, {"error": "unauthorized" if status == HTTPStatus.UNAUTHORIZED else "not_configured"})
                return False

        def do_GET(self) -> None:
            if self.path != PATH_STOP:
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            elif self._auth():
                self._reply(HTTPStatus.OK, {"engaged": service.is_engaged()})

        def do_POST(self) -> None:
            if self.path not in (PATH_STOP, PATH_RELEASE):
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not self._auth():
                return
            try:
                body = self._body()
            except (ValueError, TypeError):
                self._reply(HTTPStatus.BAD_REQUEST, {"error": "bad_request"})
                return
            operator = str(body.get("operator") or "local_api")
            try:
                if self.path == PATH_STOP:
                    result = service.engage(reason=str(body.get("reason") or "global_stop"), operator=operator)
                    # 旗標沒寫成功 -> 500（停止未生效）；旗標成功但其他步驟失敗 -> 207（部分失敗）
                    if not result.flag_engaged:
                        status = HTTPStatus.INTERNAL_SERVER_ERROR
                    elif result.errors:
                        status = HTTPStatus.MULTI_STATUS
                    else:
                        status = HTTPStatus.OK
                    self._reply(status, {
                        "control_id": result.control_id, "flag_engaged": result.flag_engaged,
                        "presence_revoked": result.presence_revoked, "delivered_to": list(result.delivered_to),
                        "errors": list(result.errors),
                    })
                else:
                    self._reply(HTTPStatus.OK, {"released": service.disengage(operator=operator)})
            except Exception:
                logger.exception("全域停止 API 處理失敗")
                self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal"})

    return Handler


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """不允許其他行程搶綁同一埠：Windows 用 SO_EXCLUSIVEADDRUSE，其餘平台關閉 reuse_address。"""

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class LocalStopServer:
    """背景執行緒中的本機 HTTP 伺服器；``port=0`` 由系統配置（``port`` 屬性取得實際埠）。"""

    def __init__(self, service: GlobalStop, *, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in _LOOPBACK:
            raise ValueError(f"全域停止 API 只能綁 loopback，收到 {host!r}")
        self._httpd = _ExclusiveHTTPServer((host, port), _make_handler(service))
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        host = self._httpd.server_address[0]
        return f"http://{host}:{self.port}"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="UepStopApi")
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(timeout=5.0)
            self._thread = None
        self._httpd.server_close()
