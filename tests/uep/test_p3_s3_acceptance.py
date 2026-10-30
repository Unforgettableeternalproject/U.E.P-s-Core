"""P3 S3 驗收：ES 綁定（P3接入方案 §5 S3 列）。

V09 一入一出、journal 內 turn 事件序列、V07 取消後晚到輸出被丟（不產生 intent）、V12 舊 generation 輸出被拒。
以 ES 的 fakes 驅動；需 PostgreSQL 與已安裝的 echo_stream（未安裝時整檔 skip）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("echo_stream")

from echo_stream.contracts.types import AudioChunk

from tests.uep.echo_support import (
    SPEAKER,
    TEXT_TWO_SENTENCES,
    WAIT,
    build_rig,
)
from uep.output.delivery import DeliveryStatus

pytestmark = pytest.mark.usefixtures("session_factory")


@pytest.fixture
def rig(session_factory, tmp_path):
    made = []

    def _build(**over):
        r = build_rig(session_factory, tmp_path, **over)
        made.append(r)
        return r

    yield _build
    for r in made:
        r.core.shutdown()


# ---- V09：一入一出 ----

def test_v09_one_input_one_output_llm_and_tts_once(rig):
    r = rig()
    result = r.say(TEXT_TWO_SENTENCES)
    assert result.ticket is not None and result.reason_code == "issued"
    kinds = r.wait_terminal(result.ticket.turn_id)
    assert kinds.count("turn.started") == 1 and kinds.count("turn.completed") == 1
    # LLM 與 TTS 各一次
    assert r.think.stream_calls == 1 and r.speak.speak_calls == 1
    # 一入：journal 只有一筆 utterance；一出：兩句各一條 intent／delivery，沒有第二條 dispatch 路徑
    assert len(r.events(types=("input.utterance_final",))) == 1
    assert [i.sequence for i in r.intents(result.ticket.turn_id)] == [0, 1]
    deliveries = r.deliveries()
    assert [d.sequence for d in deliveries] == [0, 1]
    assert len({d.output_id for d in deliveries}) == 1  # 同一個 attempt 的 output_id
    # tracer 只有一條 trace，id 就是 ticket 的 turn_id（A2：兩個 id 空間相等，沒有孤兒 trace）
    assert r.tracer._order == [result.ticket.turn_id]
    assert r.think.committed[0][0] == result.ticket.turn_id


def test_v09_journal_turn_event_sequence(rig):
    r = rig()
    result = r.say()
    turn_id = result.ticket.turn_id
    kinds = r.wait_terminal(turn_id)
    order = [k for k in kinds if k.startswith("turn.")]
    assert order == ["turn.started", "turn.generation_completed", "turn.completed"]
    seqs = {e.event_type: e.journal_seq for e in r.events(turn_id)}
    assert seqs["turn.started"] < seqs["turn.generation_completed"] < seqs["turn.completed"]
    # turn_generation 在信封 fence；source 為 core.turn；turn_id 為 Core 簽發的 turn_<ULID>
    started = r.events(turn_id, ("turn.started",))[0]
    assert turn_id.startswith("turn_") and started.source == "core.turn"
    assert started.fence == {"turn_generation": result.ticket.turn_generation}


def test_utterance_text_goes_to_blob_not_payload(rig):
    r = rig()
    secret_text = "這是一句私人的話絕對不能進 journal payload"
    result = r.say(secret_text)
    r.wait_terminal(result.ticket.turn_id)
    event = r.events(types=("input.utterance_final",))[0]
    assert secret_text not in str(event.payload)
    assert event.payload_ref is not None and event.source == "core.echo_host"
    assert event.payload["speaker_ref"] == SPEAKER and event.idempotency_key.startswith("es:")
    assert event.actor_id == "actor_a"  # 由身分映射重建，不取自 payload


# ---- V07：取消後晚到輸出被丟、不產生 intent ----

def test_v07_late_chunks_after_cancel_are_discarded_and_create_no_intent(rig):
    r = rig(realtime=True, responses=("第一句話很長很長很長很長。第二句話也很長很長很長。第三句。",))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: bool(r.host.wrappers_of(turn_id)) and r.inner_sinks and r.inner_sinks[0].chunks, what="首個 chunk")
    r.voice(seconds=3.0)  # 插話：只取消語音，think 照常
    kinds = r.wait_terminal(turn_id)
    assert kinds[-1] == "turn.cancelled"
    cancelled = r.events(turn_id, ("turn.cancelled",))[0]
    assert cancelled.payload["reason_code"] == "barge_in"
    wrapper = r.host.wrappers_of(turn_id)[-1]
    sink = r.inner_sinks[-1]
    chunks_before, intents_before = len(sink.chunks), len(r.intents(turn_id))
    deliveries_before = len(r.deliveries())
    discarded_before = r.host.metrics["echo.discarded_chunks"]  # ES 自己在插話當下可能已有晚到的 chunk
    late = [AudioChunk(pcm=b"\x00\x01" * 800, sample_rate=22050, turn_id=turn_id, sentence_index=9, chunk_index=i)
            for i in range(3)]

    async def push():
        for chunk in late:
            await wrapper.write(chunk)  # 不拋例外

    r.run_on_es(push())
    r.host._worker.drain(WAIT)
    assert len(sink.chunks) == chunks_before  # 內層沒收到
    assert r.host.metrics["echo.discarded_chunks"] == discarded_before + 3
    assert r.host.discarded_by_turn[turn_id] == discarded_before + 3
    assert len(r.intents(turn_id)) == intents_before and len(r.deliveries()) == deliveries_before
    assert not any(i.sequence == 9 for i in r.intents(turn_id))


# ---- V12：舊 generation 的輸出被拒 ----

def test_v12_old_generation_output_rejected_before_inner_write(rig):
    r = rig(realtime=True, responses=("第一句話很長很長很長很長很長。第二句話也很長很長很長很長。第三句。",))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: r.inner_sinks and r.inner_sinks[0].chunks, what="首個 chunk")
    # 模擬 Core 讓該 turn 的 generation 前進（例如取代／撤銷）而 ES 還在寫舊輸出
    r.host.generations.bump(turn_id)
    r.wait_for(lambda: bool(r.events(turn_id, ("output.rejected",))), what="output.rejected")
    sink = r.inner_sinks[0]
    r.wait_terminal(turn_id)
    count = len(sink.chunks)
    assert count >= 1  # 前進之前寫出的不可能收回；之後一個都不會增加
    assert r.kinds(turn_id)[-1] == "turn.cancelled"  # 被拒的輪以 turn.cancelled 結束，不是 completed(skipped)
    assert r.events(turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "admission_rejected"
    rejected = r.events(turn_id, ("output.rejected",))[0]
    assert rejected.payload["reason_code"] == "REJECT_STALE"
    assert rejected.fence["turn_generation"] == result.ticket.turn_generation
    wrapper = r.host.wrappers_of(turn_id)[0]
    assert wrapper.rejected is not None and wrapper.stopped
    late = AudioChunk(pcm=b"\x00\x01" * 800, sample_rate=22050, turn_id=turn_id, sentence_index=0, chunk_index=99)
    r.run_on_es(wrapper.write(late))
    assert len(sink.chunks) == count


def test_v12_revoked_lease_cancels_turn_with_core_reason_and_rejects_new_outputs(rig):
    r = rig(realtime=True, responses=("第一句話很長很長很長很長很長。第二句話也很長很長很長很長。第三句。",))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    gen0 = result.ticket.turn_generation
    r.wait_for(lambda: r.inner_sinks and r.inner_sinks[0].chunks, what="首個 chunk")
    r.core.presence.revoke("presence_lost_test")
    kinds = r.wait_terminal(turn_id)
    assert kinds[-1] == "turn.cancelled"
    cancelled = r.events(turn_id, ("turn.cancelled",))[0]
    assert cancelled.payload["reason_code"] == "presence_lost"  # Core 發起：以 Core 的原因碼為準
    assert cancelled.fence["turn_generation"] > gen0  # 取消後 generation 前進
    assert r.host.generations.get(turn_id) > gen0
    # Lease 已丟棄：之後的 utterance 不開 turn（Core 否決權）
    again = r.say("還在嗎")
    assert again.ticket is None and again.reason_code == "presence_inactive"
    skipped = r.events(types=("turn.skipped",))
    assert skipped and skipped[-1].payload["reason_code"] == "presence_inactive"
    assert {d.status for d in r.deliveries()} <= {
        DeliveryStatus.SENT.value, DeliveryStatus.ACCEPTED.value, DeliveryStatus.RENDERED_PLAYED.value,
        DeliveryStatus.CANCELLED.value, DeliveryStatus.REJECTED.value,
    }


def test_ticket_utterance_and_chunks_share_one_turn_id(rig):
    r = rig()
    result = r.say()
    r.wait_terminal(result.ticket.turn_id)
    assert r.think.committed[0][0] == result.ticket.turn_id
    assert all(c.turn_id == result.ticket.turn_id for s in r.inner_sinks for c in s.chunks)
