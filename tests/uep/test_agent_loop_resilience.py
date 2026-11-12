"""AgentLoop F-4：Supervisor 旗標自行收尾、等待模型期間的 Step 逾時、模型／工具的重試政策（離線，不需 DB）。"""
from __future__ import annotations

import threading
import time

from tests.uep.loop_support import Round, ScriptedRuntime
from tests.uep.test_agent_loop import Board, Rig, call, one_shot_rig
from uep.agent.budget import LoopBudget
from uep.agent.event_pump import TIMED_OUT, EventPump
from uep.agent.retry import RetryPolicy
from uep.tasks.manager import CancelSignal
from uep.tasks.states import RunStatus


def _no_sleep(sleeps):
    return lambda seconds: sleeps.append(seconds)


# ---- F-4：Supervisor 旗標 ----

def test_run_deadline_flag_ends_run_failed_with_reason_without_next_model_call():
    rig = Rig([Round("", (call("read.lone", "1"),)), Round("不該到這裡")], budget=LoopBudget(heartbeat_interval_s=0.0))
    rig.tasks.cancel_signal = CancelSignal("run_deadline", None)
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "run_deadline")
    assert rig.tasks.run_status == [RunStatus.FAILED]
    assert rig.tasks.checkpoints[-1]["next_step_semantic"] == "end:run_deadline"
    assert len(rig.runtime.rounds) >= 1  # 腳本的最後一回合沒被消耗


def test_task_deadline_flag_has_its_own_reason_code():
    rig = Rig([Round("", (call("read.lone", "1"),)), Round("x")], budget=LoopBudget(heartbeat_interval_s=0.0))
    rig.tasks.cancel_signal = CancelSignal("task_deadline", None)
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "task_deadline")


def test_step_timeout_flag_does_not_fail_the_run():
    rig = Rig([Round("", (call("read.lone", "1"),)), Round("完成")], budget=LoopBudget(heartbeat_interval_s=0.0))
    rig.tasks.cancel_signal = CancelSignal("step_timeout", None)
    assert rig.run().state == "completed"


def test_external_cancel_flag_with_deadline_signal_reports_deadline_reason():
    """launcher 的背景心跳把旗標轉成 cancelled()：迴圈再讀一次旗標以取得正確原因（Run 轉 FAILED，而非單純取消）。"""
    rig = Rig([Round("", (call("read.lone", "1"),)), Round("x")], cancelled=lambda: True)
    rig.tasks.cancel_signal = CancelSignal("run_deadline", None)
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "run_deadline")


# ---- Step 逾時：等待模型期間強制 ----

class HangingRuntime(ScriptedRuntime):
    """第 ``hang_rounds`` 個 stream 呼叫之前先卡住（直到 cancel）；其後照腳本。"""

    def __init__(self, rounds, hang_attempts: int) -> None:
        super().__init__(rounds)
        self.hang_attempts = hang_attempts
        self.streams = 0
        self.released = threading.Event()

    def cancel(self, attempt_id, reason):
        self.released.set()
        return super().cancel(attempt_id, reason)

    def stream(self, attempt_id):
        self.streams += 1
        if self.streams <= self.hang_attempts:
            self.released.clear()
            self.released.wait(5)
            return
            yield  # 讓這是 generator
        yield from super().stream(attempt_id)


def _hang_rig(hang_attempts, *, retry=None, sleeps=None):
    runtime = HangingRuntime([Round("好了")], hang_attempts)
    rig = Rig([], budget=LoopBudget(step_timeout_s=0.2, heartbeat_interval_s=0.05), runtime=runtime)
    rig.loop = rig.new_loop(runtime=runtime, retry=retry, sleep=_no_sleep(sleeps if sleeps is not None else []))
    return rig, runtime


def test_blocked_model_call_hits_step_timeout_without_retry_policy():
    rig, runtime = _hang_rig(1)
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "step_timeout")
    assert any(reason == "step_timeout" for _, reason in runtime.cancels)
    assert rig.tasks.steps[0].status == "FAILED"


