"""AgentLoop 接上 ``decision.consult``（切片 D2，§4.9、CN-1～CN-9）：離線，不需 DB。

hub 用真實的 DL-C1 ``LoopConsultEngine``；工具條目與 executor 薄層是 D2 的；FakeTasks 的 executor 掛點執行真正的
``ConsultToolExecutor``。
"""
from __future__ import annotations

import json

import pytest

from tests.uep.loop_support import TAGS, Round, make_registry, projector_for
from tests.uep.test_agent_loop import RUN, Rig, call, trigger
from uep.adapters.tool import ToolCall
from uep.agent.budget import LoopBudget
from uep.agent.core import stable_action_id
from uep.agent.loop import AgentLoop
from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionLoop,
    DecisionOutput,
    DecisionPlan,
    DecisionResult,
)
from uep.decision.hub import DecisionHub
from uep.decision.loop_consult import (
    DEFAULT_RULES,
    LOOP_CONSULT_SPEC,
    MAX_CONSULTS,
    NO_PROGRESS_N,
    LoopConsultEngine,
    consult_decision_id,
    register_loop_consult,
)
from uep.tasks.states import RunStatus
from uep.tools.catalog.decision import CONSULT_TAG, CONSULT_TOOL, consult_entry
from uep.tools.executors.decision import ConsultResults, ConsultToolExecutor
from uep.tools.gateway import ToolGateway, visible_tools

CTAGS = TAGS | {CONSULT_TAG}


def consult(sub_kind="loop_end", **flags):
    return (CONSULT_TOOL, {"sub_kind": sub_kind, **flags})


class Spy:
    """包住 executor，記錄真正進到 executor 的呼叫與參數。"""

    def __init__(self, inner: ConsultToolExecutor) -> None:
        self.inner = inner
        self.params: list[dict] = []

    def execute(self, call_):
        self.params.append(dict(call_.params))
        return self.inner.execute(call_)


class CRig(Rig):
    def __init__(self, rounds, *, engine=None, wide=("audio_stream", "text"), narrow=("text",), budget=None, **kw):
        self.hub = DecisionHub()
        if engine is None:
            register_loop_consult(self.hub)
        else:
            self.hub.register(LOOP_CONSULT_SPEC, engine)
        self.kinds: list[str] = []
        self.hub.add_observer(lambda req, res, spec, ms: self.kinds.append(req.kind))
        self.executor = ConsultToolExecutor(self.hub)
        self.spy = Spy(self.executor)
        self.escalations: list = []
        self._calls = 0
        self._scopes = (wide, narrow)

        def scope():
            self._calls += 1
            mods = self._scopes[0] if self._calls == 1 else self._scopes[1]  # 第一次（Run 啟動）是凍結範圍，之後是當下快照
            return {"modalities": list(mods), "platforms": ["desktop"]}

        super().__init__(
            rounds, registry=make_registry(consult_entry()), budget=budget or LoopBudget(max_consults=MAX_CONSULTS),
            consults=self.executor.results, consult_scope=scope, on_escalate=self.escalations.append, **kw,
        )
        self.tasks.executors[CONSULT_TOOL] = self.spy

    def run(self, **over):
        over.setdefault("trigger", trigger(tags=CTAGS))
        return super().run(**over)

    def consult_messages(self):
        return [m for m in self.child() if m.role == "tool" and m.content.startswith("{")]


# ───────────────────────── 模型自己呼叫 consult ─────────────────────────

def test_model_called_consult_counts_and_feeds_advice_without_other_decision_kinds():
    rig = CRig([Round("", (consult(goal_satisfied=True),)), Round("完成")])
    out = rig.run()
    assert (out.state, out.steps, out.tool_calls) == ("completed", 2, 1)
    counters = rig.tasks.checkpoints[-1]["budget_counters"]
    assert counters["consults"] == 1 and counters["tool_calls"] == 1 and counters["steps"] == 2  # 計入步數與工具次數
    assert rig.kinds == ["loop.consult"]  # 只有 consult 的一次 decide；不觸發 plan.refine 之類的其他決策
    (msg,) = rig.consult_messages()
    body = json.loads(msg.content)
    assert body["advice"]["loop"]["outcome"] == "finalize" and body["escalate"] == {"needed": False, "reason_code": ""}
    assert body["decision_id"] == consult_decision_id(RUN, "step_1", 0, "loop_end")


