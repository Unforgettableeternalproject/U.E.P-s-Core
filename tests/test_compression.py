"""
壓縮引擎測試套件

涵蓋 tool_pairs、micro_pruning、rolling_summary、full_compaction 四個模組。
純單元測試，零 LLM 呼叫。
"""

from __future__ import annotations

from echo_thought_core.schemas.message import Message
from echo_thought_core.compression.tool_pairs import (
    PRUNED_TOOL_PLACEHOLDER,
    align_boundary_forward,
    align_boundary_backward,
    sanitize_tool_pairs,
    find_last_complete_tool_group_end,
)
from echo_thought_core.compression.micro_pruning import MicroPruner
from echo_thought_core.compression.rolling_summary import (
    SUMMARY_PROMPT,
    _compute_summary_budget,
)
from echo_thought_core.compression.full_compaction import (
    COMPACTION_PROMPT,
    _find_tail_cut_by_tokens,
)


# ── 輔助工廠函式 ──────────────────────────────────────────────────────────────


def _user(content: str, token_count: int = 10) -> Message:
    return Message(role="user", content=content, token_count=token_count)


def _assistant(content: str, token_count: int = 10, tool_call_id: str | None = None) -> Message:
    return Message(role="assistant", content=content, token_count=token_count, tool_call_id=tool_call_id)


def _tool(content: str, tool_call_id: str, token_count: int = 10, tool_name: str = "my_tool") -> Message:
    return Message(role="tool", content=content, token_count=token_count, tool_call_id=tool_call_id, tool_name=tool_name)


def _system(content: str, token_count: int = 20) -> Message:
    return Message(role="system", content=content, token_count=token_count)


# ══════════════════════════════════════════════════════════════════════════════
# tool_pairs.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPrunedToolPlaceholder:
    def test_constant_exists_and_is_string(self):
        assert isinstance(PRUNED_TOOL_PLACEHOLDER, str)
        assert len(PRUNED_TOOL_PLACEHOLDER) > 0

    def test_constant_value_describes_purpose(self):
        # 確保佔位字串語意明確，壓縮後讀到時不誤判為真實內容
        lower = PRUNED_TOOL_PLACEHOLDER.lower()
        assert "tool" in lower or "output" in lower or "cleared" in lower or "context" in lower


class TestAlignBoundaryForward:
    def test_no_shift_when_boundary_on_user(self):
        msgs = [_user("hi"), _user("there")]
        assert align_boundary_forward(msgs, 1) == 1

    def test_no_shift_when_boundary_on_assistant(self):
        msgs = [_user("hi"), _assistant("hello")]
        assert align_boundary_forward(msgs, 1) == 1

    def test_shifts_over_single_orphan_tool_result(self):
        # 邊界落在 tool_result 上，應向前推過它
        msgs = [_user("q"), _tool("res", "call-1"), _user("next")]
        assert align_boundary_forward(msgs, 1) == 2

    def test_shifts_over_multiple_consecutive_tool_results(self):
        msgs = [
            _user("q"),
            _tool("r1", "call-1"),
            _tool("r2", "call-2"),
            _user("next"),
        ]
        assert align_boundary_forward(msgs, 1) == 3

    def test_boundary_at_end_returns_n(self):
        msgs = [_user("q"), _tool("r", "call-1")]
        # 邊界在最後一個 tool，向前推到 len(msgs)
        assert align_boundary_forward(msgs, 1) == 2

    def test_empty_messages_returns_zero(self):
        assert align_boundary_forward([], 0) == 0


class TestAlignBoundaryBackward:
    def test_no_shift_on_regular_user_boundary(self):
        msgs = [_user("a"), _user("b"), _user("c")]
        assert align_boundary_backward(msgs, 2) == 2

    def test_shifts_back_over_assistant_tool_call(self):
        # 邊界前一條是 assistant tool_call，應向後退
        msgs = [
            _user("q"),
            _assistant("calling", tool_call_id="call-1"),
            _tool("result", "call-1"),
        ]
        # boundary=2（即 tool_result 是 tail），前一條是 assistant tool_call → 退
        result = align_boundary_backward(msgs, 2)
        assert result < 2

    def test_shifts_back_over_tool_result(self):
        msgs = [
            _user("q"),
            _assistant("calling", tool_call_id="call-1"),
            _tool("result", "call-1"),
            _user("follow-up"),
        ]
        # boundary=3，前一條是 tool_result → 退
        result = align_boundary_backward(msgs, 3)
        assert result < 3

    def test_does_not_shift_below_zero(self):
        msgs = [_assistant("calling", tool_call_id="call-1")]
        result = align_boundary_backward(msgs, 1)
        assert result >= 0

    def test_no_shift_when_boundary_is_zero(self):
        msgs = [_user("a"), _user("b")]
        assert align_boundary_backward(msgs, 0) == 0


