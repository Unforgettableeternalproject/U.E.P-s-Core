"""輸出防線（已知中文污染詞）的測試。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from uep.stream.adapters.output_guard import OutputGuard
from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import Utterance

POLLUTED = "在呀，伯尼～我一直都在這裡陪你聊天呢 (｡･ω･｡)ﾉ 天天彩票app"
CLEAN = "在呀，伯尼～我一直都在這裡陪你聊天呢 (｡･ω･｡)ﾉ"


def test_example_is_cleaned(caplog):
    guard = OutputGuard()
    with caplog.at_level(logging.WARNING):
        text, hits = guard.clean(POLLUTED)
    assert text == CLEAN
    assert hits and "天天彩票" in hits[0]
    assert any("天天彩票" in r.getMessage() for r in caplog.records)


def test_longest_word_first_and_ascii_tail():
    guard = OutputGuard()
    assert guard.clean("好的。天天中彩票APP")[0] == "好的。"
    assert guard.clean("好的。时时彩 www.abc.com")[0] == "好的。"
    # 命中詞後的中文保留
    assert guard.clean("先說一句彩票app然後繼續")[0] == "先說一句然後繼續"


def test_pure_pollution_is_dropped():
    guard = OutputGuard()
    assert guard.clean("天天彩票app")[0] == ""
    assert guard.clean(" (｡･ω･｡)ﾉ 北京賽車 ")[0] == ""


@pytest.mark.parametrize(
    "text",
    ["這個平台很好用。", "公司的福利不錯。", "快點回家吧。", "天空有一道彩虹。", "多彩的生活"],
)
def test_normal_sentences_untouched(text):
    assert OutputGuard().clean(text) == (text, [])


def test_extra_words_apply():
    guard = OutputGuard(extra=["某某垃圾詞"])
    assert guard.clean("你好某某垃圾詞x")[0] == "你好"
    assert OutputGuard().clean("你好某某垃圾詞x")[0] == "你好某某垃圾詞x"


def test_disabled_is_passthrough():
    assert OutputGuard(enabled=False).clean(POLLUTED) == (POLLUTED, [])


def test_think_config_建立防線():
    from uep.stream.adapters.think_config import ThinkConfig
    from uep.stream.adapters.think_llm import LLMThinkStage

    stage = LLMThinkStage(config=ThinkConfig(output_blocklist_extra=("甲甲", "乙乙")))
    assert stage.output_guard.enabled and stage.output_guard.clean("好甲甲")[0] == "好"
    assert not LLMThinkStage(config=ThinkConfig(output_guard_enabled=False)).output_guard.enabled


class _Backend:
    def __init__(self, response: str) -> None:
        self.response = response

    async def stream_query(
        self, messages: Sequence[Any], system_instruction: str | None = None, **kwargs
    ) -> AsyncIterator[str]:
        for ch in self.response:
            yield ch


async def _run(response: str, guard: OutputGuard):
    stage = LLMThinkStage(_Backend(response), output_guard=guard)
    return [s async for s in stage.stream(Utterance(text="嗨", turn_id="t1"), CancellationToken())]


async def test_stage_filters_sentences():
    sentences = await _run(POLLUTED, OutputGuard())
    joined = "".join(s.text for s in sentences)
    assert "彩票" not in joined and joined.strip() == CLEAN
    assert sentences[-1].is_last


async def test_stage_drops_pure_pollution_sentence_but_keeps_last_marker():
    sentences = await _run("你好啊，今天過得如何呢。天天彩票app", OutputGuard())
    assert "".join(s.text for s in sentences).strip() == "你好啊，今天過得如何呢。"
    assert sentences[-1].is_last


async def test_stage_disabled_keeps_text():
    sentences = await _run(POLLUTED, OutputGuard(enabled=False))
    assert "天天彩票app" in "".join(s.text for s in sentences)