def test_injected_scope_counts_and_modalities_are_supplied_by_the_loop():
    rig = CRig([Round("", (consult(made_progress=True),)), Round("", (consult(made_progress=True),)), Round("完成")])
    rig.run()
    first, second = rig.spy.params
    assert first["run_id"] == RUN and first["step_id"] == "step_1" and first["ordinal"] == 0
    assert first["frozen_capability_tags"] == sorted(CTAGS) and CONSULT_TAG in first["snapshot_capability_tags"]
    assert first["frozen_modalities"] == ["audio_stream", "text"] and first["snapshot_modalities"] == ["text"]  # 呼叫端補的範圍
    assert first["frozen_platforms"] == first["snapshot_platforms"] == ["desktop"]
    assert (first["consults_used"], first["no_progress_count"]) == (0, 0)
    assert second["step_id"] == "step_2" and second["consults_used"] == 1  # loop_end 的計數由迴圈傳入


def test_no_consult_tag_in_frozen_set_means_never_offered():
    base = projector_for(make_registry(consult_entry()))
    # 投影器「硬塞」consult 也沒用：迴圈以凍結標籤再過濾一次
    rig = CRig([Round("", (consult(),)), Round("完成")], projector=lambda tags, prior: base(CTAGS, prior))
    out = rig.run(trigger=trigger(tags=TAGS))
    assert out.state == "completed" and all(CONSULT_TOOL not in names for names in rig.runtime.round_caps)
    (_, view), = next(iter(rig.runtime.submitted.values()))
    assert view["status"] == "rejected" and view["reason_code"] == "capability_missing"
    assert rig.kinds == [] and rig.spy.params == []


def test_active_consult_is_never_made_without_the_tag():
    rig = CRig(
        [Round("", (call("read.lone", "1"),)), Round("", (call("read.lone", "2"),)), Round("收束")],
        budget=LoopBudget(max_steps=4, max_consults=4, consult_loop_end_margin=2),
    )
    rig.run(trigger=trigger(tags=TAGS))
    assert rig.kinds == [] and not any(s.label == "consult" for s in rig.tasks.steps)


def test_max_consults_stops_offering_consult_and_restarts_with_narrower_set():
    rounds = [Round("", (consult("speak", user_present=True, result_ready=True),)) for _ in range(2)]
    rounds += [Round("", (consult("loop_end", made_progress=True),)), Round("完成")]
    rig = CRig(rounds, budget=LoopBudget(max_consults=2, repeat_limit=99))
    out = rig.run()
    assert out.state == "completed" and len(rig.spy.params) == 2  # 第三次被擋下，沒進到 executor
    assert CONSULT_TOOL in rig.runtime.round_caps[0] and CONSULT_TOOL not in rig.runtime.round_caps[-1]
    assert rig.runtime.cancels[0][1] == "capability_narrowed"
    views = [v for r in rig.runtime.submitted.values() for _, v in r]
    assert views[-1]["status"] == "rejected" and views[-1]["reason_code"] == "capability_missing"


def test_no_progress_limit_stops_offering_consult_before_max_consults():
    rounds = [Round("", (call("read.lone", "same"),)) for _ in range(3)] + [Round("完成")]
    rig = CRig(rounds, budget=LoopBudget(max_consults=MAX_CONSULTS, consult_no_progress_n=2, repeat_limit=99))
    out = rig.run()
    assert out.state == "completed" and out.tool_calls == 3  # consults 才 0 次，遠低於 MAX_CONSULTS
    assert CONSULT_TOOL in rig.runtime.round_caps[0] and CONSULT_TOOL not in rig.runtime.round_caps[-1]


