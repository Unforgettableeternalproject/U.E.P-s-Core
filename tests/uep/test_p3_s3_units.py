"""P3 S3 單元測試（不需資料庫）：sink 包裝、outcome 對映、記憶轉接、成員資格、generation 登記簿、端點接納。"""
from __future__ import annotations

import asyncio
import functools
import threading
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("echo_stream")

from echo_stream.contracts.output import OutcomeStatus, PlayedEvidence, TurnOutcome
from echo_stream.contracts.types import AudioChunk, TurnPhase
from echo_stream.fakes.audio import FakeAudioSink

from uep.context.snapshot import MemoryStatus
from uep.contracts.presence import AcquireResult, AuthorizationTuple, GateDecision
from uep.echo.endpoint import EchoEndpoint, StopNotice
from uep.echo.memory import GuildRoster, MemoryRetrieveBridge, make_memory_resolver
from uep.echo.outcomes import facts_from_outcome
from uep.echo.policy import CoreModeRules, HostPolicySource
from uep.echo.sink import AdmittingSink
from uep.endpoint.protocol import (
    AcquireReply,
    ConfirmReply,
    PresenceSnapshot,
    RenewReply,
)
from uep.memory.access import GuildMembership
from uep.memory.protocol import RetrieveResult
from uep.output.intent import ControlKind, ControlScope
from uep.turn.generations import TurnGenerations


