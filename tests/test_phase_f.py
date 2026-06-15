"""
Phase F 測試 — 內容消殺 + Soul Note + ReflectionModule + ConceptExtractor

驗收標準：
  1. sanitize_llm_output 正確清除偽造工具標記
  2. SoulNoteManager 讀寫正常
  3. ReflectionModule 啟發式/LLM 模式都能運作
  4. ConceptExtractor 三層閾值分流
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from echo_memory.utils.sanitizer import sanitize_llm_output, has_fake_tool_markers
from echo_memory.affect.soul_note import SoulNote, SoulNoteManager, DailyReflection
from echo_memory.affect.reflection import ReflectionModule, ReflectionOutput


# ─── 內容消殺 ──────────────────────────────────────────────────────


class TestSanitizer:
    def test_clean_text_unchanged(self):
        text = "這是正常的回覆，不包含任何問題。"
        assert sanitize_llm_output(text) == text

    def test_remove_tool_call(self):
        text = "[tool_call: something] 正常文字"
        result = sanitize_llm_output(text)
        assert "[tool_call" not in result
        assert "正常文字" in result

    def test_remove_tool_result(self):
        text = "回覆 [tool_result: ok] 結束"
        result = sanitize_llm_output(text)
        assert "[tool_result" not in result

    def test_remove_fake_execution_marker(self):
        text = "做好了 [🛠️ 系統已實際執行：某工具]"
        result = sanitize_llm_output(text)
        assert "🛠️" not in result

    def test_remove_xml_tool_use(self):
        text = "文字 <tool_use>tool_name</tool_use> 更多文字"
        result = sanitize_llm_output(text)
        assert "<tool_use>" not in result

    def test_empty_after_clean_gets_fallback(self):
        text = "[tool_call: everything]"
        result = sanitize_llm_output(text, fallback="替代文字")
        assert result == "替代文字"

    def test_has_fake_markers(self):
        assert has_fake_tool_markers("[tool_call: x]")
        assert has_fake_tool_markers("<tool_use>x</tool_use>")
        assert not has_fake_tool_markers("正常文字")

    def test_multiple_patterns(self):
        text = "[tool_call: a] 正常 [tool_result: b] 尾巴"
        result = sanitize_llm_output(text)
        assert "[tool_call" not in result
        assert "[tool_result" not in result
        assert "正常" in result


# ─── Soul Note ─────────────────────────────────────────────────────


class TestSoulNote:
    def test_note_creation(self):
        note = SoulNote(
            category="reflection",
            content="今天學了很多",
        )
        assert note.timestamp != ""
        assert note.category == "reflection"

    def test_manager_add_and_read(self, tmp_path: Path):
        mgr = SoulNoteManager(tmp_path)
        note = SoulNote(
            category="discovery",
            content="發現了新東西",
            tags=["test"],
        )
        mgr.add_note(note)

        today_notes = mgr.get_notes_today()
        assert len(today_notes) == 1
        assert today_notes[0].content == "發現了新東西"

    def test_manager_multiple_notes(self, tmp_path: Path):
        mgr = SoulNoteManager(tmp_path)
        for i in range(3):
            mgr.add_note(SoulNote(content=f"筆記 {i}"))

        notes = mgr.get_notes_today()
        assert len(notes) == 3

    def test_manager_note_count(self, tmp_path: Path):
        mgr = SoulNoteManager(tmp_path)
        for i in range(5):
            mgr.add_note(SoulNote(content=f"筆記 {i}"))

        assert mgr.get_note_count() == 5

    def test_daily_reflection(self, tmp_path: Path):
        mgr = SoulNoteManager(tmp_path)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        reflection = DailyReflection(
            date=today,
            content="今天的深度反思...",
            note_count=5,
            key_insights=["insight1", "insight2"],
        )
        mgr.save_daily_reflection(reflection)

        latest = mgr.get_latest_reflection()
        assert latest is not None
        assert latest.date == today
        assert latest.content == "今天的深度反思..."

    def test_no_reflection_returns_none(self, tmp_path: Path):
        mgr = SoulNoteManager(tmp_path)
        assert mgr.get_latest_reflection() is None


# ─── ReflectionModule ──────────────────────────────────────────────


class TestReflectionModule:
    def test_should_reflect_interval(self):
        module = ReflectionModule(reflect_interval=3)
        assert not module.should_reflect()  # turn 1
        assert not module.should_reflect()  # turn 2
        assert module.should_reflect()      # turn 3

    def test_should_reflect_high_uncertainty(self):
        module = ReflectionModule(reflect_interval=100)
        assert module.should_reflect(uncertainty=0.8)

    def test_heuristic_reflect(self):
        module = ReflectionModule(llm_fn=None)
        output = module.reflect(["summary1", "summary2"])
        assert isinstance(output, ReflectionOutput)
        assert output.action == "question"
        assert output.content != ""

    def test_heuristic_reflect_empty(self):
        module = ReflectionModule(llm_fn=None)
        output = module.reflect([])
        assert output.action == "question"

    def test_llm_reflect_success(self):
        mock_response = json.dumps({
            "action": "browse",
            "content": "需要更多關於 Python 型別系統的資訊",
            "reasoning": "對話中多次提到但未深入",
        })
        module = ReflectionModule(llm_fn=lambda _: mock_response)
        output = module.reflect(["summary1"])
        assert output.action == "browse"
        assert "Python" in output.content

    def test_llm_reflect_with_soul_notes(self, tmp_path: Path):
        mgr = SoulNoteManager(tmp_path)
        mock_response = json.dumps({
            "action": "question",
            "content": "什麼是最佳實踐？",
            "reasoning": "好奇",
        })
        module = ReflectionModule(
            llm_fn=lambda _: mock_response,
            soul_notes=mgr,
        )
        output = module.reflect(["summary"])

        # 確認反思結果被寫入 SoulNote
        notes = mgr.get_notes_today()
        assert len(notes) == 1
        assert "[question]" in notes[0].content

    def test_compress_daily_reflection(self, tmp_path: Path):
        mgr = SoulNoteManager(tmp_path)
        # 先寫入幾則小筆記
        for i in range(3):
            mgr.add_note(SoulNote(content=f"今日筆記 {i}"))

        module = ReflectionModule(
            llm_fn=lambda _: "今天是充實的一天。",
            soul_notes=mgr,
        )
        reflection = module.compress_daily_reflection()

        assert reflection is not None
        assert reflection.note_count == 3
        assert "充實" in reflection.content

    def test_compress_no_notes(self, tmp_path: Path):
        mgr = SoulNoteManager(tmp_path)
        module = ReflectionModule(soul_notes=mgr)
        reflection = module.compress_daily_reflection()
        assert reflection is None  # 無筆記不壓縮
