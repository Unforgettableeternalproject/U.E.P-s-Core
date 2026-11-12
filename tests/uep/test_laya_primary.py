"""laya.primary：Laya 為主、舊規則為底的組合式 proposer（離線，不載入真模型、不需資料庫）。"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest

from tests.uep.test_laya_shadow import _install_fake
from uep.config import UepSettings
from uep.decision.laya_schema import QUESTIONS
from uep.decision.log import DecisionLogWriter
from uep.echo.inference_wiring import MODE_AWARE_PROPOSER_IDS, build_voice_aggregator
from uep.echo.laya_wiring import (
    LAYA_PRIMARY_ID,
    SOURCE_LAYA,
    SOURCE_RULES_LOAD_FAILED,
    SOURCE_RULES_TIMEOUT,
    SOURCE_RULES_WARMING,
    LayaRulesProposer,
)
from uep.echo.policy import HostPolicySource
from uep.turn.decision_config import DecisionConfig, decision_config_from_settings
from uep.turn.engagement import ChannelMode
from uep.turn.models import (
    Proposal,
    ReplyMode,
    SessionContext,
    TurnCandidate,
    TurnOutcomeFacts,
)
from uep.turn.orchestrator import TurnOrchestrator
from uep.turn.policy import mode_flags
from uep.turn.proposer import (
    AudienceAware,
    Interjection,
    ListenerAware,
    OutcomeObserver,
    ParticipantAware,
    TickingProposer,
    validate_proposal,
)
from uep.turn.registry import PROPOSERS, create_proposer
from uep.turn.rule_proposer import RuleProposer
from uep.turn.wake import WakeDetector

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")
SECRET = "只有這句話才會有的秘密原文"
R4C_QUESTIONS = Path.home() / ".uep" / "models" / "laya" / "ft-r4c" / "e8_seed1" / "questions.json"


def _cand(text: str = SECRET, *, count: int | None = 3, ref: str = "e1", cid: str = "cand1",
          deadline: float | None = None, interaction_state: str | None = "IDLE") -> TurnCandidate:
    return TurnCandidate(
        candidate_id=cid, utterance_refs=(ref,), speakers=("discord:1",), ctx=CTX, interaction_state=interaction_state,
        interaction_version=1, presence_active=True, text=text, participant_count=count, deadline=deadline,
    )


def _lp(mode: str = "respond", route: str = "direct_chat", addressed: bool = True, reason: str = SOURCE_LAYA,
        version: str = "laya-fake:r4c", compound: bool | None = None, conf: float = 0.9) -> Proposal:
    return Proposal(
        reply_mode=ReplyMode(mode), addressed=addressed, confidence=conf, route=route, reason_code=reason,
        proposer_id="laya.shadow", proposer_version=version, compound=compound,
    )


class FakeLaya:
    """LayaProposer 的替身：可程式化的回應、延遲與例外。"""

    proposer_id = "laya.shadow"

    def __init__(self, proposal: Proposal | None = None, *, delay: float = 0.0, exc: Exception | None = None,
                 loaded: bool = True) -> None:
        self.proposal = proposal or _lp()
        self.delay, self.exc, self.loaded = delay, exc, loaded
        self.version = "laya-fake:r4c"
        self.calls = 0
        self.outcomes: list[TurnOutcomeFacts] = []
        self.audience_resets = 0

    def propose(self, candidate: TurnCandidate) -> Proposal:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.proposal

    def on_turn_outcome(self, facts: TurnOutcomeFacts) -> None:
        self.outcomes.append(facts)

    def on_audience_changed(self) -> None:
        self.audience_resets += 1


class FakeRules:
    """RuleProposer 的替身（只用於 tick／outcome 路由測試）：記錄收到的呼叫。"""

    proposer_id = "rules.default"
    version = "fake-rules"

    def __init__(self, interjections: list[Interjection] | None = None) -> None:
        self.interjections = interjections or []
        self.calls: list[tuple] = []
        self.wake = WakeDetector()
        self.channel_mode = ChannelMode.PASSIVE
        self.coupling_value = 0.5
        self.topic_term = 0.0
        self.pending_topics = 0

    def propose(self, candidate):
        self.calls.append(("propose", candidate.candidate_id))
        return Proposal(reply_mode=ReplyMode.RESPOND, reason_code="active", proposer_id=self.proposer_id,
                        proposer_version=self.version)

    def tick(self):
        out, self.interjections = self.interjections, []
        return out

    def on_turn_started(self, *a):
        self.calls.append(("started", *a))

    def on_turn_outcome(self, facts):
        self.calls.append(("outcome", facts))

    def __getattr__(self, name):  # 其餘委派介面：記錄後回傳預設
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*a, **k):
            self.calls.append((name, *a))
            return False if name == "is_listener_continuation" else None

        return record


def _ij(ref: str = "e1", reason: str = "interject_context") -> Interjection:
    return Interjection(
        speaker_ref="discord:1", text=SECRET, utterance_refs=("e0", ref), speakers=("discord:1",),
        proposal=Proposal(reply_mode=ReplyMode.RESPOND, addressed=False, confidence=0.0, reason_code=reason,
                          proposer_id="rules.default", proposer_version="x"),
    )


def _composite(laya: FakeLaya | None = None, rules: object | None = None) -> LayaRulesProposer:
    return LayaRulesProposer(laya or FakeLaya(), rules or RuleProposer(DecisionConfig()))  # type: ignore[arg-type]


# ---- A：題目與缺席路徑 ----


@pytest.mark.skipif(not R4C_QUESTIONS.is_file(), reason="本機沒有 ft-r4c 模型目錄")
def test_questions_match_r4c_model_questions_json():
    shipped = json.loads(R4C_QUESTIONS.read_text(encoding="utf-8"))
    for key, question in QUESTIONS.items():
        assert shipped[key] == question, f"{key} 與 ft-r4c 的 questions.json 不一致"


def test_absent_intent_path_does_not_use_laya_schema():
    import inspect

    from uep.decision import presence_absent_intent as absent

    source = inspect.getsource(absent)
    assert "laya_schema" not in source and "laya_wiring" not in source
    engine = absent.AbsentIntentEngine()
    assert isinstance(engine.proposer, absent.RulesAbsentIntentProposer)  # 缺席判斷仍是規則
    assert engine.spec.budget_ms == 300  # 不吃 laya 預算


# ---- C：決策與退回 ----


def test_laya_decides_and_overrides_rules_fields():
    rules = RuleProposer(DecisionConfig())
    laya = FakeLaya(_lp("respond", "task_request", True, compound=True))
    prop = _composite(laya, rules)
    result = prop.propose(_cand())  # 3 人未點名：規則會判 pass
    assert (result.reply_mode, result.route, result.addressed, result.compound) == (
        ReplyMode.RESPOND, "task_request", True, True)
    assert result.reason_code == SOURCE_LAYA and result.proposer_id == LAYA_PRIMARY_ID
    assert result.proposer_version == "laya-fake:r4c" and result.interaction_to is None
    assert validate_proposal(result) is None
    assert prop.metrics[SOURCE_LAYA] == 1
    # 規則一定有跑：未點名的句子進了 engagement buffer
    assert rules._engagement.new_turns_since_dispatch == 1


def test_rules_always_run_first_even_when_laya_decides():
    rules = FakeRules()
    prop = _composite(FakeLaya(), rules)
    prop.propose(_cand())
    assert rules.calls[0] == ("propose", "cand1")


def test_laya_pass_gets_listening_only_when_idle():
    prop = _composite(FakeLaya(_lp("pass", "ignore", False)))
    assert prop.propose(_cand(interaction_state="IDLE")).interaction_to == "LISTENING"
    assert prop.propose(_cand(interaction_state="ACTIVE")).interaction_to is None


@pytest.mark.parametrize(("laya", "label"), [
    (FakeLaya(_lp(reason="translate_unavailable", version="warming")), SOURCE_RULES_TIMEOUT),
    (FakeLaya(exc=RuntimeError("boom")), SOURCE_RULES_TIMEOUT),
    (FakeLaya(_lp(reason="laya_warming_up", version="warming")), SOURCE_RULES_WARMING),
    (FakeLaya(_lp(reason="laya_load_failed", version="warming")), SOURCE_RULES_LOAD_FAILED),
])
def test_fallback_returns_rules_proposal_with_source_label(laya, label):
    rules = RuleProposer(DecisionConfig())
    expected = RuleProposer(DecisionConfig()).propose(_cand())  # 同樣輸入、全新規則的提案
    prop = _composite(laya, rules)
    result = prop.propose(_cand())
    assert (result.reply_mode, result.route, result.addressed) == (expected.reply_mode, expected.route, expected.addressed)
    assert result.reason_code == f"{label}:{expected.reason_code}"
    assert result.proposer_id == LAYA_PRIMARY_ID and validate_proposal(result) is None
    assert prop.metrics[label] == 1 and prop.metrics[SOURCE_LAYA] == 0
    assert prop.take_shadow_diff("cand1") is None  # 沒有 Laya 的判斷就沒有差異


def test_slow_laya_times_out_inside_deadline_and_returns_rules():
    prop = _composite(FakeLaya(delay=1.0))
    start = time.monotonic()
    result = prop.propose(_cand(deadline=time.monotonic() + 0.2))
    assert time.monotonic() - start < 0.5  # 不等 Laya 跑完，外層預算內就交出規則的結果
    assert result.reason_code.startswith(SOURCE_RULES_TIMEOUT + ":")
    assert prop.metrics["laya_timeout"] == 1
    busy = prop.propose(_cand(cid="c2", deadline=time.monotonic() + 0.2))  # 上一個推論還卡著：立即退回
    assert busy.reason_code.startswith(SOURCE_RULES_TIMEOUT) and prop.metrics["laya_busy"] == 1


def test_deadline_already_passed_skips_laya():
    laya = FakeLaya()
    result = _composite(laya).propose(_cand(deadline=time.monotonic() - 1))
    assert laya.calls == 0 and result.reason_code.startswith(SOURCE_RULES_TIMEOUT)


def test_invalid_laya_proposal_falls_back_to_rules():
    bad = _lp(route="not_a_route")
    prop = _composite(FakeLaya(bad))
    assert prop.propose(_cand()).reason_code.startswith(SOURCE_RULES_TIMEOUT)
    assert prop.metrics["laya_invalid"] == 1


def test_rules_exception_does_not_drag_down_laya():
    rules = FakeRules()
    rules.propose = lambda c: (_ for _ in ()).throw(RuntimeError("rules boom"))  # type: ignore[method-assign]
    prop = _composite(FakeLaya(_lp("respond")), rules)
    assert prop.propose(_cand()).reason_code == SOURCE_LAYA
    assert prop.metrics["rules_error"] == 1
    both = _composite(FakeLaya(exc=RuntimeError("laya boom")), rules)
    with pytest.raises(RuntimeError):  # 兩邊都沒結果：交給 engine 的 D80 預設
        both.propose(_cand())


def test_laya_pass_settles_rules_pending_topic_as_pass():
    rules = FakeRules()
    prop = _composite(FakeLaya(_lp("pass", "ignore", False)), rules)
    prop.propose(_cand())
    outcome = [c[1] for c in rules.calls if c[0] == "outcome"][0]
    assert (outcome.status, outcome.end_reason, outcome.started) == ("skipped", "pass", True)
    assert outcome.candidate_id == "cand1"
    assert outcome.speakers == ()  # 不碰說話者的生成中標記（他可能另有真正進行中的回合）


def test_laya_defer_settles_rules_pending_topic_as_not_counted():
    rules = FakeRules()
    prop = _composite(FakeLaya(_lp("defer", "task_request", True)), rules)
    prop.propose(_cand())
    outcome = [c[1] for c in rules.calls if c[0] == "outcome"][0]
    assert (outcome.status, outcome.started, outcome.speakers) == ("skipped", False, ())  # 不增不減
    assert outcome.candidate_id == "cand1"


def test_real_rules_pending_topic_is_settled_without_touching_responding_speakers():
    control = RuleProposer(DecisionConfig())
    control.set_mode_override(ChannelMode.ACTIVE)
    control.propose(_cand(count=1))
    assert control.pending_topics == 1  # 對照組：沒有合成結果時話題項會一直待結算
    rules = RuleProposer(DecisionConfig())
    rules.set_mode_override(ChannelMode.ACTIVE)
    prop = _composite(FakeLaya(_lp("defer", "direct_chat", True)), rules)
    rules._responding.add("discord:1")  # 這位說話者另有進行中的回合
    prop.propose(_cand(count=1))
    assert rules.pending_topics == 0 and "discord:1" in rules._responding
    prop = _composite(FakeLaya(_lp("pass", "ignore", False)), rules)
    prop.propose(_cand(count=1, cid="c2", ref="e2"))
    assert rules.pending_topics == 0 and "discord:1" in rules._responding


def test_composite_counts_are_visible_in_orchestrator_metrics():
    laya = FakeLaya(_lp("respond", "task_request", True))
    orch = _orch(_composite(laya))
    try:
        orch._propose(_cand(deadline=time.monotonic() + 0.4))
        laya.exc = RuntimeError("x")
        orch._propose(_cand(cid="c2", ref="e2", deadline=time.monotonic() + 0.4))
        assert orch.metrics["laya_primary.laya_model"] == 1
        assert orch.metrics["laya_primary.rules_fallback_timeout"] == 1
        assert orch.metrics["laya_primary.shadow_diff"] == 1
    finally:
        orch.close()


# ---- C：介面委派 ----


def test_composite_exposes_every_optional_port_the_host_and_orchestrator_probe():
    prop = _composite()
    for port in (TickingProposer, AudienceAware, ParticipantAware, ListenerAware, OutcomeObserver):
        assert isinstance(prop, port), port
    assert isinstance(prop.wake, WakeDetector) and prop.wake is prop.rules.wake
    assert prop.channel_mode is ChannelMode.PASSIVE and callable(prop.add_mode_listener)
    seen: list[ChannelMode] = []
    prop.add_mode_listener(seen.append)
    prop.set_participant_count(1)
    prop.set_mode_override(ChannelMode.ACTIVE)
    assert prop.channel_mode is ChannelMode.ACTIVE and seen[-1] is ChannelMode.ACTIVE
    prop.set_mode_override(None)
    assert prop.is_listener_continuation("嗯") in (True, False)
    prop.on_participant_left("discord:1")
    prop.reset(2)
    prop.on_audience_changed()
    assert prop.laya.audience_resets == 2  # reset／audience 切換也清掉 Laya 的上一輪脈絡


def test_outcome_routing_started_only_to_rules_outcome_to_both():
    rules, laya = FakeRules(), FakeLaya()
    prop = _composite(laya, rules)
    prop.on_turn_started("t1", "cand1", ("discord:1",))
    assert ("started", "t1", "cand1", ("discord:1",)) in rules.calls and not hasattr(laya, "started")
    facts = TurnOutcomeFacts(turn_id="t1", candidate_id="cand1", speakers=("discord:1",), reply_mode="respond",
                             reason_code="laya_model", status="completed", generation_ok=True)
    prop.on_turn_outcome(facts)
    assert ("outcome", facts) in rules.calls and laya.outcomes == [facts]


def test_outcome_failure_in_one_side_does_not_block_the_other():
    rules, laya = FakeRules(), FakeLaya()
    rules.on_turn_outcome = lambda f: (_ for _ in ()).throw(RuntimeError("x"))  # type: ignore[method-assign]
    prop = _composite(laya, rules)
    facts = TurnOutcomeFacts(turn_id="t", candidate_id="c", speakers=(), reply_mode="respond", reason_code="r",
                             status="completed")
    prop.on_turn_outcome(facts)
    assert laya.outcomes == [facts]


# ---- C：tick 衝突（Laya 為準，只管對話內容插話）----


def test_interjection_dropped_when_laya_judged_last_utterance_pass_or_ignore():
    rules = FakeRules([_ij("e7"), _ij("e8")])
    prop = _composite(FakeLaya(_lp("pass", "ignore", False)), rules)
    prop.propose(_cand(ref="e7", cid="c7"))
    rules.interjections = [_ij("e7")]
    assert prop.tick() == []
    assert prop.metrics["interjection_dropped_by_laya"] == 1
    release = [c[1] for c in rules.calls if c[0] == "outcome" and c[1].candidate_id.startswith("tick:")][0]
    assert release.started is False  # 解除 in-flight 並套冷卻，不立刻重提同一批


def test_defer_also_drops_the_interjection():
    rules = FakeRules()
    prop = _composite(FakeLaya(_lp("defer", "task_request", True)), rules)
    prop.propose(_cand(ref="e7"))
    rules.interjections = [_ij("e7")]
    assert prop.tick() == []
    assert prop.metrics["interjection_dropped_by_laya"] == 1


def test_ignore_route_alone_also_drops_the_interjection():
    rules = FakeRules()
    prop = _composite(FakeLaya(_lp("respond", "ignore", False)), rules)
    prop.propose(_cand(ref="e7"))
    rules.interjections = [_ij("e7")]
    assert prop.tick() == []


def test_interjection_kept_when_laya_did_not_judge_it_pass_or_has_no_verdict():
    rules = FakeRules()
    prop = _composite(FakeLaya(_lp("passive", "direct_chat", False)), rules)
    prop.propose(_cand(ref="e7"))
    rules.interjections = [_ij("e7"), _ij("e99")]  # e7：Laya 判 passive；e99：Laya 沒看過
    kept = prop.tick()
    assert [ij.utterance_refs[-1] for ij in kept] == ["e7", "e99"]
    assert prop.metrics["interjection_dropped_by_laya"] == 0


def test_interjection_kept_when_laya_fell_back_for_that_utterance():
    rules = FakeRules()
    prop = _composite(FakeLaya(exc=RuntimeError("x")), rules)
    prop.propose(_cand(ref="e7"))
    rules.interjections = [_ij("e7")]
    assert len(prop.tick()) == 1


def test_rule_tick_only_covers_conversation_content_not_timers():
    """RuleProposer.tick 的插話只來自被動模式的 engagement buffer（reason interject_silence／interject_context）；
    計時、提醒、事件觸發的主動輸出走 proactive.initiate／schedule，不經 proposer。"""
    import inspect

    from uep.turn import rule_proposer

    src = inspect.getsource(rule_proposer.RuleProposer.tick)
    assert "interject_silence" in src and "interject_context" in src
    assert "schedule" not in src and "reminder" not in src and "timer" not in src


# ---- 逐句差異與日誌（無原文）----


class _Sink:
    def __init__(self):
        self.rows, self.lock = [], threading.Lock()

    def write(self, rows):
        with self.lock:
            self.rows.extend(dict(r) for r in rows)


def _orch(primary, budget_ms: int = 300, laya_budget_ms: int = 500) -> TurnOrchestrator:
    return TurnOrchestrator(
        None, None, auth_provider=lambda: None, proposer=primary,  # type: ignore[arg-type]
        decision=DecisionConfig(proposer_budget_ms=budget_ms, laya_budget_ms=laya_budget_ms),
    )


def test_disagreement_is_logged_per_utterance_without_raw_text_and_source_is_in_primary_row():
    sink = _Sink()
    prop = _composite(FakeLaya(_lp("respond", "task_request", True)))
    orch = _orch(prop)
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(orch.hub)
    try:
        agree_free = orch._propose(_cand(count=3, deadline=time.monotonic() + 0.4))  # 規則：pass；Laya：respond
        assert agree_free.reason_code == SOURCE_LAYA
        assert writer.flush()
    finally:
        orch.close()
        writer.close()
    primary = [r for r in sink.rows if r["role"] == "primary"]
    shadow = [r for r in sink.rows if r["role"] == "shadow"]
    assert len(primary) == 1 and primary[0]["reason_code"] == SOURCE_LAYA
    assert primary[0]["proposer_id"] == LAYA_PRIMARY_ID and primary[0]["utterance_refs"] == ["e1"]
    assert len(shadow) == 1
    row = shadow[0]
    assert row["proposer_id"] == "rules.default" and row["outcome"] == "pass" and row["agree"] is False
    assert row["reason_code"] == "observe:diff_mode_route_addressed"
    assert row["primary_decision_id"] == primary[0]["decision_id"] and row["candidate_id"] == "cand1"
    assert SECRET not in repr(sink.rows) and SECRET not in repr(orch._reply_engine.shadow_log)
    assert orch.metrics["shadow.proposer_diff"] == 1


def test_agreement_writes_no_shadow_row_and_fallback_is_labelled_in_primary_row():
    sink = _Sink()
    laya = FakeLaya(_lp("pass", "direct_chat", False))  # 與規則一致（3 人未點名 -> pass／direct_chat／False）
    orch = _orch(_composite(laya))
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(orch.hub)
    try:
        orch._propose(_cand(deadline=time.monotonic() + 0.4))
        laya.exc = RuntimeError("x")
        orch._propose(_cand(cid="c2", ref="e2", deadline=time.monotonic() + 0.4))
        assert writer.flush()
    finally:
        orch.close()
        writer.close()
    assert [r for r in sink.rows if r["role"] == "shadow"] == []
    reasons = [r["reason_code"] for r in sink.rows if r["role"] == "primary"]
    assert reasons[0] == SOURCE_LAYA and reasons[1].startswith(SOURCE_RULES_TIMEOUT + ":")
    assert SECRET not in repr(sink.rows)


# ---- B：預算與翻譯逾時規則 ----


def test_laya_budget_applies_only_to_turn_reply_of_laya_primary():
    laya_orch = _orch(_composite())
    rules_orch = _orch(RuleProposer(DecisionConfig()))
    try:
        assert laya_orch.hub.registry.get("turn.reply").budget_ms == 500
        assert laya_orch.hub.registry.get("turn.interject").budget_ms == 300
        assert laya_orch.hub.registry.get("presence.absent_intent").budget_ms == 300
        assert rules_orch.hub.registry.get("turn.reply").budget_ms == 300  # 其他 proposer 不變
    finally:
        laya_orch.close()
        rules_orch.close()


def test_default_laya_budget_is_500_and_proposer_budget_stays_300():
    cfg = DecisionConfig()
    assert (cfg.laya_budget_ms, cfg.proposer_budget_ms) == (500, 300)


def test_settings_override_laya_budget_only():
    st = UepSettings(_env_file=None, UEP_DECISION_PROPOSER="laya.primary", UEP_LAYA_BUDGET_MS="650")
    cfg = decision_config_from_settings(st)
    assert cfg.laya_budget_ms == 650 and cfg.proposer_budget_ms == 300


@pytest.mark.parametrize("value", ["0", "-5"])
def test_non_positive_laya_budget_is_rejected_at_startup(value):
    with pytest.raises(ValueError, match="UEP_LAYA_BUDGET_MS"):
        decision_config_from_settings(UepSettings(_env_file=None, UEP_LAYA_BUDGET_MS=value))


def test_translate_timeout_not_below_budget_warns(caplog):
    bad = UepSettings(_env_file=None, UEP_DECISION_PROPOSER="laya.primary", UEP_DECISION_TRANSLATE="true",
                      UEP_TRANSLATE_TIMEOUT_MS="500", UEP_LAYA_BUDGET_MS="500")
    with caplog.at_level(logging.WARNING, logger="uep.turn.decision_config"):
        decision_config_from_settings(bad)
    assert any("UEP_TRANSLATE_TIMEOUT_MS" in r.getMessage() for r in caplog.records)
    caplog.clear()
    margin = UepSettings(_env_file=None, UEP_DECISION_PROPOSER="laya.primary", UEP_DECISION_TRANSLATE="true",
                         UEP_TRANSLATE_TIMEOUT_MS="430", UEP_LAYA_BUDGET_MS="500")  # 430 + 80 >= 500
    with caplog.at_level(logging.WARNING, logger="uep.turn.decision_config"):
        decision_config_from_settings(margin)
    assert any("UEP_TRANSLATE_TIMEOUT_MS" in r.getMessage() for r in caplog.records)
    caplog.clear()
    ok = UepSettings(_env_file=None, UEP_DECISION_PROPOSER="laya.primary", UEP_DECISION_TRANSLATE="true",
                     UEP_TRANSLATE_TIMEOUT_MS="400", UEP_LAYA_BUDGET_MS="500")
    default_timeout = UepSettings(_env_file=None, UEP_DECISION_PROPOSER="laya.primary", UEP_DECISION_TRANSLATE="true")
    with caplog.at_level(logging.WARNING, logger="uep.turn.decision_config"):
        decision_config_from_settings(ok)
        decision_config_from_settings(default_timeout)  # 預設 400 < 500
        decision_config_from_settings(UepSettings(_env_file=None, UEP_TRANSLATE_TIMEOUT_MS="900"))  # 不是 laya.primary
    assert not caplog.records


# ---- D：兩組設定的組裝與退回 ----


def test_assembly_with_laya_primary_then_rollback_restores_rules(monkeypatch, tmp_path):
    _install_fake(monkeypatch)
    laya_env = UepSettings(
        _env_file=None, UEP_DECISION_PROPOSER="laya.primary", UEP_LAYA_MODEL=str(tmp_path), UEP_LAYA_BUDGET_MS="500",
        UEP_LAYA_DEVICE="cpu", UEP_DECISION_TRANSLATE="false", UEP_TRANSLATE_DEVICE="cpu",
    )
    cfg = decision_config_from_settings(laya_env)
    assert cfg.proposer_id == LAYA_PRIMARY_ID and cfg.shadow_proposer_ids == ("respond.minimal",)
    proposer = create_proposer(cfg.proposer_id, config=cfg, settings=laya_env)
    try:
        assert isinstance(proposer, LayaRulesProposer) and proposer.laya.wait_ready(5) and proposer.laya.loaded
        # Host 組裝會探測的介面都在，語音聚合器對 laya.primary 啟用
        assert isinstance(proposer.wake, WakeDetector) and callable(proposer.add_mode_listener)
        assert build_voice_aggregator(cfg) is not None
        orch = _orch(proposer, laya_budget_ms=cfg.laya_budget_ms)
        try:
            assert orch.hub.registry.get("turn.reply").budget_ms == 500
        finally:
            orch.close()
    finally:
        proposer.close()
    # 退回：移除 UEP_DECISION_PROPOSER -> rules.default，預算回 300，聚合器仍啟用
    rolled = decision_config_from_settings(UepSettings(_env_file=None))
    assert rolled.proposer_id == "rules.default"
    rules = create_proposer(rolled.proposer_id, config=rolled)
    assert isinstance(rules, RuleProposer) and build_voice_aggregator(rolled) is not None
    orch = _orch(rules)
    try:
        assert orch.hub.registry.get("turn.reply").budget_ms == 300
    finally:
        orch.close()


def test_mode_aware_ids_match_registered_ids_and_other_proposers_stay_excluded():
    assert {"rules.default", LAYA_PRIMARY_ID} == set(MODE_AWARE_PROPOSER_IDS)
    assert set(MODE_AWARE_PROPOSER_IDS) <= set(PROPOSERS.ids())
    for pid in ("laya.shadow", "nlp.legacy", "respond.minimal"):
        assert build_voice_aggregator(DecisionConfig(proposer_id=pid)) is None


# ---- C：passive 在下游保持沉默 ----


def test_passive_reply_mode_is_silent_and_not_bargeable_by_default_policy():
    """laya.primary 上線後 passive 會真的出現：預設政策必須讓它不出聲（回合照開、LLM 跑一次，但 mode_silent 略過）。"""
    source = HostPolicySource("website")
    snapshot = source.core_current()
    assert "passive" in snapshot.silent_modes and "passive" in snapshot.no_barge_in_modes
    assert mode_flags(snapshot, "passive") == (False, False)  # 不出聲、不可打斷
    assert mode_flags(snapshot, "respond")[0] is True
    flags = source.current().mode_rules.flags_for("passive")
    assert flags.speak is False and flags.barge_in is False
    # defer／pass 根本不開回合（Orchestrator._decide_turn 只放行 respond／passive）
    assert flags.supersede_same_owner is False


def test_laya_passive_proposal_reaches_the_orchestrator_as_passive_not_respond():
    prop = _composite(FakeLaya(_lp("passive", "direct_chat", False)))
    orch = _orch(prop)
    try:
        result = orch._propose(_cand(deadline=time.monotonic() + 0.4))
    finally:
        orch.close()
    assert result.reply_mode is ReplyMode.PASSIVE and result.reason_code == SOURCE_LAYA
