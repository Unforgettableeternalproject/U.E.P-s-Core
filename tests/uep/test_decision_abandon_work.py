"""schedule.abandon_work 決策點（規則版 v1；不需資料庫）：四級分級、冪等 signal_id、fail-closed、原文不落地。"""
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
    ENGLISH_PHRASES_CONFIRM,
    ENGLISH_PHRASES_DIRECT,
    KIND_SCHEDULE_ABANDON_WORK,
    OUTCOMES,
    AbandonWorkEngine,
    abandon_signal_id,
    grade_abandon_text,
    judge_abandon_work,
    judge_abandon_work_graded,
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


_ARGS = {
    "utterance_id": "utt_01HABCDEF", "actor_ref": "discord:123", "actor_authenticated": True,
    "source_platform": "discord", "path": "turn", "now": NOW,
}


def _judge(hub, text, **kw):
    return judge_abandon_work(hub, text, **{**_ARGS, **kw})


def _graded(hub, text, **kw):
    return judge_abandon_work_graded(hub, text, **{**_ARGS, **kw})


# ---- 分級表（§3A.1）----

# direct：明確提到「行程」（含工作行程、行程階段）或「回主機」
DIRECT = [
    "結束這次行程", "結束這次行程。", "請結束今天的行程", "先結束本次行程階段", "結束行程", "結束這次行程回主機",
    "結束今天的行程階段然後回到主機待命", "結束今天的行程回主機", "結束這次工作行程", "結束了回主機", "先結束回到主機待命",
    "結束工作階段回主機", "結束了回主機",
    "end this schedule", "end this schedule session", "Please end this schedule session.", "end today's schedule",
    "end this schedule session and go back to the host", "end this and go back to the host",
    "end this work session and go back to the host",
]
# needs_confirmation：只提「階段」或既有工作類
CONFIRM = [
    "放棄這次工作", "放棄這次工作。", "放棄 這次 工作", "放棄這次工作了", "放棄這次工作吧", "請放棄這次工作",
    "麻煩你放棄這次工作", "我要放棄這次工作", "放棄今天的工作", "放棄本次工作", "放棄這次工作階段",
    "今天的工作不用做了", "今天的工作不用做了。", "今天工作不用做了", "這次工作不用再做了",
    "請先不要工作了", "先不要工作了", "先不要工作了！", "今天先不要工作了", "先不工作了", "先不要工作",
    "結束這次工作階段", "結束工作階段", "結束這次階段", "先結束本次階段了",
    "give up this work session", "Give up this work session.", "please abandon this work session",
    "give up this work", "end this work session",
]
# pause：無錨點的結束意圖（小詞庫）
PAUSE = [
    "今天先到這裡", "今天先到這裡吧", "今天先到這裡。", "先這樣吧", "收工", "收工了", "收工吧", "我們收工吧",
    "不用待在那邊了", "不用再待在那裡了", "休息一下", "先休息一下", "休息一下吧",
]
NONE = [
    # 單獨的「不用做了」、「算了」
    "不用做了", "不用做了。", "這個不用做了", "算了", "不用了", "好了",
    # 否定
    "不要放棄這次工作", "別放棄這次工作", "不想放棄這次工作", "我不放棄這次工作", "不要先不要工作了",
    "不要結束這次行程", "別結束這次行程", "不想結束這次行程", "do not end this schedule session",
    # 疑問
    "要不要放棄這次工作？", "要不要放棄這次工作", "放棄這次工作嗎", "放棄這次工作？", "放棄這次工作?", "可以放棄這次工作嗎",
    "是不是該放棄這次工作", "今天的工作不用做了嗎", "先不要工作了嗎？", "要不要結束這次行程", "結束這次行程嗎",
    "end this schedule session?", "休息一下嗎", "收工嗎",
    # 轉述／回顧
    "他說放棄這次工作", "她說今天的工作不用做了", "我剛剛說放棄這次工作", "昨天放棄這次工作", "如果放棄這次工作會怎樣",
    "他說他先不要工作了", "聽說今天的工作不用做了", "他說結束這次行程", "如果結束這次行程會怎樣",
    # 無關或空
    "放棄", "放棄這次", "工作", "好好工作", "繼續工作", "", "   ", "今天天氣如何", "放棄這次任務",
    "放棄這次工作，但是先把報告寫完", "give up", "do not give up this work session", "should I give up this work session?",
    # 收窄：自指開頭的「先不要工作了」、泛指的「這個／這回／這輪工作」
    "我先不要工作了", "我要先不要工作了", "我們先不工作了", "放棄這個工作", "放棄這輪工作", "放棄這回工作", "這個工作不用做了",
    "x" * 200,
    "結束這個行程", "結束這輪行程", "結束這次行程，然後幫我整理房間", "結束這個階段",
    # 無錨點、也不在 pause 詞庫內
    "結束這次活動", "結束這次聚會", "結束本次聊天", "結束這次社交", "這次聊天先到這裡", "這次活動結束了", "這次就先這樣結束",
    "這次就先這樣吧", "可以收工了", "先結束了回來吧", "結束回來吧", "回主機", "回到主機待命", "回主機吧",
    "結束", "結束了", "先這樣", "先到這裡", "回來吧", "結束這次任務",
    "end this session", "end this activity", "wrap up this outing", "finish this gathering", "wrap up this chat",
    "let's call it a day", "go back to the host", "end",
]


