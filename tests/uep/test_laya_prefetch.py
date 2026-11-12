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
