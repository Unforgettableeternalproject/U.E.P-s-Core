"""
Prompt 介面測試

驗證：
- PromptContext dataclass 預設值和欄位
- PromptBuilder Protocol 契約
- DefaultPromptBuilder 行為
- 佔位符常數存在性
- ContextManager prompt_builder 注入
"""

from __future__ import annotations

import pytest

from echo_thought_core import CompactionConfig, ContextManager
from echo_thought_core.prompt import (
    ALL_MODES,
    MODE_CHAT,
    MODE_DIRECT,
    MODE_WORK,
    PLACEHOLDER_INSTRUCTIONS,
    PLACEHOLDER_MEMORY,
    PLACEHOLDER_PERSONA,
    PLACEHOLDER_SACRED,
    PLACEHOLDER_SYSTEM_VALUES,
    PLACEHOLDER_WORKFLOW,
    SECTION_MARKER_INSTRUCTIONS,
    SECTION_MARKER_MEMORY,
    SECTION_MARKER_PERSONA,
    SECTION_MARKER_STATUS,
    SECTION_MARKER_WORKFLOW,
    DefaultPromptBuilder,
    PromptBuilder,
    PromptContext,
)


# ── PromptContext ────────────────────────────────────────────────────────────


class TestPromptContext:
    """T-03-1: PromptContext dataclass"""

    def test_empty_instance(self):
        """可建立空實例（所有欄位有預設值）"""
        ctx = PromptContext()
        assert ctx.persona_text == ""
        assert ctx.status_values == {}
        assert ctx.memory_context == ""
        assert ctx.workflow_context is None
        assert ctx.sacred_content == ""
        assert ctx.custom_instructions == ""
        assert ctx.mode == "chat"

    def test_partial_fill(self):
        """可部分填入"""
        ctx = PromptContext(
            persona_text="I am U.E.P.",
            memory_context="User likes dark mode.",
        )
        assert ctx.persona_text == "I am U.E.P."
        assert ctx.memory_context == "User likes dark mode."
        assert ctx.workflow_context is None  # 未填入

    def test_full_fill(self):
        """可完整填入"""
        ctx = PromptContext(
            persona_text="persona",
            status_values={"mood": 0.8, "helpfulness": 0.9},
            memory_context="memory",
            workflow_context="step 3 of 5",
            sacred_content="sacred",
            custom_instructions="custom",
            mode="work",
        )
        assert ctx.mode == "work"
        assert ctx.status_values["mood"] == 0.8
        assert ctx.workflow_context == "step 3 of 5"

    def test_status_values_independent(self):
        """不同實例的 status_values 互相獨立"""
        ctx1 = PromptContext()
        ctx2 = PromptContext()
        ctx1.status_values["mood"] = 0.5
        assert "mood" not in ctx2.status_values


# ── 佔位符常數 ──────────────────────────────────────────────────────────────


class TestPromptConstants:
    """T-03-2: 佔位符常數"""

    def test_placeholders_are_braced(self):
        """佔位符使用 {name} 格式"""
        for ph in [
            PLACEHOLDER_PERSONA,
            PLACEHOLDER_SYSTEM_VALUES,
            PLACEHOLDER_MEMORY,
            PLACEHOLDER_WORKFLOW,
            PLACEHOLDER_SACRED,
            PLACEHOLDER_INSTRUCTIONS,
        ]:
            assert ph.startswith("{") and ph.endswith("}"), f"{ph} not braced"

    def test_mode_constants(self):
        """模式常數正確"""
        assert MODE_CHAT == "chat"
        assert MODE_WORK == "work"
        assert MODE_DIRECT == "direct"
        assert set(ALL_MODES) == {"chat", "work", "direct"}

    def test_section_markers_exist(self):
        """段落標記存在且為字串"""
        for marker in [
            SECTION_MARKER_PERSONA,
            SECTION_MARKER_STATUS,
            SECTION_MARKER_MEMORY,
            SECTION_MARKER_WORKFLOW,
            SECTION_MARKER_INSTRUCTIONS,
        ]:
            assert isinstance(marker, str) and len(marker) > 0