def sync(fn):
    """環境沒有 pytest-asyncio：以 asyncio.run 跑 async 測試。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


def chunk(index: int, duration_s: float = 0.5, sample_rate: int = 1000, turn_id: str = "turn_x") -> AudioChunk:
    frames = int(duration_s * sample_rate)
    return AudioChunk(pcm=b"\x00\x00" * frames, sample_rate=sample_rate, turn_id=turn_id, sentence_index=index)


class Hooks:
    def __init__(self, decision: GateDecision = GateDecision.ADMIT) -> None:
        self.decision = decision
        self.sentences: list[tuple[int, int, int]] = []
        self.rejected: list[GateDecision] = []
        self.discarded = 0

    def make(self, inner) -> AdmittingSink:
        return AdmittingSink(
            inner, turn_id="turn_x", attempt_no=1, output_id="out_x", admit=lambda: self.decision,
            on_sentence=lambda w, i, sr, ch: self.sentences.append((i, sr, ch)),
            on_reject=lambda w, d: self.rejected.append(d), on_discard=lambda w: self._discard(),
        )

    def _discard(self) -> None:
        self.discarded += 1


# ---- AdmittingSink ----

@sync
async def test_sink_admits_writes_and_triggers_one_intent_per_sentence():
    hooks, inner = Hooks(), FakeAudioSink(realtime=False)
    sink = hooks.make(inner)
    for index in (0, 0, 1):
        await sink.write(chunk(index))
    assert len(inner.chunks) == 3
    assert hooks.sentences == [(0, 1000, 1), (1, 1000, 1)]  # 每句第一個通過接納的 chunk 才觸發


@sync
async def test_sink_rejection_never_raises_and_stops_inner():
    hooks, inner = Hooks(GateDecision.REJECT_STALE), FakeAudioSink(realtime=False)
    sink = hooks.make(inner)
    await sink.write(chunk(0))  # 不拋例外：拋了 coordinator 會把整輪判成 FAILED
    await sink.write(chunk(0))
    assert inner.chunks == [] and sink.stopped and sink.rejected is GateDecision.REJECT_STALE
    assert hooks.rejected == [GateDecision.REJECT_STALE]  # 只通知一次
    assert hooks.discarded == 1 and hooks.sentences == []


@sync
async def test_sink_late_chunks_after_stop_are_discarded_without_intent():
    hooks, inner = Hooks(), FakeAudioSink(realtime=False)
    sink = hooks.make(inner)
    await sink.write(chunk(0))
    sink.stop()
    sink.stop()  # 冪等
    for _ in range(3):
        await sink.write(chunk(1))
    assert len(inner.chunks) == 1 and hooks.discarded == 3
    assert hooks.sentences == [(0, 1000, 1)]  # 晚到的第 1 句沒有觸發 intent


@sync
async def test_sink_stopped_during_inner_write_is_not_recorded_as_a_sentence():
    hooks = Hooks()

    class StopsMidWrite(FakeAudioSink):
        async def write(self, c):
            sink.stop()  # 寫入進行中被停止（barge-in 的執行緒同步呼叫 stop）
            await super().write(c)

    inner = StopsMidWrite(realtime=False)
    sink = hooks.make(inner)
    await sink.write(chunk(0))
    assert hooks.sentences == [] and sink.stopped  # 不記句界、不觸發 intent
    assert sink.collect_evidence() == []


@sync
async def test_sink_inner_failure_still_propagates():
    class Boom(FakeAudioSink):
        async def write(self, c):
            raise RuntimeError("播放器壞了")

    sink = Hooks().make(Boom())
    with pytest.raises(RuntimeError):
        await sink.write(chunk(0))  # 真的寫入錯誤要讓 coordinator 看到；只有「接納被拒」不拋


@sync
async def test_sink_passes_through_optional_capabilities_and_no_more():
    inner = FakeAudioSink(realtime=False)
    sink = Hooks().make(inner)
    await sink.write(chunk(0))
    assert sink.played_evidence is PlayedEvidence.PLAYBACK_CONFIRMED
    assert sink.first_frame_at is not None and sink.is_active is False
    await sink.confirm_stopped()
    assert sink.played_duration_s == pytest.approx(0.5)
    assert not hasattr(sink, "reset")  # 包裝帶每輪狀態，不可重用

    class Bare:  # 只有必備成員的 sink：包裝不能憑空宣稱有選用能力
        played_duration_s = 0.0

        async def write(self, c): ...
        def stop(self): ...
        async def drain(self): ...

    bare = Hooks().make(Bare())
    assert not hasattr(bare, "first_frame_at") and getattr(bare, "is_active", None) is None
    assert getattr(bare, "played_evidence", None) is None  # ES 預設視為 transport_sent（保守）


@sync
async def test_sink_collects_played_range_per_sentence():
    hooks, inner = Hooks(), FakeAudioSink(realtime=False)
    sink = hooks.make(inner)
    for index in (0, 1, 2):
        await sink.write(chunk(index, 0.5))  # 每句 0.5 秒
    inner._frozen_played = 0.75  # 第 0 句播完、第 1 句播一半、第 2 句沒播
    evidence = sink.collect_evidence()
    assert [e.sentence_index for e in evidence] == [0, 1]
    assert evidence[0].complete and evidence[0].played_range() == {"from_sample": 0, "to_sample": 500, "complete": True}
    assert not evidence[1].complete and evidence[1].played_range()["to_sample"] == 250
    assert all(e.confirmed for e in evidence)


@sync
async def test_sink_without_playback_confirmation_reports_unconfirmed():
    inner = FakeAudioSink(realtime=False)
    inner.played_evidence = PlayedEvidence.TRANSPORT_SENT
    sink = Hooks().make(inner)
    await sink.write(chunk(0))
    assert [e.confirmed for e in sink.collect_evidence()] == [False]  # Discord：停在 sent，不升級


# ---- outcome 對映 ----

def outcome(status: OutcomeStatus, **over) -> TurnOutcome:
    data = {"turn_id": "turn_x", "phase": TurnPhase.DONE, "generated_text": "你好", "spoken_text": "你好"}
    data.update(over)
    return TurnOutcome(status=status, **data)


@pytest.mark.parametrize(("out", "status", "reason", "gen_ok"), [
    (outcome(OutcomeStatus.COMPLETED), "completed", None, True),
    (outcome(OutcomeStatus.SKIPPED, skipped_reason="pass"), "skipped", "pass", True),
    (outcome(OutcomeStatus.SKIPPED, skipped_reason="mode_silent"), "skipped", "mode_silent", True),
    (outcome(OutcomeStatus.SKIPPED, skipped_reason="play_failed: 例外文字不能進 journal"), "skipped", "play_failed", True),
    (outcome(OutcomeStatus.CANCELLED, phase=TurnPhase.CANCELLED, barge_in=True, cancel_reason="barge_in",
             metadata={"reason_code": "barge_in"}), "cancelled", "barge_in", True),
    (outcome(OutcomeStatus.CANCELLED, phase=TurnPhase.CANCELLED, aborted=True, metadata={"reason_code": "superseded"}),
     "cancelled", "superseded", False),
    (outcome(OutcomeStatus.VOIDED, phase=TurnPhase.CANCELLED, aborted=True, metadata={"reason_code": "yielded"}),
     "voided", "yielded", False),
    (outcome(OutcomeStatus.CANCELLED, phase=TurnPhase.CANCELLED, aborted=True, metadata={"reason_code": "shutdown"}),
     "cancelled", "shutdown", False),
    (outcome(OutcomeStatus.FAILED, phase=TurnPhase.FAILED, error="RuntimeError: x"), "failed", "upstream_error", False),
    (outcome(OutcomeStatus.DROPPED_BUSY, phase=TurnPhase.CANCELLED), "dropped_busy", "dropped_busy", False),
])
def test_outcome_mapping(out, status, reason, gen_ok):
    facts = facts_from_outcome(out)
    assert (facts.status, facts.reason_code, facts.generation_ok) == (status, reason, gen_ok)


def test_outcome_spoken_text_unknown_is_not_reported_as_empty_fact():
    facts = facts_from_outcome(outcome(OutcomeStatus.COMPLETED, spoken_known=False, spoken_text=""))
    assert facts.spoken_text == "" and facts.generated_len == 2


# ---- 記憶轉接 ----

class SpyPort:
    """MemoryPort 替身（含 Access Layer 才有的 retrieve_for_audience）。"""

    def __init__(self, result: RetrieveResult | None = None, with_audience_api: bool = True) -> None:
        self.calls: list[tuple] = []
        self.result = result or RetrieveResult(text="記得的事")
        self.threads: list[int] = []
        if with_audience_api:
            self.retrieve_for_audience = self._by_audience

    def _by_audience(self, query, audience, *, actor_id, **kw):
        self.threads.append(threading.get_ident())
        self.calls.append(("audience", query, audience, actor_id, kw))
        return self.result

    def retrieve(self, scope, query, audience, **kw):
        self.threads.append(threading.get_ident())
        self.calls.append(("scope", scope, query, audience, kw))
        return self.result


@sync
async def test_bridge_denies_when_audience_missing_and_never_calls_port():
    port = SpyPort()
    bridge = MemoryRetrieveBridge(port, "discord:111")
    assert await bridge.retrieve("你好", actor_id="actor_a") is None  # 不預設 public
    assert await bridge.retrieve("你好", audience="", actor_id="actor_a") is None
    assert port.calls == []


@sync
async def test_bridge_forwards_actor_audience_and_known_kwargs_in_executor():
    port = SpyPort()
    bridge = MemoryRetrieveBridge(port, "discord:111")
    text = await bridge.retrieve(
        "你好", audience="scope_private_owner", actor_id="actor_a", session_id="seg_1", shared_turn=True,
        present_uids=["111"], speaker_ref="discord:111", evil="x",
    )
    assert text == "記得的事"
    kind, query, audience, actor, kw = port.calls[0]
    assert (kind, query, audience, actor) == ("audience", "你好", "scope_private_owner", "actor_a")
    assert kw == {"session_id": "seg_1", "shared_turn": True, "present_uids": ["111"]}  # speaker_ref 不進授權路徑
    assert port.threads[0] != threading.get_ident()  # 阻塞呼叫不在 loop 執行緒上


@sync
async def test_bridge_falls_back_to_scope_retrieve_and_maps_empty_to_none():
    port = SpyPort(RetrieveResult(text="", memory_status=MemoryStatus.TIMEOUT), with_audience_api=False)
    bridge = MemoryRetrieveBridge(port, "scope_private_owner")
    assert await bridge.retrieve("q", audience="scope_private_owner", actor_id="a") is None
    assert port.calls[0][:2] == ("scope", "scope_private_owner")
    assert bridge.last_result is not None and bridge.last_result.memory_status is MemoryStatus.TIMEOUT


@sync
async def test_memory_resolver_caches_per_scope():
    resolve = make_memory_resolver(SpyPort())
    assert resolve("a") is resolve("a") and resolve("a") is not resolve("b")


# ---- GuildMembership ----

def test_roster_implements_guild_membership_port_and_derives_present_uids():
    roster = GuildRoster({"123": {"actor_a": "111", "actor_b": "222"}})
    port: GuildMembership = roster  # 結構相容（Protocol 非 runtime_checkable；完整接線見 test_p3_s3_host）
    assert callable(port.is_member) and callable(port.members)
    assert roster.is_member("123", "actor_a") and not roster.is_member("123", "actor_z")
    assert not roster.is_member("999", "actor_a")  # 未知場域沒有成員
    assert sorted(roster.members("123")) == ["111", "222"]
    roster.join("123", "111")
    roster.join("123", "999")  # 非成員不會出現在在場名單
    assert roster.present_uids("123") == ["111"]
    roster.leave("123", "111")
    assert roster.present_uids("123") == []
    roster.join("123", "222")
    roster.set_members("123", {"actor_a": "111"})  # 成員被移出：在場名單同步裁掉
    assert roster.present_uids("123") == []


# ---- TurnGenerations ----

def test_generations_are_global_monotonic_and_bump_only_known_turns():
    gens = TurnGenerations(keep=2)
    a, b = gens.begin("turn_a"), gens.begin("turn_b")
    assert b > a and gens.get("turn_a") == a
    bumped = gens.bump("turn_a")
    assert bumped > b and gens.get("turn_a") == bumped and gens.get("turn_b") == b
    assert gens.bump("turn_unknown") is None and gens.get("turn_unknown") is None  # 不憑空建立；Gate 對 None 一律拒絕
    gens.finish("turn_a")
    gens.finish("turn_b")  # 結束後才可被修剪（live 的 turn 不會被修剪）
    gens.begin("turn_c")  # keep=2：最舊的被修剪
    assert gens.get("turn_b") is None and gens.is_current("turn_c", gens.get("turn_c"))


# ---- 策略 ----

def test_policy_source_snapshot_is_atomic_and_versioned():
    src = HostPolicySource("web")
    first = src.current()
    second = src.publish()
    assert second.version == first.version + 1 and src.current() is second and first.version == 1
    assert src.current().barge_in_min_speech_s == 0.4
    assert src.publish(platform="discord").barge_in_min_speech_s == 2.0  # 平台 preset
    rules = CoreModeRules()
    assert rules.flags_for("respond").speak and not rules.flags_for("passive").speak
    assert not rules.flags_for("passive").barge_in


# ---- 端點接納（不碰 DB）----

class FakeCore:
    def __init__(self) -> None:
        self.auth = AuthorizationTuple("uep", "ep_echo", "lease_1", 4, 2, 3)

    def acquire(self, location):
        return AcquireReply(AcquireResult.GRANTED, self.auth, 30.0)

    def confirm_activated(self, lease_id, generation):
        from uep.contracts.presence import ConfirmResult
        return ConfirmReply(ConfirmResult.ACCEPTED, 30.0)

    def renew(self, *a):
        from uep.contracts.presence import RenewResult
        return RenewReply(RenewResult.RENEWED, 30.0)

    def release(self, *a):
        from uep.contracts.presence import ReleaseResult
        return ReleaseResult.RELEASED

    def confirm_quiesced(self, *a):
        from uep.contracts.presence import ConfirmResult
        return ConfirmResult.ACCEPTED

    def snapshot(self):
        return PresenceSnapshot("ACTIVE", True, "lease_1", 3, 2, 4, 30.0, False)


def test_endpoint_admit_output_local_checks_and_stop_notices():
    notices: list[StopNotice] = []
    core = FakeCore()
    ep = EchoEndpoint("ep_echo", core, on_stop=notices.append)
    valid = datetime.now(UTC) + timedelta(seconds=30)
    assert ep.admit_output(core.auth, 5, valid) is GateDecision.REJECT_NO_PRESENCE  # 沒有 Lease
    assert ep.start_lease() is AcquireResult.GRANTED and ep.lease_auth() == core.auth
    assert ep.admit_output(core.auth, 5, valid) is GateDecision.ADMIT
    stale = AuthorizationTuple("uep", "ep_echo", "lease_old", 4, 2, 3)
    assert ep.admit_output(stale, 5, valid) is GateDecision.REJECT_STALE
    assert ep.admit_output(core.auth, 5, datetime.now(UTC) - timedelta(seconds=1)) is GateDecision.REJECT_EXPIRED
    from tests.uep.endpoint_support import signal
    ep.on_control(signal(ControlKind.STOP, scope=ControlScope.TURN, generation=3, core_epoch=2, turn_generation=5))
    ep.local_tick()
    assert notices == [StopNotice("control_stop", 5)]
    assert ep.admit_output(core.auth, 5, valid) is GateDecision.REJECT_STALE  # 該 turn 已 stop，晚到輸出不得播
    assert ep.admit_output(core.auth, 6, valid) is GateDecision.ADMIT  # 較新的回合不受影響
    ep.on_control(signal(ControlKind.REVOKE, generation=3, core_epoch=2))
    ep.local_tick()
    assert notices[-1] == StopNotice("presence_lost") and ep.lease_auth() is None
    assert ep.admit_output(core.auth, 6, valid) is not GateDecision.ADMIT
    ep.close()


def test_endpoint_global_stop_signal_notifies_once_and_send_is_idempotent():
    notices: list[StopNotice] = []
    core = FakeCore()
    ep = EchoEndpoint("ep_echo", core, on_stop=notices.append)
    assert ep.start_lease() is AcquireResult.GRANTED
    from tests.uep.endpoint_support import intent_for_auth, signal
    intent = intent_for_auth(
        core.auth, kind="audio_stream", content_format={"sample_rate": 1, "channels": 1, "codec": "x"},
        valid_until=datetime.now(UTC) + timedelta(seconds=30),
    )
    first = ep.send(intent, "dsp_1")
    assert first.evidence.value == "transport_sent" and ep.send(intent, "dsp_1") is first  # 同 dispatch_id 不重複效果
    assert ep.lookup("dsp_1").outcome == "found"
    ep.on_control(signal(ControlKind.STOP, generation=3, core_epoch=2))
    ep.local_tick()
    ep.local_tick()
    assert notices == [StopNotice("global_stop")]
    ep.close()
