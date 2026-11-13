"""權限契約型別（PM-1）：數值帶、對照表驗證、硬下限、只能收緊、authority↔tier 一致性、系統元件閉集。"""
import ast
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from uep.contracts import permission as perm
from uep.contracts import schedule as sc
from uep.contracts.permission import (
    DEFAULT_POLICY,
    DEFAULT_RULES,
    GRADE_LEVEL,
    HARD_FLOORS,
    SYSTEM_COMPONENTS,
    InstructionRule,
    PermissionPolicy,
    SystemComponent,
    WorkGrade,
    load_policy,
    validate_rules,
)
from uep.contracts.permission import (
    InstructionLevel as L,
)
from uep.contracts.permission import (
    RequesterTier as T,
)
from uep.schedule import arbiter as ar


def _rules(**changes: InstructionRule) -> dict:
    rules = dict(DEFAULT_RULES)
    for name, rule in changes.items():
        rules[L[name]] = rule
    return rules


# ---- 數值帶 ----

def test_tier_band_values_and_priority_mapping():
    assert [int(t) for t in T] == [10, 30, 50, 75, 100, 200, 300]
    assert [t.name for t in T] == ["GUEST", "UNVERIFIED", "MEMBER", "ADMIN", "SYSTEM", "OWNER", "EMERGENCY"]
    assert (sc.PRIORITY_PLAN, sc.PRIORITY_REQUESTER, sc.PRIORITY_SYSTEM, sc.PRIORITY_OWNER, sc.PRIORITY_EMERGENCY) \
        == (0, 50, 100, 200, 300)
    assert sc.PRIORITY_REQUESTER == T.MEMBER and sc.PRIORITY_SYSTEM == T.SYSTEM
    assert sc.PRIORITY_OWNER == T.OWNER and sc.PRIORITY_EMERGENCY == T.EMERGENCY
    assert sc.PRIORITY_PLAN < T.GUEST  # 行程 directive 的帶嚴格最低
    assert perm.TIER_BAND == {t: int(t) for t in T}
    assert sc.PRIORITY_BY_AUTHORITY == {"member": 50, "system": 100, "owner": 200}


def test_instruction_levels_complete_and_ordered():
    assert len(L) == 12
    assert [int(level) for level in L] == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120]
    assert GRADE_LEVEL == {WorkGrade.SUPERIOR: L.TASK_SUPERIOR, WorkGrade.NORMAL: L.TASK_NORMAL,
                           WorkGrade.MINOR: L.TASK_MINOR}


# ---- 預設表（§2.2）----

def test_default_table_matches_contract():
    expected = {
        L.FORCED_SLEEP: T.OWNER, L.SESSION_INTERRUPT: T.OWNER, L.SYSTEM_INFO: T.SYSTEM, L.TEMP_SESSION: T.ADMIN,
        L.PAUSE: T.ADMIN, L.TASK_SUPERIOR: T.MEMBER, L.TASK_NORMAL: T.MEMBER, L.TASK_MINOR: T.UNVERIFIED,
        L.ACTION: T.UNVERIFIED, L.PUBLIC_INFO: T.UNVERIFIED, L.MEMORY: T.UNVERIFIED, L.CHAT: T.GUEST,
    }
    assert {level: rule.min_tier for level, rule in DEFAULT_RULES.items()} == expected
    assert {level for level, rule in DEFAULT_RULES.items() if rule.requires_human_origin} == {L.FORCED_SLEEP}
    assert {level for level, rule in DEFAULT_RULES.items() if rule.requires_confirmation} == {L.FORCED_SLEEP}
    assert validate_rules(DEFAULT_RULES) == []
    assert DEFAULT_POLICY.required_tier(L.PAUSE) == T.ADMIN


def test_authorize_default_policy():
    p = DEFAULT_POLICY
    assert p.authorize(T.GUEST, L.CHAT) and not p.authorize(T.GUEST, L.MEMORY)  # 訪客永遠不能用記憶
    assert p.authorize(T.UNVERIFIED, L.MEMORY) and not p.authorize(T.UNVERIFIED, L.TASK_NORMAL)
    assert p.authorize(T.MEMBER, L.TASK_SUPERIOR) and not p.authorize(T.MEMBER, L.PAUSE)
    assert p.authorize(T.ADMIN, L.TEMP_SESSION) and not p.authorize(T.ADMIN, L.SYSTEM_INFO)  # 管理員不能要求系統資訊
    assert p.authorize(T.SYSTEM, L.SYSTEM_INFO) and p.authorize(T.OWNER, L.SYSTEM_INFO)
    assert p.authorize(T.EMERGENCY, L.SYSTEM_INFO)
    assert p.authorize(T.OWNER, L.SESSION_INTERRUPT) and not p.authorize(T.SYSTEM, L.SESSION_INTERRUPT)


