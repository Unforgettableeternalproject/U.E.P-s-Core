"""預設策略與 spoken 推算純函式（T-07b PR ②）。零依賴、不跑管線。"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.output import PolicySnapshot
from uep.stream.contracts.turn import VoiceEvent, VoiceState
from uep.stream.contracts.types import Sentence
from uep.stream.pipeline import PipelineRunner
from uep.stream.policies import (
    AccumulatingBargeIn,
    NeverYield,
    build_sentence_spans,
    partial_spoken,
    whole_sentence_spoken,
)
from uep.stream.policies import StaticPolicySource
from uep.fakes.stream import FakeThinkStage


@dataclass
class View:
    turn_id: str = "t1"
    owner_id: str | None = "u1"
    reply_mode: str | None = None
    audio_started: bool = False
    behind_playing: bool = False
    sink_active: bool = True
    barge_users: set[str] = field(default_factory=lambda: {"u1"})
    meta: dict = field(default_factory=dict)


def ev(t: float, speaker: str | None = "u1", chunk: float = 0.5, **kw) -> VoiceEvent:
    return VoiceEvent(
        state=VoiceState.SPEECH, timestamp=t, duration_s=0.0, speaker_id=speaker,
        is_voiced=True, chunk_s=chunk, **kw,
    )


def policy(**kw) -> AccumulatingBargeIn:
    return AccumulatingBargeIn(StaticPolicySource(PolicySnapshot.for_platform("discord")), **kw)


# ───────────────────────── AccumulatingBargeIn ─────────────────────────


def test_threshold_is_2s_for_discord_preset() -> None:
    p, v = policy(), View()
    for k in range(3):  # 1.5s
        assert not p.on_voiced(v, ev(1.0 + 0.5 * k)).should_interrupt
    assert p.on_voiced(v, ev(2.5)).should_interrupt  # 2.0s


def test_gap_longer_than_half_second_resets() -> None:
    p, v = policy(), View()
    for k in range(3):
        p.on_voiced(v, ev(1.0 + 0.5 * k))
    assert not p.on_voiced(v, ev(3.01)).should_interrupt  # 間隔 > 0.5s 歸零
    assert p.accumulated("t1", "u1") == 0.5
    assert not p.on_voiced(v, ev(3.5)).should_interrupt


def test_inactive_sink_only_accumulates_then_carries_over() -> None:
    p, v = policy(), View(sink_active=False)
    for k in range(6):  # 排隊中累積 3s，絕不觸發
        assert not p.on_voiced(v, ev(1.0 + 0.5 * k)).should_interrupt
    v.sink_active = True  # 開播瞬間：累積值直接帶入，下一個有聲事件就成立
    assert p.on_voiced(v, ev(4.0)).should_interrupt


def test_only_designated_users_accumulate() -> None:
    p, v = policy(), View()
    for k in range(8):
        assert not p.on_voiced(v, ev(1.0 + 0.5 * k, speaker="stranger")).should_interrupt
    assert p.accumulated("t1", "stranger") == 0.0


def test_trigger_flag_prevents_repeat_without_resetting() -> None:
    p, v = policy(), View()
    hits = [p.on_voiced(v, ev(1.0 + 0.5 * k)).should_interrupt for k in range(8)]
    assert hits.count(True) == 1
    assert p.accumulated("t1", "u1") >= 2.0  # 觸發後不歸零
    p.forget("t1")
    assert p.accumulated("t1", "u1") == 0.0


def test_accumulators_are_per_turn() -> None:
    p = policy()
    a, b = View("a", sink_active=True), View("b", sink_active=False)
    for k in range(3):
        p.on_voiced(a, ev(1.0 + 0.5 * k))
        p.on_voiced(b, ev(1.0 + 0.5 * k))
    assert p.on_voiced(a, ev(2.5)).should_interrupt
    assert not p.on_voiced(b, ev(2.5)).should_interrupt  # b 開播前不判定


def test_empty_barge_users_open_when_fresh_closed_when_stale() -> None:
    fresh = policy(min_speech_s=0.4)
    v = View(barge_users=set())
    assert fresh.on_voiced(v, ev(1.0, speaker=None, chunk=0.5)).should_interrupt
    stale = AccumulatingBargeIn(
        StaticPolicySource(PolicySnapshot(ttl_s=0.0, issued_at=-100.0)), min_speech_s=0.4
    )
    assert not stale.on_voiced(View(barge_users=set()), ev(1.0, speaker=None)).should_interrupt
    # 過期時對已指定的說話者仍照常生效（停播是安全方向）
    assert stale.on_voiced(View(), ev(1.0, chunk=0.5)).should_interrupt


def test_timestamp_delta_used_when_chunk_s_missing() -> None:
    p, v = policy(), View()
    # 沒有 chunk_s 的事件（duration_s 是狀態累積值，不能直接加）：第 5 個事件累積 2.0s 才觸發
    hits = [p.on_voiced(v, ev(1.0 + 0.5 * k, chunk=0.0)).should_interrupt for k in range(6)]
    assert hits == [False, False, False, False, True, False]


def test_unvoiced_events_ignored_and_never_yield() -> None:
    p = policy()
    silent = VoiceEvent(state=VoiceState.SILENCE, timestamp=1.0, duration_s=1.0, speaker_id="u1")
    assert not p.on_voiced(View(), silent).should_interrupt
    assert NeverYield().on_voiced(ev(1.0), [View()]) == []


# ───────────────────────── spoken 純函式 ─────────────────────────


def bot_spoken_text(sent_text, sent_audio_s, sink_s, interrupted):
    """bot ``VoiceTurn.spoken_text`` 的原樣副本（voice_output.py:457-484），當 oracle。"""
    if not sent_text:
        return None
    if not isinstance(sink_s, (int, float)):
        return None
    order = sorted(sent_text)
    out: list[str] = []
    cum = 0.0
    for pos, idx in enumerate(order):
        dur = sent_audio_s.get(idx, 0.0)
        if dur <= 0:
            break
        cum += dur
        if cum > sink_s + 0.05:
            break
        has_later = any(sent_audio_s.get(j, 0.0) > 0 for j in order[pos + 1:])
        if interrupted and not has_later:
            break
        out.append(sent_text[idx])
    return "".join(out)


def test_whole_sentence_matches_bot_oracle_differentially() -> None:
    rng = random.Random(20251029)
    for _ in range(2000):
        n = rng.randint(0, 6)
        texts = {i * 2 + rng.randint(0, 1): f"句{i}。" for i in range(n)}
        audio = {
            idx: rng.choice([0.0, 0.5, 1.0, 1.0, 2.0, 0.04]) for idx in texts if rng.random() < 0.9
        }
        played = rng.choice([None, 0.0, 0.99, 1.0, 1.04, 1.06, 2.5, 3.0, 99.0, "x"])
        cut = rng.random() < 0.5
        assert whole_sentence_spoken(texts, audio, played, cut) == bot_spoken_text(
            texts, audio, played, cut
        )


def test_whole_sentence_edge_semantics() -> None:
    texts = {0: "甲。", 1: "乙。", 2: "丙。"}
    audio = {0: 1.0, 1: 1.0, 2: 1.0}
    assert whole_sentence_spoken({}, {}, 1.0, False) is None  # 沒有送出的句子
    assert whole_sentence_spoken(texts, audio, None, False) is None  # 沒有進度資訊
    assert whole_sentence_spoken(texts, audio, 1.05, False) == "甲。"  # 0.05s 容差
    assert whole_sentence_spoken(texts, audio, 1.0499, True) == "甲。"  # 後面有別句音訊
    assert whole_sentence_spoken(texts, audio, 3.0, False) == "甲。乙。丙。"
    assert whole_sentence_spoken(texts, audio, 3.0, True) == "甲。乙。"  # 被打斷：最後一句不確知
    assert whole_sentence_spoken(texts, {0: 1.0, 1: 0.0, 2: 1.0}, 3.0, False) == "甲。"  # dur<=0


def _sentences(texts: dict[int, str]) -> dict[int, Sentence]:
    return {i: Sentence(text=t, turn_id="t", index=i) for i, t in texts.items()}


def test_partial_matches_pipeline_runner_resolve_spoken_differentially() -> None:
    rng = random.Random(7)

    class SinkStub:
        def __init__(self, played: float) -> None:
            self.played_duration_s = played

    for _ in range(1500):
        n = rng.randint(1, 5)
        texts = {i: f"句{i}。" for i in range(n)}
        timeline: list[tuple[int, float]] = []
        cum = 0.0
        for i in range(n):
            for _c in range(rng.randint(1, 3)):
                cum += rng.choice([0.5, 1.0, 2.0])
                timeline.append((i, cum))
        played = rng.choice([None, 0.0, 0.3, 1.0, 2.5, cum, cum + 5])
        runner = PipelineRunner()
        if played is not None:
            runner.sink = SinkStub(played)  # type: ignore[assignment]
        expected = runner._resolve_spoken(_sentences(texts), timeline, cum)
        assert partial_spoken(texts, timeline, cum, played) == expected


def test_sentence_spans_follow_the_play_timeline() -> None:
    timeline = [(0, 1.0), (0, 1.5), (1, 3.0)]
    spans = build_sentence_spans(timeline, {0: "甲。", 1: "乙。"})
    assert [(s.index, s.start_s, s.end_s) for s in spans] == [(0, 0.0, 1.5), (1, 1.5, 3.0)]
    assert spans[0].text == "甲。"


# ───────────────────────── 通用 settle（依 turn_id 改寫歷史）─────────────────────────


async def test_settle_rewrites_assistant_item_by_turn_id_without_extra_keys() -> None:
    for think in (FakeThinkStage(), LLMThinkStage(backend=object())):
        think._pending_user_text = "問"
        await think.commit("t1", "原文一", "原文一")
        think._pending_user_text = "再問"
        await think.commit("t2", "原文二", "原文二")
        assert await think.settle("t1", "（私訊占位）") is True
        assert [h["content"] for h in think.history] == ["問", "（私訊占位）", "再問", "原文二"]
        assert await think.settle("t1", "可透露的版本") is True  # 之後再回填
        assert think.history[1]["content"] == "可透露的版本"
        assert await think.settle("nope", "x") is False  # 找不到不報錯
        assert all(set(h) == {"role", "content"} for h in think.history)  # 不污染送 API 的內容
        assert await think.settle("t2", "") is True  # 空字串 = 移除 assistant 項
        assert [h["role"] for h in think.history] == ["user", "assistant", "user"]
        assert await think.settle("t2", "x") is False
