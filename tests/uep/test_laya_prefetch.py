"""Laya 預翻譯：句子進來先翻、propose 命中快取或等進行中的翻譯；失敗不影響主路徑；當句優先於背景脈絡（離線）。"""
from __future__ import annotations

import threading
import time

from tests.uep.test_action_unavailable import AUTH, CTX, _Publisher
from uep.echo.laya_wiring import LayaRulesProposer
from uep.echo.translate_wiring import BoundaryTranslator
from uep.turn.aggregator import BufferedAggregator, VoiceRoutedAggregator
from uep.turn.decision_config import AggregatorConfig, DecisionConfig
from uep.turn.generations import TurnGenerations
from uep.turn.models import Proposal, ReplyMode, TurnCandidate, UtteranceFact
from uep.turn.orchestrator import TurnOrchestrator
from uep.turn.proposer import PrefetchProposer
from uep.turn.rule_proposer import RuleProposer

LONG = "我想請你幫我整理一下上週開會的所有重點，然後把需要跟進的事項列成清單，最後再提醒我明天早上要交報告"


class SlowMT:
    """假 MT：可控延遲、記錄呼叫順序，可指定某句失敗。"""

    def __init__(self, delay: float = 0.0, fail: set[str] | None = None) -> None:
        self.delay, self.fail = delay, fail or set()
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def translate(self, text: str) -> str:
        with self.lock:
            self.calls.append(text)
        time.sleep(self.delay)
        if text in self.fail:
            raise RuntimeError("mt boom")
        return f"en {text}"


def _tr(mt: SlowMT, timeout_ms: int = 400) -> BoundaryTranslator:
    tr = BoundaryTranslator(backend_factory=lambda d: mt, device="cpu", warmup_sentences=(), timeout_ms=timeout_ms)
    tr.start_warmup()
    assert tr.wait_ready(5)
    return tr


def test_prefetch_long_sentence_finishes_before_propose_and_propose_hits_cache():
    mt = SlowMT(delay=0.3)
    tr = _tr(mt, timeout_ms=100)  # 沒預翻譯時 100ms 內翻不完
    start = time.monotonic()
    tr.prefetch(LONG)
    assert time.monotonic() - start < 0.1  # 不阻塞
    time.sleep(0.5)  # 模擬 turn 在 aggregator 等待的時間
    result = tr.translate(LONG)
    assert result.status == "ok" and result.text_en == f"en {LONG}" and result.reason == "cache"
    assert mt.calls == [LONG]  # 只翻一次


def test_without_prefetch_the_same_long_sentence_times_out():
    tr = _tr(SlowMT(delay=0.3), timeout_ms=100)
    assert tr.translate(LONG).status == "unavailable"


def test_translate_waits_for_inflight_prefetch_without_second_translation():
    mt = SlowMT(delay=0.2)
    tr = _tr(mt, timeout_ms=400)
    tr.prefetch(LONG)
    time.sleep(0.05)
    result = tr.translate(LONG)  # 預翻譯進行中：等它，不重翻
    assert result.status == "ok" and result.reason == "inflight"
    assert mt.calls == [LONG]


def test_inflight_wait_is_still_bounded_by_the_original_timeout():
    mt = SlowMT(delay=0.5)
    tr = _tr(mt, timeout_ms=100)
    tr.prefetch(LONG)
    start = time.monotonic()
    result = tr.translate(LONG)
    assert result.status == "unavailable" and result.reason == "timeout" and time.monotonic() - start < 0.3
    time.sleep(0.6)  # 預翻譯不因 propose 放棄而取消，結果仍進快取
    assert tr.translate(LONG).reason == "cache"
    assert mt.calls == [LONG]


def test_prefetch_skips_cached_inflight_and_non_cjk():
    mt = SlowMT(delay=0.1)
    tr = _tr(mt)
    tr.prefetch("hello there")
    tr.prefetch("   ")
    tr.prefetch(LONG)
    tr.prefetch(LONG)
    time.sleep(0.3)
    tr.prefetch(LONG)
    assert mt.calls == [LONG]
    assert tr.metrics["prefetch_started"] == 1 and tr.metrics["prefetch_skip_inflight"] == 1
    assert tr.metrics["prefetch_skip_cached"] == 1


