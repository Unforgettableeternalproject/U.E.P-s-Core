"""schedule.abandon_work 決策點（規則版 v1；不需資料庫）：命中／不命中、冪等 signal_id、fail-closed、原文不落地。"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from uep.contracts.schedule import AbandonWorkSignal
from uep.decision import DecisionHub
from uep.decision.log import DecisionLogWriter
from uep.decision.schedule_abandon_work import (
    DECIDER_ID,
    ENGLISH_PHRASES,
    KIND_SCHEDULE_ABANDON_WORK,
    AbandonWorkEngine,
    abandon_signal_id,
    judge_abandon_work,
    matches_abandon_work,
    register_abandon_work,
    schedule_abandon_work_spec,
)

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


class _Sink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, rows) -> None:
        self.rows.extend(rows)


def _hub(sink: _Sink | None = None) -> tuple[DecisionHub, DecisionLogWriter | None]:
    hub = DecisionHub()
    register_abandon_work(hub)
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
    return judge_abandon_work(hub, text, **args)


POSITIVE = [
    "放棄這次工作", "放棄這次工作。", "放棄 這次 工作", "放棄這次工作了", "放棄這次工作吧", "請放棄這次工作",
    "麻煩你放棄這次工作", "我要放棄這次工作", "放棄今天的工作", "放棄本次工作", "放棄這次工作階段",
    "今天的工作不用做了", "今天的工作不用做了。", "今天工作不用做了", "這次工作不用再做了",
    "請先不要工作了", "先不要工作了", "先不要工作了！", "今天先不要工作了", "先不工作了", "先不要工作",
    "give up this work session", "Give up this work session.", "please abandon this work session",
]


@pytest.mark.parametrize("text", POSITIVE)
def test_positive_phrases_match(text):
    assert matches_abandon_work(text)
    sig = _judge(_hub()[0], text)
    assert isinstance(sig, AbandonWorkSignal)


NEGATIVE = [
    # 規格明列：單獨的「不用做了」、「休息一下」
    "不用做了", "不用做了。", "這個不用做了", "休息一下", "先休息一下", "休息一下吧",
    # 否定
    "不要放棄這次工作", "別放棄這次工作", "不想放棄這次工作", "我不放棄這次工作", "不要先不要工作了",
    # 疑問
    "要不要放棄這次工作？", "要不要放棄這次工作", "放棄這次工作嗎", "放棄這次工作？", "放棄這次工作?", "可以放棄這次工作嗎",
    "是不是該放棄這次工作", "今天的工作不用做了嗎", "先不要工作了嗎？",
    # 轉述／回顧
    "他說放棄這次工作", "她說今天的工作不用做了", "我剛剛說放棄這次工作", "昨天放棄這次工作", "如果放棄這次工作會怎樣",
    "他說他先不要工作了", "聽說今天的工作不用做了",
    # 無關或空
    "放棄", "放棄這次", "工作", "好好工作", "繼續工作", "", "   ", "今天天氣如何", "放棄這次任務",
    "放棄這次工作，但是先把報告寫完", "give up", "do not give up this work session", "should I give up this work session?",
    # 收窄：自指開頭的「先不要工作了」、泛指的「這個／這回／這輪工作」
    "我先不要工作了", "我要先不要工作了", "我們先不工作了", "放棄這個工作", "放棄這輪工作", "放棄這回工作", "這個工作不用做了",
    "x" * 200,
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative_phrases_do_not_match(text):
    assert not matches_abandon_work(text)
    assert _judge(_hub()[0], text) is None


def test_non_string_text_is_not_a_match():
    assert not matches_abandon_work(None)
    assert not matches_abandon_work(123)
    assert _judge(_hub()[0], None) is None


def test_english_phrase_list_matches_documented():
    for phrase in ENGLISH_PHRASES:
        assert matches_abandon_work(phrase)


# ---- 訊號欄位與冪等 ----


def test_signal_fields_pass_contract_and_have_no_owner_or_schedule_fields():
    sig = _judge(_hub()[0], "放棄這次工作")
    assert sig is not None
    assert sig.signal_id == "abandon:utt_01HABCDEF" and sig.actor_ref == "discord:123"
    assert sig.actor_authenticated is True and sig.source_platform == "discord" and sig.path == "turn"
    assert sig.decided_at == NOW and sig.decider_id == DECIDER_ID and sig.decider_version == "1"
    assert sig.reason_ref is None and sig.confidence is not None and 0.0 <= sig.confidence <= 1.0
    # 重新走一次契約的 fail-closed 驗證
    dataclasses.replace(sig)
    names = {f.name for f in dataclasses.fields(sig)}
    assert not [n for n in names if "owner" in n or "item" in n or "round" in n or "slot" in n]


def test_signal_id_is_stable_for_same_utterance():
    hub, _ = _hub()
    a = _judge(hub, "放棄這次工作", now=NOW)
    b = _judge(hub, "先不要工作了", now=datetime(2026, 11, 13, 9, 5, tzinfo=UTC))  # 重送／換句型仍同一 utterance
    assert a.signal_id == b.signal_id == abandon_signal_id("utt_01HABCDEF")
    assert _judge(hub, "放棄這次工作", utterance_id="utt_other").signal_id != a.signal_id


def test_unauthenticated_actor_still_yields_fact_for_consumer_to_judge():
    sig = _judge(_hub()[0], "放棄這次工作", actor_authenticated=False)
    assert sig is not None and sig.actor_authenticated is False


# ---- fail-closed ----


@pytest.mark.parametrize("actor_ref", ["Discord:123", "小明", "", "a b", "x" * 129, None, 123])
def test_invalid_actor_ref_returns_none(actor_ref):
    assert _judge(_hub()[0], "放棄這次工作", actor_ref=actor_ref) is None


@pytest.mark.parametrize("kw", [
    {"utterance_id": ""}, {"utterance_id": "bad id"}, {"utterance_id": "中文"}, {"utterance_id": None},
    {"utterance_id": "u" * 200}, {"source_platform": ""}, {"source_platform": None}, {"path": "other"},
    {"now": datetime(2026, 11, 13, 9, 0)}, {"now": "2026-11-13"}, {"actor_authenticated": "yes"},
])
def test_malformed_inputs_return_none_without_raising(kw):
    assert _judge(_hub()[0], "放棄這次工作", **kw) is None


def test_unregistered_hub_returns_none():
    assert _judge(DecisionHub(), "放棄這次工作") is None


def test_engine_failure_returns_none():
    class Boom:
        spec = schedule_abandon_work_spec()

        def decide(self, request):
            raise RuntimeError("boom")

    hub = DecisionHub()
    hub.register(Boom.spec, Boom())
    assert _judge(hub, "放棄這次工作") is None


def test_path_absent_is_accepted():
    sig = _judge(_hub()[0], "放棄這次工作", path="absent")
    assert sig is not None and sig.path == "absent"


# ---- 決策日誌 ----

SECRET = "放棄這次工作"


def test_every_judgment_leaves_one_log_row_without_raw_text():
    sink = _Sink()
    hub, writer = _hub(sink)
    assert _judge(hub, SECRET, utterance_id="utt_a") is not None
    assert _judge(hub, "今天天氣如何，順便說一個秘密 ZZZ", utterance_id="utt_b") is None
    assert writer.flush(2.0)
    writer.close()
    assert len(sink.rows) == 2
    hit, miss = sink.rows
    assert hit["kind"] == KIND_SCHEDULE_ABANDON_WORK and hit["outcome"] == "abandon"
    assert hit["reason_code"] == "abandon_phrase" and hit["status"] == "ok" and hit["fallback"] is False
    assert hit["candidate_id"] == "utt_a" and hit["proposer_id"] == DECIDER_ID and hit["engine"] == "rule"
    assert miss["outcome"] == "none" and miss["reason_code"] == "abandon_no_match" and miss["candidate_id"] == "utt_b"
    dump = repr(sink.rows)
    assert SECRET not in dump and "ZZZ" not in dump and "秘密" not in dump and "discord:123" not in dump


def test_spec_is_sync_native_without_extra_plan_sections():
    spec = schedule_abandon_work_spec()
    assert spec.mode == "sync" and spec.allow_native and not spec.shadow and spec.plan_sections == ()
    assert AbandonWorkEngine().spec == spec