# ── PromptBuilder Protocol ──────────────────────────────────────────────────


class TestPromptBuilderProtocol:
    """T-03-3: PromptBuilder Protocol"""

    def test_default_is_prompt_builder(self):
        """DefaultPromptBuilder 滿足 PromptBuilder Protocol"""
        builder = DefaultPromptBuilder()
        assert isinstance(builder, PromptBuilder)

    def test_custom_builder_protocol(self):
        """自訂類別只要實作方法就滿足 Protocol"""

        class MyBuilder:
            def build_system_prompt(self, mode, context):
                return "custom"

            def build_status_injection(self, status):
                return "injected"

            def get_available_modes(self):
                return ["chat", "work"]

        builder = MyBuilder()
        assert isinstance(builder, PromptBuilder)


# ── DefaultPromptBuilder ────────────────────────────────────────────────────


class TestDefaultPromptBuilder:
    """T-03-3: DefaultPromptBuilder 行為"""

    def test_empty_context(self):
        """空 context 回傳空字串"""
        builder = DefaultPromptBuilder()
        result = builder.build_system_prompt("chat", PromptContext())
        assert result == ""

    def test_persona_only(self):
        """只有 persona"""
        builder = DefaultPromptBuilder()
        ctx = PromptContext(persona_text="You are U.E.P.")
        result = builder.build_system_prompt("chat", ctx)
        assert result == "You are U.E.P."

    def test_persona_and_memory(self):
        """persona + memory 串接"""
        builder = DefaultPromptBuilder()
        ctx = PromptContext(
            persona_text="You are U.E.P.",
            memory_context="User prefers dark mode.",
        )
        result = builder.build_system_prompt("chat", ctx)
        assert "You are U.E.P." in result
        assert "User prefers dark mode." in result
        assert "\n\n" in result  # 用雙換行分隔

    def test_all_fields(self):
        """所有欄位都串接"""
        builder = DefaultPromptBuilder()
        ctx = PromptContext(
            persona_text="persona",
            memory_context="memory",
            custom_instructions="instructions",
        )
        result = builder.build_system_prompt("chat", ctx)
        assert "persona" in result
        assert "memory" in result
        assert "instructions" in result

    def test_status_injection_empty(self):
        """預設不注入狀態"""
        builder = DefaultPromptBuilder()
        assert builder.build_status_injection({"mood": 0.8}) == ""

    def test_available_modes(self):
        """僅支援 default 模式"""
        builder = DefaultPromptBuilder()
        assert builder.get_available_modes() == ["default"]

    def test_ignores_mode(self):
        """DefaultPromptBuilder 不做模式分派"""
        builder = DefaultPromptBuilder()
        ctx = PromptContext(persona_text="test")
        result_chat = builder.build_system_prompt("chat", ctx)
        result_work = builder.build_system_prompt("work", ctx)
        assert result_chat == result_work  # 不區分模式


# ── ContextManager 注入 ────────────────────────────────────────────────────


class TestContextManagerPromptBuilder:
    """T-03-5: ContextManager prompt_builder 注入"""

    def test_default_none(self):
        """預設為 None"""
        mgr = ContextManager(config=CompactionConfig(context_window_size=1000))
        assert mgr.prompt_builder is None

    def test_inject_builder(self):
        """注入 PromptBuilder"""
        builder = DefaultPromptBuilder()
        mgr = ContextManager(
            config=CompactionConfig(context_window_size=1000),
            prompt_builder=builder,
        )
        assert mgr.prompt_builder is builder

    def test_setter(self):
        """可以後續設定"""
        mgr = ContextManager(config=CompactionConfig(context_window_size=1000))
        assert mgr.prompt_builder is None

        builder = DefaultPromptBuilder()
        mgr.prompt_builder = builder
        assert mgr.prompt_builder is builder

    def test_backward_compat(self):
        """不傳 prompt_builder 時正常運作"""
        mgr = ContextManager(config=CompactionConfig(context_window_size=1000))
        # 正常操作不受影響
        mgr.add_message("user", "hello")
        msgs = mgr.get_messages()
        assert len(msgs) == 1
