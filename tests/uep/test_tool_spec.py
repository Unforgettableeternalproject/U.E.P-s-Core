"""Part S 切片 0：ToolSpec／ToolSpecResolver、tool_version hash 域、TG-1／TG-5、migration 0009。

對應 VC-S2-1（宣告繞過）、VC-S2-4（版本綁定）、缺解析器拒絕組裝、TG-5 兩方向。
"""
# ruff: noqa: F811  (fixture 以同名參數使用)
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import inspect, select

from tests.uep.test_assembly import kwargs as assembly_kwargs
from tests.uep.test_task_model import (  # noqa: F401  fixture 與輔助函式沿用 test_task_model
    HUMAN,
    SCOPE,
    StubExecutor,
    declare,
    executor,
    make_manager,
    mgr,
    running,
)
from uep.adapters.tool import (
    EnumerableToolSpecResolver,
    ToolCall,
    ToolSpec,
    ToolSpecResolver,
)
from uep.assembly import AssemblyError, _assert_wired, assemble_core
from uep.fakes import runtime as rt
from uep.fakes.runtime import FakeRuntime
from uep.fakes.tool import FakeToolExecutor
from uep.fakes.tool_spec import FakeToolSpecResolver
from uep.tasks.manager import (
    ActionBlocked,
    ApprovalInvalid,
    PolicyViolation,
    TaskManager,
    params_hash,
)
from uep.tasks.models import ActionIntentRow
from uep.tasks.states import ActionStatus
from uep.tools.registry import TOOL_UNKNOWN, StaticToolRegistry

POST = "chatroom.post"


def _action(mgr, run, step, params, tool=POST, **kw):
    return mgr.create_action(
        run_id=run.run_id, step_id=step.step_id, tool=tool, params=params, worker_epoch=run.worker_epoch, **kw
    )


def _approve(mgr, tool, version, params):
    return mgr.record_approval(
        principal=HUMAN, action_hash=params_hash(tool, version, params), target=tool, scope=SCOPE,
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )


# ---- ToolSpec／resolver 本身 ----

def test_tool_spec_defaults_and_frozen():
    spec = ToolSpec(
        executor="e", side_effect_class="internal_read", idempotency="native", presence_bound=False, tool_version=1
    )
    assert (spec.origin, spec.provider, spec.targeting) == ("builtin", "core", "core_local")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.executor = "x"  # type: ignore[misc]


def test_tool_call_tool_version_defaults_to_none():
    call = ToolCall(
        action_id="a", tool="t", params={}, params_hash="h", side_effect_class="internal_read", idempotency="none"
    )
    assert call.tool_version is None


def test_static_registry_resolves_and_is_read_only():
    spec = ToolSpec("e", "external_effect", "via_action_id", True, 3)
    reg = StaticToolRegistry({POST: spec})
    assert isinstance(reg, ToolSpecResolver) and isinstance(reg, EnumerableToolSpecResolver)
    assert reg.resolve(POST) is spec
    with pytest.raises(PolicyViolation) as exc:
        reg.resolve("nope")
    assert exc.value.code == TOOL_UNKNOWN
    with pytest.raises(TypeError):
        reg.specs()["x"] = spec  # type: ignore[index]
    for bad in (0, True, "1"):
        with pytest.raises(ValueError):
            StaticToolRegistry({"t": dataclasses.replace(spec, tool_version=bad)})  # type: ignore[arg-type]


def test_fake_resolver_register_overrides_and_resolve_reads_current_data():
    fake = FakeToolSpecResolver()
    assert isinstance(fake, ToolSpecResolver)
    fake.register("t", side_effect_class="internal_read")
    assert fake.resolve("t").side_effect_class == "internal_read"
    fake.register("t", side_effect_class="high_impact", tool_version=2)
    assert (fake.resolve("t").side_effect_class, fake.resolve("t").tool_version) == ("high_impact", 2)
    with pytest.raises(PolicyViolation) as exc:
        fake.resolve("missing")
    assert exc.value.code == TOOL_UNKNOWN