def test_blocked_model_call_is_retried_with_backoff_then_completes():
    sleeps: list[float] = []
    rig, runtime = _hang_rig(1, retry=RetryPolicy(model_max=2, model_backoff_s=1.0), sleeps=sleeps)
    out = rig.run()
    assert (out.state, out.text) == ("completed", "好了")
    assert len(runtime.requests) == 2 and sleeps and sleeps[0] <= 0.5  # 退避以短片段睡眠（期間持續檢查取消）


def test_model_retry_has_an_upper_bound():
    rig, runtime = _hang_rig(10, retry=RetryPolicy(model_max=2, model_backoff_s=0.0))
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "step_timeout")
    assert len(runtime.requests) == 3  # 首次 + 2 次重試


def test_cancel_flag_is_honoured_while_waiting_for_the_model():
    flag = threading.Event()
    runtime = HangingRuntime([Round("好了")], 1)
    rig = Rig([], budget=LoopBudget(step_timeout_s=30.0, heartbeat_interval_s=0.05), runtime=runtime, cancelled=flag.is_set)
    timer = threading.Timer(0.2, flag.set)
    timer.start()
    out = rig.run()
    timer.cancel()
    assert out.state == "cancelled" and any(a for a, _ in runtime.cancels)


class SlowStreamRuntime(ScriptedRuntime):
    """每個事件前先睡 ``gap`` 秒；``hang_after_first``：第一個 attempt 吐出首個事件後卡住（直到 cancel）。"""

    def __init__(self, rounds, gap=0.0, hang_after_first=False) -> None:
        super().__init__(rounds)
        self.gap, self.hang_after_first, self.streams = gap, hang_after_first, 0
        self.released = threading.Event()

    def cancel(self, attempt_id, reason):
        self.released.set()
        return super().cancel(attempt_id, reason)

    def stream(self, attempt_id):
        self.streams += 1
        first = self.streams == 1
        for ev in super().stream(attempt_id):
            time.sleep(self.gap)
            yield ev
            if first and self.hang_after_first and ev.kind == "text_delta":
                self.released.wait(5)
                return


def test_steady_event_stream_longer_than_step_timeout_still_succeeds():
    """step_timeout_s 是事件閒置上限，不是整段串流上限。"""
    runtime = SlowStreamRuntime([Round("一二三")], gap=0.2)
    rig = Rig([], budget=LoopBudget(step_timeout_s=0.3, heartbeat_interval_s=0.05), runtime=runtime)
    rig.loop = rig.new_loop(runtime=runtime)
    started = time.monotonic()
    out = rig.run()
    assert out.state == "completed" and time.monotonic() - started >= 0.35


def test_idle_gap_between_events_longer_than_step_timeout_is_retried():
    runtime = SlowStreamRuntime([Round("開頭"), Round("好了")], hang_after_first=True)
    rig = Rig([], budget=LoopBudget(step_timeout_s=0.2, heartbeat_interval_s=0.05), runtime=runtime)
    rig.loop = rig.new_loop(runtime=runtime, retry=RetryPolicy(model_max=1, model_backoff_s=0.0), sleep=_no_sleep([]))
    out = rig.run()
    assert out.state == "completed" and len(runtime.requests) == 2


def test_tool_execution_longer_than_step_timeout_does_not_fail_the_run():
    """Step 計時只管模型等待：工具跑得比 step_timeout_s 久，波與波之間的 gate 不會讓 Run 失敗。"""
    def slow(args):
        time.sleep(0.4)
        return "慢但成功"

    rig = Rig(
        [Round("", (call("read.lone", "1"),)), Round("", (call("read.lone", "2"),)), Round("完成")],
        budget=LoopBudget(step_timeout_s=0.2, heartbeat_interval_s=0.05, repeat_limit=99), behaviors={"read.lone": slow},
    )
    out = rig.run()
    assert (out.state, out.text) == ("completed", "完成")


