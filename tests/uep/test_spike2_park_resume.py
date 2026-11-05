"""Spike 2（Agent 架構提案 §7.4／§8.2）：park／resume 離線驗證，不需要 DB。

以 in-memory ``Store`` 模擬 Core 的持久層（child session、checkpoint、receipt、approval），以 fake executor 執行工具，
以 PydanticAI 的 FunctionModel 驅動 ``PydanticAIModelPort``（遇工具呼叫即結束 run）。
「崩潰」＝丟棄 ``Driver`` 與 ``Port`` 物件，只留 ``Store`` 內的資料，再由新的 ``Driver`` 重建。

- S2-1 崩潰重建後 history 的 hash 一致，且重建後送進 SDK 的訊息逐則相等
- S2-2 重複核准／重放 resume／併發 resume／副作用後崩潰，副作用恆只有一次
- S2-3 resume 後送往 SDK 的工具集合 ⊆ 凍結集合（AL-3）

這是 Spike 用的最小 harness：正式的 ``park_run``、否決路徑、child session 儲存屬切片 D／E，這裡不實作。
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from uep.agent.core import stable_action_id
from uep.models import (
    ChatMessage,
    ModelRequest,
    history_hash,
    messages_from_json,
    messages_to_json,
)
from uep.models.pydantic_ai_port import PydanticAIModelPort
from uep.tasks.manager import CHECKPOINT_KEYS, check_safe_value, params_hash


def _tool(name: str, version: int = 1) -> dict[str, Any]:
    return {
        "tool": name, "version": version, "description": f"{name} 說明", "variant": None,
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    }


FROZEN_CAP = {"tools": [_tool("deep_recall"), _tool("desktop_write"), _tool("member_recall")]}
NEEDS_APPROVAL = {"desktop_write"}
RUN_ID, STEP_ID = "run_spike2", "step_1"


class ContextLost(Exception):
    """child context 遺失或 hash 不一致：fail-closed（Run FAILED(context_lost)），不以不完整歷史續跑。"""


class StaleEpoch(Exception):
    pass


@dataclass
class Store:
    """in-memory 的持久層；崩潰後唯一存活的東西。"""

    children: dict[str, str] = field(default_factory=dict)  # child_context_ref -> 正規化 JSON
    checkpoint: dict[str, Any] | None = None
    epoch: int = 1
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)  # action_id -> receipt
    marks: dict[str, str] = field(default_factory=dict)  # action_id -> EXECUTING
    approvals: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)  # 真正發生的副作用（action_id）
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_approval(self, action_hash: str) -> None:
        self.approvals.append({"action_hash": action_hash})  # 重送核准只是多一筆，不得造成多一次副作用

    def bump_epoch(self) -> int:
        with self.lock:
            self.epoch += 1
            return self.epoch

    def save_checkpoint(self, cp: dict[str, Any], *, epoch: int) -> None:
        with self.lock:
            if epoch != self.epoch:
                raise StaleEpoch(f"目前 epoch={self.epoch}，收到 {epoch}")
            check_safe_value(cp, "checkpoint")
            self.checkpoint = dict(cp)


class FakeExecutor:
    """fake executor：以 action_id 冪等（receipt）；EXECUTING 後崩潰 ⇒ 之後視為 UNKNOWN，不重做。"""

    def __init__(self, store: Store, crash_after_effect: bool = False) -> None:
        self.store = store
        self.crash_after_effect = crash_after_effect

    def execute(self, action_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        with self.store.lock:
            if action_id in self.store.receipts:
                return {**self.store.receipts[action_id], "replayed": True}
            if self.store.marks.get(action_id) == "EXECUTING":
                return {"status": "unknown", "reason_code": "unknown_unresolved"}  # 另一個 worker 在做，或上次崩潰
            self.store.marks[action_id] = "EXECUTING"
        self.store.side_effects.append(action_id)  # 副作用
        if self.crash_after_effect:
            raise RuntimeError("副作用後、寫 receipt 前崩潰")
        receipt = {"status": "ok", "content": f"{tool}:{args.get('q', '')}"}
        with self.store.lock:
            self.store.receipts[action_id] = receipt
        return receipt


READ_ACTION = stable_action_id(RUN_ID, STEP_ID, 0)  # deep_recall（park 前已執行）
WRITE_ACTION = stable_action_id(RUN_ID, STEP_ID, 1)  # desktop_write（需核准，park 的對象）


def _result_msg(call_id: str, result: dict[str, Any]) -> ChatMessage:
    text = result["content"] if result.get("status") == "ok" else f"tool {result['status']}: {result.get('reason_code', '')}"
    return ChatMessage("tool", text, tool_call_id=call_id)


def sdk_shape(messages: list[Any]) -> list[tuple[str, list[tuple[Any, ...]]]]:
    """SDK 訊息的結構投影（去掉 timestamp 等隨執行而變的欄位）。"""
    out = []
    for m in messages:
        parts = []
        for p in m.parts:
            parts.append((
                type(p).__name__, getattr(p, "content", None), getattr(p, "tool_name", None),
                getattr(p, "tool_call_id", None), repr(getattr(p, "args", None)),
            ))
        out.append((type(m).__name__, parts))
    return out


class Model:
    """FunctionModel 的腳本：第一輪吐文字＋三個工具呼叫（含需核准者）；有 tool return 之後吐最終文字。

    ``seen``：每次被呼叫時，SDK 收到的 (訊息結構, 工具名稱集合)。"""

    def __init__(self, late_call: str | None = None) -> None:
        self.seen: list[tuple[list[Any], set[str]]] = []
        self.late_call = late_call  # 第二輪故意再叫一個工具（S2-3：凍結集合外的呼叫）

    async def fn(self, messages, info: AgentInfo):
        self.seen.append((sdk_shape(messages), {t.name for t in info.function_tools}))
        has_return = any(type(p).__name__ == "ToolReturnPart" for m in messages for p in m.parts)
        if not has_return:
            yield "我先查一下"
            yield {1: DeltaToolCall(name="deep_recall", json_args='{"q": "貓"}', tool_call_id="call_0")}
            yield {2: DeltaToolCall(name="desktop_write", json_args='{"q": "筆記"}', tool_call_id="call_1")}
            yield {3: DeltaToolCall(name="member_recall", json_args='{"q": "艾斯"}', tool_call_id="call_2")}
        elif self.late_call:
            yield {1: DeltaToolCall(name=self.late_call, json_args='{"q": "x"}', tool_call_id="call_late")}
        else:
            yield "完成了"

    def port(self) -> PydanticAIModelPort:
        return PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=self.fn))


class Driver:
    """最小 Agent 迴圈：遇工具呼叫即結束 run -> 依 index 逐項處理 -> 需核准者 park；resume 以新 Driver 重建。"""

    def __init__(self, store: Store, port: PydanticAIModelPort, executor: FakeExecutor, registry: dict[str, Any]) -> None:
        self.store, self.port, self.executor = store, port, executor
        self.registry = registry  # resume 時「當下」的註冊表（工具名稱 -> 版本）

    @staticmethod
    def frozen(cap: dict[str, Any]) -> dict[str, int]:
        return {t["tool"]: t["version"] for t in cap["tools"]}

    async def _stream(self, messages: list[ChatMessage], cap: dict[str, Any], attempt: str):
        req = ModelRequest(attempt_id=attempt, messages=tuple(messages), capability_set=cap)
        return [e async for e in self.port.stream(req)]

    async def run_until_park(self, cap: dict[str, Any]) -> list[ChatMessage]:
        messages = [ChatMessage("system", "你是助理"), ChatMessage("user", "幫我整理並寫筆記")]
        events = await self._stream(messages, cap, f"{RUN_ID}:{STEP_ID}:a1")
        assert events[-1].finish_reason == "tool_calls"
        intents = sorted((e for e in events if e.kind == "tool_intent"), key=lambda e: e.index)
        text = "".join(e.text for e in events if e.kind == "text_delta")
        messages.append(ChatMessage("assistant", text, tool_calls=tuple(
            {"id": i.call_id, "name": i.tool, "args": dict(i.args or {})} for i in intents
        )))
        pending: list[str] = []
        consumed: list[str] = []
        for ordinal, i in enumerate(intents):  # 依 index（§6.4-7）
            action_id = stable_action_id(RUN_ID, STEP_ID, ordinal)
            if pending:  # 已 park：同批後續項不執行（§6.4 屏障）
                continue
            if i.tool in NEEDS_APPROVAL:
                pending.append(action_id)
                continue
            messages.append(_result_msg(i.call_id, self.executor.execute(action_id, i.tool, dict(i.args or {}))))
            consumed.append(action_id)
        assert pending, "腳本應該在 desktop_write 處 park"
        ref = f"loop:{RUN_ID}"
        self.store.children[ref] = messages_to_json(messages)  # child session（ContextManager 的 session store）
        self.store.save_checkpoint({
            "next_step_semantic": f"await_approval:{pending[0]}",
            "pending_approvals": pending, "consumed_action_ids": consumed,
            # 以下為契約 §4.10／§8.2 要求、但目前 CHECKPOINT_KEYS 沒有的鍵（切片 D 需擴充）
            "child_context_ref": ref, "child_revision": 1, "history_hash": history_hash(messages),
            "frozen_tools": self.frozen(cap), "step_id": STEP_ID,
        }, epoch=self.store.epoch)
        return messages

    async def resume(self, wide_cap: dict[str, Any] | None = None) -> tuple[list[ChatMessage], list[Any]]:
        """回傳 (resume 前重建的 history, 續跑後的模型事件)。"""
        cp = self.store.checkpoint
        assert cp is not None
        epoch = self.store.bump_epoch()
        raw = self.store.children.get(cp["child_context_ref"])
        if raw is None:
            raise ContextLost("child context 遺失")
        messages = messages_from_json(raw)
        if history_hash(messages) != cp["history_hash"]:
            raise ContextLost("history_hash 不一致")
        frozen: dict[str, int] = cp["frozen_tools"]
        if wide_cap is not None and not set(self.frozen(wide_cap)) <= set(frozen):
            raise ValueError("resume 的工具集合超出凍結集合（AL-3）")
        # 只收窄：凍結集合 ∩ 當下註冊表（名稱與版本都相同）
        allowed = {n for n, v in frozen.items() if self.registry.get(n) == v}
        cap = {"tools": [t for t in FROZEN_CAP["tools"] if t["tool"] in allowed]}
        rebuilt = list(messages)

        assistant = next(m for m in reversed(messages) if m.role == "assistant")
        done = {m.tool_call_id for m in messages if m.role == "tool"}
        for ordinal, call in enumerate(assistant.tool_calls):
            if call["id"] in done:
                continue
            action_id = stable_action_id(RUN_ID, cp["step_id"], ordinal)
            if call["name"] not in allowed:
                result: dict[str, Any] = {"status": "blocked", "reason_code": "tool_not_in_snapshot"}
            elif action_id in cp["pending_approvals"]:
                want = params_hash(call["name"], frozen[call["name"]], call["args"])
                if want not in {a["action_hash"] for a in self.store.approvals}:
                    result = {"status": "blocked", "reason_code": "approval_missing"}
                else:
                    result = self.executor.execute(action_id, call["name"], dict(call["args"]))
            else:
                result = {"status": "blocked", "reason_code": "deferred_by_approval_wait"}
            messages.append(_result_msg(call["id"], result))
        events = await self._stream(messages, cap, f"{RUN_ID}:{cp['step_id']}:e{epoch}")
        self._events = events
        return rebuilt, events


REGISTRY = {"deep_recall": 1, "desktop_write": 1, "member_recall": 1}


def _approved_store(store: Store) -> None:
    store.record_approval(params_hash("desktop_write", 1, {"q": "筆記"}))


async def _park(late_call: str | None = None) -> tuple[Store, list[ChatMessage], Model]:
    store = Store()
    model = Model(late_call)
    driver = Driver(store, model.port(), FakeExecutor(store), dict(REGISTRY))
    history = await driver.run_until_park(FROZEN_CAP)
    return store, history, model


# ───────────────────────── S2-1 ─────────────────────────

async def test_s2_1_rebuild_after_crash_history_equal_and_hash_equal():
    store, before, model1 = await _park()
    assert model1.seen[0][1] == {"deep_recall", "desktop_write", "member_recall"}
    hash_before = store.checkpoint["history_hash"]  # type: ignore[index]
    assert hash_before == history_hash(before)
    del before  # 崩潰前記憶體中的 history 之後只用 expected 對照
    expected = messages_from_json(store.children["loop:run_spike2"])

    # 崩潰：丟掉 Driver／Port／Model，只留 Store
    _approved_store(store)
    model2 = Model()
    driver = Driver(store, model2.port(), FakeExecutor(store), dict(REGISTRY))
    rebuilt, events = await driver.resume()
    assert rebuilt == expected  # dataclass 逐則相等
    assert [m.role for m in rebuilt] == ["system", "user", "assistant", "tool"]
    assert history_hash(rebuilt) == hash_before
    assert events[-1].finish_reason == "stop"
    assert "".join(e.text for e in events if e.kind == "text_delta") == "完成了"


async def test_s2_1_sdk_messages_after_resume_are_message_by_message_equal():
    """Core 重組的 history 轉成 SDK 訊息後，與「不崩潰、直接在記憶體續跑」得到的逐則相等（含 tool_call_id 與 args）。"""
    store, before, _ = await _park()
    _approved_store(store)
    # 對照組：不經 JSON／儲存，直接用記憶體 history 補上同樣的工具結果後送模型
    ref_model = Model()
    ref_driver = Driver(store, ref_model.port(), FakeExecutor(Store()), dict(REGISTRY))
    direct = list(before) + [
        ChatMessage("tool", "desktop_write:筆記", tool_call_id="call_1"),
        ChatMessage("tool", "tool blocked: deferred_by_approval_wait", tool_call_id="call_2"),
    ]
    await ref_driver._stream(direct, FROZEN_CAP, "ref")

    model2 = Model()
    await Driver(store, model2.port(), FakeExecutor(store), dict(REGISTRY)).resume()
    assert model2.seen[-1][0] == ref_model.seen[-1][0]
    shape = model2.seen[-1][0]
    # ToolCallPart／ToolReturnPart 的 id 在 resume 後保持對應
    calls = [p for _, parts in shape for p in parts if p[0] == "ToolCallPart"]
    returns = [p for _, parts in shape for p in parts if p[0] == "ToolReturnPart"]
    assert [c[3] for c in calls] == ["call_0", "call_1", "call_2"]
    assert [r[3] for r in returns] == ["call_0", "call_1", "call_2"]  # 依 index 回餵（§6.4-7）
    assert [r[2] for r in returns] == ["deep_recall", "desktop_write", "member_recall"]


def test_s2_1_hash_is_canonical_and_sensitive():
    a = [ChatMessage("assistant", "x", tool_calls=({"id": "c", "name": "t", "args": {"a": 1, "b": 2}},))]
    b = [ChatMessage("assistant", "x", tool_calls=({"name": "t", "id": "c", "args": {"b": 2, "a": 1}},))]
    assert history_hash(a) == history_hash(b)  # 鍵順序不影響
    c = [ChatMessage("assistant", "x", tool_calls=({"id": "c", "name": "t", "args": {"a": 1, "b": 3}},))]
    assert history_hash(a) != history_hash(c)
    d = [ChatMessage("assistant", "x。", tool_calls=a[0].tool_calls)]
    assert history_hash(a) != history_hash(d)
    # JSON 往返（含中文）不變
    assert messages_from_json(messages_to_json(a)) == a
    assert history_hash(messages_from_json(messages_to_json(a))) == history_hash(a)


def test_s2_1_hash_changes_for_every_field_mutation():
    """逐欄位 mutate：role、content、tool_call_id、tool_calls 的 id／name／args、順序、數量、None 與 "" 的差別。"""
    call = {"id": "c1", "name": "t", "args": {"a": 1}}
    base = [
        ChatMessage("user", "hi"),
        ChatMessage("assistant", "x", tool_calls=(call,)),
        ChatMessage("tool", "r", tool_call_id="c1"),
    ]
    h0 = history_hash(base)

    def mutated(i: int, msg: ChatMessage) -> list[ChatMessage]:
        return [*base[:i], msg, *base[i + 1:]]

    variants = {
        "role": mutated(0, ChatMessage("system", "hi")),
        "content": mutated(0, ChatMessage("user", "hi!")),
        "tool_call_id": mutated(2, ChatMessage("tool", "r", tool_call_id="c2")),
        "tool_call_id None vs ''": mutated(2, ChatMessage("tool", "r", tool_call_id="")),
        "tool_call_id 有 vs None": mutated(2, ChatMessage("tool", "r", tool_call_id=None)),
        "call id": mutated(1, ChatMessage("assistant", "x", tool_calls=({**call, "id": "c9"},))),
        "call name": mutated(1, ChatMessage("assistant", "x", tool_calls=({**call, "name": "u"},))),
        "call args 值": mutated(1, ChatMessage("assistant", "x", tool_calls=({**call, "args": {"a": 2}},))),
        "call args 鍵": mutated(1, ChatMessage("assistant", "x", tool_calls=({**call, "args": {"b": 1}},))),
        "call 數量": mutated(1, ChatMessage("assistant", "x", tool_calls=(call, call))),
        "call 移除": mutated(1, ChatMessage("assistant", "x")),
        "順序": [base[1], base[0], base[2]],
        "少一則": base[:2],
        "多一則": [*base, ChatMessage("assistant", "")],
        "content None-like 空字串": mutated(0, ChatMessage("user", "")),
    }
    for name, msgs in variants.items():
        assert history_hash(msgs) != h0, name
    assert len({history_hash(m) for m in variants.values()} | {h0}) == len(variants) + 1  # 彼此也不相撞
    # 往返保留 tool_call_id（含 None 與 ""）與 tool_calls
    back = messages_from_json(messages_to_json([*base, ChatMessage("tool", "z", tool_call_id="")]))
    assert [m.tool_call_id for m in back] == [None, None, "c1", ""]
    assert back[1].tool_calls[0]["id"] == "c1" and back[1].tool_calls[0]["args"] == {"a": 1}
    with pytest.raises(ValueError):
        messages_to_json([ChatMessage("assistant", "x", tool_calls=({"id": "c", "name": "t", "args": {"a": float("nan")}},))])


async def test_s2_1_tampered_or_lost_child_fails_closed_without_model_call_or_effect():
    store, _, _ = await _park()
    _approved_store(store)
    ref = "loop:run_spike2"
    good = store.children[ref]

    store.children[ref] = good.replace("貓", "狗")  # 內容被改
    model = Model()
    with pytest.raises(ContextLost):
        await Driver(store, model.port(), FakeExecutor(store), dict(REGISTRY)).resume()
    del store.children[ref]  # 遺失
    with pytest.raises(ContextLost):
        await Driver(store, model.port(), FakeExecutor(store), dict(REGISTRY)).resume()
    assert model.seen == [] and store.side_effects == [READ_ACTION]  # 只有 park 前的唯讀那一次


def test_s2_1_checkpoint_contract_gap_is_known():
    """已存在的 §6.4 鍵與 Spike 新增鍵分開：新增鍵目前不在 ``CHECKPOINT_KEYS``，``save_checkpoint`` 會以 PolicyViolation 拒絕。
    切片 D 必須擴充 CHECKPOINT_KEYS；擴充後本測試的 extra 會變空，屆時把這個測試改成斷言為空。"""
    cp_keys = {
        "next_step_semantic", "pending_approvals", "consumed_action_ids", "child_context_ref", "child_revision",
        "history_hash", "frozen_tools", "step_id",
    }
    assert sorted(cp_keys - CHECKPOINT_KEYS) == [
        "child_context_ref", "child_revision", "frozen_tools", "history_hash", "step_id",
    ]


# ───────────────────────── S2-2 ─────────────────────────

async def test_s2_2_duplicate_approval_and_replayed_resume_produce_one_side_effect():
    store, _, _ = await _park()
    _approved_store(store)
    _approved_store(store)  # 重送核准
    for _ in range(3):  # 重放 resume（每次都是崩潰後的新 Driver、新 epoch）
        await Driver(store, Model().port(), FakeExecutor(store), dict(REGISTRY)).resume()
    assert store.side_effects == [READ_ACTION, WRITE_ACTION]  # 各一次；唯讀的也沒有在 resume 時重做
    assert store.epoch == 4  # 1 + 3 次接手


async def test_s2_2_concurrent_resume_produces_one_side_effect():
    store, _, _ = await _park()
    _approved_store(store)
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            asyncio.run(Driver(store, Model().port(), FakeExecutor(store), dict(REGISTRY)).resume())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert store.side_effects.count(WRITE_ACTION) == 1 and store.side_effects.count(READ_ACTION) == 1


async def test_s2_2_crash_after_effect_before_receipt_is_unknown_not_redone():
    store, _, _ = await _park()
    _approved_store(store)
    with pytest.raises(RuntimeError):
        await Driver(store, Model().port(), FakeExecutor(store, crash_after_effect=True), dict(REGISTRY)).resume()
    model = Model()
    _, events = await Driver(store, model.port(), FakeExecutor(store), dict(REGISTRY)).resume()
    assert store.side_effects.count(WRITE_ACTION) == 1  # 沒有重做
    assert events[-1].finish_reason == "stop"
    last_shape = model.seen[-1][0]
    ret = next(p for _, parts in last_shape for p in parts if p[0] == "ToolReturnPart" and p[3] == "call_1")
    assert "unknown" in ret[1]  # 模型看到的是 unknown 短碼，不會以新 action 重送


async def test_s2_2_without_approval_nothing_executes():
    store, _, _ = await _park()
    model = Model()
    await Driver(store, model.port(), FakeExecutor(store), dict(REGISTRY)).resume()
    assert WRITE_ACTION not in store.side_effects
    ret = next(p for _, parts in model.seen[-1][0] for p in parts if p[0] == "ToolReturnPart" and p[3] == "call_1")
    assert "approval_missing" in ret[1]


async def test_s2_2_stale_epoch_worker_cannot_write_checkpoint():
    store, _, _ = await _park()
    old_epoch = store.epoch
    store.bump_epoch()  # 另一個 worker 已接手
    with pytest.raises(StaleEpoch):
        store.save_checkpoint({"next_step_semantic": "x"}, epoch=old_epoch)


# ───────────────────────── S2-3 ─────────────────────────

async def test_s2_3_resume_tool_set_is_subset_of_frozen_when_registry_grows_or_shrinks():
    frozen_names = set(Driver.frozen(FROZEN_CAP))
    # 註冊表新增工具：SDK 看不到
    store, _, _ = await _park()
    _approved_store(store)
    grown = {**REGISTRY, "brand_new_tool": 1}
    model = Model()
    await Driver(store, model.port(), FakeExecutor(store), grown).resume()
    assert model.seen[-1][1] <= frozen_names and "brand_new_tool" not in model.seen[-1][1]
    assert model.seen[-1][1] == frozen_names

    # 註冊表移除／升版：收窄，且受影響的待處理 action 不執行
    for changed in ({"deep_recall": 1, "member_recall": 1}, {**REGISTRY, "desktop_write": 2}):
        store, _, _ = await _park()
        _approved_store(store)
        model = Model()
        await Driver(store, model.port(), FakeExecutor(store), changed).resume()
        assert model.seen[-1][1] < frozen_names and "desktop_write" not in model.seen[-1][1]
        assert WRITE_ACTION not in store.side_effects
        ret = next(p for _, parts in model.seen[-1][0] for p in parts if p[0] == "ToolReturnPart" and p[3] == "call_1")
        assert "tool_not_in_snapshot" in ret[1]


async def test_s2_3_widening_the_capability_set_on_resume_is_refused():
    store, _, _ = await _park()
    wide = {"tools": [*FROZEN_CAP["tools"], _tool("brand_new_tool")]}
    model = Model()
    with pytest.raises(ValueError, match="凍結集合"):
        await Driver(store, model.port(), FakeExecutor(store), dict(REGISTRY)).resume(wide_cap=wide)
    assert model.seen == []


async def test_s2_3_model_calling_tool_outside_snapshot_never_reaches_executor():
    """續跑的模型亂叫凍結集合外的工具：該名稱不在 SDK 的工具清單內，SDK 自己把它當未知工具、重試一次後放棄
    （``UnexpectedModelBehavior``）。Port 以 ``finish=error`` 收尾、不會產生可執行（args_complete）的 tool_intent，所以不會進到 Core 的 executor。

    限制（寫進 §7.4）：這個重試發生在 SDK 內部，同一個 run 會多打一次模型請求（本例 seen 為 2）。"""
    store, _, _ = await _park(late_call="brand_new_tool")
    _approved_store(store)
    model = Model(late_call="brand_new_tool")
    _, events = await Driver(store, model.port(), FakeExecutor(store), dict(REGISTRY)).resume()
    assert model.seen[-1][1] <= set(Driver.frozen(FROZEN_CAP))
    # 兩次重試的 tool_call_delta 同 index，被 ToolCallAssembler 串在一起 -> 壞 JSON：args_complete=False，Core 一律拒絕、不執行
    assert all(not e.args_complete and e.error_code == "invalid_tool_args" for e in events if e.kind == "tool_intent")
    assert events[-1].finish_reason == "error"
    assert len(model.seen) == 2  # resume 後的第一次 + SDK 內部重試（同一個 run 內多打一次模型）
    assert store.side_effects == [READ_ACTION, WRITE_ACTION]
