"""跨行程控制傳輸層（localhost TCP，換行分隔 JSON），獨立於輸出資料通道（P-8）。

- 一條連線同時承載：Core -> 端點的控制訊號推送（stop／abort／revoke），以及端點 -> Core 的簽章請求
  （acquire／renew／snapshot…，見 ``uep.endpoint``）。這條連線與輸出資料通道（OutputAdapter.send）無關，
  資料通道塞住不影響控制訊號。
- 連線建立須先通過身分驗證（hello 請求以 per-endpoint secret 簽章，R2）；之後每個請求各自驗證，
  且請求的 endpoint_id 必須等於連線已驗證的身分。
- 伺服器與客戶端都只允許 loopback；Windows 上伺服器 socket 設 ``SO_EXCLUSIVEADDRUSE``，避免別的行程
  搶綁同一個埠。伺服器身分：hello 帶 client 的 nonce，Core 以端點 secret 對該 nonce 簽章放進 ``hello_ok``，
  client 驗證通過才信任這條連線（冒名綁埠的行程沒有 secret，無法讓端點相信持有 Lease）；之後的回應與推送
  不逐則簽章，信任邊界是本機 loopback 與已驗證的連線。若日後改為跨機器傳輸，須加 TLS／回應簽章。
  hello_ok 送出成功後才把連線登記為推送目標。
- 安全邊界：hello 的雙向證明只做到「抗冒名」，不抗 relay（中間人可把雙方的 hello 與證明原樣轉發）。
  跨機器（Part H）前須加每則 MAC 或通道綁定（TLS 憑證綁定／channel binding），否則不得離開 loopback。
- 同一 endpoint_id 重新 hello（重連）會取代舊連線。
- ``SocketControlServer.send`` 與 ``ControlChannel.send`` 同形，可直接交給 PresenceManager 的 ``control``。
"""
from __future__ import annotations

import hmac
import itertools
import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from uep.control.channel import ControlSendResult
from uep.endpoint.auth import (
    EndpointAuthError,
    SignedRequest,
    server_proof,
    sign_request,
)
from uep.endpoint.protocol import RpcCoreApi
from uep.endpoint.service import EndpointService
from uep.output.intent import ControlSignal

logger = logging.getLogger("uep.control.socket")

