"""Chatroom 工具的 ``ToolExecutor``（宣告見 ``uep.tools.catalog.chatroom``）：list_rooms／join／leave／read／wait／post／pin／
ask_human／read_answer／board／board_outline／board_item。

對接 Chatroom Hub 的 HTTP API（與其 MCP bridge 同一套）：

- 認證：``Authorization: Bearer <token>``。token 只在這個模組內存在（``ChatroomClient``），不進 LLM 面、不進結果、不寫 log
  （log 只記例外類別與房間 id）。
- 身分：``UEP_CHATROOM_SESSION_KEY`` 是她自己的 session key（固定人格身分，join 冪等）；``UEP_CHATROOM_DISPLAY_NAME`` 當偏好名稱；
  ``UEP_CHATROOM_KIND`` 是 join 時自報的 kind（預設 ``other``）。
- **主動加入制**（艾斯維爾 2026-11-16）：沒有房間白名單、不自動 join。模型依 ``list_rooms`` → ``join`` → read／wait／post 等 → ``leave`` 的
  流程操作；房間類工具只能用在**本行程已加入**的房間，未加入回 ``not_joined``（提示先 join）。房間 id 是模型給的外部輸入：
  只接受 ``[A-Za-z0-9_-]{1,64}``，不合法一律當參數錯誤，不會進到 URL。
- **已加入狀態只存在記憶體**：行程重啟後一律視為未加入（需要的話模型重新 ``list_rooms``／``join``）；Hub 端同一 session key 的 join 冪等，
  重啟後再 join 會拿回同一個身分，持久化游標也照用（不漏訊息、不重讀）。重啟前沒離開的房間由 Hub 的閒置逾時（約 10 分鐘）清掉。
- **背景 heartbeat**：只對已加入的房間，每 ``heartbeat_s``（預設 120 秒，遠小於 Hub 的 10 分鐘閒置逾時）送一次 ``POST /heartbeat``；
  第一次 join 時啟動，沒有已加入的房間或 ``close()`` 時停止。heartbeat 或任何房間請求得到「身分失效」（被閒置移出、被踢、已離開、401）
  就把該房標成未加入、記下原因，下一次對該房的呼叫以 Hub 的原因碼回報（例如 ``participant_removed_idle``），**不自動重新 join**
  （加入必須是模型主動決定）。連線失敗不當成移出（下一輪再試）。
- 游標：每個房間一個（模型不管 seq）。記憶體為準，同時依「本端 session key＋房間」持久化成 JSON 檔（目錄 ``UEP_CHATROOM_CURSOR_DIR``，
  未設 = ``~/.uep/chatroom_cursors``；檔名用 session key 的雜湊，不落明文；原子寫入）。join 時有游標檔就沿用；沒有檔、檔毀損，
  或游標超過房內最新值（Hub 重建）則從房內目前最新的 ``max(seq, update_seq)`` 起算（不重讀歷史、不對舊訊息起反應）。
  ``leave`` 會刪掉該房的游標檔（下次加入視為新加入）。游標只往前；``wait`` 被取消時不推進（取到的訊息丟棄，下次還會收到）。
  寫檔失敗只記 warning、不影響工具結果。游標不放進 AgentLoop checkpoint。
- 房內訊息、房名、看板內容是外部內容：結果一律帶 ``untrusted=true`` 與固定提示，內容經清理（去控制字元）、單則與總量截斷，再放進回合結果暫存，
  由 Gateway 的 ``outcome_view`` 過濾後交給模型。
- ``wait`` 以短切片 long-poll（每片不超過 ``slice_s``），片與片之間檢查取消旗標與期限；``cancel`` 先於 ``execute`` 到達也會記下。
  取消的 ``execute`` 簽回失敗 receipt（``error_code="cancelled"``），不拋例外。
- ``post``／``ask_human`` 建立題目的連線失敗（確定未送出）回失敗；送出後回覆遺失（讀取逾時等）拋 ``ToolResponseLost``，由 TaskManager 標 UNKNOWN，不重送。
- 未設定（``config=None``）時所有呼叫回 ``not_configured``：刻意不用 ``unavailable``，那個代碼在 AgentLoop 被當成暫時性失敗而重試，
  而「沒設定」重試永遠不會好。
- ``lookup_authoritative=False``：結果暫存只在記憶體。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
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
from uep.config import get_settings
from uep.ids import new_id
from uep.tools.catalog.chatroom import (
    BOARD_RESULT_MAX_CHARS,
    CHATROOM_TOOLS,
    ID_MAX,
    LIST_ROOMS_MAX,
    MENTION_MAX,
    MESSAGE_MAX_CHARS,
    OPTION_MAX_CHARS,
    POST_MAX_CHARS,
    POST_MAX_MENTIONS,
    QUESTION_MAX_CHARS,
    QUESTION_OPTIONS_MAX,
    QUESTION_TTL_MAX_S,
    READ_LIMIT_DEFAULT,
    READ_LIMIT_MAX,
    RESULT_MAX_CHARS,
    TOOL_ASK_HUMAN,
    TOOL_BOARD,
    TOOL_BOARD_ITEM,
    TOOL_BOARD_OUTLINE,
    TOOL_JOIN,
    TOOL_LEAVE,
    TOOL_LIST_ROOMS,
    TOOL_PIN,
    TOOL_POST,
    TOOL_READ,
    TOOL_READ_ANSWER,
    TOOL_WAIT,
    UNTRUSTED_NOTICE,
    WAIT_DEFAULT_S,
    WAIT_MAX_S,
)
from uep.tools.outcomes import TurnOutcomeCache

logger = logging.getLogger("uep.tools.executors.chatroom")

CANCELLED = "cancelled"
NOT_CONFIGURED = "not_configured"  # 不可叫 unavailable：AgentLoop 的 TRANSIENT_TOOL_ERRORS 會把它當暫時性失敗重試
UNREACHABLE = "unreachable"
NOT_JOINED = "not_joined"
DEFAULT_SLICE_S = 3.0  # wait 每片 long-poll 秒數，也是取消生效的最大延遲
HEARTBEAT_INTERVAL_S = 120.0  # Hub 閒置 10 分鐘會移出；間隔取其 1/5
HTTP_TIMEOUT_S = 10.0
_MAX_PENDING_CANCELS = 256
CODE_MAX = 64
NAME_MAX = 80
TOPIC_MAX = 200
NOTE_MAX = 200
ANSWER_MAX_CHARS = 2000
LATEST_WINDOW = 50  # 起算游標時往回看的訊息數
_SEQ_CEILING = 2**53  # 「比任何訊息都新」的 before_seq，用來取房內最新一則
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ID_RE = re.compile(rf"[A-Za-z0-9_-]{{1,{ID_MAX}}}")
_KINDS = frozenset({"claude", "codex", "other", "uep"})  # join 可自報的 kind（human 不開放）
# 「這個身分已經不在房內」的 Hub 代碼：標成未加入並回報，不自動重新 join（participant_wrong_room 不在內：那是訊息不屬於這個房）
MEMBERSHIP_LOST_CODES = frozenset({
    "participant_kicked", "participant_removed_idle", "participant_not_active", "participant_left", "not_a_member", "http_401",
})
# 這些工具送出後回覆遺失時結果未明（不得重送）
_LOSSY_TOOLS = frozenset({TOOL_POST, TOOL_ASK_HUMAN})


@dataclass(frozen=True)
class ChatroomConfig:
    """連線與身分設定。``token`` 只在這裡；``repr`` 隱藏它。"""

    url: str
    session_key: str
    token: str = field(repr=False)
    kind: str = "other"
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
    """設定不齊（缺 URL／身分 key／token）回 None：工具 unavailable，組裝不失敗。kind 不合法退回 ``other``。"""
    url = (getattr(settings, "chatroom_url", None) or "").strip().rstrip("/")
    key = (getattr(settings, "chatroom_session_key", None) or "").strip()
    secret = getattr(settings, "chatroom_token", None)
    token = secret.get_secret_value().strip() if secret is not None else ""
    token_file = getattr(settings, "chatroom_token_file", None)
    if not token and token_file is not None:
        token = _read_token_file(Path(token_file)) or ""
    if not (url and key and token):
        return None
    name = (getattr(settings, "chatroom_display_name", None) or "").strip() or None
    kind = str(getattr(settings, "chatroom_kind", None) or "other").strip().lower()
    if kind not in _KINDS:
        logger.warning("UEP_CHATROOM_KIND 不合法，改用 other")
        kind = "other"
    return ChatroomConfig(url=url, session_key=key, token=token, kind=kind, display_name=name)


class ChatroomError(Exception):
    """Hub 請求失敗。``code`` 是短碼（unreachable／ambiguous／hub 的 detail.code／http_<status>）；訊息不含 token 或內容。

    ``text`` 給模型看的說明（省略時用通用句）。"""

    def __init__(self, code: str, *, status: int = 0, sent: bool = False, text: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.sent = sent  # 請求可能已抵達 Hub（回覆遺失）
        self.text = text
        self.lost = False  # 只有實際送出訊息／題目那一步的 sent 錯誤才會被標成 True（結果未明）


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
                code = _safe_code(detail.get("code", "")) if isinstance(detail, dict) else ""
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


def _key(item: Mapping[str, Any], *, updates: bool) -> int:
    """訊息在游標上的位置：/updates 用 max(seq, update_seq)，/messages 只用 seq；非整數視為 0。"""
    out = 0
    for name in (("seq", "update_seq") if updates else ("seq",)):
        value = item.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            out = max(out, value)
    return out


def _safe_code(value: Any) -> str:
    """Hub 回傳的 detail.code 是外部內容：限小寫英數與底線、最長 64 字，才放進結果與 receipt。"""
    return re.sub(r"[^a-z0-9_]", "", str(value).lower())[:CODE_MAX]


@dataclass
class _Room:
    participant_id: str
    cursor: int
    own_name: str


def _clean(text: Any, limit: int) -> tuple[str, bool]:
    value = _CONTROL_CHARS.sub("", str(text if text is not None else ""))
    return (value[:limit], True) if len(value) > limit else (value, False)


def _count(value: Any) -> int:
    """Hub 回報的「受影響項目」可能是清單或數字：一律轉成數量。"""
    if isinstance(value, list):
        return len(value)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class ChatroomToolExecutor:
    """滿足 ``uep.adapters.tool.CancellableToolExecutor``。未設定（``config=None``）時所有呼叫回 ``not_configured``。"""

    def __init__(
        self, config: ChatroomConfig | None, *, cache: TurnOutcomeCache | None = None,
        transport: httpx.BaseTransport | None = None, slice_s: float = DEFAULT_SLICE_S,
        clock: Callable[[], float] = time.monotonic, cursor_dir: Path | None = None,
        heartbeat_s: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._config = config
        self._cursor_dir = cursor_dir  # None = 用設定 ``chatroom_cursor_dir``（每次取用時解析）
        self._client = ChatroomClient(config, transport=transport) if config is not None else None
        self.cache = cache if cache is not None else TurnOutcomeCache()
        self._slice_s = slice_s
        self._clock = clock
        self._heartbeat_s = heartbeat_s
        self._lock = threading.Lock()
        self._rooms: dict[str, _Room] = {}  # 本行程已加入的房間（只在記憶體）
        self._lost: dict[str, str] = {}  # 被移出（或身分失效）還沒回報給模型的房間 → Hub 原因碼
        self._waiting: dict[str, threading.Event] = {}
        self._pending_cancels: dict[str, None] = {}
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self.executions: list[str] = []

    @property
    def configured(self) -> bool:
        return self._config is not None

    @property
    def lookup_authoritative(self) -> bool:
        return False

    def joined_rooms(self) -> frozenset[str]:
        """本行程目前視為已加入的房間 id（診斷與測試用）。"""
        with self._lock:
            return frozenset(self._rooms)

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

    def close(self) -> None:
        """停止背景 heartbeat（冪等）。不替模型離開房間：沒離開的房間由 Hub 的閒置逾時清掉。"""
        with self._lock:
            thread, self._hb_thread = self._hb_thread, None
            self._hb_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def execute(self, call: ToolCall) -> ToolReceipt:
        existing = self.cache.get_receipt(call.action_id)
        if existing is not None and call.idempotency in ("native", "via_action_id"):
            return existing
        params = call.params if isinstance(call.params, Mapping) else {}
        lost = False
        try:
            outcome = self._run(call.action_id, call.tool, params)
        except ChatroomError as exc:
            outcome = {
                "success": False, "result": exc.text or f"Chatroom request failed ({exc.code}).", "code": exc.code,
            }
            lost = exc.lost and call.tool in _LOSSY_TOOLS
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
            raise ToolResponseLost(f"{call.action_id}: {call.tool} 送出後回覆遺失，結果未明")
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
        return json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))

    @staticmethod
    def _fail(code: str, text: str) -> dict[str, Any]:
        return {"success": False, "result": text, "code": code}

    def _ok(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"success": True, "result": self._json(payload)}

    def _run(self, action_id: str, tool: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._config is None or self._client is None:
            return self._fail(NOT_CONFIGURED, "Chatroom is not configured.")
        if tool not in CHATROOM_TOOLS:
            return self._fail("denied", f"Tool {tool!r} is not available.")
        if tool == TOOL_LIST_ROOMS:
            return self._list_rooms()
        if tool == TOOL_READ_ANSWER:
            return self._read_answer(params)
        room_id = params.get("room")
        if not isinstance(room_id, str) or not _ID_RE.fullmatch(room_id):
            return self._fail("failed", f"Invalid arguments for {tool!r}: room must be a room id from chatroom.list_rooms.")
        if tool == TOOL_JOIN:
            return self._join_tool(room_id)
        if tool == TOOL_LEAVE:
            return self._leave(room_id)
        if tool == TOOL_POST:
            return self._post(room_id, params)
        if tool == TOOL_READ:
            return self._read(room_id, params)
        if tool == TOOL_WAIT:
            return self._wait(action_id, room_id, params)
        if tool == TOOL_PIN:
            return self._pin(room_id, params)
        if tool == TOOL_ASK_HUMAN:
            return self._ask_human(room_id, params)
        return self._board(room_id, tool, params)

    # ---- 成員狀態 ----

    def _require(self, room_id: str) -> _Room:
        """已加入的房間；否則拋 ``not_joined``（或被移出的原因碼，回報一次後清掉）。"""
        with self._lock:
            known = self._rooms.get(room_id)
            reason = self._lost.pop(room_id, None) if known is None else None
        if known is not None:
            return known
        if reason is not None:
            raise ChatroomError(reason, text=_lost_text(reason))
        raise ChatroomError(
            NOT_JOINED,
            text="You have not joined this room. Call chatroom.list_rooms to get the room id, then chatroom.join it first.",
        )

    def _mark_lost(self, room_id: str, code: str) -> None:
        """身分失效：標成未加入並記下原因（游標檔保留；不自動重新 join）。"""
        with self._lock:
            if self._rooms.pop(room_id, None) is not None:
                self._lost[room_id] = code
                logger.warning("chatroom 房間身分失效，已標為未加入：room=%s code=%s", room_id, code)

    def _call(self, room_id: str, call: Callable[[_Room], dict[str, Any]]) -> dict[str, Any]:
        """對已加入的房間執行請求；Hub 回「身分已失效」時標成未加入並以原因碼回報。"""
        room = self._require(room_id)
        try:
            return call(room)
        except ChatroomError as exc:
            if exc.code in MEMBERSHIP_LOST_CODES:
                self._mark_lost(room_id, exc.code)
                with self._lock:
                    self._lost.pop(room_id, None)  # 這次就是回報，不留到下一次
                raise ChatroomError(exc.code, status=exc.status, text=_lost_text(exc.code)) from exc
            raise

    def _join(self, room_id: str) -> tuple[_Room, bool]:
        assert self._config is not None and self._client is not None
        data = self._client.request(
            "POST", f"/api/rooms/{room_id}/join", session_key=self._config.session_key,
            body={
                "kind": self._config.kind, "session_key": self._config.session_key, "role": "agent",
                "preferred_name": self._config.display_name,
            },
        )
        pid = data.get("participant_id")
        if not isinstance(pid, str) or not pid:
            raise ChatroomError("bad_response")
        rejoined = bool(data.get("rejoined"))
        cursor = self._load_cursor(room_id)  # 有持久化游標就沿用（同一 session key 的 join 冪等，房內 seq 不變）
        if cursor is not None:
            # 只有游標超過房內最新值（Hub 重建、seq 空間重來）才視為失效
            latest = self._latest_seq(room_id, pid)
            if cursor > latest:
                logger.warning("chatroom 游標超過房內最新 seq，改從最新起算：room=%s", room_id)
                cursor = None
        if cursor is None:
            cursor = self._latest_seq(room_id, pid)
            joined = data.get("joined_seq")
            if not rejoined and isinstance(joined, int) and not isinstance(joined, bool):
                cursor = max(cursor, joined)  # 全新加入：joined_seq 就是房內最後一則
            self._save_cursor(room_id, pid, cursor)
        room = _Room(pid, cursor, str(data.get("display_name") or ""))
        with self._lock:
            kept = self._rooms.get(room_id)
            if kept is not None:  # 並行 join：沿用既有游標，不倒退
                room.cursor = max(room.cursor, kept.cursor)
            self._rooms[room_id] = room
            self._lost.pop(room_id, None)
        return room, rejoined

    def _join_tool(self, room_id: str) -> dict[str, Any]:
        with self._lock:
            known = self._rooms.get(room_id)
        if known is not None:
            return self._ok({"room": room_id, "joined": True, "already_joined": True, "display_name": known.own_name})
        room, rejoined = self._join(room_id)
        self._ensure_heartbeat()
        payload: dict[str, Any] = {"room": room_id, "joined": True, "display_name": room.own_name}
        if rejoined:
            payload["rejoined"] = True
        return self._ok(payload)

    def _leave(self, room_id: str) -> dict[str, Any]:
        assert self._client is not None
        with self._lock:
            room = self._rooms.get(room_id)
            was_lost = room is None and self._lost.pop(room_id, None) is not None
        if room is None:
            if not was_lost:
                self._require(room_id)  # 拋 not_joined
            self._drop_cursor(room_id)  # 已被移出：離開視為成功（冪等），並清掉游標檔
            return self._ok({"room": room_id, "joined": False, "already_removed": True})
        data: dict[str, Any] = {}
        try:
            data = self._client.request(
                "POST", f"/api/rooms/{room_id}/leave", participant_id=room.participant_id, body={},
            )
        except ChatroomError as exc:
            if exc.code not in MEMBERSHIP_LOST_CODES:
                raise  # 已經不在房內（被移出等）視同已離開；其他錯誤（例如連線失敗）保留加入狀態，讓模型可重試
        with self._lock:
            self._rooms.pop(room_id, None)
            self._lost.pop(room_id, None)
        self._drop_cursor(room_id)
        payload: dict[str, Any] = {"room": room_id, "joined": False}
        for name in ("cancelled_questions", "cascaded_subagents", "orphaned_tasks"):
            if _count(data.get(name)):
                payload[name] = _count(data.get(name))
        successor = data.get("admin_transferred_to")
        if successor:
            payload["admin_transferred_to"] = _clean(successor, NAME_MAX)[0]
        if data.get("archived"):
            payload["archived"] = True
        return self._ok(payload)

    # ---- heartbeat ----

    def _ensure_heartbeat(self) -> None:
        with self._lock:
            if self._hb_thread is not None and self._hb_thread.is_alive():
                return
            self._hb_stop = threading.Event()
            thread = threading.Thread(
                target=self._heartbeat_loop, args=(self._hb_stop,), name="chatroom-heartbeat", daemon=True,
            )
            self._hb_thread = thread
        thread.start()

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self._heartbeat_s):
            if not self.heartbeat_once():
                with self._lock:
                    if not self._rooms and self._hb_stop is stop:  # 沒有已加入的房間：結束；下次 join 再啟動
                        self._hb_thread = None
                        return

    def heartbeat_once(self) -> bool:
        """對每個已加入的房間送一次 heartbeat；回傳是否還有已加入的房間。身分失效的房間標成未加入（連線失敗不算移出）。"""
        with self._lock:
            snapshot = dict(self._rooms)
        for room_id, room in snapshot.items():
            try:
                assert self._client is not None
                self._client.request("POST", f"/api/rooms/{room_id}/heartbeat", participant_id=room.participant_id)
            except ChatroomError as exc:
                if exc.code in MEMBERSHIP_LOST_CODES:
                    self._mark_lost(room_id, exc.code)
                else:
                    logger.debug("chatroom heartbeat 失敗：room=%s code=%s", room_id, exc.code)
            except Exception as exc:  # noqa: BLE001 - 背景執行緒不得因單次失敗結束
                logger.warning("chatroom heartbeat 例外：room=%s %s", room_id, type(exc).__name__)
        with self._lock:
            return bool(self._rooms)

    # ---- 游標 ----

    def _cursor_path(self, room_id: str) -> Path:
        """游標檔：``<目錄>/<session key 雜湊>_<房間 id>.json``（房間 id 清掉路徑字元）。"""
        assert self._config is not None
        base = self._cursor_dir or get_settings().chatroom_cursor_dir or Path.home() / ".uep" / "chatroom_cursors"
        ident = hashlib.sha256(self._config.session_key.encode("utf-8")).hexdigest()[:16]
        return Path(base) / f"{ident}_{re.sub(r'[^A-Za-z0-9_.-]', '_', room_id)[:CODE_MAX]}.json"

    def _load_cursor(self, room_id: str) -> int | None:
        """讀持久化游標；檔不存在、毀損或格式不對一律回 None（視為沒有游標）。檔內 participant_id 只供除錯，不作判斷。"""
        try:
            data = json.loads(self._cursor_path(room_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("chatroom 游標檔無法讀取，改從房內最新起算：room=%s", room_id)
            return None
        cursor = data.get("cursor") if isinstance(data, dict) else None
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            return None
        return cursor

    def _save_cursor(self, room_id: str, participant_id: str, cursor: int) -> None:
        """原子寫入（暫存檔再 replace）；失敗只記 warning。呼叫端持有 ``self._lock`` 或尚未發布 room，寫入不會交錯。"""
        try:
            path = self._cursor_path(room_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps({"participant_id": participant_id, "cursor": cursor}, ensure_ascii=False), encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001 - 持久化失敗不影響工具結果
            logger.warning("chatroom 游標寫檔失敗：room=%s %s", room_id, type(exc).__name__)

    def _drop_cursor(self, room_id: str) -> None:
        """離開時刪游標檔：下次加入視為新加入（從房內最新起算）。失敗只記 warning。"""
        try:
            self._cursor_path(room_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("chatroom 游標檔刪除失敗：room=%s %s", room_id, type(exc).__name__)

    def _latest_seq(self, room_id: str, participant_id: str) -> int:
        """房內目前最新的游標值（空房 0）：取最新一批訊息的 ``max(seq, update_seq)``，
        避免起點落在被 pin／刪除過的舊訊息的 update_seq 之前。"""
        assert self._client is not None
        data = self._client.request(
            "GET", f"/api/rooms/{room_id}/messages", participant_id=participant_id,
            params={"before_seq": _SEQ_CEILING, "limit": LATEST_WINDOW},
        )
        raw = data.get("messages")
        if not isinstance(raw, list):
            return 0
        return max((_key(m, updates=True) for m in raw if isinstance(m, Mapping)), default=0)

    def _advance(self, room_id: str, position: int) -> None:
        """游標只往前（單調）；讀寫都在 ``self._lock`` 內，是並行競態的第二道防線（第一道是 conflict_keys）。"""
        with self._lock:
            room = self._rooms.get(room_id)
            if room is not None and position > room.cursor:
                room.cursor = position
                self._save_cursor(room_id, room.participant_id, position)

    def _cursor(self, room_id: str, fallback: int) -> int:
        with self._lock:
            room = self._rooms.get(room_id)
            return room.cursor if room is not None else fallback

    # ---- list_rooms ----

    def _list_rooms(self) -> dict[str, Any]:
        assert self._config is not None and self._client is not None
        data = self._client.request(
            "GET", "/api/rooms", session_key=self._config.session_key, params={"status": "active"},
        )
        raw = data.get("rooms")
        items = [r for r in raw if isinstance(r, Mapping)] if isinstance(raw, list) else []
        with self._lock:
            joined = frozenset(self._rooms)
            removed = sorted(self._lost)
            self._lost.clear()  # 回報一次
        rooms: list[dict[str, Any]] = []
        for item in items:
            rid = item.get("id")
            if not isinstance(rid, str) or not _ID_RE.fullmatch(rid):
                continue
            if len(rooms) >= LIST_ROOMS_MAX:
                break
            entry: dict[str, Any] = {"id": rid, "name": _clean(item.get("name"), NAME_MAX)[0]}
            topic = _clean(item.get("topic"), TOPIC_MAX)[0]
            if topic:
                entry["topic"] = topic
            members = item.get("member_count")
            if isinstance(members, int) and not isinstance(members, bool):
                entry["members"] = members
            entry["joined"] = rid in joined
            rooms.append(entry)
        assignments: list[dict[str, Any]] = []
        pending = data.get("pending_assignments")
        for item in (pending if isinstance(pending, list) else [])[:10]:
            if isinstance(item, Mapping) and isinstance(item.get("room_id"), str) and _ID_RE.fullmatch(item["room_id"]):
                entry = {"room": item["room_id"], "room_name": _clean(item.get("room_name"), NAME_MAX)[0]}
                note = _clean(item.get("note"), NOTE_MAX)[0]
                if note:
                    entry["note"] = note
                assignments.append(entry)
        payload: dict[str, Any] = {
            "untrusted": True, "notice": UNTRUSTED_NOTICE, "rooms": rooms, "more": len(items) > len(rooms),
        }
        if assignments:
            payload["pending_assignments"] = assignments
        if removed:
            payload["removed_from"] = removed  # 先前被移出（閒置等）的房間；要回去請重新 join
        return self._ok(payload)

    # ---- 讀取 ----

    def _view(
        self, room_id: str, room: _Room, raw: list[Any], *, cursor: int, updates: bool, more: bool,
        timed_out: bool = False,
    ) -> tuple[dict[str, Any], int]:
        """組結果並回傳新的游標位置。

        - 依游標位置（updates=True 為 max(seq, update_seq)，否則 seq）遞增處理；因總量截斷而沒納入的項目不推進，留給下一次。
        - 略過但推進：自己發的、已刪除的、seq <= 起始游標的（已讀過的訊息即使被 pin／更新也不再投遞）。
        """
        entries = sorted(
            (item for item in raw if isinstance(item, Mapping) and _key(item, updates=updates) > 0),
            key=lambda item: _key(item, updates=updates),
        )
        messages: list[dict[str, Any]] = []
        used = 0
        position = cursor
        for item in entries:
            seq = _key(item, updates=False)
            key = _key(item, updates=updates)
            if item.get("sender_id") == room.participant_id or item.get("deleted") or seq <= cursor:
                position = max(position, key)
                continue
            content, cut = _clean(item.get("content"), MESSAGE_MAX_CHARS)
            sender, _ = _clean(item.get("sender_name") or "系統", 80)
            if messages and used + len(content) > RESULT_MAX_CHARS:
                more = True
                break
            used += len(content)
            entry: dict[str, Any] = {}
            message_id = item.get("id")
            if isinstance(message_id, str) and _ID_RE.fullmatch(message_id):
                entry["id"] = message_id
            entry.update({"seq": seq, "sender": sender, "content": content})
            if cut:
                entry["truncated"] = True
            messages.append(entry)
            position = max(position, key)
        # 固定鍵序：untrusted 與 notice 在 messages 前面
        payload: dict[str, Any] = {
            "untrusted": True, "notice": UNTRUSTED_NOTICE, "room": room_id, "messages": messages, "more": more,
        }
        if timed_out:
            payload["timed_out"] = True
        return {"success": True, "result": self._json(payload)}, position

    def _read(self, room_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        limit = params.get("limit", READ_LIMIT_DEFAULT)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= READ_LIMIT_MAX:
            return self._fail("failed", "Invalid arguments for 'chatroom.read'.")
        assert self._client is not None

        def fetch(room: _Room) -> dict[str, Any]:
            cursor = self._cursor(room_id, room.cursor)
            data = self._client.request(
                "GET", f"/api/rooms/{room_id}/messages", participant_id=room.participant_id,
                params={"after_seq": cursor, "limit": limit},
            )
            raw = data.get("messages")
            outcome, position = self._view(
                room_id, room, raw if isinstance(raw, list) else [], cursor=cursor, updates=False,
                more=bool(data.get("has_more")),
            )
            self._advance(room_id, position)
            return outcome

        return self._call(room_id, fetch)

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
                    outcome, _ = self._view(room_id, room, [], cursor=0, updates=True, more=False, timed_out=True)
                    return outcome
                chunk = max(0.1, min(remaining, self._slice_s))
                cursor = self._cursor(room_id, room.cursor)
                started = self._clock()
                data = self._client.request(
                    "GET", f"/api/rooms/{room_id}/updates", participant_id=room.participant_id,
                    params={"after_seq": cursor, "timeout": chunk}, timeout=chunk + HTTP_TIMEOUT_S,
                )
                if event.is_set():  # 取消：取到的訊息丟棄、不推進游標，下次仍會收到
                    return self._fail(CANCELLED, "cancelled")
                raw = data.get("messages")
                outcome, position = self._view(
                    room_id, room, raw if isinstance(raw, list) else [], cursor=cursor, updates=True, more=False,
                )
                if json.loads(outcome["result"])["messages"]:
                    self._advance(room_id, position)
                    return outcome
                head = data.get("last_seq")  # Hub 的 last_seq 就是 max(seq, update_seq)；沒有可投遞訊息時直接跟到它
                if isinstance(head, int) and not isinstance(head, bool):
                    position = max(position, head)
                self._advance(room_id, position)
                # 空結果（沒新訊息、或只有略過的更新）：這一片要等滿才打下一片，避免 Hub 立刻回應時熱迴圈
                pause = min(chunk - (self._clock() - started), max(deadline - self._clock(), 0))
                if pause > 0 and event.wait(pause):
                    return self._fail(CANCELLED, "cancelled")

        try:
            return self._call(room_id, poll)
        finally:
            with self._lock:
                self._waiting.pop(action_id, None)

    # ---- 寫入 ----

    def _post(self, room_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        content = params.get("content")
        mentions = params.get("mentions", [])
        if (
            not isinstance(content, str) or not content.strip() or len(content) > POST_MAX_CHARS
            or not isinstance(mentions, list) or len(mentions) > POST_MAX_MENTIONS
            or not all(isinstance(m, str) and 0 < len(m) <= MENTION_MAX for m in mentions)
        ):
            return self._fail("failed", "Invalid arguments for 'chatroom.post'.")
        assert self._client is not None
        body = {"content": content, "mentions": mentions, "card_refs": [], "reply_to": None}

        def send(room: _Room) -> dict[str, Any]:
            try:
                data = self._client.request(
                    "POST", f"/api/rooms/{room_id}/messages", participant_id=room.participant_id, body=body,
                )
            except ChatroomError as exc:
                exc.lost = exc.sent  # 只有這一步（實際送出訊息）的 sent 錯誤才算結果未明
                raise
            seq = data.get("seq")
            payload: dict[str, Any] = {"room": room_id, "posted": True}
            if isinstance(seq, int) and not isinstance(seq, bool):
                payload["seq"] = seq
            unresolved = data.get("unresolved_mentions")
            if isinstance(unresolved, list) and unresolved:
                payload["unresolved_mentions"] = [str(n)[:80] for n in unresolved[:POST_MAX_MENTIONS]]
            return self._ok(payload)

        return self._call(room_id, send)

    def _pin(self, room_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        message_id = params.get("message_id")
        if not isinstance(message_id, str) or not _ID_RE.fullmatch(message_id):
            return self._fail("failed", "Invalid arguments for 'chatroom.pin'.")
        assert self._client is not None

        def pin(room: _Room) -> dict[str, Any]:
            data = self._client.request("POST", f"/api/messages/{message_id}/pin", participant_id=room.participant_id)
            payload: dict[str, Any] = {"room": room_id, "pinned": True}
            if data.get("already_pinned"):
                payload["already_pinned"] = True
            return self._ok(payload)

        return self._call(room_id, pin)

    def _ask_human(self, room_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        question, target = params.get("question"), params.get("target_name")
        options = params.get("options", [])
        free_text, multi = params.get("allow_free_text", True), params.get("multi_select", False)
        wait_s, ttl_s = params.get("wait_s", 0), params.get("ttl_s", 0)
        if (
            not isinstance(question, str) or not question.strip() or len(question) > QUESTION_MAX_CHARS
            or not isinstance(target, str) or not target.strip() or len(target) > NAME_MAX
            or not isinstance(options, list) or len(options) > QUESTION_OPTIONS_MAX
            or not all(isinstance(o, str) and 0 < len(o) <= OPTION_MAX_CHARS for o in options)
            or not isinstance(free_text, bool) or not isinstance(multi, bool)
            or isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or not 0 <= wait_s <= WAIT_MAX_S
            or isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)) or not 0 <= ttl_s <= QUESTION_TTL_MAX_S
            or (not options and not free_text)
        ):
            return self._fail("failed", "Invalid arguments for 'chatroom.ask_human'.")
        assert self._client is not None

        def ask(room: _Room) -> dict[str, Any]:
            detail = self._client.request("GET", f"/api/rooms/{room_id}", participant_id=room.participant_id)
            people = [p for p in detail.get("participants", []) if isinstance(p, Mapping) and p.get("status") == "active"]
            match = next((p for p in people if p.get("display_name") == target and p.get("role") == "human"), None)
            target_id = match.get("id") if match is not None else None
            if not isinstance(target_id, str) or not target_id:
                humans = "、".join(
                    _clean(p.get("display_name"), NAME_MAX)[0] for p in people if p.get("role") == "human"
                ) or "（房裡沒有人類）"
                return self._fail("target_not_found", f"No human member named {target!r} in this room. Humans present: {humans}.")
            body: dict[str, Any] = {
                "prompt": question, "options": [{"label": o} for o in options], "allow_free_text": free_text,
                "multi_select": multi, "target_participant_id": target_id,
            }
            if ttl_s:
                body["timeout_seconds"] = float(ttl_s)
            try:
                created = self._client.request(
                    "POST", f"/api/rooms/{room_id}/questions", participant_id=room.participant_id, body=body,
                )
            except ChatroomError as exc:
                exc.lost = exc.sent  # 題目可能已建立：結果未明，不重送
                raise
            qid = created.get("id")
            if not isinstance(qid, str) or not _ID_RE.fullmatch(qid):
                raise ChatroomError("bad_response")
            view: dict[str, Any] = {"question_id": qid, "status": "pending", "target_name": _clean(target, NAME_MAX)[0]}
            if created.get("target_active") is False:
                view["target_active"] = False
            if wait_s:
                got = self._client.request(
                    "GET", f"/api/questions/{qid}", params={"wait": float(wait_s)}, timeout=float(wait_s) + HTTP_TIMEOUT_S,
                )
                question_data = got.get("question")
                if isinstance(question_data, Mapping):
                    view = {**view, **self._question_view(qid, question_data)}
            else:
                expires = created.get("expires_in_seconds")
                if isinstance(expires, (int, float)) and not isinstance(expires, bool):
                    view["expires_in_seconds"] = int(expires)
            if view["status"] == "pending":
                view["hint"] = f"Not answered yet. Use chatroom.read_answer with question_id={qid} later; do not ask again."
            return self._ok(view)

        return self._call(room_id, ask)

    @staticmethod
    def _question_view(question_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        raw_status = data.get("status")
        status = (_safe_code(raw_status) if isinstance(raw_status, str) else "") or "unknown"
        view: dict[str, Any] = {"question_id": question_id, "status": status}
        answer = data.get("answer")
        if status in ("answered", "skipped") and isinstance(answer, str) and answer:
            view["answer"] = _clean(answer, ANSWER_MAX_CHARS)[0]
        raw_kind = data.get("answer_kind")
        kind = _safe_code(raw_kind) if isinstance(raw_kind, str) else ""
        if kind:
            view["answer_kind"] = kind
        chosen = data.get("answer_options")
        if isinstance(chosen, list) and chosen:
            view["answer_options"] = [_clean(o, OPTION_MAX_CHARS)[0] for o in chosen[:QUESTION_OPTIONS_MAX]]
        expires = data.get("expires_in_seconds")
        if status == "pending" and isinstance(expires, (int, float)) and not isinstance(expires, bool):
            view["expires_in_seconds"] = max(0, int(expires))
        if data.get("answer_attachments"):
            view["has_attachments"] = True
        return view

    def _read_answer(self, params: Mapping[str, Any]) -> dict[str, Any]:
        question_id = params.get("question_id")
        if not isinstance(question_id, str) or not _ID_RE.fullmatch(question_id):
            return self._fail("failed", "Invalid arguments for 'chatroom.read_answer'.")
        assert self._client is not None
        data = self._client.request("GET", f"/api/questions/{question_id}")
        question = data.get("question")
        if not isinstance(question, Mapping):
            raise ChatroomError("bad_response")
        return self._ok(self._question_view(question_id, question))

    # ---- board（唯讀）----

    def _board(self, room_id: str, tool: str, params: Mapping[str, Any]) -> dict[str, Any]:
        query: dict[str, Any] = {}
        for name in ("objective_id", "checklist_id"):
            value = params.get(name)
            if value is None or value == "":
                continue
            if tool == TOOL_BOARD_ITEM or not isinstance(value, str) or not _ID_RE.fullmatch(value):
                return self._fail("failed", f"Invalid arguments for {tool!r}.")
            query[name] = value
        for name in ("include_settled", "open_only"):
            value = params.get(name)
            if value is None:
                continue
            if not isinstance(value, bool) or (name == "include_settled") != (tool == TOOL_BOARD):
                return self._fail("failed", f"Invalid arguments for {tool!r}.")
            query[name] = "true" if value else "false"
        path = f"/api/rooms/{room_id}/board"
        if tool == TOOL_BOARD_OUTLINE:
            path += "/outline"
        elif tool == TOOL_BOARD_ITEM:
            item_id = params.get("item_id")
            if not isinstance(item_id, str) or not _ID_RE.fullmatch(item_id):
                return self._fail("failed", f"Invalid arguments for {tool!r}.")
            path += f"/items/{item_id}"
        assert self._client is not None

        def fetch(room: _Room) -> dict[str, Any]:
            data = self._client.request("GET", path, participant_id=room.participant_id, params=query)
            text = self._json(data)
            truncated = len(text) > BOARD_RESULT_MAX_CHARS
            payload: dict[str, Any] = {"untrusted": True, "notice": UNTRUSTED_NOTICE, "room": room_id, "truncated": truncated}
            if truncated:  # 截斷後不再是合法 JSON：給文字，請模型改用較小的範圍（週期／階段／單卡）
                payload["text"] = _clean(text, BOARD_RESULT_MAX_CHARS)[0]
            else:
                payload["data"] = data
            return self._ok(payload)

        return self._call(room_id, fetch)


def _lost_text(code: str) -> str:
    return (
        f"You are no longer in this room ({code}). Call chatroom.list_rooms and chatroom.join again if you still need it; "
        "it will not be rejoined automatically."
    )