def test_step_timeout_zero_setting_disables_it():
    from uep.config import UepSettings

    assert UepSettings(UEP_AGENT_STEP_TIMEOUT_S=0).agent_step_timeout_s == 0
    from uep import assembly
    from uep.assembly import _loop_resilience_kwargs

    class _S:
        agent_step_timeout_s = 0.0
        agent_model_retry_max = 2
        agent_model_retry_backoff_s = 1.0
        agent_tool_retry_max = 1
        agent_tool_retry_backoff_s = 0.5

    original = assembly.get_settings
    assembly.get_settings = lambda: _S()
    try:
        assert _loop_resilience_kwargs()["step_timeout_s"] is None
    finally:
        assembly.get_settings = original


# ---- 模型錯誤重試 ----

def test_retryable_provider_error_is_retried():
    sleeps: list[float] = []
    rig = Rig(
        [Round(error="PROVIDER_UNAVAILABLE", retryable=True), Round("恢復")],
        retry=RetryPolicy(model_max=2, model_backoff_s=0.0), sleep=_no_sleep(sleeps),
    )
    out = rig.run()
    assert (out.state, out.text) == ("completed", "恢復") and len(rig.runtime.requests) == 2


def test_non_retryable_error_fails_immediately():
    rig = Rig([Round(error="PROVIDER_UNAVAILABLE", retryable=False), Round("不該到這裡")], retry=RetryPolicy(model_max=2))
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "runtime_provider_unavailable") and len(rig.runtime.requests) == 1


def test_non_whitelisted_error_code_is_not_retried_even_if_flagged():
    rig = Rig([Round(error="EGRESS_DENIED", retryable=True), Round("x")], retry=RetryPolicy(model_max=2))
    assert rig.run().reason_code == "runtime_egress_denied"


def test_retries_are_consecutive_limited_and_reset_after_progress():
    sleeps: list[float] = []
    rounds = [
        Round(error="RATE_LIMITED", retryable=True),
        Round("", (call("read.lone", "1"),)),  # 成功推進一批 → 歸零
        Round(error="RATE_LIMITED", retryable=True),
        Round("完成"),
    ]
    rig = Rig(rounds, retry=RetryPolicy(model_max=1, model_backoff_s=0.0), sleep=_no_sleep(sleeps))
    assert rig.run().state == "completed"


def test_model_retry_default_off_keeps_single_call():
    rig = Rig([Round(error="TIMEOUT", retryable=True), Round("x")])
    assert rig.run().reason_code == "runtime_timeout" and len(rig.runtime.requests) == 1


# ---- 工具重試 ----

def _fail_once(rig, tool, code):
    rig.tasks.fail_tools.add(tool)
    rig.tasks.fail_codes[tool] = code
    calls = {"n": 0}

    def behavior(args):
        calls["n"] += 1
        if calls["n"] >= 2:
            rig.tasks.fail_tools.discard(tool)
        return f"{tool}:{args.get('path')}"

    rig.tasks.behaviors[tool] = behavior
    return calls


def test_transient_read_failure_is_retried_as_new_action_with_retry_of():
    sleeps: list[float] = []
    rig = Rig(
        [Round("", (call("read.lone", "1"),)), Round("完成")],
        retry=RetryPolicy(tool_max=1, tool_backoff_s=0.0), sleep=_no_sleep(sleeps),
    )
    calls = _fail_once(rig, "read.lone", "timeout")
    out = rig.run()
    assert out.state == "completed" and calls["n"] == 2
    assert len(rig.tasks.retries) == 1
    (new_id, original), = rig.tasks.retries.items()
    assert new_id != original and out.tool_calls == 2  # 重試計入工具次數
    tools = [m for m in rig.child() if m.role == "tool"]
    assert tools[0].content == "read.lone:1"  # 模型看到的是重試後的成功結果


