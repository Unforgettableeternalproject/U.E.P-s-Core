"""迴圈的平行 context（Agent 架構提案 §4.10、AL-8）：fork／持久化／merge-back。

術語：parent ＝該 conversation 的 ContextManager 工作 context；child ＝Run 專屬的平行 context（鍵 ``loop:<run_id>``）。

- ``uep/`` 不能 import ``echo_thought_core``（import boundary）：ContextManager 與其 session store 在這裡只是 ``LoopContextStore``
  協定；production 實作在 ``uep.echo.loop_store_wiring.SessionLoopContextStore``（接 echo_thought_core 的 SessionStore）。
  離線用的實作在 ``uep.fakes.loop_context``。
- fork 只取：Persona 系統提示、目標代碼與觸發內容、parent 壓縮摘要、有界的近期視窗（只含純文字對話）；
  不取 parent 的工具呼叫與結果訊息，也不取待合併的 aggregation。迴圈期間 parent 的新訊息不自動進 child。
- Core 的 checkpoint 只存 ``child_context_ref``、``child_revision``、``history_hash``（內容走 child session，D33）。
  resume 時取回 child 並比對 hash，遺失或不一致一律 ``ContextLost``（fail-closed，不以不完整歷史續跑）。
- merge-back 一次、冪等（鍵 ``loop_return:{run_id}``）：``summary``（有長度上限）／``result``（只有已實際交付才寫）／
  ``trail_ref``（Run 軌跡的引用）。思考文字、原始工具結果、中間訊息都不回饋。失敗／取消／預算耗盡只回一則狀態註記。
  park 時 parent 只收一則冪等的狀態標記（鍵 ``loop_status:{run_id}:{state}``）。
- 迴圈內壓縮（丟舊 thought、大型結果換摘要＋ref、最後才壓對話）尚未實作；``child_revision`` 目前等於 child 的儲存次數。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from uep.models.port import ChatMessage, history_hash

SUMMARY_MAX_CHARS = 400
RECENT_WINDOW_DEFAULT = 8


class ContextLost(Exception):
    """child context 遺失或 ``history_hash`` 不一致：Run 以 ``FAILED(context_lost)`` 結束，不續跑。"""


def child_ref(run_id: str) -> str:
    return f"loop:{run_id}"


@dataclass(frozen=True)
class ParentSnapshot:
    """fork 當下從 parent 取得的視圖。``recent`` 必須由 parent 端依 audience／scope 預先篩過（只縮不放，E-6）。"""

    parent_id: str
    revision: int
    summary: str = ""
    recent: tuple[ChatMessage, ...] = ()


@dataclass(frozen=True)
class ForkManifest:
    """child 記錄「取了什麼」：引用與計數，不含內容。"""

    parent_context_id: str
    parent_revision: int
    persona_version: int
    scope_ref: str
    taken: tuple[str, ...]


@dataclass(frozen=True)
class ChildContext:
    ref: str
    revision: int
    messages: tuple[ChatMessage, ...]
    manifest: ForkManifest


@dataclass(frozen=True)
class MergeBack:
    """寫進 parent 的內容（§4.10 的三部分或狀態註記）。"""

    run_id: str
    scope_ref: str
    state: str  # completed｜failed｜cancelled｜budget_exhausted 等 LoopOutcome.state
    reason_code: str | None = None
    summary: str | None = None  # 來源標記 loop:<run_id>
    result: str | None = None  # 已實際交付才有值
    trail_ref: str | None = None


class LoopContextStore(Protocol):
    """ContextManager 側的 child session 儲存與 parent 寫入介面（呼叫端以 echo_thought_core 實作）。"""

    def create_child(self, ref: str, messages: Sequence[ChatMessage], manifest: ForkManifest) -> int:
        """建立 child，回傳 revision（首次為 1）。"""
        ...

    def save_child(self, ref: str, messages: Sequence[ChatMessage]) -> int:
        """覆寫 child 的內容，回傳新的 revision。"""
        ...

    def load_child(self, ref: str) -> tuple[list[ChatMessage], int] | None:
        """取回 ``(messages, revision)``；遺失回 None。"""
        ...

    def merge_into_parent(self, key: str, parent_id: str, payload: MergeBack) -> bool:
        """以 ``key`` 冪等寫入 parent；已寫過回 False（不重複寫入）。"""
        ...

    def mark_parent_status(self, key: str, parent_id: str, state: str, reason_code: str | None) -> bool:
        """parent 的狀態標記（例如等待核准）；以 ``key`` 冪等。"""
        ...


def _plain_dialogue(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """近期視窗只保留純文字的 user／assistant 對話；工具呼叫與結果訊息不進 child。"""
    return [m for m in messages if m.role in ("user", "assistant") and not m.tool_calls and m.content]


def fork_child(
    store: LoopContextStore, run_id: str, *, parent: ParentSnapshot, system_text: str, goal_code: str,
    trigger_text: str = "", persona_version: int, scope_ref: str, recent_limit: int = RECENT_WINDOW_DEFAULT,
) -> ChildContext:
    """Run 啟動時建立 child。``system_text`` 是已渲染的 Persona／工具用法提示（由呼叫端以 ``persona_version`` 取得，
    不從 parent 繼承）；``trigger_text`` 只有 ``user_turn`` 才有（觸發的那句使用者原文）。"""
    recent = _plain_dialogue(parent.recent)[-recent_limit:] if recent_limit > 0 else []
    messages: list[ChatMessage] = [ChatMessage("system", system_text)]
    taken = ["persona", f"goal:{goal_code}"]
    if parent.summary:
        messages.append(ChatMessage("system", f"[parent 摘要] {parent.summary}"))
        taken.append("summary")
    messages.extend(recent)
    taken.append(f"recent:{len(recent)}")
    messages.append(ChatMessage("system", f"目標代碼：{goal_code}"))
    if trigger_text:
        messages.append(ChatMessage("user", trigger_text))
        taken.append("trigger_text")
    manifest = ForkManifest(parent.parent_id, parent.revision, persona_version, scope_ref, tuple(taken))
    ref = child_ref(run_id)
    revision = store.create_child(ref, messages, manifest)
    return ChildContext(ref, revision, tuple(messages), manifest)


def persist_child(store: LoopContextStore, ref: str, messages: Sequence[ChatMessage]) -> tuple[int, str]:
    """寫回 child，回傳 ``(child_revision, history_hash)``（checkpoint 只存這兩者與 ref）。"""
    return store.save_child(ref, messages), history_hash(messages)


def load_verified(store: LoopContextStore, ref: str, expected_hash: str) -> tuple[list[ChatMessage], int]:
    """resume 取回 child 並比對 hash；遺失或不一致拋 ``ContextLost``（log 與例外只含引用，不含內容）。"""
    loaded = store.load_child(ref)
    if loaded is None:
        raise ContextLost(f"child context 遺失：{ref}")
    messages, revision = loaded
    if history_hash(messages) != expected_hash:
        raise ContextLost(f"child context 的 history_hash 不一致：{ref}")
    return messages, revision


def clip_summary(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_MAX_CHARS else text[: SUMMARY_MAX_CHARS - 1] + "…"


def merge_back(
    store: LoopContextStore, *, run_id: str, parent_id: str, scope_ref: str, state: str,
    reason_code: str | None = None, summary: str = "", result: str | None = None, delivered: bool = False,
    trail_ref: str | None = None, partial: bool = False,
) -> bool:
    """Run 終態時回饋 parent：一次、冪等。``state=completed``（或預算中斷的 ``partial``）才帶 summary／result／trail_ref，
    且 ``result`` 只有 ``delivered`` 為真（已實際交付）才寫入；其餘狀態只寫狀態註記。回傳是否為第一次寫入。"""
    if state == "completed" or partial:
        payload = MergeBack(
            run_id, scope_ref, state, reason_code, summary=f"loop:{run_id} {clip_summary(summary)}".strip(),
            result=result if delivered and result else None, trail_ref=trail_ref,
        )
    else:
        payload = MergeBack(run_id, scope_ref, state, reason_code)
    return store.merge_into_parent(f"loop_return:{run_id}", parent_id, payload)


def mark_parked(store: LoopContextStore, *, run_id: str, parent_id: str, state: str = "awaiting_approval") -> bool:
    return store.mark_parent_status(f"loop_status:{run_id}:{state}", parent_id, state, None)

