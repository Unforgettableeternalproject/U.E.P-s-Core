"""測試 Console 是獨立平台 ``console``，只存在於測試模式：enum／presence CHECK／不是排程位置／preset／投影／V18 守門／
仲裁排除／acquire 測試模式門／judge_now(target=None)。除標明需要資料庫者外全部離線。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.uep.test_tool_projection import ALL_TAGS, _caps, names
from uep.assembly import AssemblyError, check_source_location_policy
from uep.contracts.platform import platform_from_presence_location
from uep.contracts.presence import PresenceLocation
from uep.contracts.schedule import (
    PLAN_LOCATIONS,
    REQUEST_TARGET_LOCATIONS,
    InterruptFacts,
)
from uep.decision.presence_absent_intent import TARGET_LOCATIONS
from uep.presence import models as presence_models
from uep.schedule import db as schedule_db
from uep.schedule.arbiter_shadow import PresenceArbiterShadow
from uep.schedule.planner import PresenceReading
from uep.stream.contracts.output import BARGE_IN_PRESETS, PolicySnapshot
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry


def _check_text(table, name_suffix: str) -> str:
    return next(str(c.sqltext) for c in table.constraints if getattr(c, "name", None) and str(c.name).endswith(name_suffix))


def test_enum_and_platform_vocabulary_include_console() -> None:
    assert PresenceLocation("console") is PresenceLocation.CONSOLE
    assert platform_from_presence_location("console") == "console"


def test_only_presence_current_check_allows_console() -> None:
    assert "console" in presence_models.LOCATIONS
    assert "'console'" in _check_text(presence_models.PresenceCurrentRow.__table__, "location")
    assert "'console'" not in _check_text(schedule_db.PlanItemRow.__table__, "location")  # 行程不能指派 console
    assert "'console'" not in _check_text(schedule_db.PlanOverrideRow.__table__, "location")  # override 也不行


def test_console_is_not_a_plan_location_nor_request_target() -> None:
    assert "console" not in PLAN_LOCATIONS
    assert REQUEST_TARGET_LOCATIONS == PLAN_LOCATIONS
    assert "console" not in TARGET_LOCATIONS  # Laya／rules 的移轉目標詞彙不含 console
    assert set(TARGET_LOCATIONS) == REQUEST_TARGET_LOCATIONS  # 兩邊內容一致（型別不同：tuple／frozenset）


def test_arbiter_request_rejects_console_target() -> None:
    from uep.contracts.schedule import ArbiterRequest

    ArbiterRequest(request_id="r1", authority="owner", requester_ref="a", actor_authenticated=True, target="desktop")
    with pytest.raises(ValueError):
        ArbiterRequest(request_id="r1", authority="owner", requester_ref="a", actor_authenticated=True, target="console")


def test_migration_0022_only_touches_presence_current_offline_sql() -> None:
    """對 0022 產出離線 SQL：只動 presence_current，約束名稱依命名慣例展開為雙重前綴。"""
    import io
    import re

    from alembic import command

    from tests.uep.conftest import alembic_config

    def sql(spec: str, downgrade: bool = False) -> str:
        buf = io.StringIO()
        cfg = alembic_config("postgresql+psycopg://u:p@localhost/offline_sql_only")
        cfg.output_buffer = buf
        (command.downgrade if downgrade else command.upgrade)(cfg, spec, sql=True)
        return buf.getvalue()

    up = sql("0021_presence_plan:0022_console_location")
    assert "ck_presence_current_ck_presence_current_location" in up and "presence_current" in up
    assert "'console'" in up and "presence_plan_items" not in up.split("alembic_version")[0]
    assert re.search(r"DROP CONSTRAINT ck_presence_current_ck_presence_current_location", up)
    down = sql("0022_console_location:0021_presence_plan", downgrade=True)
    assert "ck_presence_current_ck_presence_current_location" in down and "presence_plan_items" not in down


def test_barge_in_preset_covers_console() -> None:
    assert BARGE_IN_PRESETS["console"] == BARGE_IN_PRESETS["desktop"]
    assert PolicySnapshot.for_platform("console").platform == "console"


def test_tool_projection_console_gets_native_tools_only() -> None:
    registry = ToolRegistry.build(_caps())
    active = frozenset({"console"})
    console = names(registry.project(ALL_TAGS, ProjectionContext(platform="console", presence_active_platforms=active)))
    desktop = names(registry.project(ALL_TAGS, ProjectionContext(platform="desktop", presence_active_platforms=frozenset({"desktop"}))))
    assert "desktop.file_read" in desktop and "desktop.file_read" not in console  # 不繼承 desktop 平台工具
    assert {"deep_recall", "notify.user"} <= set(console)  # 原生（global）工具照常
    # desktop 變體也不會套用到 console
    notify = {t["tool"]: t for t in registry.project(ALL_TAGS, ProjectionContext(platform="console"))["tools"]}["notify.user"]
    assert notify["variant"] is None


def test_v18_guard_accepts_console_but_not_other_locations() -> None:
    check_source_location_policy(("console",), False)
    check_source_location_policy(("desktop", "console"), False)
    for other in (("discord",), ("website",), ("unknown",), ("console", "discord")):
        with pytest.raises(AssemblyError):
            check_source_location_policy(other, False)
    check_source_location_policy(("console", "discord"), True)


def _shadow(judged: list) -> PresenceArbiterShadow:
    shadow = PresenceArbiterShadow(
        SimpleNamespace(), lambda: PresenceReading("ACTIVE", "console"), None, InterruptFacts)
    shadow._judge_request = lambda request: judged.append(request.target)  # type: ignore[method-assign]
    return shadow


def test_arbiter_never_judges_console_as_request_target() -> None:
    judged: list = []
    shadow = _shadow(judged)
    for target in ("console", "desktop"):
        shadow.on_presence_request(
            SimpleNamespace(intent="presence_request", target_location=target), request_id=f"r-{target}",
            actor_ref="discord:1", actor_authenticated=True, is_owner=True, source_platform="discord")
    assert judged == ["desktop"]


def test_console_runtime_uses_console_platform_and_location(tmp_path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from uep.console.rig import build_fake_runtime

    factory = sessionmaker(bind=create_engine("sqlite://"))  # 不啟動 Core、不連資料庫
    rt = build_fake_runtime(factory, tmp_path / "e2e", start=False)
    assert rt.host.location == "console"
    assert rt.host._session.platform == "console"
    assert rt.host.endpoint._location == "console"


def test_stt_stage_declares_console_source_location() -> None:
    from uep.config import UepSettings
    from uep.console import real_rig

    for flag in (False, True):
        stage = real_rig.build_stt_stage(UepSettings(_env_file=None, UEP_CONSOLE_INGRESS_ENDPOINT=flag), {})
        assert stage.source_location == PresenceLocation.CONSOLE


def _consult_facts(platforms: list[str]) -> dict:
    scope = {"capability_tags": ["decision.consult"], "modalities": ["text"], "platforms": platforms}
    return {
        "sub_kind": "placement", "run_id": "run_1", "step_id": "step_1", "ordinal": 0, "frozen": scope,
        "capability_snapshot": scope, "target_platform": "console", "executor_ref": "console",
        "current_platform": "console", "available_executors": ["console"],
    }


def test_consult_placement_on_console_turn_is_advice_when_scope_names_console() -> None:
    from uep.decision.loop_consult import LoopConsultEngine

    result = LoopConsultEngine().consult(_consult_facts(["console"]))
    assert not result.escalate.needed and result.outcome == "placement_set"
    assert result.advice.placement.target_platform == "console"


def test_consult_placement_escalates_when_scope_lacks_console() -> None:
    from uep.decision.loop_consult import LoopConsultEngine

    result = LoopConsultEngine().consult(_consult_facts(["desktop"]))  # 不繼承 desktop 範圍；placement 安全意圖不變
    assert result.escalate.needed and result.escalate.reason_code == "placement_review"


def test_console_turn_queries_judge_now_with_none_and_gets_work_in_progress() -> None:
    """不 patch：console 平台的回合走真實 ``_query_busy``，送進 judge_now 的 target 是 None，discord 工作格 -> work_in_progress。"""
    import dataclasses

    from tests.uep.test_action_unavailable import CTX
    from tests.uep.test_schedule_judge_now import make
    from tests.uep.test_turn_judge_now import OWNER, _orch
    from uep.turn.models import UtteranceFact

    shadow = make()
    calls: list = []

    def judge(**kw):
        decision = shadow.judge_now(**kw)
        calls.append((kw["target"], decision))
        return decision

    orch = _orch(judge)
    try:
        ticket = orch.on_utterance(
            dataclasses.replace(CTX, platform="console"),
            UtteranceFact(speaker_ref=OWNER, text="你在做什麼", seq=1, source_endpoint_id="ep")).ticket
    finally:
        orch.close()
        shadow.stop()
    assert len(calls) == 1 and calls[0][0] is None
    assert (calls[0][1].outcome, calls[0][1].reason_code) == ("REJECTED", "work_in_progress")
    assert ticket is not None and ticket.presence_busy == "work"


def test_acquire_console_denied_unless_test_mode(session_factory, tmp_path) -> None:
    """需要資料庫。正式組裝（預設）acquire(console) 固定 reason 拒絕；測試模式組裝才核發 Lease。"""
    from tests.uep.presence_support import FakeClock
    from uep.contracts.presence import AcquireDenyReason, AcquireResult
    from uep.presence.epoch import HostEpochStore
    from uep.presence.manager import PresenceManager

    prod = PresenceManager(session_factory, epoch_store=HostEpochStore(tmp_path / "p.json"), clock=FakeClock())
    prod.start()
    denied = prod.acquire("ep_console", "console")
    assert denied.result is AcquireResult.DENIED and denied.reason is AcquireDenyReason.CONSOLE_NOT_ALLOWED
    assert denied.grant is None
    assert prod.acquire("ep_desktop", "desktop").result is AcquireResult.GRANTED  # 其他位置不受影響


def test_acquire_console_granted_in_test_mode(session_factory, tmp_path) -> None:
    """需要資料庫。"""
    from tests.uep.presence_support import FakeClock
    from uep.contracts.presence import AcquireResult
    from uep.presence.epoch import HostEpochStore
    from uep.presence.manager import PresenceManager

    manager = PresenceManager(
        session_factory, epoch_store=HostEpochStore(tmp_path / "t.json"), clock=FakeClock(), allow_console=True)
    manager.start()
    assert manager.acquire("ep_console", "console").result is AcquireResult.GRANTED


def test_assemble_core_default_does_not_allow_console_and_rig_does() -> None:
    import inspect

    from uep.assembly import assemble_core
    from uep.console import rig

    assert inspect.signature(assemble_core).parameters["allow_console_platform"].default is False
    assert '"allow_console_platform": True' in inspect.getsource(rig.build_fake_runtime)


def test_endpoint_cli_location_has_no_console() -> None:
    from uep.endpoint.__main__ import main

    with pytest.raises(SystemExit):
        main(["--port", "1", "--location", "console"])


def test_console_not_allowed_is_not_retryable() -> None:
    from tests.uep.endpoint_support import FakeCore, Mono, wall_now
    from uep.contracts.presence import AcquireDenyReason, AcquireResult
    from uep.endpoint.endpoint import CliEndpoint
    from uep.endpoint.protocol import AcquireReply

    class Denied(FakeCore):
        def acquire(self, location):
            return AcquireReply(AcquireResult.DENIED, reason=AcquireDenyReason.CONSOLE_NOT_ALLOWED)

    ep = CliEndpoint("ep_cli", Denied(), clock=Mono(), wall_clock=wall_now)
    assert ep.start_lease() is AcquireResult.DENIED and not ep.acquire_retryable
