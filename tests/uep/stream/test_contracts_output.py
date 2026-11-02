"""輸出端契約（T-07b PR ①）的型別層測試。零依賴，不跑管線。"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path

import pytest

from uep.stream.contracts import TurnResult, Utterance
from uep.stream.contracts.output import (
    BARGE_IN_PRESETS,
    CommitMode,
    ModeFlags,
    OutcomeStatus,
    PlayedEvidence,
    PolicySnapshot,
    SkipReason,
    SpokenRule,
    TurnOutcome,
    TurnRequest,
    resolve_spoken_rule,
    sink_can_reset,
    sink_first_frame_at,
    sink_is_active,
    sink_played_evidence,
)
from uep.stream.contracts.stages import SettleCapable, ThinkStage
from uep.stream.contracts.types import AudioChunk, TurnPhase
from uep.stream import tracer
from uep.stream.policies import StandaloneTicketIssuer, StaticPolicySource
from uep.fakes.stream.audio import FakeAudioSink
from uep.stream.io.sinks import NullSink


def _chunk(seconds: float = 0.5) -> AudioChunk:
    return AudioChunk(pcm=bytes(int(22_050 * seconds) * 2), sample_rate=22_050,
                      sentence_index=0, turn_id="t")


# --- ticket / issuer ---

def test_standalone_ticket_maps_source_and_matches() -> None:
    utt = Utterance(text="嗨", speaker_id="u1")
    ticket = StandaloneTicketIssuer(snapshot_version=3).issue(utt, reply_mode="active")
    assert ticket.turn_id == utt.turn_id and ticket.source_turn_id == utt.turn_id
    assert ticket.owner_id == "u1" and ticket.reply_mode == "active"
    assert ticket.snapshot_version == 3
    assert ticket.matches(utt)
    assert not ticket.matches(Utterance(text="別人"))


# --- snapshot ---

def test_snapshot_platform_presets() -> None:
    assert PolicySnapshot.for_platform("discord").barge_in_min_speech_s == 2.0
    assert PolicySnapshot.for_platform("web").barge_in_min_speech_s == 0.4
    assert PolicySnapshot().barge_in_min_speech_s == BARGE_IN_PRESETS["web"]
    custom = PolicySnapshot.for_platform("discord", barge_in_min_speech_s=1.0, version=2)
    assert custom.barge_in_min_speech_s == 1.0 and custom.version == 2
    with pytest.raises(ValueError):
        PolicySnapshot.for_platform("nope")


def test_snapshot_staleness_and_mode_rules() -> None:
    snap = PolicySnapshot(issued_at=100.0, ttl_s=5.0)
    assert not snap.is_stale(now=104.0) and snap.is_stale(now=106.0)
    assert not PolicySnapshot(ttl_s=None).is_stale(now=1e12)
    assert PolicySnapshot().flags_for("anything") == ModeFlags()

    class Rules:
        def flags_for(self, reply_mode):
            return ModeFlags(speak=reply_mode != "passive", barge_in=reply_mode != "passive")

    assert not PolicySnapshot(mode_rules=Rules()).flags_for("passive").speak


def test_static_policy_source_replace() -> None:
    src = StaticPolicySource()
    assert src.current().version == 0
    src.replace(PolicySnapshot(version=7))
    assert src.current().version == 7


# --- spoken rule / evidence ---

def test_resolve_spoken_rule() -> None:
    confirmed, sent = PlayedEvidence.PLAYBACK_CONFIRMED, PlayedEvidence.TRANSPORT_SENT
    assert resolve_spoken_rule(SpokenRule.AUTO, confirmed) is SpokenRule.PARTIAL
    assert resolve_spoken_rule(SpokenRule.AUTO, sent) is SpokenRule.WHOLE_SENTENCE
    # 明確指定者不被證據覆寫
    assert resolve_spoken_rule(SpokenRule.PARTIAL, sent) is SpokenRule.PARTIAL
    assert resolve_spoken_rule(SpokenRule.WHOLE_SENTENCE, confirmed) is SpokenRule.WHOLE_SENTENCE


def test_sink_helpers_defaults_for_plain_sink() -> None:
    sink = NullSink()
    assert sink_is_active(sink) is False
    assert sink_is_active(sink, fallback=True) is True
    assert sink_first_frame_at(sink) is None
    assert sink_played_evidence(sink) is PlayedEvidence.TRANSPORT_SENT
    assert not sink_can_reset(sink)


def test_fake_sink_optional_members() -> None:
    async def scenario() -> None:
        sink = FakeAudioSink(realtime=True)
        assert not sink.is_active and sink.first_frame_at is None
        assert sink_played_evidence(sink) is PlayedEvidence.PLAYBACK_CONFIRMED
        assert sink_can_reset(sink)
        await sink.write(_chunk(0.5))
        assert sink.first_frame_at is not None and sink.is_active
        sink.stop()
        assert not sink.is_active
        await sink.confirm_stopped()
        sink.reset()
        assert sink.first_frame_at is None
        await sink.write(_chunk(0.5))  # stop 後 reset 再 write
        assert sink.is_active

    asyncio.run(scenario())


# --- TurnResult 加法相容 / Outcome 結束路徑 ---

def test_turn_result_existing_call_still_valid() -> None:
    r = TurnResult(turn_id="a", phase=TurnPhase.DONE, spoken_text="x")
    assert r.skipped_reason is None and r.cut_short is False
    assert r.audio_started is False and r.first_audio_at is None and r.sentence_spans == []


def test_outcome_extends_result_with_defaults() -> None:
    o = TurnOutcome(turn_id="a", phase=TurnPhase.DONE)
    assert isinstance(o, TurnResult)
    assert o.status is OutcomeStatus.COMPLETED and o.attempt_id == 1
    assert o.queue_wait_s == 0.0 and o.regenerated == 0


@pytest.mark.parametrize(
    ("status", "phase", "extra"),
    [
        (OutcomeStatus.COMPLETED, TurnPhase.DONE, {}),
        (OutcomeStatus.CANCELLED, TurnPhase.CANCELLED,
         {"barge_in": True, "cancel_reason": "barge_in"}),
        (OutcomeStatus.CANCELLED, TurnPhase.CANCELLED, {"superseded": True}),
        (OutcomeStatus.CANCELLED, TurnPhase.CANCELLED,
         {"aborted": True, "cancel_reason": "shutdown"}),
        (OutcomeStatus.FAILED, TurnPhase.FAILED, {"error": "RuntimeError: x"}),
        (OutcomeStatus.SKIPPED, TurnPhase.DONE, {"skipped_reason": SkipReason.PASS}),
        (OutcomeStatus.DROPPED_BUSY, TurnPhase.CANCELLED,
         {"skipped_reason": SkipReason.DROPPED_BUSY}),
        (OutcomeStatus.VOIDED, TurnPhase.CANCELLED, {"skipped_reason": SkipReason.VOIDED}),
    ],
)
def test_outcome_can_express_every_end_path(status, phase, extra) -> None:
    """每個 submit 恰好一個 outcome：所有結束路徑都必須可表達且可區分。"""
    o = TurnOutcome(turn_id="a", phase=phase, status=status, **extra)
    assert o.status is status
    if status in (OutcomeStatus.DROPPED_BUSY, OutcomeStatus.VOIDED):
        assert o.skipped_reason == status.value
        assert not o.audio_started and o.generated_text == ""


def test_outcome_status_covers_invariant_paths() -> None:
    assert {s.value for s in OutcomeStatus} >= {
        "completed", "cancelled", "failed", "skipped", "dropped_busy", "voided",
    }


# --- request ---

def test_turn_request_defaults_and_separate_speech_end_stt_done() -> None:
    utt = Utterance(text="嗨")
    ticket = StandaloneTicketIssuer().issue(utt)
    req = TurnRequest(ticket=ticket, utterance=utt, sink_factory=lambda view: "no_sink")
    assert req.spoken_rule is SpokenRule.AUTO and req.commit_mode is CommitMode.SETTLED
    assert req.speech_end_at is None and req.stt_done_at is None  # 兩個獨立欄位
    assert req.history_spoken_only is False and req.retract_on_yield is False  # Q2／Q3 預設維持現況
    assert req.max_regenerate == 0
    names = {f.name for f in dataclasses.fields(TurnRequest)}
    assert {"speech_end_at", "stt_done_at"} <= names


# --- settle 選用 hook ---

def test_settle_is_optional_hook() -> None:
    class PlainThink:
        name = "plain"

        async def prepare(self): ...
        async def aclose(self): ...
        def stream(self, utterance, token): ...
        async def commit(self, turn_id, spoken_text, generated_text): ...

    class SettlingThink(PlainThink):
        async def settle(self, turn_id, spoken_text, generated_text): ...

    assert isinstance(PlainThink(), ThinkStage) and not isinstance(PlainThink(), SettleCapable)
    assert isinstance(SettlingThink(), ThinkStage) and isinstance(SettlingThink(), SettleCapable)


# --- tracer 常數 ---

def test_new_tracer_marks() -> None:
    assert tracer.MARK_QUEUE_ACQUIRED == "queue_acquired"
    assert tracer.MARK_FIRST_FRAME == "first_frame"
    t = tracer.LatencyTracer()
    t.start("x")
    t.mark("x", tracer.MARK_QUEUE_ACQUIRED)
    t.mark("x", tracer.MARK_FIRST_FRAME)


# --- 依賴方向 ---

def test_contracts_and_core_do_not_import_subsystems() -> None:
    root = Path(__file__).resolve().parents[3] / "uep" / "stream"
    pattern = re.compile(
        r"^\s*(?:from|import)\s+(echo_stt|echo_tts|echo_memory|torch|numpy|pydantic)\b", re.M
    )
    for sub in ("contracts", "audio", "io"):
        for f in (root / sub).rglob("*.py"):
            assert not pattern.search(f.read_text(encoding="utf-8")), f
    for f in root.glob("*.py"):
        assert not pattern.search(f.read_text(encoding="utf-8")), f