def test_deterministic_failure_is_not_retried():
    rig = Rig([Round("", (call("read.lone", "1"),)), Round("完成")], retry=RetryPolicy(tool_max=1, tool_backoff_s=0.0))
    calls = _fail_once(rig, "read.lone", "FAKE_FAILURE")
    rig.run()
    assert calls["n"] == 1 and not rig.tasks.retries


def test_retry_is_limited_to_one_new_action():
    rig = Rig([Round("", (call("read.lone", "1"),)), Round("完成")], retry=RetryPolicy(tool_max=5, tool_backoff_s=0.0))
    rig.tasks.fail_tools.add("read.lone")
    rig.tasks.fail_codes["read.lone"] = "timeout"
    rig.run()
    assert len(rig.tasks.retries) == 1  # 永遠失敗也只重試一次


def test_side_effect_tools_are_never_retried():
    rig = Rig(
        [Round("", (call("write.s", "1"),)), Round("完成")], retry=RetryPolicy(tool_max=3, tool_backoff_s=0.0),
    )
    rig.tasks.fail_tools.add("write.s")
    rig.tasks.fail_codes["write.s"] = "timeout"
    rig.run()
    assert not rig.tasks.retries and len([1 for _, t in rig.tasks.executions if t == "write.s"]) == 1


def test_tool_retry_default_off():
    rig = Rig([Round("", (call("read.lone", "1"),)), Round("完成")])
    rig.tasks.fail_tools.add("read.lone")
    rig.tasks.fail_codes["read.lone"] = "timeout"
    rig.run()
    assert not rig.tasks.retries


# ---- EventPump ----

def test_event_pump_times_out_then_delivers_same_pending_item():
    gate = threading.Event()

    def source():
        gate.wait(5)
        yield "a"
        yield "b"

    pump = EventPump(source())
    assert pump.get(0.05) is TIMED_OUT
    gate.set()
    assert pump.get(2) == "a" and pump.get(2) == "b" and pump.get(2) is None
    pump.close()


def test_event_pump_reraises_source_errors():
    def source():
        raise ValueError("壞掉")
        yield

    pump = EventPump(source())
    try:
        pump.get(2)
    except ValueError as exc:
        assert str(exc) == "壞掉"
    else:
        raise AssertionError("應該重新拋出")
    pump.close()


# ---- parallel_one_shot 收到 deadline 旗標 ----

def test_deadline_flag_during_one_shot_wave_fails_run_with_flag_reason():
    board = Board("stopped")
    rig = one_shot_rig(
        board, [Round("", (call("once.a", "p1"), call("once.b", "p2"))), Round("好")], timeout_s=30.0,
        behaviors={"once.a": board.hang, "once.b": board.hang},
    )
    rig.budget = rig.loop._budget = LoopBudget(max_parallel=2, repeat_limit=99, heartbeat_interval_s=0.05)
    timer = threading.Timer(0.2, lambda: setattr(rig.tasks, "cancel_signal", CancelSignal("run_deadline", None)))
    timer.start()  # 波開始執行之後才出現旗標
    try:
        out = rig.run()
    finally:
        timer.cancel()
        board.release_all()
    assert (out.state, out.reason_code) == ("failed", "run_deadline") and board.cancels


# ---- 工具重試遵守牆鐘 ----

def test_tool_retry_is_skipped_when_run_deadline_flag_is_set():
    rig = Rig(
        [Round("", (call("read.lone", "1"),)), Round("完成")], budget=LoopBudget(heartbeat_interval_s=0.0),
        retry=RetryPolicy(tool_max=1, tool_backoff_s=0.0), sleep=_no_sleep([]),
    )
    rig.tasks.fail_tools.add("read.lone")
    rig.tasks.fail_codes["read.lone"] = "timeout"
    rig.tasks.cancel_signal = CancelSignal("run_deadline", None)
    rig.run()
    assert not rig.tasks.retries
