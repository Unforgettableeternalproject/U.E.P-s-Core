"""Chatroom 工具的 ``ToolExecutor``（``chatroom.read``／``chatroom.wait``／``chatroom.post``；宣告見 ``uep.tools.catalog.chatroom``）。

對接 Chatroom Hub 的 HTTP API（與其 MCP bridge 同一套）：

- 認證：``Authorization: Bearer <token>``。token 只在這個模組內存在（``ChatroomClient``），不進 LLM 面、不進結果、不寫 log
  （log 只記例外類別與房間 id）。
- 身分：``UEP_CHATROOM_SESSION_KEY`` 是她自己的 session key（固定人格身分，join 冪等）；``UEP_CHATROOM_DISPLAY_NAME`` 當偏好名稱。
  加入房間由 executor 依白名單**自動**處理（首次使用某房間時 ``POST /api/rooms/{id}/join``，kind=other、role=agent），不另提供
  ``chatroom.join``：不在白名單的房間一律拒絕，不會有模型指定房間去加入的路徑。身分失效（閒置移出等）自動重新 join 一次；
  被踢（``participant_kicked``）不自動加回。
- 游標：每個房間一個，保存在 executor 記憶體（模型不管 seq）。新行程第一次 join 時從房內目前最新的 seq 起算（不重讀歷史、不對舊訊息起反應）。
  ``wait`` 被取消時不推進游標（取到的訊息丟棄，下次還會收到）。游標不持久化：行程重啟後同樣從當下最新的 seq 起算。
- 房內訊息是外部內容：結果一律帶 ``untrusted=true`` 與固定提示，內容經清理（去控制字元）、單則與總量截斷，再放進回合結果暫存，
  由 Gateway 的 ``outcome_view`` 過濾後交給模型。
- ``wait`` 以短切片 long-poll（每片不超過 ``slice_s``），片與片之間檢查取消旗標與期限；``cancel`` 先於 ``execute`` 到達也會記下。
  取消的 ``execute`` 簽回失敗 receipt（``error_code="cancelled"``），不拋例外。
- ``post`` 的連線失敗（確定未送出）回失敗；送出後回覆遺失（讀取逾時等）拋 ``ToolResponseLost``，由 TaskManager 標 UNKNOWN，不重送。
- ``lookup_authoritative=False``：結果暫存只在記憶體。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from uep.adapters.tool import (
    CancelOutcome,
    ReceiptLookup,
    ToolCall,
    ToolReceipt,
    ToolResponseLost,
)
from uep.ids import new_id
from uep.tools.catalog.chatroom import (
    MESSAGE_MAX_CHARS,
    POST_MAX_CHARS,
    POST_MAX_MENTIONS,
    READ_LIMIT_DEFAULT,
    READ_LIMIT_MAX,
    RESULT_MAX_CHARS,
    TOOL_POST,
    TOOL_READ,
    TOOL_WAIT,
    UNTRUSTED_NOTICE,
    WAIT_DEFAULT_S,
    WAIT_MAX_S,
)
from uep.tools.outcomes import TurnOutcomeCache

logger = logging.getLogger("uep.tools.executors.chatroom")

CANCELLED = "cancelled"
UNAVAILABLE = "unavailable"
UNREACHABLE = "unreachable"
DEFAULT_SLICE_S = 3.0  # wait 每片 long-poll 秒數，也是取消生效的最大延遲
HTTP_TIMEOUT_S = 10.0
_MAX_PENDING_CANCELS = 256
_SEQ_CEILING = 2**53  # 「比任何訊息都新」的 before_seq，用來取房內最新一則
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 身分失效但重新 join 是正確處置的 Hub 代碼（participant_kicked 刻意不在內：移出是人為決定）
_REJOIN_CODES = frozenset({
    "participant_header_required", "participant_wrong_room", "participant_removed_idle",
    "participant_not_active", "not_a_member", "participant_left",
})


@dataclass(frozen=True)
class ChatroomConfig:
    """連線與身分設定。``token`` 只在這裡；``repr`` 隱藏它。"""

    url: str
    session_key: str
    token: str = field(repr=False)
    rooms: frozenset[str]
    display_name: str | None = None


def _read_token_file(path: Path) -> str | None:
    """純 token 檔，或含 ``CHATROOM_TOKEN=...`` 行的 .env。讀不到回 None（只記例外類別，不記內容）。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        logger.warning("無法讀取 UEP_CHATROOM_TOKEN_FILE：%s", type(exc).__name__)
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "CHATROOM_TOKEN":
            return value.strip().strip("\"'") or None
    plain = text.strip()
    return plain if plain and "\n" not in plain and "=" not in plain else None