def test_prefetch_before_ready_does_not_raise():
    cold = BoundaryTranslator(backend_factory=lambda d: SlowMT(), device="cpu", warmup_sentences=())
    cold.prefetch(LONG)  # 還沒載入：略過（並觸發懶載入），不拋例外


def test_prefetch_backend_failure_is_swallowed_and_propose_path_reports_unavailable():
    mt = SlowMT(fail={LONG})
    tr = _tr(mt)
    tr.prefetch(LONG)  # 不拋
    time.sleep(0.1)
    assert tr.translate(LONG).status == "unavailable"


def test_current_sentence_has_priority_over_background_context_translation():
    mt = SlowMT(delay=0.15)
    tr = _tr(mt, timeout_ms=1000)
    blocker = threading.Thread(target=tr.translate, args=("先佔住模型的一句話",), daemon=True)
    blocker.start()
    time.sleep(0.05)  # 模型忙著翻 blocker
    bg = threading.Thread(target=lambda: tr.translate_background("上一輪的使用者脈絡內容", timeout_ms=3000), daemon=True)
    bg.start()
    time.sleep(0.03)
    tr.prefetch(LONG)  # 當句晚到，但要排在背景脈絡前面
    for t in (blocker, bg):
        t.join(3)
    deadline = time.monotonic() + 3
    while len(mt.calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert mt.calls == ["先佔住模型的一句話", LONG, "上一輪的使用者脈絡內容"]


# ---- LayaProposer／composite／Orchestrator 接線 ----


class FakeLayaWithPrefetch:
    proposer_id = "laya.shadow"
    version = "v"
    loaded = True

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.prefetched: list[str] = []

    def propose(self, candidate: TurnCandidate) -> Proposal:
        return Proposal(reply_mode=ReplyMode.RESPOND, reason_code="laya_model", proposer_id="laya.shadow",
                        proposer_version="v")

    def prefetch(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("prefetch boom")
        self.prefetched.append(text)

    def on_turn_outcome(self, facts) -> None: ...

    def on_audience_changed(self) -> None: ...


def _orch(proposer, aggregator=None) -> TurnOrchestrator:
    return TurnOrchestrator(
        _Publisher(), TurnGenerations(), auth_provider=lambda: AUTH,  # type: ignore[arg-type]
        proposer=proposer, aggregator=aggregator, decision=DecisionConfig(),
    )


def _comp(laya) -> LayaRulesProposer:
    return LayaRulesProposer(laya, RuleProposer(DecisionConfig()))  # type: ignore[arg-type]


def test_composite_is_a_prefetch_proposer_and_forwards_to_laya():
    laya = FakeLayaWithPrefetch()
    comp = _comp(laya)
    assert isinstance(comp, PrefetchProposer)
    comp.prefetch("你好")
    assert laya.prefetched == ["你好"]


def test_orchestrator_prefetches_each_utterance_before_publish():
    laya = FakeLayaWithPrefetch()
    orch = _orch(_comp(laya))
    order: list[str] = []
    orig_publish = orch._publisher.publish
    orch._publisher.publish = lambda d, t: (order.append("publish"), orig_publish(d, t))[1]  # type: ignore[method-assign]
    laya.prefetch = lambda text: order.append("prefetch")  # type: ignore[method-assign]
    orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:1", text=LONG, seq=1))
    assert order[:2] == ["prefetch", "publish"]


def test_prefetch_failure_does_not_affect_the_main_path():
    orch = _orch(_comp(FakeLayaWithPrefetch(fail=True)))
    result = orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:1", text=LONG, seq=1))
    assert result.proposal is not None and result.reason_code != "buffered"
    assert orch.metrics["proposer.prefetch_error"] == 1


def test_non_prefetch_proposers_are_untouched():
    class Plain:
        proposer_id = "plain"

        def propose(self, candidate):
            return Proposal(reply_mode=ReplyMode.PASS, reason_code="x", proposer_id="plain", proposer_version="1")

    orch = _orch(Plain())
    orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:1", text=LONG, seq=1))
    assert orch.metrics["proposer.prefetch_error"] == 0
    assert not isinstance(Plain(), PrefetchProposer)


def test_voice_merged_text_is_prefetched_and_matches_the_final_candidate_text():
    laya = FakeLayaWithPrefetch()
    agg = VoiceRoutedAggregator(BufferedAggregator(AggregatorConfig(merge_gap_ms=5000, max_wait_ms=5000)))
    orch = _orch(_comp(laya), agg)
    first, second = "我想請你幫我整理上週開會的重點", "然後把待辦事項列成清單"
    for seq, text in enumerate((first, second), 1):
        res = orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:1", text=text, seq=seq, from_voice=True))
        assert res.reason_code == "buffered"
    merged = f"{first} {second}"
    assert merged in laya.prefetched  # 緩衝中的合併全文已先翻
    turns = agg.flush_all()  # 交出 turn 時的文字與預翻譯的 key 一致
    assert [t.text for t in turns] == [merged]


def test_buffered_text_interface():
    agg = BufferedAggregator(AggregatorConfig())
    assert agg.buffered_text("discord:1") is None
    assert VoiceRoutedAggregator(agg).buffered_text("discord:1") is None


def test_laya_proposer_prefetch_then_propose_translates_once(monkeypatch):
    from tests.uep.test_laya_translate import _cand, _proposer

    mt = SlowMT(delay=0.1)
    proposer, agent, translator = _proposer(monkeypatch, mt)  # type: ignore[arg-type]
    proposer.prefetch(LONG)
    time.sleep(0.3)
    proposer.propose(_cand(text=LONG))
    assert mt.calls == [LONG] and agent.calls[-1][0]["message"] == f"en {LONG}"
    assert translator.metrics["prefetch_started"] == 1


# ---- 審查修正：取代舊工作、否決後才預翻、競態 ----

BASE = "我想請你幫我整理上週開會的重點"


def test_growing_voice_prefetches_drop_stale_pending_jobs_and_final_text_finishes():
    mt = SlowMT(delay=0.15)
    tr = _tr(mt, timeout_ms=1000)
    texts = [BASE, BASE + "然後列成清單", BASE + "然後列成清單再提醒我", BASE + "然後列成清單再提醒我明天交報告"]
    tr.prefetch(texts[0])
    time.sleep(0.05)  # 第一個已拿到模型開始推論（無法放棄）
    for t in texts[1:]:
        tr.prefetch(t)
    result = tr.translate(texts[-1])
    assert result.status == "ok"
    assert mt.calls == [texts[0], texts[-1]]  # 中間兩個在開始前就被較新的取代
    assert tr.metrics["prefetch_superseded"] == 2


def test_pending_prefetch_cap_drops_the_oldest_unstarted_one():
    mt = SlowMT(delay=0.15)
    tr = _tr(mt, timeout_ms=1000)
    a, b, c, d = (f"{i}號，請幫我查一下今天的天氣怎麼樣" for i in "甲乙丙丁")
    tr.prefetch(a)
    time.sleep(0.05)
    for t in (b, c, d):
        tr.prefetch(t)  # 彼此不是前綴；未開始的上限 2，丟掉最舊的 b
    assert tr.translate(d).status == "ok"
    time.sleep(0.3)
    assert b not in mt.calls and a in mt.calls and c in mt.calls and d in mt.calls


def test_background_translation_is_not_starved_by_current_sentence_work():
    tr = _tr(SlowMT(), timeout_ms=1000)
    tr._BACKGROUND_MAX_YIELD_S = 0.1  # type: ignore[misc]
    tr._urgent_enter()  # 模擬一直有當句翻譯在等
    try:
        start = time.monotonic()
        result = tr.translate_background("上一輪的使用者脈絡內容", timeout_ms=1500)
        assert result.status == "ok" and time.monotonic() - start < 1.0
    finally:
        tr._urgent_exit()


def test_creator_timeout_does_not_abandon_job_while_another_waiter_remains():
    mt = SlowMT(delay=0.3)
    tr = _tr(mt, timeout_ms=1000)
    first = threading.Thread(target=lambda: tr.translate(LONG, timeout_ms=50), daemon=True)
    second_result = []
    first.start()
    time.sleep(0.02)
    second = threading.Thread(target=lambda: second_result.append(tr.translate(LONG, timeout_ms=1500)), daemon=True)
    second.start()
    first.join(2)
    second.join(3)
    assert second_result and second_result[0].status == "ok" and mt.calls == [LONG]


def test_submit_rechecks_cache_and_thread_start_failure_does_not_raise(monkeypatch):
    mt = SlowMT()
    tr = _tr(mt)
    prepared = tr.replace_aliases(LONG)
    tr._cache[prepared] = "cached en"
    job, created = tr._submit(prepared, urgent=True, abandonable=True, waiter=True)
    assert not created and job.out == "cached en" and mt.calls == []

    def boom(*a, **k):
        raise RuntimeError("can't start new thread")

    other = "請幫我查一下明天台北的天氣預報然後告訴我要不要帶傘"
    monkeypatch.setattr(threading.Thread, "start", boom)
    result = tr.translate(other)  # 不拋
    monkeypatch.undo()
    assert result.status == "unavailable" and not tr._inflight and tr._urgent == 0
    assert tr.translate(other).status == "ok"  # 回滾乾淨，之後可以正常翻


class _Gate:
    """假 veto／auth：計算被呼叫次數。"""

    def __init__(self, reason):
        self.reason, self.calls = reason, 0

    def __call__(self, ctx):
        self.calls += 1
        return self.reason


def _orch_v(proposer, veto, aggregator=None) -> TurnOrchestrator:
    return TurnOrchestrator(
        _Publisher(), TurnGenerations(), auth_provider=lambda: AUTH,  # type: ignore[arg-type]
        proposer=proposer, aggregator=aggregator, veto=veto, decision=DecisionConfig(),
    )


def test_no_prefetch_when_vetoed_including_voice_merge():
    laya = FakeLayaWithPrefetch()
    agg = VoiceRoutedAggregator(BufferedAggregator(AggregatorConfig(merge_gap_ms=5000, max_wait_ms=5000)))
    orch = _orch_v(_comp(laya), _Gate("presence_inactive"), agg)
    for seq, text in enumerate((BASE, "然後把待辦事項列成清單"), 1):
        orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:1", text=text, seq=seq, from_voice=True))
    assert laya.prefetched == []
    ok = FakeLayaWithPrefetch()
    _orch_v(_comp(ok), _Gate(None)).on_utterance(CTX, UtteranceFact(speaker_ref="discord:1", text=LONG, seq=1))
    assert ok.prefetched == [LONG]


def test_non_prefetch_proposer_does_not_even_call_veto_for_prefetch():
    class Plain:
        proposer_id = "plain"

        def propose(self, candidate):
            return Proposal(reply_mode=ReplyMode.PASS, reason_code="x", proposer_id="plain", proposer_version="1")

    gate = _Gate(None)
    _orch_v(Plain(), gate).on_utterance(CTX, UtteranceFact(speaker_ref="discord:1", text=LONG, seq=1))
    assert gate.calls == 1  # 只有 _decide_turn 的那一次


def test_turn_handed_off_in_the_same_feed_prefetches_its_merged_text():
    from uep.turn.aggregator import AggregatedTurn

    class Handoff:
        def feed(self, segment):
            return [AggregatedTurn(segment.speaker_ref, "前一段 " + segment.text, (segment,))]

        def tick(self):
            return []

        def flush_all(self):
            return []

    laya = FakeLayaWithPrefetch()
    orch = _orch_v(_comp(laya), _Gate(None), Handoff())
    orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:1", text="小U 你好嗎", seq=1))
    assert laya.prefetched == ["小U 你好嗎", "前一段 小U 你好嗎"]