_IO_TIMEOUT = 0.5
_MAX_LINE = 1 << 20
_HELLO_TIMEOUT = 5.0
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class _Wire:
    """換行分隔 JSON 的收發；寫入以鎖序列化，任何 I/O 錯誤即關閉連線。"""

    def __init__(self, sock: socket.socket) -> None:
        sock.settimeout(_IO_TIMEOUT)
        self._sock = sock
        self._buf = bytearray()
        self._wlock = threading.Lock()
        self.closed = False
        # 此連線上等待回應的請求（以 wire 為單位：連線中斷只喚醒自己的請求，不誤傷重連後的新請求）
        self.waiting: dict[int, tuple[threading.Event, list[dict[str, Any]]]] = {}
        self.waiting_lock = threading.Lock()

    def read(self, total_timeout: float | None = None) -> dict[str, Any] | None:
        """回傳下一則訊息；連線結束、逾時（有給 total_timeout）或格式錯誤回 None。"""
        deadline = None if total_timeout is None else time.monotonic() + total_timeout
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line = bytes(self._buf[:newline])
                del self._buf[: newline + 1]
                try:
                    data = json.loads(line)
                except ValueError:
                    return None
                return data if isinstance(data, dict) else None
            if self.closed or len(self._buf) > _MAX_LINE:
                return None
            if deadline is not None and time.monotonic() >= deadline:
                return None
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            self._buf += chunk

    def write(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._wlock:
            if self.closed:
                raise ConnectionError("連線已關閉")
            try:
                self._sock.sendall(data)
            except OSError:
                self.close()
                raise

    def close(self) -> None:
        self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass


class SocketControlServer:
    """Core 端：接受端點連線、推送控制訊號、把簽章請求轉給 ``EndpointService``。"""

    def __init__(self, service: EndpointService, *, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in _LOOPBACK:
            raise ValueError("控制傳輸層只允許綁定 loopback")
        self._service = service
        self._host, self._port = host, port
        self._listener: socket.socket | None = None
        self._lock = threading.Lock()
        self._conns: dict[str, _Wire] = {}
        self._closed = threading.Event()

    @property
    def address(self) -> tuple[str, int]:
        if self._listener is None:
            raise RuntimeError("伺服器尚未啟動")
        host, port = self._listener.getsockname()[:2]
        return host, port

    def start(self) -> tuple[str, int]:
        listener = socket.socket(socket.AF_INET6 if ":" in self._host else socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # 僅 Windows
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((self._host, self._port))
        listener.listen()
        listener.settimeout(_IO_TIMEOUT)
        self._listener = listener
        threading.Thread(target=self._accept_loop, daemon=True, name="UepControlAccept").start()
        return self.address

    def close(self) -> None:
        self._closed.set()
        if self._listener is not None:
            self._listener.close()
        with self._lock:
            wires, self._conns = list(self._conns.values()), {}
        for wire in wires:
            wire.close()

    def connected(self, endpoint_id: str) -> bool:
        with self._lock:
            wire = self._conns.get(endpoint_id)
        return wire is not None and not wire.closed

    def send(self, signal: ControlSignal, *, endpoint_id: str | None = None) -> ControlSendResult:
        """與 ``ControlChannel.send`` 同形：推送給指定（或所有）已驗證連線的端點；不執行端點 handler。"""
        with self._lock:
            targets = list(self._conns.items()) if endpoint_id is None else (
                [(endpoint_id, self._conns[endpoint_id])] if endpoint_id in self._conns else []
            )
        message = {"type": "control", "signal": signal.model_dump(mode="json")}
        delivered: list[str] = []
        for target_id, wire in targets:
            try:
                wire.write(message)
                delivered.append(target_id)
            except OSError:
                self._drop(target_id, wire)
        return ControlSendResult(signal.control_id, tuple(delivered), duplicate=False)

    # ---- 內部 ----

    def _drop(self, endpoint_id: str, wire: _Wire) -> None:
        with self._lock:
            if self._conns.get(endpoint_id) is wire:
                del self._conns[endpoint_id]
        wire.close()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._closed.is_set():
            try:
                sock, _addr = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True, name="UepControlConn").start()

    def _serve(self, sock: socket.socket) -> None:
        wire = _Wire(sock)
        endpoint_id = self._handshake(wire)
        if endpoint_id is None:
            wire.close()
            return
        try:
            while True:
                message = wire.read()
                if message is None:
                    return
                wire.write(self._respond(endpoint_id, message))
        except OSError:
            pass
        finally:
            self._drop(endpoint_id, wire)

    def _handshake(self, wire: _Wire) -> str | None:
        message = wire.read(_HELLO_TIMEOUT)
        try:
            if message is None or message.get("type") != "hello":
                raise EndpointAuthError("缺少 hello")
            request = SignedRequest.from_wire(message["req"])
            if request.op != "hello":
                raise EndpointAuthError("hello 請求不合法")
            endpoint_id = self._service.authenticate(request)
        except (EndpointAuthError, KeyError, TypeError):
            try:
                wire.write({"type": "error", "error": "AUTH_FAILED"})
            except OSError:
                pass
            return None
        try:
            wire.write({"type": "hello_ok", "proof": self._service.prove(endpoint_id, request.nonce)})
        except (OSError, EndpointAuthError):
            wire.close()
            return None
        with self._lock:  # hello_ok 送達後才登記，之前不會有推送搶在 hello_ok 前寫入
            old = self._conns.get(endpoint_id)
            self._conns[endpoint_id] = wire  # 重連取代舊連線
        if old is not None:
            old.close()
        return endpoint_id

    def _respond(self, endpoint_id: str, message: dict[str, Any]) -> dict[str, Any]:
        reply: dict[str, Any] = {"type": "resp", "id": message.get("id")}
        try:
            request = SignedRequest.from_wire(message["req"])
        except (EndpointAuthError, KeyError, TypeError):
            return {**reply, "resp": {"ok": False, "error": "BAD_ARGS"}}
        if request.endpoint_id != endpoint_id:
            return {**reply, "resp": {"ok": False, "error": "AUTH_FAILED"}}
        return {**reply, "resp": self._service.handle(request)}


class SocketControlClient:
    """端點端：連線、推送控制訊號給 ``on_control``（只應設旗標）、提供簽章後的 Core API。"""

    def __init__(
        self, host: str, port: int, endpoint_id: str, secret: bytes, *,
        on_control: Callable[[ControlSignal], None], clock: Callable[[], float] = time.time,
        request_timeout: float = 2.0,
    ) -> None:
        if host not in _LOOPBACK:
            raise ValueError("控制傳輸層只允許連線到 loopback")
        # request_timeout 須遠小於 Lease TTL（預設 30 秒），RPC 卡住才不會吃掉續約窗口
        self._addr = (host, port)
        self._endpoint_id = endpoint_id
        self._secret = secret
        self._on_control = on_control
        self._clock = clock
        self._timeout = request_timeout
        self._wire: _Wire | None = None
        self._connect_lock = threading.Lock()  # 背景重連與手動 reconnect 並行 connect 時序列化
        self._ids = itertools.count(1)
        self.api = RpcCoreApi(endpoint_id, secret, self._transport, clock=clock)

    @property
    def connected(self) -> bool:
        wire = self._wire
        return wire is not None and not wire.closed

    def connect(self) -> None:
        """建立連線並通過身分驗證；可重複呼叫（重連）。失敗拋 ConnectionError／EndpointAuthError。

        並行呼叫會序列化：否則各執行緒互相關閉對方的連線、伺服器端 re-hello 又會關掉較早的連線，
        造成偶發的 hello 失敗（T-19 審查轉入 (5)）。
        """
        with self._connect_lock:
            self._connect()

    def _connect(self) -> None:
        self.close()
        try:
            sock = socket.create_connection(self._addr, timeout=self._timeout)
        except OSError as exc:
            raise ConnectionError("無法連線到 Core 控制端點") from exc
        wire = _Wire(sock)
        try:
            hello = sign_request(self._secret, self._endpoint_id, "hello", {}, clock=self._clock)
            wire.write({"type": "hello", "req": hello.to_wire()})
            reply = wire.read(_HELLO_TIMEOUT)
        except OSError as exc:
            wire.close()
            raise ConnectionError("hello 失敗") from exc
        if reply is None or reply.get("type") != "hello_ok":
            wire.close()
            if reply is not None and reply.get("error") == "AUTH_FAILED":
                raise EndpointAuthError("Core 拒絕端點身分")
            raise ConnectionError("hello 未獲回應")
        proof = reply.get("proof")
        expected = server_proof(self._secret, self._endpoint_id, hello.nonce)
        if not isinstance(proof, str) or not hmac.compare_digest(proof.encode("utf-8"), expected.encode("utf-8")):
            wire.close()  # 伺服器無法證明持有 secret：不信任 hello_ok 與之後的任何回應
            raise EndpointAuthError("Core 身分驗證失敗（伺服器證明不符）")
        self._wire = wire
        threading.Thread(
            target=self._read_loop, args=(wire,), daemon=True, name=f"UepControlClient[{self._endpoint_id}]"
        ).start()

    def close(self) -> None:
        wire, self._wire = self._wire, None
        if wire is not None:
            wire.close()

    def _read_loop(self, wire: _Wire) -> None:
        while True:
            message = wire.read()
            if message is None:
                break
            kind = message.get("type")
            if kind == "control":
                try:
                    self._on_control(ControlSignal.model_validate(message["signal"]))
                except Exception:
                    logger.exception("控制訊號處理失敗")
            elif kind == "resp":
                with wire.waiting_lock:
                    waiter = wire.waiting.pop(message.get("id"), None)  # type: ignore[arg-type]
                if waiter is not None:
                    waiter[1].append(message.get("resp") or {})
                    waiter[0].set()
        wire.close()
        with wire.waiting_lock:  # 連線中斷：只喚醒這條連線上等待中的請求
            waiters, wire.waiting = list(wire.waiting.values()), {}
        for event, _slot in waiters:
            event.set()

    def _transport(self, request: dict[str, Any]) -> dict[str, Any]:
        wire = self._wire
        if wire is None or wire.closed:
            raise ConnectionError("尚未連線")
        request_id = next(self._ids)
        event, slot = threading.Event(), []
        with wire.waiting_lock:
            wire.waiting[request_id] = (event, slot)
        try:
            if wire.closed:
                raise ConnectionError("連線已中斷")
            wire.write({"type": "req", "id": request_id, "req": request})
            if not event.wait(self._timeout):
                raise TimeoutError("Core 請求逾時")
        finally:
            with wire.waiting_lock:
                wire.waiting.pop(request_id, None)
        if not slot:
            raise ConnectionError("連線在等待回應時中斷")
        return slot[0]