# 供 test_decision_commitment_change 的互斥檢查引用：命中（direct／needs_confirmation）與不命中（pause／none）
POSITIVE = DIRECT + CONFIRM
NEGATIVE = PAUSE + NONE


@pytest.mark.parametrize("text", DIRECT)
def test_direct_grade_yields_signal(text):
    assert grade_abandon_text(text)[0] == "direct"
    assert matches_abandon_work(text)
    sig = _judge(_hub()[0], text)
    assert isinstance(sig, AbandonWorkSignal)
    j = _graded(_hub()[0], text)
    assert j.grade == "direct" and j.signal is not None and j.reason_code == "abandon_anchored"


@pytest.mark.parametrize("text", CONFIRM)
def test_needs_confirmation_grade_has_no_signal(text):
    assert grade_abandon_text(text)[0] == "needs_confirmation"
    assert matches_abandon_work(text)  # 與 commitment.change 的互斥仍涵蓋
    assert _judge(_hub()[0], text) is None  # 收窄的 judge_abandon_work 只回 direct
    j = _graded(_hub()[0], text)
    assert j.grade == "needs_confirmation" and j.signal is None and j.reason_code == "abandon_unanchored"
    assert j.candidate_ref == "utt_01HABCDEF"


@pytest.mark.parametrize("text", PAUSE)
def test_pause_grade_has_no_abandon_signal(text):
    assert grade_abandon_text(text)[0] == "pause"
    assert not matches_abandon_work(text)  # pause 不與承諾判斷互斥
    assert _judge(_hub()[0], text) is None
    j = _graded(_hub()[0], text)
    assert j.grade == "pause" and j.signal is None and j.reason_code == "abandon_to_pause"


@pytest.mark.parametrize("text", NONE)
def test_none_grade(text):
    assert grade_abandon_text(text)[0] == "none"
    assert not matches_abandon_work(text)
    assert _judge(_hub()[0], text) is None
    assert _graded(_hub()[0], text) is None


def test_non_string_text_is_not_a_match():
    assert not matches_abandon_work(None)
    assert not matches_abandon_work(123)
    assert _judge(_hub()[0], None) is None
    assert _graded(_hub()[0], None) is None


def test_english_phrase_list_matches_documented():
    assert set(ENGLISH_PHRASES) == set(ENGLISH_PHRASES_DIRECT) | set(ENGLISH_PHRASES_CONFIRM)
    for phrase in ENGLISH_PHRASES_DIRECT:
        assert grade_abandon_text(phrase)[0] == "direct"
    for phrase in ENGLISH_PHRASES_CONFIRM:
        assert grade_abandon_text(phrase)[0] == "needs_confirmation"


def test_stage_only_never_direct_but_route_or_host_is():
    """D9：只提「階段」不得 direct；含「行程」二字（行程階段）或「回主機」才 direct。"""
    assert grade_abandon_text("結束工作階段")[0] == "needs_confirmation"
    assert grade_abandon_text("結束這次階段")[0] == "needs_confirmation"
    assert grade_abandon_text("放棄這次工作階段")[0] == "needs_confirmation"
    assert grade_abandon_text("結束行程階段")[0] == "direct"
    assert grade_abandon_text("結束工作階段回主機")[0] == "direct"


def test_outcomes_cover_four_grades():
    assert OUTCOMES == ("abandon", "abandon_confirm", "pause", "none")


# ---- 訊號欄位與冪等 ----