def test_forced_sleep_needs_owner_and_human_origin():
    p = DEFAULT_POLICY
    assert not p.authorize(T.OWNER, L.FORCED_SLEEP)  # 預設 fail-closed：未明示 human-origin
    assert p.authorize(T.OWNER, L.FORCED_SLEEP, human_origin=True)
    assert not p.authorize(T.ADMIN, L.FORCED_SLEEP, human_origin=True)
    assert not p.authorize(T.EMERGENCY, L.FORCED_SLEEP)  # 緊急不含強制睡眠權（Q-PM-11）


def test_policy_rules_are_read_only_copy():
    source = dict(DEFAULT_RULES)
    policy = PermissionPolicy(7, source)
    source[L.CHAT] = InstructionRule(T.OWNER)
    assert policy.required_tier(L.CHAT) == T.GUEST and policy.revision == 7
    with pytest.raises(TypeError):
        policy.rules[L.CHAT] = InstructionRule(T.OWNER)  # type: ignore[index]


# ---- 驗證：單調性、硬下限、只能收緊 ----

def test_validation_requires_all_levels():
    rules = dict(DEFAULT_RULES)
    del rules[L.PAUSE]
    assert any("缺少" in e for e in validate_rules(rules))
    assert validate_rules("nope") and validate_rules({L.CHAT: "x"})


def test_validation_monotonic():
    # 在預設之上收緊也要單調：PAUSE 抬高到 SYSTEM，則 TEMP_SESSION（ADMIN）反而更低 → 違反
    bad = _rules(PAUSE=InstructionRule(T.SYSTEM))
    assert any("單調性" in e for e in validate_rules(bad))
    # 一併抬高後的單調表通過
    ok = _rules(PAUSE=InstructionRule(T.SYSTEM), TEMP_SESSION=InstructionRule(T.SYSTEM))
    assert validate_rules(ok) == []


def test_validation_hard_floors_cannot_be_lowered():
    assert dict(HARD_FLOORS) == {L.FORCED_SLEEP: T.OWNER, L.SESSION_INTERRUPT: T.OWNER, L.MEMORY: T.UNVERIFIED}
    for name in ("FORCED_SLEEP", "SESSION_INTERRUPT"):
        errors = validate_rules(_rules(**{name: InstructionRule(T.ADMIN)}))
        assert any("硬下限" in e for e in errors), name
    # 記憶降到訪客：硬下限違反（即使單調性與其他檢查都滿足）
    errors = validate_rules(_rules(MEMORY=InstructionRule(T.GUEST)))
    assert any("硬下限" in e and "MEMORY" in e for e in errors)


def test_validation_can_only_tighten():
    # 降低任何一格都違反；抬高則允許
    errors = validate_rules(_rules(TASK_NORMAL=InstructionRule(T.UNVERIFIED), TASK_SUPERIOR=InstructionRule(T.UNVERIFIED)))
    assert any("只能收緊" in e for e in errors)
    assert validate_rules(_rules(CHAT=InstructionRule(T.UNVERIFIED))) == []
    # 取消 human-origin／確認要求也是放寬
    relaxed = _rules(FORCED_SLEEP=InstructionRule(T.OWNER, requires_human_origin=False, requires_confirmation=True))
    assert any("human-origin" in e for e in validate_rules(relaxed))
    relaxed = _rules(FORCED_SLEEP=InstructionRule(T.OWNER, requires_human_origin=True, requires_confirmation=False))
    assert any("確認" in e for e in validate_rules(relaxed))
    # 對一般格再加 human-origin 是收緊
    assert validate_rules(_rules(PAUSE=InstructionRule(T.ADMIN, requires_human_origin=True))) == []


def test_load_policy_accepts_valid_and_falls_back_on_invalid(caplog):
    tightened = _rules(MEMORY=InstructionRule(T.MEMBER), PUBLIC_INFO=InstructionRule(T.MEMBER),
                       ACTION=InstructionRule(T.MEMBER), TASK_MINOR=InstructionRule(T.MEMBER))
    loaded = load_policy(tightened, 5)
    assert loaded.revision == 5 and loaded.required_tier(L.MEMORY) == T.MEMBER

    with caplog.at_level(logging.WARNING, logger=perm.__name__):
        fallback = load_policy(_rules(MEMORY=InstructionRule(T.GUEST)), 9)
    assert fallback is DEFAULT_POLICY and fallback.revision == perm.DEFAULT_POLICY_REVISION
    assert any("回退內建預設表" in r.message for r in caplog.records)

    assert load_policy(None, 2) is DEFAULT_POLICY
    assert load_policy({}, 2) is DEFAULT_POLICY
    assert load_policy(dict(DEFAULT_RULES), "3") is DEFAULT_POLICY  # revision 型別不合法也回退


