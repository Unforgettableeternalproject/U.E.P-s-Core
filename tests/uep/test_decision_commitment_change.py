"""commitment.change 決策點（規則版 v1，只判斷取消；離線，不需資料庫）：句型、互斥、訊號、日誌、orchestrator 接線。"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from tests.uep.test_action_unavailable import AUTH, CTX
from tests.uep.test_decision_abandon_work import NEGATIVE as ABANDON_NEGATIVE
from tests.uep.test_decision_abandon_work import POSITIVE as ABANDON_POSITIVE
from tests.uep.test_presence_sinks import OWNER, Capture, Pass, _absent, _orch
from uep.contracts.schedule import CommitmentChangeSignal
from uep.decision import DecisionHub
from uep.decision.commitment_change import (
    DECIDER_ID,
    ENGLISH_PHRASES,
    KIND_COMMITMENT_CHANGE,
    CommitmentChangeEngine,
    commitment_change_spec,
    commitment_signal_id,
    judge_commitment_change,
    matches_commitment_cancel,
    register_commitment_change,
)
from uep.decision.log import DecisionLogWriter
from uep.decision.schedule_abandon_work import matches_abandon_work
from uep.memory.identity import IdentityDirectory
from uep.turn.generations import TurnGenerations
from uep.turn.models import UtteranceFact
from uep.turn.orchestrator import TurnOrchestrator

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


class _Sink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, rows) -> None:
        self.rows.extend(rows)


def _hub(sink: _Sink | None = None):
    hub = DecisionHub()
    register_commitment_change(hub)
    writer = None
    if sink is not None:
        writer = DecisionLogWriter(sink)
        writer.attach(hub)
    return hub, writer


def _judge(hub, text, **kw):
    args = {
        "utterance_id": "utt_01HABCDEF", "actor_ref": "discord:123", "actor_authenticated": True,
        "source_platform": "discord", "path": "turn", "now": NOW,
    }
    args.update(kw)
    return judge_commitment_change(hub, text, **args)


POSITIVE = [
    "不用提醒我了", "不用提醒我了。", "不用再提醒我了", "請不用提醒我了", "不用提醒我", "不用 提醒我 了",
    "那個提醒取消", "那個提醒取消了", "那個提醒不用了", "這個提醒不用了", "剛剛的提醒取消", "取消那個提醒", "取消剛才的提醒",
    "剛剛拜託你的事不用了", "剛剛拜託你的事，不用了。", "剛才請你的事就不用了", "剛剛拜託你的那件事不用做了",
    "取消剛才那件事", "取消剛剛那件事", "取消剛剛拜託你的事",
    "cancel that reminder", "Cancel that reminder.", "please cancel that reminder", "never mind that reminder",
    "Never mind that reminder!", "nevermind that reminder", "forget that reminder",
]


@pytest.mark.parametrize("text", POSITIVE)
def test_positive_phrases_match(text):
    assert matches_commitment_cancel(text)
    sig = _judge(_hub()[0], text)
    assert isinstance(sig, CommitmentChangeSignal)


NEGATIVE = [
    # 沒有指涉對象的短句：太寬，不觸發
    "算了", "不用了", "算了不用了", "取消", "取消吧", "不用", "那個取消", "那件事不用了", "不要了", "never mind", "cancel that", "cancel",
    # 否定
    "不要取消那個提醒", "別取消那個提醒", "不是不用提醒我了", "沒有要取消那個提醒", "don't cancel that reminder",
    "do not cancel the reminder", "不要不用提醒我", 
    # 疑問
    "不用提醒我了嗎", "不用提醒我了？", "不用提醒我了?", "可以取消那個提醒嗎", "要不要取消那個提醒", "那個提醒取消了嗎",
    "cancel that reminder?", "should I cancel that reminder",
    # 轉述
    "他說不用提醒我了", "她說那個提醒取消", "我剛剛說不用提醒我了", "聽說不用提醒我了", 
    "昨天不用提醒我了", "he said cancel that reminder",
    # 無關或空
    "提醒我明天開會", "幫我訂位", "不用幫我了", "提醒",
    # 即時指令與放棄工作：已移除「不用幫我○○了」句型
    "不用幫我開燈了", "不用幫我關冷氣了", "不用幫我查天氣了", "不用幫我播放音樂了", "不用幫我做今天的工作了",
    "不用幫我訂位了", "不用再幫我查了",
    # 英文只收 that
    "cancel the reminder", "cancel my reminder", "cancel this reminder", "never mind the reminder", "forget about the reminder",
    "forget the reminder", "please forget about that reminder",
 "", "   ", "今天天氣如何", "x" * 200,
    "不用提醒我了，但是明天還是要叫我起床",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative_phrases_do_not_match(text):
    assert not matches_commitment_cancel(text)
    assert _judge(_hub()[0], text) is None


QUOTED = [
    "「不用提醒我了」", "『那個提醒取消』", "\"cancel that reminder\"", "'never mind that reminder'", "“cancel that reminder”",
    "他說「不用提醒我了」", "「取消那個提醒", "cancel that reminder\"",
]


@pytest.mark.parametrize("text", QUOTED)
def test_quoted_sentences_are_treated_as_quotes(text):
    assert not matches_commitment_cancel(text)
    assert _judge(_hub()[0], text) is None


def test_english_phrase_list_is_consistent_with_patterns():
    from uep.decision.commitment_change import _EN_PATTERNS, _normalize_en

    assert ENGLISH_PHRASES and all(any(p.fullmatch(_normalize_en(t)) for p in _EN_PATTERNS) for t in ENGLISH_PHRASES)
    # 反向：只收 that，換成 the／my／this 的變體不在正則內
    for t in ENGLISH_PHRASES:
        for det in ("the", "my", "this"):
            variant = t.replace("that", det)
            assert not any(p.fullmatch(variant) for p in _EN_PATTERNS), variant
    assert all("that reminder" in t for t in ENGLISH_PHRASES)


def test_over_length_cap_is_not_a_match():
    long = "不用提醒我了" + "，" * 5 + "好" * 30
    assert not matches_commitment_cancel(long)
    assert not matches_commitment_cancel("不用提醒我了" + "了" * 40)


def test_non_string_text_is_not_a_match():
    assert not matches_commitment_cancel(None)
    assert not matches_commitment_cancel(123)
    assert _judge(_hub()[0], None) is None


def test_english_phrase_list_matches_documented():
    for phrase in ENGLISH_PHRASES:
        assert matches_commitment_cancel(phrase)


# ---- 與 abandon_work 互斥 ----


def test_no_sentence_triggers_both_decision_points():
    corpus = set(POSITIVE) | set(NEGATIVE) | set(ABANDON_POSITIVE) | set(ABANDON_NEGATIVE)
    for text in corpus:
        assert not (matches_commitment_cancel(text) and matches_abandon_work(text)), text
    assert not any(matches_commitment_cancel(t) for t in ABANDON_POSITIVE)
    assert not any(matches_abandon_work(t) for t in POSITIVE)


# ---- 訊號欄位與冪等 ----


def test_signal_fields_pass_contract_and_have_no_text_or_owner():
    sig = _judge(_hub()[0], "不用提醒我了")
    assert sig is not None
    assert sig.signal_id == "commitment:utt_01HABCDEF" and sig.actor_ref == "discord:123" and sig.kind == "cancel"
    assert sig.source_platform == "discord" and sig.path == "turn" and sig.decided_at == NOW
    assert sig.decider_id == DECIDER_ID and sig.decider_version == "1" and sig.reason_code == "commitment_cancel_phrase"
    dataclasses.replace(sig)  # 重新走一次契約的 fail-closed 驗證
    names = {f.name for f in dataclasses.fields(sig)}
    assert not [n for n in names if "owner" in n or "text" in n]
    assert "不用提醒我了" not in repr(sig)


def test_signal_id_is_stable_for_same_utterance():
    hub, _ = _hub()
    a = _judge(hub, "不用提醒我了")
    b = _judge(hub, "取消那個提醒", now=datetime(2026, 11, 13, 9, 5, tzinfo=UTC))
    assert a.signal_id == b.signal_id == commitment_signal_id("utt_01HABCDEF")
    assert _judge(hub, "不用提醒我了", utterance_id="utt_other").signal_id != a.signal_id


@pytest.mark.parametrize("kwargs", [
    {"signal_id": "commitment:"}, {"signal_id": "commitment:" + "u" * 117}, {"signal_id": "commitment:bad id"},
    {"decided_at": "2026-11-13"}, {"decided_at": None},
    {"kind": "reschedule"}, {"path": "other"}, {"actor_ref": "小明"}, {"signal_id": "abandon:x"},
    {"decided_at": datetime(2026, 11, 13)}, {"source_platform": ""},
])
def test_contract_rejects_invalid_fields(kwargs):
    sig = _judge(_hub()[0], "不用提醒我了")
    with pytest.raises(ValueError):
        dataclasses.replace(sig, **kwargs)


# ---- fail-closed ----


@pytest.mark.parametrize("actor_ref", ["Discord:123", "小明", "", "a b", "x" * 129, None, 123])
def test_invalid_actor_ref_returns_none(actor_ref):
    assert _judge(_hub()[0], "不用提醒我了", actor_ref=actor_ref) is None


@pytest.mark.parametrize("kw", [
    {"utterance_id": ""}, {"utterance_id": "bad id"}, {"utterance_id": "中文"}, {"utterance_id": None},
    {"utterance_id": "u" * 200}, {"source_platform": ""}, {"source_platform": None}, {"path": "other"},
    {"now": datetime(2026, 11, 13, 9, 0)}, {"now": "2026-11-13"}, {"actor_authenticated": "yes"},
])
def test_malformed_inputs_return_none_without_raising(kw):
    assert _judge(_hub()[0], "不用提醒我了", **kw) is None


def test_unregistered_hub_returns_none():
    assert _judge(DecisionHub(), "不用提醒我了") is None


def test_engine_failure_returns_none():
    class Boom:
        spec = commitment_change_spec()

        def decide(self, request):
            raise RuntimeError("boom")

    hub = DecisionHub()
    hub.register(Boom.spec, Boom())
    assert _judge(hub, "不用提醒我了") is None


# ---- 決策日誌 ----

SECRET = "不用提醒我了"


def test_every_judgment_leaves_one_log_row_without_raw_text():
    sink = _Sink()
    hub, writer = _hub(sink)
    assert _judge(hub, SECRET, utterance_id="utt_a") is not None
    assert _judge(hub, "今天天氣如何，順便說一個秘密 ZZZ", utterance_id="utt_b") is None
    assert writer.flush(2.0)
    writer.close()
    assert len(sink.rows) == 2
    hit, miss = sink.rows
    assert hit["kind"] == KIND_COMMITMENT_CHANGE and hit["outcome"] == "cancel"
    assert hit["reason_code"] == "commitment_cancel_phrase" and hit["status"] == "ok" and hit["fallback"] is False
    assert hit["candidate_id"] == "utt_a" and hit["proposer_id"] == DECIDER_ID and hit["engine"] == "rule"
    assert miss["outcome"] == "none" and miss["reason_code"] == "commitment_no_match" and miss["candidate_id"] == "utt_b"
    dump = repr(sink.rows)
    assert SECRET not in dump and "ZZZ" not in dump and "秘密" not in dump and "discord:123" not in dump


def test_spec_is_sync_native_without_budget_or_shadow():
    spec = commitment_change_spec()
    assert spec.kind == "commitment.change" and spec.mode == "sync" and spec.allow_native
    assert spec.budget_ms is None and not spec.shadow and spec.plan_sections == ()
    assert CommitmentChangeEngine().spec == spec


# ---- orchestrator 接線 ----


def _sink_recorder(got: list, flags: list | None = None):
    """承諾訊號 sink 替身：簽名為 ``(signal, *, is_owner, actor_authenticated)``。"""
    def sink(signal, *, is_owner, actor_authenticated):
        got.append(signal)
        if flags is not None:
            flags.append((is_owner, actor_authenticated))

    return sink


PHRASE = "不用提醒我了"


def test_turn_path_emits_signal_with_idempotency_key_and_owner_flags():
    got, flags = [], []
    orch = _orch(Capture(), on_commitment_signal=_sink_recorder(got, flags))
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text=PHRASE, seq=1, source_endpoint_id="ep"))
    assert len(got) == 1 and got[0].path == "turn" and got[0].actor_ref == "actor_owner"
    assert got[0].signal_id == "commitment:turn:run1:" + CTX.session_ref + ":1"
    assert got[0].kind == "cancel" and flags == [(True, True)]
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="算了", seq=2, source_endpoint_id="ep"))
    assert len(got) == 1  # 沒有指涉對象


def test_absent_path_emits_signal_with_declared_source_and_idempotency_key():
    got, flags = [], []
    orch = _orch(Capture(), on_commitment_signal=_sink_recorder(got, flags))
    _absent(orch, text=PHRASE)
    assert len(got) == 1 and got[0].path == "absent" and got[0].source_platform == "discord"
    assert got[0].signal_id.startswith("commitment:es:run1:") and got[0].signal_id.endswith(":1")
    assert flags == [(True, True)]


def test_guest_or_unauthenticated_actor_does_not_emit():
    got = []
    orch = _orch(Capture(), on_commitment_signal=_sink_recorder(got))
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text=PHRASE, seq=1))  # 沒有 endpoint：未認證
    orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:999", text=PHRASE, seq=2, source_endpoint_id="ep"))  # 未映射
    _absent(orch, text=PHRASE, endpoint=None, seq=3)
    _absent(orch, text=PHRASE, speaker="discord:999", seq=4)
    assert got == []


def test_non_owner_authenticated_actor_gets_is_owner_false():
    got, flags = [], []
    other = "discord:200"
    identity = IdentityDirectory({OWNER: "actor_owner", other: "actor_guest"}, {"actor_owner": "owner1"})
    orch = TurnOrchestrator(
        Capture(), TurnGenerations(), auth_provider=lambda: AUTH, proposer=Pass(),  # type: ignore[arg-type]
        identity=identity, host_location="desktop", run_id="run1", on_commitment_signal=_sink_recorder(got, flags),
    )
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=other, text=PHRASE, seq=1, source_endpoint_id="ep"))
    _absent(orch, text=PHRASE, speaker=other, seq=2)
    assert [s.actor_ref for s in got] == ["actor_guest", "actor_guest"] and flags == [(False, True), (False, True)]


def test_no_sink_means_no_judgment_and_no_registration():
    plain = _orch(Capture())
    assert plain.hub.engine(KIND_COMMITMENT_CHANGE) is None
    calls = []
    plain._judge_commitment = lambda **kw: calls.append(kw)  # type: ignore[method-assign]
    plain.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text=PHRASE, seq=1, source_endpoint_id="ep"))
    _absent(plain, text=PHRASE, seq=2)
    assert calls == []


def test_sink_exception_is_swallowed_and_counted_on_both_paths():
    def boom(signal, **kw):
        raise RuntimeError("down")

    orch = _orch(Capture(), on_commitment_signal=boom)
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text=PHRASE, seq=1, source_endpoint_id="ep"))
    assert orch.metrics["intake.commitment_signal_error"] == 1
    result = _absent(orch, text=PHRASE, seq=2)
    assert result.reason_code == "absent_platform_input" and orch.metrics["intake.commitment_signal_error"] == 2


def test_vetoed_turn_does_not_judge():
    judged = []
    orch = TurnOrchestrator(
        Capture(), TurnGenerations(), auth_provider=lambda: AUTH, proposer=Pass(),  # type: ignore[arg-type]
        identity=IdentityDirectory({OWNER: "actor_owner"}, {"actor_owner": "owner1"}),
        on_commitment_signal=_sink_recorder([]), veto=lambda ctx: "audience_blocked",
    )
    orch._judge_commitment = lambda **kw: judged.append(kw)  # type: ignore[method-assign]
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text=PHRASE, seq=1, source_endpoint_id="ep"))
    assert judged == []


def test_abandon_and_commitment_sinks_are_independent_and_mutually_exclusive():
    ab, cm = [], []

    def ab_sink(signal, *, is_owner):
        ab.append(signal)

    orch = _orch(Capture(), on_abandon_signal=ab_sink, on_commitment_signal=_sink_recorder(cm))
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text="放棄這次工作", seq=1, source_endpoint_id="ep"))
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text=PHRASE, seq=2, source_endpoint_id="ep"))
    assert len(ab) == 1 and len(cm) == 1
    assert ab[0].signal_id.startswith("abandon:") and cm[0].signal_id.startswith("commitment:")


def test_shared_hub_with_kind_already_registered_is_reused():
    hub = DecisionHub()
    register_commitment_change(hub)
    got = []
    orch = _orch(Capture(), decision_hub=hub, on_commitment_signal=_sink_recorder(got))
    orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text=PHRASE, seq=1, source_endpoint_id="ep"))
    assert len(got) == 1


def test_overlong_utterance_id_counts_overflow_and_warns_once(caplog):
    import logging

    got = []
    orch = _orch(Capture(), on_commitment_signal=_sink_recorder(got))
    orch.run_id = "r" * 130  # 讓 turn 路徑的 utterance id 超出長度上限
    with caplog.at_level(logging.WARNING, logger="uep.turn.orchestrator"):
        orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text=PHRASE, seq=1, source_endpoint_id="ep"))
        orch.on_utterance(CTX, UtteranceFact(speaker_ref=OWNER, text=PHRASE, seq=2, source_endpoint_id="ep"))
    assert got == [] and orch.metrics["intake.commitment_signal_id_overflow"] == 2
    assert sum("冪等鍵" in r.getMessage() for r in caplog.records) == 1
    assert orch.metrics["intake.commitment_signal_error"] == 0