def test_consult_no_progress_default_is_derived_and_strictly_below_run_failure_threshold():
    default = LoopBudget()
    # 預設值下 Run 在「重複第 2 次」（計數 2 含第一次）就失敗；consult 的上限必須更早（計數單位：重複次數不含第一次）
    assert default.consult_no_progress_limit == 1 and default.consult_no_progress_limit < default.repeat_limit - 1 + 1
    assert LoopBudget(repeat_limit=6, rejected_limit=6).consult_no_progress_limit == 4
    assert LoopBudget(repeat_limit=2).consult_no_progress_limit == 1  # 下限 1
    with pytest.raises(ValueError, match="嚴格小於"):
        LoopBudget(consult_no_progress_n=NO_PROGRESS_N)  # 預設 repeat_limit=3 時 3 來不及收掉 consult
    with pytest.raises(ValueError):
        LoopBudget(consult_loop_end_margin=1)
    assert MAX_CONSULTS > 0


def test_with_defaults_consult_is_withdrawn_before_the_run_fails_for_no_progress():
    rounds = [Round("", (call("read.lone", "same"),)) for _ in range(5)]
    rig = CRig(rounds, budget=LoopBudget(max_consults=MAX_CONSULTS))  # 全部用預設門檻
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "no_progress") and out.tool_calls == 3
    assert CONSULT_TOOL in rig.runtime.round_caps[0]
    assert CONSULT_TOOL not in rig.runtime.round_caps[2]  # 第三輪開始前（Run 失敗之前）consult 已被收掉


def test_consult_with_same_kind_and_same_result_repeatedly_is_no_progress():
    same = consult("speak", user_present=True, result_ready=True)
    rig = CRig([Round("", (same, same)), Round("不該到這裡")], budget=LoopBudget(max_consults=8, repeat_limit=2))
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "no_progress") and out.tool_calls == 2
    assert len(rig.runtime.rounds) == 1


# ───────────────────────── 逾時／busy／invalid ⇒ advice=∅ ─────────────────────────

@pytest.mark.parametrize("reason,default", [("consult_timeout", "finalize"), ("consult_busy", "finalize"), ("consult_invalid", "finalize")])
def test_hub_fallback_feeds_empty_advice_and_loop_continues(reason, default):
    class Fallback:
        def decide(self, request):
            return DecisionResult(
                decision_id=consult_decision_id(RUN, "step_1", 0, "loop_end"), request_id=request.request_id, kind=request.kind,
                outcome="none", plan=None, confidence=0.0, reason_code=reason,
                engine=DecisionEngineInfo(tier="fallback", id="x", version="1"), fallback=True,
            )

    rig = CRig([Round("", (consult(),)), Round("照常進行")], engine=Fallback())
    out = rig.run()
    assert (out.state, out.text) == ("completed", "照常進行")
    body = json.loads(rig.consult_messages()[0].content)
    assert body["advice"] is None and body["reason_code"] == reason and body["default"] == default and body["fallback"] is True
    assert rig.escalations == []


# ───────────────────────── escalate（釘選 #76） ─────────────────────────

def test_escalate_ends_run_emits_candidate_and_is_not_fed_back_to_model():
    rig = CRig([
        Round("", (consult("output_modality", proposed_modalities=["audio_stream"]), call("read.lone", "不該執行"))),
        Round("不該到這裡"),
    ])
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "escalated") and rig.tasks.run_status == [RunStatus.FAILED]
    assert len(rig.runtime.rounds) == 1  # 模型沒有再被喚醒、同一個 Run 沒有續做
    assert rig.runtime.submitted == {next(iter(rig.runtime.submitted)): []}  # 沒有任何結果回饋給模型
    assert not any(m.role == "tool" for m in rig.child())  # escalate 的結果與其後各項都不進 history
    assert [t for _, t in rig.tasks.executions if t == "read.lone"] == []  # 同批其餘項不執行
    (candidate,) = rig.escalations
    assert candidate.decision_id == consult_decision_id(RUN, "step_1", 0, "output_modality") and candidate.gap_code == "scope_exceeded"
    assert candidate.sub_kind == "output_modality" and candidate.run_id == RUN
    assert candidate.trigger.kind == "external_event" and candidate.trigger.capability_tags == CTAGS  # 只帶原本凍結的標籤
    assert candidate.trigger.origin_ref == candidate.decision_id
    assert rig.tasks.checkpoints[-1]["next_step_semantic"] == "end:escalated"