def test_signal_fields_pass_contract_and_have_no_owner_or_schedule_fields():
    sig = _judge(_hub()[0], "結束這次行程")
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
    a = _judge(hub, "結束這次行程", now=NOW)
    b = _judge(hub, "結束今天的行程", now=datetime(2026, 11, 13, 9, 5, tzinfo=UTC))  # 重送／換句型仍同一 utterance
    assert a.signal_id == b.signal_id == abandon_signal_id("utt_01HABCDEF")
    assert _judge(hub, "結束這次行程", utterance_id="utt_other").signal_id != a.signal_id


def test_unauthenticated_actor_still_yields_fact_for_consumer_to_judge():
    sig = _judge(_hub()[0], "結束這次行程", actor_authenticated=False)
    assert sig is not None and sig.actor_authenticated is False


# ---- fail-closed ----


@pytest.mark.parametrize("actor_ref", ["Discord:123", "小明", "", "a b", "x" * 129, None, 123])
def test_invalid_actor_ref_returns_none(actor_ref):
    assert _judge(_hub()[0], "結束這次行程", actor_ref=actor_ref) is None
    assert _graded(_hub()[0], "放棄這次工作", actor_ref=actor_ref) is None


@pytest.mark.parametrize("kw", [
    {"utterance_id": ""}, {"utterance_id": "bad id"}, {"utterance_id": "中文"}, {"utterance_id": None},
    {"utterance_id": "u" * 200}, {"source_platform": ""}, {"source_platform": None}, {"path": "other"},
    {"now": datetime(2026, 11, 13, 9, 0)}, {"now": "2026-11-13"}, {"actor_authenticated": "yes"},
])
def test_malformed_inputs_return_none_without_raising(kw):
    assert _judge(_hub()[0], "結束這次行程", **kw) is None
    assert _graded(_hub()[0], "放棄這次工作", **kw) is None


def test_unregistered_hub_returns_none():
    assert _judge(DecisionHub(), "結束這次行程") is None
    assert _graded(DecisionHub(), "放棄這次工作") is None


def test_engine_failure_returns_none():
    class Boom:
        spec = schedule_abandon_work_spec()

        def decide(self, request):
            raise RuntimeError("boom")

    hub = DecisionHub()
    hub.register(Boom.spec, Boom())
    assert _judge(hub, "結束這次行程") is None
    assert _graded(hub, "放棄這次工作") is None


def test_path_absent_is_accepted_by_the_judge_itself():
    """決策點本身不管來源路徑；不在場路徑不呼叫它是 orchestrator 的責任（D7）。"""
    sig = _judge(_hub()[0], "結束這次行程", path="absent")
    assert sig is not None and sig.path == "absent"


# ---- 決策日誌 ----

SECRET = "結束這次行程"


def test_every_judgment_leaves_one_log_row_without_raw_text():
    sink = _Sink()
    hub, writer = _hub(sink)
    assert _judge(hub, SECRET, utterance_id="utt_a") is not None
    assert _graded(hub, "放棄這次工作", utterance_id="utt_c") is not None
    assert _graded(hub, "收工", utterance_id="utt_d") is not None
    assert _judge(hub, "今天天氣如何，順便說一個秘密 ZZZ", utterance_id="utt_b") is None
    assert writer.flush(2.0)
    writer.close()
    assert len(sink.rows) == 4
    hit, confirm, pause, miss = sink.rows
    assert hit["kind"] == KIND_SCHEDULE_ABANDON_WORK and hit["outcome"] == "abandon"
    assert hit["reason_code"] == "abandon_anchored" and hit["status"] == "ok" and hit["fallback"] is False
    assert hit["candidate_id"] == "utt_a" and hit["proposer_id"] == DECIDER_ID and hit["engine"] == "rule"
    assert confirm["outcome"] == "abandon_confirm" and confirm["reason_code"] == "abandon_unanchored"
    assert pause["outcome"] == "pause" and pause["reason_code"] == "abandon_to_pause"
    assert miss["outcome"] == "none" and miss["reason_code"] == "abandon_no_match" and miss["candidate_id"] == "utt_b"
    dump = repr(sink.rows)
    assert SECRET not in dump and "ZZZ" not in dump and "秘密" not in dump and "discord:123" not in dump
    assert "放棄這次工作" not in dump and "收工" not in dump


def test_spec_is_sync_native_without_extra_plan_sections():
    spec = schedule_abandon_work_spec()
    assert spec.mode == "sync" and spec.allow_native and not spec.shadow and spec.plan_sections == ()
    assert AbandonWorkEngine().spec == spec