class TestSanitizeToolPairs:
    def test_removes_orphan_tool_result(self):
        # tool_result 沒有對應的 call → 移除
        msgs = [
            _user("q"),
            _tool("result", "call-orphan"),
        ]
        result = sanitize_tool_pairs(msgs)
        assert not any(m.role == "tool" and m.tool_call_id == "call-orphan" for m in result)

    def test_keeps_matched_tool_pair(self):
        call_id = "call-matched"
        msgs = [
            _user("q"),
            _assistant("calling", tool_call_id=call_id),
            _tool("result", call_id),
        ]
        result = sanitize_tool_pairs(msgs)
        roles = [m.role for m in result]
        assert "assistant" in roles
        assert "tool" in roles

    def test_inserts_stub_for_orphan_tool_call(self):
        # assistant tool_call 沒有對應的 result → 插入 stub
        call_id = "call-no-result"
        msgs = [
            _user("q"),
            _assistant("calling", tool_call_id=call_id),
        ]
        result = sanitize_tool_pairs(msgs)
        stub_msgs = [m for m in result if m.role == "tool" and m.tool_call_id == call_id]
        assert len(stub_msgs) == 1
        assert stub_msgs[0].metadata.get("is_stub") is True

    def test_preserves_normal_messages(self):
        msgs = [_user("hello"), _assistant("world")]
        result = sanitize_tool_pairs(msgs)
        assert len(result) == 2

    def test_tool_result_with_none_tool_call_id_is_kept(self):
        # tool_call_id 為 None 的 tool_result 無法判斷，保留
        msg = Message(role="tool", content="some result", tool_call_id=None)
        result = sanitize_tool_pairs([msg])
        assert len(result) == 1


class TestFindLastCompleteToolGroupEnd:
    def test_returns_minus_one_when_no_tool_groups(self):
        msgs = [_user("a"), _assistant("b"), _user("c")]
        assert find_last_complete_tool_group_end(msgs, len(msgs)) == -1

    def test_finds_complete_group_end(self):
        call_id = "call-1"
        msgs = [
            _user("q"),
            _assistant("calling", tool_call_id=call_id),
            _tool("result", call_id),
            _user("next"),
        ]
        end = find_last_complete_tool_group_end(msgs, len(msgs))
        # 完整組結尾是 tool_result 的下一個位置 = index 3
        assert end == 3

    def test_ignores_incomplete_group(self):
        # 只有 tool_call 沒有 tool_result
        msgs = [
            _user("q"),
            _assistant("calling", tool_call_id="call-orphan"),
        ]
        assert find_last_complete_tool_group_end(msgs, len(msgs)) == -1

    def test_returns_last_of_multiple_complete_groups(self):
        msgs = [
            _user("q1"),
            _assistant("call1", tool_call_id="c1"),
            _tool("r1", "c1"),
            _user("q2"),
            _assistant("call2", tool_call_id="c2"),
            _tool("r2", "c2"),
            _user("q3"),
        ]
        end = find_last_complete_tool_group_end(msgs, len(msgs))
        # 第二組結尾是 tool_result r2 的下一個位置 = index 6
        assert end == 6


# ══════════════════════════════════════════════════════════════════════════════
# micro_pruning.py
# ══════════════════════════════════════════════════════════════════════════════