def test_placement_is_never_offered_and_escalates_if_it_reaches_the_hub():
    rig = CRig([Round("", (consult("placement"),)), Round("完成")])
    out = rig.run()
    assert out.state == "completed" and rig.kinds == []  # schema 的 enum 沒有 placement ⇒ 被 Gateway 擋下，沒進到中樞
    (_, view), = next(iter(rig.runtime.submitted.values()))
    assert view["status"] == "rejected" and view["reason_code"] == "invalid_tool_args"


def test_loop_rechecks_advice_outside_scope_and_treats_it_as_escalate():
    class Overreach:
        """壞掉的 engine：回了超出「凍結 ∩ 快照」的 advice（縱深防禦 CN-2）。"""

        def decide(self, request):
            return DecisionResult(
                decision_id=consult_decision_id(RUN, "step_1", 0, "output_modality"), request_id=request.request_id,
                kind=request.kind, outcome="output_set", plan=DecisionPlan(output=DecisionOutput(modalities=("audio_stream",), tts=True)),
                confidence=0.9, reason_code="output_modality_in_scope", engine=DecisionEngineInfo(tier="rule", id="x", version="1"),
            )

    rig = CRig([Round("", (consult("output_modality", proposed_modalities=["audio_stream"]),)), Round("不該到這裡")], engine=Overreach())
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "escalated") and rig.escalations[0].gap_code == "scope_exceeded"
    assert len(rig.runtime.rounds) == 1