# ---- 系統元件閉集 ----

def test_system_components_closed_set():
    assert all(isinstance(c, SystemComponent) for c in SYSTEM_COMPONENTS)
    ids = {c.component_id for c in SYSTEM_COMPONENTS}
    assert ids == {"core.schedule_planner", "core.global_stop", "core.health_watchdog"}
    assert perm.is_system_component("core.global_stop") and not perm.is_system_component("core.unknown")
    assert perm.system_component("core.global_stop").max_tier == T.EMERGENCY
    assert perm.system_component("core.schedule_planner").may_propose_transfer is True
    assert perm.system_component("core.chatroom_assignment") is None  # Chatroom 指派視為資料
    assert SystemComponent("core.x").may_propose_transfer is False  # 能力預設關閉
    assert perm.system_component("discord.bot") is None
    with pytest.raises(ValueError):
        SystemComponent("discord.system")
    with pytest.raises(ValueError):
        SystemComponent("core.x", max_tier=T.OWNER)


# ---- ArbiterRequest：tier 推導與 authority↔tier 一致性 ----

def _req(authority="member", **kw):
    base = {"request_id": "r1", "authority": authority, "requester_ref": "a", "actor_authenticated": True,
            "target": "desktop"}
    base.update(kw)
    return sc.ArbiterRequest(**base)


def test_arbiter_request_tier_derived_from_authority():
    assert _req("member").tier == T.MEMBER and _req("system").tier == T.SYSTEM and _req("owner").tier == T.OWNER
    assert _req("system", emergency=True).tier == T.EMERGENCY
    assert _req("owner", emergency=True).tier == T.EMERGENCY  # 既有建構式仍可建；由仲裁器以 emergency_requires_system 拒絕
    r = _req("owner")
    assert r.instruction is None and r.human_origin is False


def test_arbiter_request_admin_uses_member_path():
    assert _req("member", tier=T.ADMIN).tier == T.ADMIN
    assert ar.priority_of(_req("member", tier=T.ADMIN)) == 75


@pytest.mark.parametrize("authority,tier", [
    ("owner", T.MEMBER), ("owner", T.SYSTEM), ("system", T.OWNER), ("system", T.EMERGENCY),
    ("member", T.OWNER), ("member", T.GUEST), ("member", T.UNVERIFIED), ("member", T.SYSTEM),
])
def test_arbiter_request_rejects_inconsistent_tier(authority, tier):
    with pytest.raises(ValueError):
        _req(authority, tier=tier)


def test_arbiter_request_emergency_tier_consistency():
    with pytest.raises(ValueError):
        _req("system", emergency=True, tier=T.SYSTEM)
    assert _req("system", emergency=True, tier=T.EMERGENCY).tier == T.EMERGENCY
    with pytest.raises(TypeError):
        _req("system", tier="system")  # 非 RequesterTier


def test_arbiter_request_field_validation():
    assert _req("owner", instruction=L.TEMP_SESSION, human_origin=True).instruction == L.TEMP_SESSION
    with pytest.raises(ValueError):
        _req("owner", instruction=90)
    with pytest.raises(ValueError):
        _req("owner", human_origin="yes")


def test_priority_of_goes_through_tier_without_changing_values():
    assert ar.priority_of(_req("member")) == 50 and ar.priority_of(_req("system")) == 100
    assert ar.priority_of(_req("owner")) == 200 and ar.priority_of(_req("system", emergency=True)) == 300
    assert ar.priority_of(_req("owner", emergency=True)) == 300  # 與改動前一致


# ---- 零依賴 ----

def test_permission_module_depends_on_nothing_in_uep():
    path = Path(perm.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tops = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.level == 0
            tops.add(node.module.split(".")[0])
    assert not tops & {"uep", "core", "modules", "utils", "devtools", "Entry", "echo_memory", "echo_stt", "echo_tts",
                       "echo_thought_core", "echo_stream", "llm_integration", "mcp_server"}


def test_replace_keeps_tier_consistent():
    r = _req("member", tier=T.ADMIN)
    assert replace(r, target="discord").tier == T.ADMIN