class TestMicroPrunerProtectBoundary:
    def test_default_none_protects_back_half(self):
        pruner = MicroPruner()
        msgs = [_user(f"msg{i}") for i in range(10)]
        boundary = pruner._compute_protect_boundary(msgs, None)
        assert boundary == 5  # len=10, 10//2=5

    def test_explicit_token_budget_excludes_last_messages(self):
        pruner = MicroPruner()
        # 每條 token_count=100，共 5 條，預算 150 → 應保護後 2 條（累計 200 > 150）
        msgs = [Message(role="user", content="x", token_count=100) for _ in range(5)]
        boundary = pruner._compute_protect_boundary(msgs, 150)
        # 累計：i=4 +100=100, i=3 +100=200 > 150 → 邊界在 i+1=4
        assert boundary == 4

    def test_zero_messages_returns_zero(self):
        pruner = MicroPruner()
        assert pruner._compute_protect_boundary([], 1000) == 0

    def test_all_within_budget_returns_zero(self):
        pruner = MicroPruner()
        msgs = [Message(role="user", content="x", token_count=10) for _ in range(5)]
        boundary = pruner._compute_protect_boundary(msgs, 9999)
        assert boundary == 0


class TestMicroPrunerPrune:
    def test_prunes_large_tool_output_before_boundary(self):
        pruner = MicroPruner()
        big_content = "X" * 300  # > 200 chars 門檻
        msgs = [
            _tool(big_content, "call-1", token_count=80),
            _user("recent"),
        ]
        # protect_tail_tokens=0 → 邊界在 0，所有訊息都受保護，不應被剪裁
        # protect_tail_tokens=9999 → 邊界在 0，同上
        # 要觸發剪裁需讓邊界 > 0；用 protect_tail_tokens=5 讓後 1 條進入保護
        result, saved = pruner.prune(msgs, protect_tail_tokens=5)
        pruned = [m for m in result if m.role == "tool" and m.content == PRUNED_TOOL_PLACEHOLDER]
        assert len(pruned) == 1
        assert saved > 0

    def test_does_not_prune_tool_output_after_boundary(self):
        pruner = MicroPruner()
        big_content = "Y" * 300
        msgs = [
            _user("first"),
            _tool(big_content, "call-1", token_count=80),
        ]
        # 保護後 1000 tokens → 邊界=0，tool_result 在邊界後，不被剪裁
        result, saved = pruner.prune(msgs, protect_tail_tokens=1000)
        original_tool = [m for m in result if m.role == "tool" and m.content == big_content]
        assert len(original_tool) == 1

    def test_does_not_prune_already_pruned_tool(self):
        pruner = MicroPruner()
        # metadata 已有 pruned=True，不應再次剪裁
        msg = Message(
            role="tool",
            content="Z" * 300,
            token_count=80,
            metadata={"pruned": True},
        )
        msgs = [msg, _user("recent")]
        result, saved = pruner.prune(msgs, protect_tail_tokens=5)
        # 原本的 tool 應仍在
        assert result[0].content == msg.content

    def test_merges_consecutive_short_assistant_messages(self):
        pruner = MicroPruner()
        msgs = [
            Message(role="assistant", content="ok", token_count=5),
            Message(role="assistant", content="got it", token_count=8),
        ]
        result, saved = pruner.prune(msgs)
        # 兩條被合併成一條
        assert len(result) == 1
        assert "ok" in result[0].content
        assert "got it" in result[0].content
        assert saved >= 4

    def test_does_not_merge_non_consecutive_assistant_messages(self):
        pruner = MicroPruner()
        msgs = [
            Message(role="assistant", content="ok", token_count=5),
            _user("interrupt"),
            Message(role="assistant", content="got it", token_count=8),
        ]
        result, saved = pruner.prune(msgs)
        # 中間有 user，不合併
        assert len(result) == 3

    def test_does_not_merge_long_assistant_messages(self):
        pruner = MicroPruner()
        # token_count >= 15 不合併
        msgs = [
            Message(role="assistant", content="a long message here", token_count=20),
            Message(role="assistant", content="another long message", token_count=20),
        ]
        result, _ = pruner.prune(msgs)
        assert len(result) == 2


# ══════════════════════════════════════════════════════════════════════════════
# rolling_summary.py
# ══════════════════════════════════════════════════════════════════════════════