def chatroom_config_from_settings(settings: Any) -> ChatroomConfig | None:
    """設定不齊（缺 URL／身分 key／token／房間白名單）回 None：工具 unavailable，組裝不失敗。"""
    url = (getattr(settings, "chatroom_url", None) or "").strip().rstrip("/")
    key = (getattr(settings, "chatroom_session_key", None) or "").strip()
    rooms = frozenset(r.strip() for r in (getattr(settings, "chatroom_rooms", None) or "").split(",") if r.strip())
    secret = getattr(settings, "chatroom_token", None)
    token = secret.get_secret_value().strip() if secret is not None else ""
    token_file = getattr(settings, "chatroom_token_file", None)
    if not token and token_file is not None:
        token = _read_token_file(Path(token_file)) or ""
    if not (url and key and token and rooms):
        return None
    name = (getattr(settings, "chatroom_display_name", None) or "").strip() or None
    return ChatroomConfig(url=url, session_key=key, token=token, rooms=rooms, display_name=name)


class ChatroomError(Exception):
    """Hub 請求失敗。``code`` 是短碼（unreachable／ambiguous／hub 的 detail.code／http_<status>）；訊息不含 token 或內容。"""

    def __init__(self, code: str, *, status: int = 0, sent: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.sent = sent  # 請求可能已抵達 Hub（回覆遺失）：post 不得假設未送出


class ChatroomClient:
    """對 Hub 的薄 HTTP 客戶端（``transport`` 供測試注入 ``httpx.MockTransport``）。"""

    def __init__(self, config: ChatroomConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self._config = config
        self._transport = transport

    def request(
        self, method: str, path: str, *, participant_id: str | None = None, session_key: str | None = None,
        params: Mapping[str, Any] | None = None, body: Mapping[str, Any] | None = None, timeout: float = HTTP_TIMEOUT_S,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._config.token}"}
        if participant_id:
            headers["X-Participant-Id"] = participant_id
        if session_key:
            headers["X-Session-Key"] = session_key
        try:
            with httpx.Client(
                base_url=self._config.url, headers=headers, timeout=timeout, transport=self._transport,
            ) as client:
                response = client.request(method, path, params=dict(params) if params else None, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise ChatroomError(UNREACHABLE) from exc  # 連線未建立：請求沒有送出
        except httpx.HTTPError as exc:
            raise ChatroomError("ambiguous", sent=True) from exc  # 可能已送出（讀取逾時、連線中斷）
        if response.status_code >= 400:
            code = ""
            try:
                detail = response.json().get("detail")
                code = str(detail.get("code", "")) if isinstance(detail, dict) else ""
            except (ValueError, AttributeError):
                pass
            raise ChatroomError(code or f"http_{response.status_code}", status=response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise ChatroomError("bad_response") from exc
        if not isinstance(data, dict):
            raise ChatroomError("bad_response")
        return data


@dataclass
class _Room:
    participant_id: str
    cursor: int
    own_name: str


def _clean(text: Any, limit: int) -> tuple[str, bool]:
    value = _CONTROL_CHARS.sub("", str(text if text is not None else ""))
    return (value[:limit], True) if len(value) > limit else (value, False)


class ChatroomToolExecutor:
    """滿足 ``uep.adapters.tool.CancellableToolExecutor``。未設定（``config=None``）時所有呼叫回 ``unavailable``。"""

    def __init__(
        self, config: ChatroomConfig | None, *, cache: TurnOutcomeCache | None = None,
        transport: httpx.BaseTransport | None = None, slice_s: float = DEFAULT_SLICE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._client = ChatroomClient(config, transport=transport) if config is not None else None
        self.cache = cache if cache is not None else TurnOutcomeCache()
        self._slice_s = slice_s
        self._clock = clock
        self._lock = threading.Lock()
        self._rooms: dict[str, _Room] = {}
        self._cursors: dict[str, int] = {}  # 身分失效、重新 join 之間暫存的游標
        self._waiting: dict[str, threading.Event] = {}
        self._pending_cancels: dict[str, None] = {}
        self.executions: list[str] = []

    @property
    def configured(self) -> bool:
        return self._config is not None

    @property
    def lookup_authoritative(self) -> bool:
        return False

    def lookup_receipt(self, action_id: str) -> ReceiptLookup:
        receipt = self.cache.get_receipt(action_id)
        return ReceiptLookup("found", receipt) if receipt is not None else ReceiptLookup("undetermined")

    def cancel(self, action_id: str) -> CancelOutcome:
        with self._lock:
            event = self._waiting.get(action_id)
            if event is None:
                self._pending_cancels[action_id] = None
                while len(self._pending_cancels) > _MAX_PENDING_CANCELS:
                    self._pending_cancels.pop(next(iter(self._pending_cancels)))
            else:
                event.set()
        return "stopped"

    def execute(self, call: ToolCall) -> ToolReceipt:
        existing = self.cache.get_receipt(call.action_id)
        if existing is not None and call.idempotency in ("native", "via_action_id"):
            return existing
        params = call.params if isinstance(call.params, Mapping) else {}
        lost = False
        try:
            outcome = self._run(call.action_id, call.tool, params)
        except ChatroomError as exc:
            outcome = {"success": False, "result": f"Chatroom request failed ({exc.code}).", "code": exc.code}
            lost = exc.sent and call.tool == TOOL_POST
            if exc.code == UNREACHABLE:
                outcome["degraded"] = True
        except Exception as exc:  # noqa: BLE001 - 單一工具失敗不拖垮整輪；只記例外類別
            logger.warning("chatroom 工具執行失敗：%s %s", call.tool, type(exc).__name__)
            outcome = {"success": False, "result": "tool failed", "code": "failed"}
        code = outcome.pop("code", None)
        degraded = bool(outcome.pop("degraded", False))
        self.cache.put(call.action_id, outcome)
        self.executions.append(call.action_id)
        if lost:
            raise ToolResponseLost(f"{call.action_id}: chatroom.post 送出後回覆遺失，結果未明")
        succeeded = bool(outcome.get("success"))
        status = "executed" if succeeded else ("degraded" if degraded else str(code or "failed"))
        receipt = ToolReceipt(
            receipt_id=new_id("rcpt"), action_id=call.action_id, succeeded=succeeded,
            result_ref=f"blob_{call.action_id}", recorded_at=datetime.now(UTC), source_verified=True,
            error_code=None if succeeded else str(code or "failed"), meta={"status": status},
        )
        self.cache.put_receipt(call.action_id, receipt)
        return receipt

    # ---- 內部 ----

    @staticmethod
    def _json(payload: Mapping[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _fail(code: str, text: str) -> dict[str, Any]:
        return {"success": False, "result": text, "code": code}

    def _resolve_room(self, params: Mapping[str, Any]) -> str | None:
        """白名單內的房間 id；省略時僅在只有一個房間可用時取它。不在白名單回 None（denied）。"""
        assert self._config is not None
        room = params.get("room")
        if room is None:
            return next(iter(self._config.rooms)) if len(self._config.rooms) == 1 else None
        return room if isinstance(room, str) and room in self._config.rooms else None

    def _run(self, action_id: str, tool: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._config is None or self._client is None:
            return self._fail(UNAVAILABLE, "Chatroom is not configured.")
        if tool not in (TOOL_READ, TOOL_WAIT, TOOL_POST):
            return self._fail("denied", f"Tool {tool!r} is not available.")
        room_id = self._resolve_room(params)
        if room_id is None:
            return self._fail("denied", "Room is not allowed or not specified.")
        if tool == TOOL_POST:
            return self._post(room_id, params)
        if tool == TOOL_READ:
            return self._read(room_id, params)
        return self._wait(action_id, room_id, params)

    def _join(self, room_id: str) -> _Room:
        assert self._config is not None and self._client is not None
        data = self._client.request(
            "POST", f"/api/rooms/{room_id}/join", session_key=self._config.session_key,
            body={
                "kind": "other", "session_key": self._config.session_key, "role": "agent",
                "preferred_name": self._config.display_name,
            },
        )
        pid = data.get("participant_id")
        if not isinstance(pid, str) or not pid:
            raise ChatroomError("bad_response")
        with self._lock:
            cursor = self._cursors.pop(room_id, None)  # 身分失效後重新 join：沿用原游標
        if cursor is None:  # 新行程第一次 join：從房內目前最新的 seq 起算，不重讀歷史（持久化游標留待之後）
            cursor = self._latest_seq(room_id, pid)
        room = _Room(pid, cursor, str(data.get("display_name") or ""))
        with self._lock:
            kept = self._rooms.get(room_id)
            if kept is not None:  # 並行 join：沿用既有游標，不倒退
                room.cursor = max(room.cursor, kept.cursor)
            self._rooms[room_id] = room
        return room

    def _latest_seq(self, room_id: str, participant_id: str) -> int:
        """房內目前最新訊息的 seq（空房 0）：取 ``before_seq`` 很大時的最後一則。"""
        assert self._client is not None
        data = self._client.request(
            "GET", f"/api/rooms/{room_id}/messages", participant_id=participant_id,
            params={"before_seq": _SEQ_CEILING, "limit": 1},
        )
        raw = data.get("messages")
        seqs = [m["seq"] for m in raw if isinstance(m, Mapping) and isinstance(m.get("seq"), int)] if isinstance(raw, list) else []
        return max(seqs, default=0)

    def _room(self, room_id: str) -> _Room:
        with self._lock:
            known = self._rooms.get(room_id)
        return known if known is not None else self._join(room_id)

    def _with_room(self, room_id: str, call: Callable[[_Room], dict[str, Any]]) -> dict[str, Any]:
        """身分失效（可重新 join 的代碼）時重新 join 並重試一次。"""
        room = self._room(room_id)
        try:
            return call(room)
        except ChatroomError as exc:
            if exc.code not in _REJOIN_CODES:
                raise
            with self._lock:
                stale = self._rooms.pop(room_id, None)
                if stale is not None:
                    self._cursors[room_id] = stale.cursor
            return call(self._join(room_id))

    def _advance(self, room_id: str, seq: int) -> None:
        with self._lock:
            room = self._rooms.get(room_id)
            if room is not None and seq > room.cursor:
                room.cursor = seq

    def _view(
        self, room_id: str, room: _Room, raw: list[Any], *, more: bool, timed_out: bool = False,
    ) -> tuple[dict[str, Any], int]:
        """組結果並回傳實際納入的最後 seq（游標只推進到這裡）。自己發的訊息略過但仍推進。"""
        messages: list[dict[str, Any]] = []
        used = 0
        last_seq = 0
        for item in raw:
            if not isinstance(item, Mapping) or isinstance(item.get("seq"), bool) or not isinstance(item.get("seq"), int):
                continue
            seq = item["seq"]
            if item.get("sender_id") == room.participant_id or item.get("deleted"):
                last_seq = seq
                continue
            content, cut = _clean(item.get("content"), MESSAGE_MAX_CHARS)
            sender, _ = _clean(item.get("sender_name") or "系統", 80)
            if messages and used + len(content) > RESULT_MAX_CHARS:
                more = True
                break
            used += len(content)
            entry: dict[str, Any] = {"seq": seq, "sender": sender, "content": content}
            if cut:
                entry["truncated"] = True
            messages.append(entry)
            last_seq = seq
        payload: dict[str, Any] = {
            "untrusted": True, "notice": UNTRUSTED_NOTICE, "room": room_id, "messages": messages, "more": more,
        }
        if timed_out:
            payload["timed_out"] = True
        return {"success": True, "result": self._json(payload)}, last_seq

    def _read(self, room_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        limit = params.get("limit", READ_LIMIT_DEFAULT)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= READ_LIMIT_MAX:
            return self._fail("failed", "Invalid arguments for 'chatroom.read'.")
        assert self._client is not None

        def fetch(room: _Room) -> dict[str, Any]:
            data = self._client.request(
                "GET", f"/api/rooms/{room_id}/messages", participant_id=room.participant_id,
                params={"after_seq": room.cursor, "limit": limit},
            )
            raw = data.get("messages")
            outcome, last = self._view(
                room_id, room, raw if isinstance(raw, list) else [], more=bool(data.get("has_more")),
            )
            if last:
                self._advance(room_id, last)
            return outcome

        return self._with_room(room_id, fetch)

    def _wait(self, action_id: str, room_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        seconds = params.get("timeout_s", WAIT_DEFAULT_S)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= WAIT_MAX_S:
            return self._fail("failed", "Invalid arguments for 'chatroom.wait'.")
        assert self._client is not None
        event = threading.Event()
        with self._lock:
            if action_id in self._pending_cancels:  # cancel 先到：不等，直接以取消收尾
                del self._pending_cancels[action_id]
                event.set()
            self._waiting[action_id] = event
        deadline = self._clock() + float(seconds)

        def poll(room: _Room) -> dict[str, Any]:
            while True:
                if event.is_set():
                    return self._fail(CANCELLED, "cancelled")
                remaining = deadline - self._clock()
                if remaining <= 0:
                    outcome, _ = self._view(room_id, room, [], more=False, timed_out=True)
                    return outcome
                chunk = max(0.1, min(remaining, self._slice_s))
                with self._lock:
                    cursor = self._rooms[room_id].cursor if room_id in self._rooms else room.cursor
                data = self._client.request(
                    "GET", f"/api/rooms/{room_id}/updates", participant_id=room.participant_id,
                    params={"after_seq": cursor, "timeout": chunk}, timeout=chunk + HTTP_TIMEOUT_S,
                )
                if event.is_set():  # 取消：取到的訊息丟棄、不推進游標，下次仍會收到
                    return self._fail(CANCELLED, "cancelled")
                raw = data.get("messages")
                batch = raw if isinstance(raw, list) else []
                outcome, last = self._view(room_id, room, batch, more=False)
                head = data.get("last_seq")
                if last:
                    self._advance(room_id, last)
                elif isinstance(head, int) and not isinstance(head, bool) and batch:
                    self._advance(room_id, head)  # 只有自己的訊息：推進游標但繼續等
                if json.loads(outcome["result"])["messages"]:
                    return outcome

        try:
            return self._with_room(room_id, poll)
        finally:
            with self._lock:
                self._waiting.pop(action_id, None)

    def _post(self, room_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        content = params.get("content")
        mentions = params.get("mentions", [])
        if (
            not isinstance(content, str) or not content.strip() or len(content) > POST_MAX_CHARS
            or not isinstance(mentions, list) or len(mentions) > POST_MAX_MENTIONS
            or not all(isinstance(m, str) and m for m in mentions)
        ):
            return self._fail("failed", "Invalid arguments for 'chatroom.post'.")
        assert self._client is not None
        body = {"content": content, "mentions": mentions, "card_refs": [], "reply_to": None}

        def send(room: _Room) -> dict[str, Any]:
            data = self._client.request(
                "POST", f"/api/rooms/{room_id}/messages", participant_id=room.participant_id, body=body,
            )
            seq = data.get("seq")
            payload: dict[str, Any] = {"room": room_id, "posted": True}
            if isinstance(seq, int) and not isinstance(seq, bool):
                payload["seq"] = seq
            unresolved = data.get("unresolved_mentions")
            if isinstance(unresolved, list) and unresolved:
                payload["unresolved_mentions"] = [str(n)[:80] for n in unresolved[:POST_MAX_MENTIONS]]
            return {"success": True, "result": self._json(payload)}

        return self._with_room(room_id, send)
