"""測試 Console 是獨立平台 ``console``：enum／CHECK／行程詞彙／preset／投影／V18 守門／仲裁排除。全部離線。"""
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


def test_db_checks_allow_console_except_override() -> None:
    assert "console" in presence_models.LOCATIONS
    assert "'console'" in _check_text(presence_models.PresenceCurrentRow.__table__, "location")
    assert "'console'" in _check_text(schedule_db.PlanItemRow.__table__, "location")
    assert "'console'" not in _check_text(schedule_db.PlanOverrideRow.__table__, "location")  # override 不能移到測試入口


def test_plan_locations_include_console_but_request_targets_do_not() -> None:
    assert "console" in PLAN_LOCATIONS
    assert "console" not in REQUEST_TARGET_LOCATIONS and REQUEST_TARGET_LOCATIONS == PLAN_LOCATIONS - {"console"}
    assert "console" not in TARGET_LOCATIONS  # Laya／rules 的移轉目標詞彙不含 console
    assert set(TARGET_LOCATIONS) == REQUEST_TARGET_LOCATIONS  # 兩邊內容一致（型別不同：tuple／frozenset）


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
