"""uep.fakes 本身行為（無 DB）。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.uep.factories import make_intent
from uep.adapters.output import OutputAdapter, SendFailed, SendResponseLost
from uep.adapters.runtime import Runtime, RuntimeEvent, RuntimeRequest
from uep.adapters.tool import ToolCall, ToolExecutor, ToolResponseLost
from uep.fakes import runtime as rt
from uep.fakes.crash import CrashInjector, SimulatedCrash
from uep.fakes.output import FakeOutputAdapter
from uep.fakes.runtime import FakeRuntime
from uep.fakes.tool import FakeToolExecutor
from uep.output.delivery import DeliveryEvidence
from uep.output.intent import ControlSignal


def _request(attempt_id: str = "attempt_1", **kw) -> RuntimeRequest:
    data = {
        "run_id": "run_1", "attempt_id": attempt_id, "snapshot_ref": "snap_1", "capability_set": {},
        "cancel_scope": {"turn_generation": 5}, "deadline": datetime.now(UTC) + timedelta(minutes=1),
        "mode": "direct_chat",
    }
    data.update(kw)
    return RuntimeRequest(**data)


def _call(action_id="act_1", idempotency="via_action_id") -> ToolCall:
    return ToolCall(action_id=action_id, tool="t", params={}, params_hash="h",
                    side_effect_class="external_effect", idempotency=idempotency)


# ---- 協定相容 ----
def test_fakes_satisfy_protocols():
    assert isinstance(FakeRuntime(), Runtime)
    assert isinstance(FakeToolExecutor(), ToolExecutor)
    assert isinstance(FakeOutputAdapter(), OutputAdapter)


# ---- RuntimeEvent 型別 ----
def test_runtime_event_validation():
    with pytest.raises(ValueError):
        RuntimeEvent(attempt_id="a", seq=1, kind="text_delta")  # 缺同名欄位
    with pytest.raises(ValueError):
        RuntimeEvent(attempt_id="a", seq=1, kind="error", error={"code": "NOPE"})
    with pytest.raises(ValueError):
        RuntimeEvent(attempt_id="a", seq=1, kind="text_delta", text_delta="x", result={})


# ---- FakeRuntime ----
def test_runtime_streams_script_with_monotonic_seq():
    fake = FakeRuntime()
    fake.script([rt.text("你"), rt.text("好"), rt.result()])
    aid = fake.start(_request())
    events = list(fake.stream(aid))
    assert [e.kind for e in events] == ["text_delta", "text_delta", "result"]
    assert [e.seq for e in events] == [1, 2, 3]
    assert fake.start_calls == 1 and fake.status(aid).state == "completed"


def test_runtime_resume_requires_checkpoint_and_counts():
    fake = FakeRuntime()
    with pytest.raises(ValueError):
        fake.resume(_request())
    fake.resume(_request("attempt_2", checkpoint_ref="ckpt_1"))
    assert fake.resume_calls == 1
    with pytest.raises(ValueError):  # attempt_id 不可重用（R-4）
        fake.start(_request("attempt_2"))


def test_cancel_isolated_drop_policy_discards_late_text_and_result_but_keeps_receipt():
    fake = FakeRuntime(cancel_ack="isolated", late_policy="drop")
    fake.script([rt.text("a"), rt.text("late1"), rt.tool_intent("send_mail"),
                 rt.receipt("act_1", "rcpt_x"), rt.result()])
    aid = fake.start(_request())
    it = fake.stream(aid)
    assert next(it).text_delta == "a"
    assert fake.cancel(aid, "user_interrupt") == "isolated"
    assert list(it) == []  # drop：晚到事件一個都不產生
    assert fake.discarded[aid] == 2  # text_delta + result（VC-3-1）
    assert fake.dropped_tool_intents[aid] == 1
    assert fake.receipts[aid] == [{"action_id": "act_1", "receipt_id": "rcpt_x"}]  # receipt 不丟棄（R-2）
    assert fake.status(aid).state == "isolated"


def test_cancel_isolated_emit_policy_really_emits_late_events():
    fake = FakeRuntime(cancel_ack="isolated")  # 預設 emit
    fake.script([rt.text("a"), rt.text("late1"), rt.receipt("act_1", "rcpt_x"), rt.result()])
    aid = fake.start(_request())
    it = fake.stream(aid)
    next(it)
    fake.cancel(aid, "user_interrupt")
    late = list(it)
    assert [e.kind for e in late] == ["text_delta", "result"]  # 由 Core 負責丟棄
    assert fake.emitted_late[aid] == 2 and not fake.discarded
    assert len(fake.receipts[aid]) == 1


def test_cancel_unknown_attempt_is_idempotent():
    fake = FakeRuntime()
    assert fake.cancel("attempt_nope", "x") == "stopped"
    assert fake.cancel("attempt_nope", "y") == "stopped"
    assert fake.status("attempt_nope").state == "cancelled"


def test_cancel_stopped_stops_generation_and_is_idempotent():
    fake = FakeRuntime(cancel_ack="stopped")
    fake.script([rt.text("a"), rt.text("b"), rt.result()])
    aid = fake.start(_request())
    it = fake.stream(aid)
    next(it)
    assert fake.cancel(aid, "r1") == "stopped"
    fake.cancel_ack = "isolated"
    assert fake.cancel(aid, "r2") == "stopped"  # 冪等：以第一次 ack 為準
    assert list(it) == [] and not fake.discarded and not fake.emitted_late
    assert len(fake.cancel_calls) == 2


def test_receipt_before_cancel_is_recorded_not_emitted():
    fake = FakeRuntime()
    fake.script([rt.receipt("act_1", "rcpt_x"), rt.result()])
    aid = fake.start(_request())
    assert [e.kind for e in fake.stream(aid)] == ["result"]
    assert len(fake.receipts[aid]) == 1


def test_runtime_crash_point():
    fake = FakeRuntime()
    fake.script([rt.text("a"), rt.text("b")])
    fake.crash.arm("runtime.before_event", on_hit=2)
    it = fake.stream(fake.start(_request()))
    assert next(it).text_delta == "a"
    with pytest.raises(SimulatedCrash):
        next(it)


# ---- FakeToolExecutor ----
def test_tool_counts_executions_separately_from_calls():
    ex = FakeToolExecutor()
    r1 = ex.execute(_call())
    r2 = ex.execute(_call())  # via_action_id：同 action 不重跑
    assert r1 is r2 and ex.execution_count("act_1") == 1 and ex.call_count("act_1") == 2
    ex.execute(_call("act_2", idempotency="none"))
    ex.execute(_call("act_2", idempotency="none"))
    assert ex.execution_count("act_2") == 2


def test_tool_response_lost_after_exec_is_recorded_and_lookup_finds_it():
    ex = FakeToolExecutor()
    ex.execute_behavior("act_1", "lose_after_exec")
    with pytest.raises(ToolResponseLost):
        ex.execute(_call())
    assert ex.execution_count("act_1") == 1
    found = ex.lookup_receipt("act_1")
    assert found.outcome == "found" and found.receipt.action_id == "act_1"


def test_tool_lost_before_exec_lookup_confirmed_not_executed():
    ex = FakeToolExecutor()
    ex.execute_behavior("act_1", "lose_before_exec")
    with pytest.raises(ToolResponseLost):
        ex.execute(_call())
    assert ex.execution_count("act_1") == 0
    assert ex.lookup_receipt("act_1").outcome == "confirmed_not_executed"


def test_tool_non_authoritative_lookup_cannot_confirm_absence():
    ex = FakeToolExecutor(lookup_authoritative=False)
    assert ex.lookup_authoritative is False
    assert ex.lookup_receipt("act_1").outcome == "undetermined"
    ex.lookup_authoritative = True
    assert ex.lookup_receipt("act_1").outcome == "confirmed_not_executed"


def test_tool_lookup_three_outcomes_and_record_loss_injection():
    ex = FakeToolExecutor()
    ex.execute(_call("act_1", idempotency="none"))
    ex.lookup_outcome("act_1", "found")
    assert ex.lookup_receipt("act_1").outcome == "found"
    # 已執行但查核遺失紀錄：兩種誤報
    ex.lookup_outcome("act_1", "undetermined")
    assert ex.lookup_receipt("act_1").outcome == "undetermined"
    ex.lookup_outcome("act_1", "confirmed_not_executed")
    assert ex.lookup_receipt("act_1").outcome == "confirmed_not_executed"
    assert ex.execution_count("act_1") == 1  # 實際上已執行過一次
    ex.lookup_outcome("act_9", "found")
    with pytest.raises(ValueError):  # 未執行卻要求回 receipt 是測試錯誤
        ex.lookup_receipt("act_9")


def test_tool_unverified_receipt_mode():
    ex = FakeToolExecutor()
    ex.unverified_receipt("act_1")
    assert ex.execute(_call()).source_verified is False
    assert ex.execute(_call("act_2")).source_verified is True


def test_tool_fail_mode_and_crash_points():
    ex = FakeToolExecutor()
    ex.execute_behavior("act_1", "fail")
    assert ex.execute(_call()).succeeded is False
    ex.crash.arm("tool.after_execute")
    with pytest.raises(SimulatedCrash):
        ex.execute(_call("act_2"))
    assert ex.execution_count("act_2") == 1  # 崩潰發生在副作用之後
    assert ex.lookup_receipt("act_2").outcome == "found"
    ex.crash.arm("tool.before_execute")
    with pytest.raises(SimulatedCrash):
        ex.execute(_call("act_3"))
    assert ex.execution_count("act_3") == 0


def test_simulated_crash_not_swallowed_by_except_exception():
    inj = CrashInjector()
    inj.arm("p")
    with pytest.raises(SimulatedCrash):
        try:
            inj.hit("p")
        except Exception:  # noqa: BLE001
            pytest.fail("SimulatedCrash 不應被 except Exception 吞掉")
    inj.hit("p")  # 觸發後自動解除
    assert inj.hits["p"] == 2


# ---- FakeOutputAdapter ----
def test_output_played_and_transport_only():
    out = FakeOutputAdapter()
    intent = make_intent()
    assert out.send(intent, "dsp_1").evidence is DeliveryEvidence.LOCAL_PLAYBACK
    out.send_behavior(intent.output_id, 1, "transport_only")
    second = make_intent(output_id=intent.output_id, sequence=1)
    res = out.send(second, "dsp_2")
    assert res.evidence is DeliveryEvidence.TRANSPORT_SENT and res.played_range is None
    assert out.send_count(intent.output_id, 0) == 1 and out.send_count(intent.output_id, 1) == 1


def test_output_lose_ack_counts_delivery_and_lookup():
    out = FakeOutputAdapter(default_mode="lose_ack")
    intent = make_intent()
    with pytest.raises(SendResponseLost):
        out.send(intent, "dsp_1")
    assert out.lookup("dsp_1").outcome == "found"
    assert out.lookup("dsp_x").outcome == "confirmed_not_sent"
    out.lookup_lost = True
    assert out.lookup("dsp_1").outcome == "undetermined"  # 查核失聯不拋例外
    out.lookup_lost = False
    out.lookup_authoritative = False
    assert out.lookup("dsp_x").outcome == "undetermined"  # 非權威不能確認未送出
    out.lookup_outcome("dsp_1", "confirmed_not_sent")  # 誤報：已送出卻說未送出
    assert out.lookup("dsp_1").outcome == "confirmed_not_sent"


def test_output_played_partial_has_no_complete_flag():
    out = FakeOutputAdapter(default_mode="played_partial")
    res = out.send(make_intent(), "dsp_1")
    assert res.evidence is DeliveryEvidence.LOCAL_PLAYBACK and "complete" not in res.played_range


def test_output_fail_not_delivered_and_control_idempotent():
    out = FakeOutputAdapter(default_mode="fail")
    with pytest.raises(SendFailed):
        out.send(make_intent(), "dsp_1")
    assert not out.delivered
    sig = ControlSignal.model_validate({
        "control_id": "ctl_1", "kind": "stop", "scope": "turn",
        "target_fence": {"core_epoch": 1, "turn_generation": 5}, "issued_at": datetime.now(UTC),
    })
    out.control(sig)
    out.control(sig)
    assert out.stop_count == 1 and len(out.controls) == 1


def test_output_effect_count_and_idempotent_by_dispatch():
    intent = make_intent()
    plain = FakeOutputAdapter()
    plain.send(intent, "dsp_1")
    plain.send(intent, "dsp_1")
    assert plain.send_count(intent.output_id, 0) == 2 and plain.effect_count(intent.output_id, 0) == 2
    idem = FakeOutputAdapter(idempotent_by_dispatch=True)
    idem.send(intent, "dsp_1")
    idem.send(intent, "dsp_1")
    assert idem.send_count(intent.output_id, 0) == 2 and idem.effect_count(intent.output_id, 0) == 1


def test_output_crash_after_send_has_delivered():
    out = FakeOutputAdapter()
    out.crash.arm("output.after_send")
    with pytest.raises(SimulatedCrash):
        out.send(make_intent(), "dsp_1")
    assert "dsp_1" in out.delivered


# ---- submit_tool_result（D61 選項 A）----
def _tool_result(**over):
    data = {"items": [{"ref": "r1", "scope": "scope_private_owner", "content_ref": "blob_1"}],
            "status": "ok", "degraded": False, "partial": False}
    data.update(over)
    return data


def test_submit_tool_result_pauses_stream_until_result_then_resumes():
    fake = FakeRuntime()
    fake.script([rt.text("a"), rt.tool_intent("lore.recall", {"q": "x"}, await_result=True), rt.text("b"), rt.result()])
    aid = fake.start(_request())
    first = list(fake.stream(aid))
    assert [e.kind for e in first] == ["text_delta", "tool_intent"]  # 暫停在 tool_intent 之後
    assert fake.status(aid).state == "running"  # 暫停不是完成
    assert list(fake.stream(aid)) == []  # 沒結果仍暫停
    fake.submit_tool_result(aid, "act_1", _tool_result())
    rest = list(fake.stream(aid))
    assert [e.kind for e in rest] == ["text_delta", "result"]
    assert [e.seq for e in first + rest] == [1, 2, 3, 4]  # seq 接續
    assert fake.status(aid).state == "completed"
    ((action_id, received),) = fake.tool_results[aid]
    assert action_id == "act_1"
    assert received["status"] == "ok" and received["degraded"] is False and received["partial"] is False
    assert received["items"][0]["ref"] == "r1"


def test_submit_inside_the_stream_loop_continues_same_generator():
    fake = FakeRuntime()
    fake.script([rt.tool_intent("t", await_result=True), rt.text("done"), rt.result()])
    aid = fake.start(_request())
    kinds = []
    for event in fake.stream(aid):
        kinds.append(event.kind)
        if event.kind == "tool_intent":
            fake.submit_tool_result(aid, "act_1", _tool_result())
    assert kinds == ["tool_intent", "text_delta", "result"]


def test_tool_intent_without_await_flag_does_not_pause():
    fake = FakeRuntime()
    fake.script([rt.tool_intent("t"), rt.text("x"), rt.result()])
    aid = fake.start(_request())
    assert [e.kind for e in fake.stream(aid)] == ["tool_intent", "text_delta", "result"]
    assert "await_result" not in rt.tool_intent("t").value  # 不洩漏到事件內容


def test_submit_tool_result_validates_shape_and_attempt():
    fake = FakeRuntime()
    aid = fake.start(_request())
    with pytest.raises(ValueError):
        fake.submit_tool_result("nope", "act_1", _tool_result())
    with pytest.raises(TypeError):
        fake.submit_tool_result(aid, "act_1", {"status": "ok"})  # 缺 items
    with pytest.raises(ValueError):
        fake.submit_tool_result(aid, "act_1", _tool_result(excluded_summary={"n": 1}))  # D61：不含 excluded_summary
    with pytest.raises(ValueError):
        fake.submit_tool_result(aid, "act_1", _tool_result(partial="no"))
    assert fake.tool_results[aid] == []


def test_cancel_stopped_while_awaiting_result_stops_generation():
    fake = FakeRuntime()
    fake.script([rt.tool_intent("t", await_result=True), rt.text("never"), rt.result()])
    aid = fake.start(_request())
    assert [e.kind for e in fake.stream(aid)] == ["tool_intent"]
    fake.cancel(aid, "stop")
    fake.submit_tool_result(aid, "act_1", _tool_result())  # 晚到的結果只記錄，不讓 stopped 的 attempt 復活
    assert list(fake.stream(aid)) == []
    assert fake.status(aid).state == "cancelled"