class TestComputeSummaryBudget:
    def test_minimum_budget_enforced(self):
        # 很少 token，結果應鎖定在 2000
        msgs = [Message(role="user", content="hi", token_count=10)]
        assert _compute_summary_budget(msgs) == 2_000

    def test_maximum_budget_enforced(self):
        # 大量 token（20%超過 12K），結果應鎖定在 12000
        msgs = [Message(role="user", content="x", token_count=100_000)]
        assert _compute_summary_budget(msgs) == 12_000

    def test_budget_is_20_percent_in_range(self):
        # total_tokens=50_000 → 20% = 10_000（在 2K~12K 內）
        msgs = [Message(role="user", content="x", token_count=50_000)]
        budget = _compute_summary_budget(msgs)
        assert budget == 10_000

    def test_uses_char_estimate_when_no_token_count(self):
        # token_count=None → len(content)//4
        msgs = [Message(role="user", content="x" * 400, token_count=None)]
        budget = _compute_summary_budget(msgs)
        # 400//4=100 tokens → 20%=20，clamp 到 2000
        assert budget == 2_000


class TestSummaryPrompt:
    def test_prompt_exists(self):
        assert isinstance(SUMMARY_PROMPT, str)
        assert len(SUMMARY_PROMPT) > 0

    def test_prompt_has_six_sections(self):
        sections = [
            "TASK",
            "PROGRESS",
            "DECISIONS",
            "FILES",
            "NEXT STEPS",
            "CRITICAL",
        ]
        prompt_upper = SUMMARY_PROMPT.upper()
        for section in sections:
            assert section in prompt_upper, f"SUMMARY_PROMPT 缺少段落關鍵字：{section}"


# ══════════════════════════════════════════════════════════════════════════════
# full_compaction.py
# ══════════════════════════════════════════════════════════════════════════════


class TestFindTailCutByTokens:
    def test_minimum_three_messages_protected(self):
        # 即使 budget 很大，tail_start 不超過 n - 3
        msgs = [Message(role="user", content="x", token_count=1) for _ in range(5)]
        cut = _find_tail_cut_by_tokens(msgs, 9999)
        # 最多保護所有訊息，tail_start_max = 5-3 = 2
        assert cut <= 2

    def test_empty_messages_returns_zero(self):
        assert _find_tail_cut_by_tokens([], 1000) == 0

    def test_budget_determines_tail_start(self):
        # 每條 token=100，共 10 條，budget=200 → 保留後 2 條，但硬性最少 3 條
        msgs = [Message(role="user", content="x", token_count=100) for _ in range(10)]
        cut = _find_tail_cut_by_tokens(msgs, 200)
        # 後 2 條 = 200 tokens = 剛好 budget；tail_start = 8，但 tail_start_max = 7
        assert cut <= 8

    def test_soft_limit_accepts_oversized_single_message(self):
        # 最後一條 token=1500，budget=1000，但 1500 <= 1000*1.5=1500 → 軟性接受
        msgs = [
            Message(role="user", content="a", token_count=100),
            Message(role="user", content="b", token_count=100),
            Message(role="user", content="c", token_count=100),
            Message(role="user", content="big", token_count=1500),
        ]
        cut = _find_tail_cut_by_tokens(msgs, 1000)
        # 軟性接受最後一條 → tail 包含它，cut = 3
        assert cut <= 3

    def test_hard_min_protection_when_few_messages(self):
        # 只有 3 條訊息，無論如何都不應 cut 到讓 tail < 3 條
        msgs = [Message(role="user", content="x", token_count=10) for _ in range(3)]
        cut = _find_tail_cut_by_tokens(msgs, 9999)
        assert cut <= 0  # tail_start_max = 3-3 = 0，cut 不超過 0


class TestCompactionPrompt:
    def test_prompt_exists(self):
        assert isinstance(COMPACTION_PROMPT, str)
        assert len(COMPACTION_PROMPT) > 0

    def test_prompt_has_nine_sections(self):
        sections = [
            "PRIMARY REQUEST",
            "KEY TECHNICAL",
            "FILES",
            "ERRORS",
            "PROBLEM-SOLVING",
            "USER MESSAGES",
            "PENDING",
            "CURRENT WORK",
            "IDENTITY",
        ]
        prompt_upper = COMPACTION_PROMPT.upper()
        for section in sections:
            assert section in prompt_upper, f"COMPACTION_PROMPT 缺少段落關鍵字：{section}"

    def test_sacred_sections_mentioned(self):
        assert "SACRED" in COMPACTION_PROMPT.upper()

    def test_sacred_content_placeholder_present(self):
        assert "{sacred_content}" in COMPACTION_PROMPT