# ---- VC-S2-1：宣告繞過 ----

def test_tool_resolver_is_required(session_factory):
    from uep.events.bus import EventBus
    from uep.events.publisher import EventPublisher

    pub = EventPublisher(session_factory, EventBus())
    with pytest.raises(TypeError):
        TaskManager(session_factory, pub, {}, tool_resolver=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TaskManager(session_factory, pub, {})  # type: ignore[call-arg]


@pytest.mark.parametrize("legacy", [
    {"side_effect_class": "internal_read"}, {"executor": "stub"}, {"idempotency": "none"},
])
def test_vc_s2_1_legacy_declaration_params_are_rejected(mgr, legacy):
    declare(mgr, POST, side_effect="external_effect")
    _task, run, step = running(mgr)
    with pytest.raises(TypeError):
        _action(mgr, run, step, {"m": "hi"}, **legacy)


def test_vc_s2_1_side_effect_class_always_from_registry_and_hint_ignored(mgr, executor):
    declare(mgr, POST, side_effect="external_effect", idempotency="via_action_id")
    fake_rt = FakeRuntime()
    fake_rt.script([rt.tool_intent(POST, {"m": "hi"}, side_effect_hint="internal_read")])
    from tests.uep.test_fakes import _request
    attempt = fake_rt.start(_request())
    (event,) = list(fake_rt.stream(attempt))
    assert event.tool_intent["side_effect_hint"] == "internal_read"  # Runtime 說謊：宣稱唯讀

    _task, run, step = running(mgr)
    intent = event.tool_intent
    action = _action(mgr, run, step, intent["args"], tool=intent["tool"])
    assert action.side_effect_class == "external_effect"  # 恆為註冊表值，hint 不參與
    assert (action.executor, action.idempotency, action.tool_version) == ("stub", "via_action_id", 1)
    from uep.tasks.manager import ApprovalRequired
    with pytest.raises(ApprovalRequired):  # 真的被當 external_effect 要求核准
        mgr.execute_action(action.action_id, intent["args"], worker_epoch=run.worker_epoch)
    assert executor.calls == []


def test_unknown_tool_is_rejected_without_row(mgr, session_factory):
    _task, run, step = running(mgr)
    with pytest.raises(PolicyViolation) as exc:
        _action(mgr, run, step, {}, tool="not.registered")
    assert exc.value.code == TOOL_UNKNOWN
    with session_factory() as s:
        assert s.scalars(select(ActionIntentRow)).first() is None


def test_resolver_is_consulted_on_every_call_not_snapshotted(session_factory):
    resolver = FakeToolSpecResolver()
    mgr = make_manager(session_factory, StubExecutor(), tool_resolver=resolver)  # 建構時註冊表是空的
    _task, run, step = running(mgr)
    with pytest.raises(PolicyViolation):
        _action(mgr, run, step, {})
    declare(mgr, POST, side_effect="reversible_local")  # 建構之後才登記
    assert _action(mgr, run, step, {}).side_effect_class == "reversible_local"
    declare(mgr, POST, side_effect="external_effect")  # 之後改分類
    assert _action(mgr, run, step, {"n": 2}).side_effect_class == "external_effect"


def test_presence_bound_only_tightens(mgr):
    _task, run, step = running(mgr)
    mgr.tool_resolver.register(
        "loose", executor="stub", side_effect_class="internal_read", idempotency="none", presence_bound=False
    )
    mgr.tool_resolver.register(
        "tight", executor="stub", side_effect_class="internal_read", idempotency="none", presence_bound=True
    )
    assert _action(mgr, run, step, {}, tool="loose").presence_bound is False
    assert _action(mgr, run, step, {"a": 1}, tool="loose", presence_bound=True).presence_bound is True
    assert _action(mgr, run, step, {}, tool="tight").presence_bound is True
    with pytest.raises(PolicyViolation):
        _action(mgr, run, step, {"b": 1}, tool="tight", presence_bound=False)  # 不可放寬


# ---- VC-S2-4：版本綁定 ----

def test_hash_domain_includes_tool_and_version():
    p = {"m": "hi"}
    assert params_hash(POST, 1, p) != params_hash(POST, 2, p)
    assert params_hash(POST, 1, p) != params_hash("chatroom.read", 1, p)
    assert params_hash(POST, 1, p) == TaskManager.params_hash(POST, 1, p)
    for bad_version in (True, "1", 1.0):
        with pytest.raises(PolicyViolation):
            params_hash(POST, bad_version, p)  # type: ignore[arg-type]


def test_vc_s2_4_upgrade_changes_hash_and_invalidates_old_approval(mgr, executor):
    params = {"m": "hi"}
    declare(mgr, POST, side_effect="external_effect")
    _task, run, step = running(mgr)
    v1 = _action(mgr, run, step, params)
    approval = _approve(mgr, POST, 1, params)  # appr_1 對應 chatroom.post@v1

    mgr.tool_resolver.register(
        POST, executor="stub", side_effect_class="external_effect", idempotency="via_action_id", tool_version=2
    )
    v2 = _action(mgr, run, step, params)  # 同 params、v2
    assert v2.tool_version == 2 and v2.params_hash != v1.params_hash
    with pytest.raises(ApprovalInvalid) as exc:
        mgr.execute_action(v2.action_id, params, worker_epoch=run.worker_epoch, approval_id=approval.approval_id)
    assert exc.value.code == "hash_mismatch"
    assert executor.calls == []


def test_vc_s2_4_v1_intended_action_blocked_after_upgrade(mgr, executor):
    params = {"p": 1}
    declare(mgr, "files.write", side_effect="internal_read")
    _task, run, step = running(mgr)
    old = _action(mgr, run, step, params, tool="files.write")
    mgr.tool_resolver.register(
        "files.write", executor="stub", side_effect_class="internal_read", idempotency="via_action_id",
        tool_version=2,
    )
    with pytest.raises(ActionBlocked, match="tool_version_changed"):
        mgr.execute_action(old.action_id, params, worker_epoch=run.worker_epoch)
    assert executor.calls == []
    assert mgr.get_action(old.action_id).status == ActionStatus.INTENDED.value
    fresh = _action(mgr, run, step, params, tool="files.write")  # 新 action 以 v2 重來即可執行
    assert mgr.execute_action(fresh.action_id, params, worker_epoch=run.worker_epoch).executed


def test_finished_action_replay_not_blocked_by_later_upgrade(mgr, executor):
    params = {"p": 1}
    declare(mgr, "files.write", side_effect="internal_read")
    _task, run, step = running(mgr)
    act = _action(mgr, run, step, params, tool="files.write")
    assert mgr.execute_action(act.action_id, params, worker_epoch=run.worker_epoch).executed
    mgr.tool_resolver.register(
        "files.write", executor="stub", side_effect_class="internal_read", idempotency="via_action_id",
        tool_version=9,
    )
    again = mgr.execute_action(act.action_id, params, worker_epoch=run.worker_epoch)
    assert not again.executed and again.status is ActionStatus.SUCCEEDED
    assert executor.calls == [act.action_id]


def test_execute_hashes_with_stored_tool_and_version_and_passes_version_to_executor(session_factory):
    from tests.uep.acceptance_support import make_tm, setup_run

    ex = FakeToolExecutor()
    tm = make_tm(session_factory, ex)
    _task, run, _step, (item,) = setup_run(tm, 1)
    row = tm.get_action(item.action_id)
    assert row.tool_version == 1 and row.params_hash == params_hash("files.write", 1, item.params)
    tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    assert ex.calls[0].tool_version == 1


# ---- TG-1／TG-5：組裝層 ----

def test_assert_wired_requires_tool_resolver():
    wired = SimpleNamespace(
        presence_check_configured=True, transition_guard_configured=True, action_guard_configured=True,
        tool_resolver=None,
    )
    with pytest.raises(AssemblyError, match="tool_resolver"):
        _assert_wired(wired, SimpleNamespace(_gate=object()))  # type: ignore[arg-type]


def test_assembly_refuses_without_resolver(session_factory, tmp_path):
    with pytest.raises(AssemblyError, match="tool_resolver"):
        assemble_core(session_factory, **assembly_kwargs(session_factory, tmp_path, tool_resolver=None))
    with pytest.raises(TypeError):  # 連參數都沒給也不行
        base = assembly_kwargs(session_factory, tmp_path)
        del base["tool_resolver"]
        assemble_core(session_factory, **base)


def test_tg5_registry_referencing_unknown_executor_is_refused(session_factory, tmp_path):
    resolver = FakeToolSpecResolver(a={"executor": "fake"}, b={"executor": "ghost"})
    with pytest.raises(AssemblyError, match="ghost"):
        assemble_core(session_factory, **assembly_kwargs(session_factory, tmp_path, tool_resolver=resolver))


def test_tg5_unreferenced_executor_is_refused_unless_declared_internal(session_factory, tmp_path):
    over = {
        "executors": {"fake": FakeToolExecutor(), "spare": FakeToolExecutor()},
        "tool_resolver": FakeToolSpecResolver(a={}),
    }
    with pytest.raises(AssemblyError, match="spare"):
        assemble_core(session_factory, **assembly_kwargs(session_factory, tmp_path, **over))
    core = assemble_core(
        session_factory, **assembly_kwargs(session_factory, tmp_path, internal_executors=("spare",), **over)
    )
    assert core.tasks.tool_resolver is over["tool_resolver"]


def test_tg5_skipped_for_non_enumerable_resolver(session_factory, tmp_path):
    class Dynamic:
        def resolve(self, tool: str) -> ToolSpec:
            raise PolicyViolation("none", code=TOOL_UNKNOWN)

    over = {"executors": {"fake": FakeToolExecutor(), "spare": FakeToolExecutor()}, "tool_resolver": Dynamic()}
    assert not isinstance(over["tool_resolver"], EnumerableToolSpecResolver)
    assert assemble_core(session_factory, **assembly_kwargs(session_factory, tmp_path, **over))


def test_assembly_accepts_static_registry(session_factory, tmp_path):
    reg = StaticToolRegistry({POST: ToolSpec("fake", "external_effect", "via_action_id", False, 1)})
    core = assemble_core(session_factory, **assembly_kwargs(session_factory, tmp_path, tool_resolver=reg))
    assert core.tasks.tool_resolver is reg


# ---- migration 0009 ----

def test_migration_0009_adds_tool_version_and_is_reversible(alembic_cfg, engine, session_factory):
    def column():
        return next(c for c in inspect(engine).get_columns("action_intents") if c["name"] == "tool_version")

    col = column()
    assert col["nullable"] is False and str(col["default"]).strip("'") == "1"
    try:
        command.downgrade(alembic_cfg, "0008_receipt_worker_epoch")
        assert "tool_version" not in {c["name"] for c in inspect(engine).get_columns("action_intents")}
    finally:
        command.upgrade(alembic_cfg, "head")
    assert column()["nullable"] is False


# ---- 審查修正：action.presence_bound 執行（D62）、重送 create_action 的比對、TG-5 補強 ----

def _action_bound_setup(session_factory, state, *, hook=None):
    from tests.uep.test_task_model import POLICY, new_task

    executor = StubExecutor()
    kw = {"crash_hook": hook} if hook else {}
    mgr = make_manager(session_factory, executor, presence_check=lambda _p: state["ok"], **kw)
    task = new_task(mgr, policy=POLICY)
    assert task.presence_bound is False  # Task 非 presence_bound，只有工具宣告為 true
    run = mgr.start_run(task.task_id, persona_version=1, policy_version=1)
    step = mgr.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    mgr.tool_resolver.register(
        "files.write", executor="stub", side_effect_class="internal_read", idempotency="via_action_id",
        presence_bound=True, tool_version=1,
    )
    params = {"a": 1}
    action = mgr.create_action(
        run_id=run.run_id, step_id=step.step_id, tool="files.write", params=params, worker_epoch=run.worker_epoch,
    )
    assert action.presence_bound is True
    return mgr, executor, run, action, params


def test_action_presence_bound_enforced_in_stage1(session_factory):
    from uep.tasks.manager import PresenceRequired

    state = {"ok": False}
    mgr, executor, run, action, params = _action_bound_setup(session_factory, state)
    with pytest.raises(PresenceRequired) as err:
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert err.value.retryable is True and executor.calls == []
    assert mgr.get_action(action.action_id).status == "INTENDED"


def test_action_presence_bound_enforced_in_stage15(session_factory):
    from uep.tasks.manager import PresenceRequired

    state = {"ok": True}

    def hook(point: str) -> None:
        if point == "action.after_executing_commit":
            state["ok"] = False

    mgr, executor, run, action, params = _action_bound_setup(session_factory, state, hook=hook)
    with pytest.raises(PresenceRequired) as err:
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert err.value.retryable is False and executor.calls == []
    assert mgr.get_action(action.action_id).status == "FAILED"


def test_resend_create_action_after_version_bump_is_tool_version_changed(mgr):
    _task, run, step = running(mgr)
    mgr.tool_resolver.register("files.write", executor="stub", side_effect_class="internal_read", tool_version=1)
    first = _action(mgr, run, step, {"a": 1}, tool="files.write")
    mgr.tool_resolver.register("files.write", executor="stub", side_effect_class="internal_read", tool_version=2)
    with pytest.raises(ActionBlocked, match="^tool_version_changed"):
        _action(mgr, run, step, {"a": 1}, tool="files.write", action_id=first.action_id)


def test_resend_create_action_cannot_silently_keep_looser_presence_bound(mgr):
    _task, run, step = running(mgr)
    mgr.tool_resolver.register("files.write", executor="stub", side_effect_class="internal_read", tool_version=1)
    first = _action(mgr, run, step, {"a": 1}, tool="files.write", presence_bound=False)
    assert first.presence_bound is False
    with pytest.raises(PolicyViolation):  # 呼叫端這次要求收緊，既有列較寬鬆
        _action(mgr, run, step, {"a": 1}, tool="files.write", presence_bound=True, action_id=first.action_id)
    # 同條件重送仍冪等
    again = _action(mgr, run, step, {"a": 1}, tool="files.write", presence_bound=False, action_id=first.action_id)
    assert again.action_id == first.action_id


def test_tg5_internal_executor_must_exist_in_executors(session_factory, tmp_path):
    over = {"tool_resolver": FakeToolSpecResolver(a={})}
    with pytest.raises(AssemblyError, match="ghost"):
        assemble_core(
            session_factory, **assembly_kwargs(session_factory, tmp_path, internal_executors=("ghost",), **over)
        )


def test_tg5_non_enumerable_resolver_logs_warning(session_factory, tmp_path, caplog):
    class Dynamic:
        def resolve(self, tool: str) -> ToolSpec:
            raise PolicyViolation("none", code=TOOL_UNKNOWN)

    with caplog.at_level("WARNING", logger="uep.assembly"):
        assemble_core(session_factory, **assembly_kwargs(session_factory, tmp_path, tool_resolver=Dynamic()))
    assert sum("TG-5" in r.getMessage() for r in caplog.records) == 1