class _NarrowAfterConsult:
    """包住 executor：consult 跑完後，投影器不再給 consult 標籤（執行期間能力被收窄）。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.narrowed = False

    def execute(self, call_):
        res = self.inner.execute(call_)
        self.narrowed = True
        return res


def _narrowing_rig(rounds, **kw):
    base = projector_for(make_registry(consult_entry()))
    holder: dict = {}
    rig = CRig(rounds, projector=lambda tags, prior: base(TAGS if holder["gate"].narrowed else CTAGS, prior), **kw)
    holder["gate"] = rig.tasks.executors[CONSULT_TOOL] = _NarrowAfterConsult(rig.spy)
    return rig


def test_capability_narrowed_during_consult_escalates_on_recheck(monkeypatch):
    rig = _narrowing_rig([Round("", (consult(goal_satisfied=True),)), Round("不該到這裡")])
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "escalated") and rig.escalations[0].gap_code == "scope_exceeded"
    assert len(rig.runtime.rounds) == 1

    # 證明檢查有效：改回只看 st.cap 的 _snapshot_tags（舊行為）時，同一情境不會 escalate
    monkeypatch.setattr(AgentLoop, "_projection_tags", lambda self, st: self._snapshot_tags(st))
    old = _narrowing_rig([Round("", (consult(goal_satisfied=True),)), Round("完成")])
    out = old.run()
    assert out.state == "completed" and old.escalations == []


def test_consult_removed_by_budget_is_not_a_capability_narrowing(monkeypatch):
    def rounds():
        return [Round("", (consult("speak", user_present=True, result_ready=True),)), Round("完成")]

    rig = CRig(rounds(), budget=LoopBudget(max_consults=1, repeat_limit=99))
    out = rig.run()
    assert out.state == "completed" and rig.escalations == [] and len(rig.spy.params) == 1
    assert CONSULT_TOOL not in rig.runtime.round_caps[-1]  # 預算讓 _offer 拿掉了 consult，但能力沒有收窄

    # 反面：若再驗改用經過 _offer 的投影，這個預算移除會被誤判成收窄而 escalate
    def via_offer(self, st):
        prior = visible_tools(st.cap)
        return self._cap_tags(self._offer(st, self._narrow(self._projector(st.frozen_tags, prior), st.frozen_tools, prior)))

    monkeypatch.setattr(AgentLoop, "_projection_tags", via_offer)
    wrong = CRig(rounds(), budget=LoopBudget(max_consults=1, repeat_limit=99))
    assert wrong.run().reason_code == "escalated"


def test_consult_result_never_widens_capability_set():
    rig = CRig([Round("", (consult(goal_satisfied=True),)), Round("", (consult("speak", user_present=True),)), Round("完成")])
    rig.run()
    assert rig.runtime.round_caps[0] == rig.runtime.round_caps[-1] and len(rig.runtime.requests) == 1  # 集合不變、沒有重開
    assert set(rig.tasks.checkpoints[-1]["visible_tools"]) <= set(rig.tasks.checkpoints[-1]["frozen_tools"])


# ───────────────────────── 迴圈主動諮詢（CN-8） ─────────────────────────

def _finalize_engine():
    rules = dict(DEFAULT_RULES)
    rules["loop_end"] = lambda facts, scope: (DecisionPlan(loop=DecisionLoop(outcome="finalize")), "finalize", "loop_end_test")
    return LoopConsultEngine(rules=rules)


def test_budget_nearly_exhausted_loop_asks_and_hub_finalize_ends_gracefully():
    rig = CRig(
        [Round("", (call("read.lone", "1"),)), Round("", (call("read.lone", "2"),)), Round("收束的回答")],
        engine=_finalize_engine(), budget=LoopBudget(max_steps=4, max_consults=4, consult_loop_end_margin=2),
    )
    out = rig.run()
    assert (out.state, out.reason_code, out.text) == ("completed", "consult_finalized", "收束的回答")
    assert [s.label for s in rig.tasks.steps].count("consult") == 1 and rig.kinds == ["loop.consult"]
    counters = rig.tasks.checkpoints[-1]["budget_counters"]
    assert counters["consults"] == 1 and counters["tool_calls"] == 3 and counters["steps"] >= 3  # 計入步數與工具次數
    assert [s.label for s in rig.tasks.steps][-2:] == ["consult", "finalize"]
    assert rig.runtime.round_caps[-1] == []  # 收束時 tools=[]
    assert rig.tasks.checkpoints[-1]["next_step_semantic"] == "end:consult_finalized"


def test_active_consult_continue_advice_falls_through_to_normal_budget_finalize():
    rig = CRig(
        [Round("", (call("read.lone", "1"),)), Round("", (call("read.lone", "2"),)), Round("", (call("read.lone", "3"),)),
         Round("預算收束")],
        budget=LoopBudget(max_steps=4, max_consults=4, consult_loop_end_margin=2),
    )
    out = rig.run()
    assert (out.state, out.reason_code) == ("completed", "budget_finalized") and rig.kinds == ["loop.consult"]


def test_active_consult_is_off_by_default():
    rig = CRig(
        [Round("", (call("read.lone", "1"),)), Round("", (call("read.lone", "2"),)), Round("完成")], budget=LoopBudget(max_steps=3, max_consults=4),
    )
    assert LoopBudget().consult_loop_end_margin is None and rig.run().state == "completed"
    assert rig.kinds == [] and not any(s.label == "consult" for s in rig.tasks.steps)


def test_active_consult_never_takes_the_last_step():
    rig = CRig(
        [Round("", (call("read.lone", "1"),)), Round("完成")], budget=LoopBudget(max_steps=2, max_consults=4, consult_loop_end_margin=5),
    )
    assert rig.run().state == "completed" and rig.kinds == []  # 剩 1 步時不問（< 2）


def test_hub_finalize_advice_does_not_bypass_finalize_on_exhausted_policy():
    rig = CRig(
        [Round("", (call("read.lone", "1"),)), Round("", (call("read.lone", "2"),)), Round("", (call("read.lone", "3"),)),
         Round("不該到這裡")],
        engine=_finalize_engine(),
        budget=LoopBudget(max_steps=4, max_consults=4, consult_loop_end_margin=2, finalize_on_exhausted=False),
    )
    out = rig.run()
    assert rig.kinds == ["loop.consult"]  # 有問，但建議被忽略（政策不允許 finalize）
    assert (out.state, out.reason_code) == ("failed", "budget_exhausted") and all(names for names in rig.runtime.round_caps)


# ───────────────────────── 重放、resume ─────────────────────────

def _park_with_consult():
    rig = CRig(
        [Round("先問再發", (consult(goal_satisfied=True), ("post.ask", {"path": "2"}), call("read.lone", "3"))), Round("全部完成")],
        budget=LoopBudget(max_consults=4, repeat_limit=99),
    )
    out = rig.run()
    assert out.state == "awaiting_approval"
    return rig, dict(rig.tasks.checkpoints[-1])


def test_replay_and_resume_keep_the_same_decision_id_without_recomputing():
    rig, checkpoint = _park_with_consult()
    action = stable_action_id(RUN, "step_1", 0)
    before = rig.consult_messages()[0].content
    decision_id = consult_decision_id(RUN, "step_1", 0, "loop_end")
    assert json.loads(before)["decision_id"] == decision_id and len(rig.spy.params) == 1
    # 同 action 重放：已有 receipt，不再進 executor
    rig.tasks.execute_action(action, rig.tasks.actions[action]["params"], worker_epoch=1)
    assert len(rig.spy.params) == 1 and rig.kinds == ["loop.consult"]
    # 崩潰後 resume（結果暫存遺失、新的 executor）：history 裡的 consult 結果原封不動；再算一次也是同一個 decision_id
    rig.tasks.epoch = 2
    fresh = ConsultToolExecutor(rig.hub, ConsultResults())
    fresh.execute(ToolCall(action_id=action, tool=CONSULT_TOOL, params=rig.tasks.actions[action]["params"], params_hash="h",
                           side_effect_class="internal_read", idempotency="native"))
    assert fresh.results.get(action)["decision_id"] == decision_id
    checkpoint["pending_approvals"] = []
    rig.tasks.waiting_approval = False  # 本測試只驗 consult 重放；視為核准已讓 Run 回 RUNNING
    loop = rig.new_loop(runtime=type(rig.runtime)([Round("完成")]), worker_epoch=2, consults=fresh.results)
    out = loop.resume(RUN, checkpoint, trigger=trigger(tags=CTAGS), parent_id="ctx_parent")
    assert out.state == "completed" and rig.consult_messages()[0].content == before
    assert len(rig.spy.params) == 1  # resume 沒有重新執行 consult


def test_checkpoint_persists_consult_scope():
    _rig, checkpoint = _park_with_consult()
    assert checkpoint["frozen_modalities"] == ["audio_stream", "text"] and checkpoint["frozen_platforms"] == ["desktop"]
    assert checkpoint["last_decision_id"] == consult_decision_id(RUN, "step_1", 0, "loop_end")  # CN-9


@pytest.mark.parametrize("key", ["frozen_modalities", "frozen_platforms"])
def test_resume_without_consult_scope_keys_fails_closed(key):
    """D1 時代的 checkpoint（沒有 consult 範圍鍵）一律 fail-closed，不以空集合補（空集合會讓 output_modality consult 全部 escalate）。"""
    rig, checkpoint = _park_with_consult()
    del checkpoint[key]
    checkpoint["pending_approvals"] = []
    rig.tasks.epoch = 2
    runtime = type(rig.runtime)([Round("不該到這裡")])
    out = rig.new_loop(runtime=runtime, worker_epoch=2).resume(RUN, checkpoint, trigger=trigger(tags=CTAGS), parent_id="ctx_parent")
    assert (out.state, out.reason_code) == ("failed", "checkpoint_incomplete")
    assert runtime.requests == [] and rig.tasks.run_status == [RunStatus.FAILED] and rig.escalations == []


# ───────────────────────── CN-9：decision_id 進 checkpoint 與 receipt；escalate 的耐久順序 ─────────────────────────

def test_escalate_terminal_checkpoint_is_written_before_failed_transition_and_callback():
    seen: dict = {}

    def on_escalate(candidate):
        seen["log"] = list(rig.tasks.log)
        seen["candidate"] = candidate

    rig = CRig([Round("", (consult("output_modality", proposed_modalities=["audio_stream"]),)), Round("不該到這裡")])
    rig.loop_kwargs["on_escalate"] = on_escalate
    rig.loop = rig.new_loop()
    out = rig.run()
    assert out.reason_code == "escalated"
    cp = rig.tasks.checkpoints[-1]
    decision_id = consult_decision_id(RUN, "step_1", 0, "output_modality")
    assert cp["next_step_semantic"] == "end:escalated" and cp["escalate_gap"] == "scope_exceeded" and cp["last_decision_id"] == decision_id
    log = seen["log"]
    assert log.index("transition:FAILED") > max(i for i, e in enumerate(log) if e == "checkpoint")  # 終態 checkpoint 先於轉移
    assert log[-1] == "transition:FAILED" and seen["candidate"].decision_id == decision_id  # 回呼最後才呼叫（log 在回呼當下已齊）


def test_on_escalate_exception_is_only_logged():
    def boom(candidate):
        raise RuntimeError("消費端壞了")

    rig = CRig([Round("", (consult("output_modality", proposed_modalities=["audio_stream"]),)), Round("不該到這裡")])
    rig.loop_kwargs["on_escalate"] = boom
    rig.loop = rig.new_loop()
    out = rig.run()
    assert (out.state, out.reason_code) == ("failed", "escalated") and rig.tasks.run_status == [RunStatus.FAILED]


def test_consult_receipt_meta_carries_decision_id_and_passes_value_check():
    from uep.tasks.manager import check_safe_value

    hub = DecisionHub()
    register_loop_consult(hub)
    receipt = ConsultToolExecutor(hub).execute(ToolCall(
        action_id="act_1", tool=CONSULT_TOOL, params={
            "sub_kind": "loop_end", "run_id": RUN, "step_id": "step_1", "ordinal": 0, "goal_satisfied": True,
            "frozen_capability_tags": list(CTAGS), "snapshot_capability_tags": list(CTAGS),
        }, params_hash="h", side_effect_class="internal_read", idempotency="native",
    ))
    assert receipt.meta["decision_id"] == consult_decision_id(RUN, "step_1", 0, "loop_end")
    check_safe_value(dict(receipt.meta), "meta")  # TaskManager 的 receipt 脫敏白名單：不會被脫敏


# ───────────────────────── 結果暫存的生命週期（S4） ─────────────────────────

def test_consult_results_are_discarded_when_the_run_reaches_a_terminal_state():
    done = CRig([Round("", (consult(goal_satisfied=True),)), Round("完成")])
    done.run()
    assert len(done.executor.results) == 0
    failed = CRig([Round("", (consult("output_modality", proposed_modalities=["audio_stream"]),))])
    failed.run()
    assert len(failed.executor.results) == 0
    cancelled = CRig([Round("", (consult(goal_satisfied=True),)), Round("不該到這裡")], cancelled=lambda: bool(cancelled.kinds))
    assert cancelled.run().state == "cancelled" and len(cancelled.executor.results) == 0


def test_consult_results_are_kept_while_parked():
    rig, _checkpoint = _park_with_consult()
    assert len(rig.executor.results) == 1  # park 不是終態：resume 還要用


def test_consult_results_capacity_evicts_the_oldest():
    results = ConsultResults(max_entries=2)
    for n in range(3):
        results.put(f"act_{n}", (RUN, "step_1", n, "loop_end"), {"decision_id": f"dec_{n}"})
    assert results.get("act_0") is None and results.get("act_1") and results.get("act_2") and len(results) == 2
    assert results.identity((RUN, "step_1", 0, "loop_end")) is None and results.identity((RUN, "step_1", 2, "loop_end"))  # 去重鍵同上限


def test_lost_consult_content_makes_replayed_action_unknown_not_invented():
    """結果暫存被清掉（Run 終態／容量）後，同 action 重放只剩 receipt、沒有內容 ⇒ 視圖 unknown（不編造）。"""
    rig = CRig([Round("", (consult(goal_satisfied=True),)), Round("完成")])
    rig.run()
    action = stable_action_id(RUN, "step_1", 0)
    outcome = rig.tasks.outcomes[action]
    view = ToolGateway(rig.registry).outcome_view(outcome, content_lookup=rig.executor.results.content)
    assert view["status"] == "unknown"


def test_agent_loop_without_consult_wiring_behaves_like_d1():
    """consults／consult_scope 都沒給：即使註冊表與凍結標籤都有 consult，也不投出（沒有結果暫存就讀不到 escalate）。"""
    registry = make_registry(consult_entry())
    rig = Rig([Round("", (call("read.lone", "1"),)), Round("完成")], registry=registry, budget=LoopBudget(max_consults=4))
    out = rig.run(trigger=trigger(tags=CTAGS))
    assert out.state == "completed" and all(CONSULT_TOOL not in names for names in rig.runtime.round_caps)
    assert isinstance(rig.loop, AgentLoop)


# ───────────────────────── resume 遇終態 checkpoint 不續跑 ─────────────────────────

def _crash_before_transition(rig: CRig) -> dict:
    """模擬「終態 checkpoint 已寫、FAILED 轉移尚未提交」就崩潰：保留 checkpoint，抹掉轉移紀錄，換新 epoch。"""
    checkpoint = dict(rig.tasks.checkpoints[-1])
    assert checkpoint["next_step_semantic"].startswith("end:")
    rig.tasks.run_status.clear()
    rig.tasks.log[:] = [e for e in rig.tasks.log if not e.startswith("transition:")]
    rig.tasks.epoch = 2
    return checkpoint


def test_resume_after_terminal_checkpoint_escalated_completes_failed_transition_without_waking_model():
    rig = CRig([Round("", (consult("output_modality", proposed_modalities=["audio_stream"]),)), Round("不該到這裡")])
    assert rig.run().reason_code == "escalated" and len(rig.escalations) == 1
    checkpoint = _crash_before_transition(rig)
    runtime = type(rig.runtime)([Round("不該到這裡")])
    executions = list(rig.tasks.executions)
    out = rig.new_loop(runtime=runtime, worker_epoch=2).resume(RUN, checkpoint, trigger=trigger(tags=CTAGS), parent_id="ctx_parent")
    assert (out.state, out.reason_code) == ("failed", "escalated")
    assert runtime.requests == [] and len(runtime.rounds) == 1  # 模型 0 次呼叫
    assert rig.tasks.run_status == [RunStatus.FAILED] and rig.tasks.executions == executions
    assert len(rig.escalations) == 1  # on_escalate 不在 resume 重送（屬切片 F 的恢復掃描器）


def test_resume_after_terminal_checkpoint_other_failure_reason_keeps_the_reason():
    rig = CRig(
        [Round("", (call("read.lone", "1"),)), Round("不該")], budget=LoopBudget(max_steps=1, finalize_on_exhausted=False),
    )
    assert rig.run().reason_code == "budget_exhausted"
    checkpoint = _crash_before_transition(rig)
    assert checkpoint["next_step_semantic"] == "end:budget_exhausted:steps"
    runtime = type(rig.runtime)([Round("不該到這裡")])
    out = rig.new_loop(runtime=runtime, worker_epoch=2).resume(RUN, checkpoint, trigger=trigger(tags=CTAGS), parent_id="ctx_parent")
    assert (out.state, out.reason_code) == ("failed", "budget_exhausted")
    assert runtime.requests == [] and rig.tasks.run_status == [RunStatus.FAILED]


def test_resume_after_terminal_checkpoint_with_unknown_reason_fails_checkpoint_incomplete():
    rig = CRig([Round("", (call("read.lone", "1"),)), Round("完成")])
    rig.run()
    checkpoint = dict(rig.tasks.checkpoints[-1])
    checkpoint["next_step_semantic"] = "end:made_up_reason"
    rig.tasks.run_status.clear()
    rig.tasks.epoch = 2
    runtime = type(rig.runtime)([Round("不該到這裡")])
    out = rig.new_loop(runtime=runtime, worker_epoch=2).resume(RUN, checkpoint, trigger=trigger(tags=CTAGS), parent_id="ctx_parent")
    assert (out.state, out.reason_code) == ("failed", "checkpoint_incomplete")
    assert runtime.requests == [] and rig.tasks.run_status == [RunStatus.FAILED]


def test_resume_after_completed_terminal_checkpoint_does_nothing():
    rig = CRig([Round("", (call("read.lone", "1"),)), Round("完成")])
    assert rig.run().state == "completed"
    checkpoint = dict(rig.tasks.checkpoints[-1])
    assert checkpoint["next_step_semantic"] == "end:completed"
    transitions = list(rig.tasks.run_status)
    rig.tasks.epoch = 2
    runtime = type(rig.runtime)([Round("不該到這裡")])
    out = rig.new_loop(runtime=runtime, worker_epoch=2).resume(RUN, checkpoint, trigger=trigger(tags=CTAGS), parent_id="ctx_parent")
    assert out.state == "completed" and runtime.requests == [] and rig.tasks.run_status == transitions
