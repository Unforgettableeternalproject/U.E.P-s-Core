"""DecisionPlan.policy（僅記錄區段）：往返、白名單、engine 填值，以及「P2 之前不得新增消費者」的 AST 鎖。

AST 鎖判準（``_violations``，對單一檔案原始碼回傳違規描述）：
(i) 名稱 ``DecisionPolicy``（Name／Attribute／import 別名）只能出現在 contracts.py、policy_publish.py、decision/__init__.py；
(ii) 讀取 plan 的 policy 區段只能出現在 contracts.py、policy_publish.py、hub.py（hub 僅做 plan_sections 白名單檢查）。判為「讀 plan.policy」的型態：
    - ``<expr>.plan.policy``、``<expr>.advice.policy``（屬性鏈上一層名為 plan／advice）；
    - 名稱為 ``plan``／``advice``／``decision_plan``／``*_plan`` 的變數取 ``.policy``；
    - 以字串 "policy" 對 ``plan_to_dict(...)`` 或名稱含 plan 的變數取鍵（下標或 ``.get``）；
    - ``DecisionPlan(policy=...)`` 關鍵字建構（寫入也算新增耦合）。
擋得住：上述型態與 DecisionPolicy 的 import／使用。擋不住：把 plan 先賦值給其他名稱再讀（``p = x.plan; p.policy``）、
getattr(plan, "policy") 以變數組字串、經由 dataclasses.asdict／fields 反射、以別名接收的 dict 取鍵。
這些屬刻意迴避，由審查負責；host.policy 等其他物件的 ``.policy`` 因接收者不是 plan 而不誤報。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from uep.decision import (
    DecisionHub,
    DecisionPlan,
    DecisionPointSpec,
    DecisionRequest,
    DecisionResult,
)
from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionFollowup,
    DecisionLoop,
    DecisionOutput,
    DecisionPolicy,
    DecisionReply,
    plan_from_dict,
    plan_to_dict,
)
from uep.decision.hub import REASON_PLAN_SECTION_REJECTED
from uep.decision.policy_publish import (
    KIND_CHANNEL_MODE,
    KIND_POLICY_SNAPSHOT,
    ChannelModeEngine,
    PolicySnapshotEngine,
    channel_mode_spec,
    policy_snapshot_spec,
)

# ---- 往返 ----


def test_policy_roundtrip():
    plan = DecisionPlan(policy=DecisionPolicy(channel_mode="active", snapshot_version=3))
    data = plan_to_dict(plan)
    assert data == {"policy": {"channel_mode": "active", "snapshot_version": 3}}
    assert plan_from_dict(json.loads(json.dumps(data))) == plan


def test_policy_none_not_emitted_and_old_dict_gives_none():
    assert "policy" not in plan_to_dict(DecisionPlan(route="ignore"))
    legacy = {"route": "direct_chat", "reply": {"mode": "respond", "interaction_to": "u1", "addressed": True}}
    plan = plan_from_dict(legacy)
    assert plan.policy is None
    assert plan_to_dict(plan) == legacy
    assert plan_from_dict({}).policy is None


def test_policy_does_not_disturb_other_sections():
    plan = DecisionPlan(
        route="direct_chat", reply=DecisionReply(mode="respond"), output=DecisionOutput(modalities=("text",)),
        loop=DecisionLoop(outcome="finalize"), followups=(DecisionFollowup(kind="completion_report"),),
        policy=DecisionPolicy(channel_mode="passive", snapshot_version=1),
    )
    assert plan_from_dict(plan_to_dict(plan)) == plan
    assert set(plan_to_dict(plan)) == {"route", "reply", "output", "loop", "followups", "policy"}


def test_policy_unknown_keys_ignored():
    assert plan_from_dict({"policy": {"channel_mode": "active", "x": 1}}).policy == DecisionPolicy(channel_mode="active")


# ---- engine 填值 ----

def _facts(**over):
    base = {
        "version": 4, "prev_version": 3, "platform": "web", "cause": "channel_mode", "ttl_s": None, "es_version": 4, "core_version": 4,
        "channel_mode": "passive", "prev_channel_mode": "active", "core_channel_mode": "passive",
    }
    return {**base, **over}


def _req(kind, **over):
    return DecisionRequest(request_id="r1", kind=kind, trigger="event", facts=_facts(**over))


def test_engines_fill_policy():
    snap = PolicySnapshotEngine().decide(_req(KIND_POLICY_SNAPSHOT))
    assert snap.plan.policy == DecisionPolicy(channel_mode="passive", snapshot_version=4)
    mode = ChannelModeEngine().decide(_req(KIND_CHANNEL_MODE))
    assert mode.plan.policy == DecisionPolicy(channel_mode="passive", snapshot_version=4)
    none_mode = PolicySnapshotEngine().decide(
        _req(KIND_POLICY_SNAPSHOT, channel_mode=None, core_channel_mode=None, prev_channel_mode=None)
    )
    assert none_mode.plan.policy == DecisionPolicy(channel_mode=None, snapshot_version=4)


def test_invalid_paths_keep_empty_plan():
    r = PolicySnapshotEngine().decide(_req(KIND_POLICY_SNAPSHOT, version=True))
    assert r.fallback and r.plan == DecisionPlan()
    r = ChannelModeEngine().decide(_req(KIND_CHANNEL_MODE, channel_mode=None, core_channel_mode=None))
    assert r.fallback and r.plan == DecisionPlan()


# ---- 白名單 ----

def test_spec_declares_policy_section_only_for_two_kinds():
    assert policy_snapshot_spec().plan_sections == ("policy",)
    assert channel_mode_spec().plan_sections == ("policy",)
    assert DecisionPointSpec(kind="x").plan_sections == ()


class _PolicyEngine:
    def decide(self, request):
        return DecisionResult(
            decision_id="d", request_id=request.request_id, kind=request.kind, outcome="o",
            plan=DecisionPlan(policy=DecisionPolicy(channel_mode="active")), confidence=1.0, reason_code="r",
            engine=DecisionEngineInfo(tier="rule", id="t", version="1"),
        )


def test_non_whitelisted_kind_with_policy_is_stripped_to_fallback():
    hub = DecisionHub()
    seen = []
    hub.add_observer(lambda req, res, spec, ms: seen.append(res))
    hub.register(
        DecisionPointSpec(kind="other.kind", mode="sync", outcomes=("o",), on_invalid_reason="bad_section"),
        _PolicyEngine(),
    )
    result = hub.decide("other.kind", DecisionRequest(request_id="r", kind="other.kind"))  # 不拋例外
    assert result.fallback and result.reason_code == "bad_section" and result.outcome == "o"
    assert result.plan.policy is None
    assert seen == [result]  # 觀察者看得到這筆 fallback
    hub.register(DecisionPointSpec(kind="bare.kind", mode="sync", outcomes=("o",)), _PolicyEngine())
    bare = hub.decide("bare.kind", DecisionRequest(request_id="r", kind="bare.kind"))
    assert bare.fallback and bare.reason_code == REASON_PLAN_SECTION_REJECTED and bare.plan.policy is None
    hub.register(
        DecisionPointSpec(kind="ok.kind", mode="sync", outcomes=("o",), plan_sections=("policy",)), _PolicyEngine()
    )
    assert hub.decide("ok.kind", DecisionRequest(request_id="r", kind="ok.kind")).plan.policy.channel_mode == "active"


# ---- AST 消費者鎖 ----

_UEP = Path(__file__).resolve().parents[2] / "uep"
_TYPE_ALLOWED = {"decision/contracts.py", "decision/policy_publish.py", "decision/__init__.py"}
_READ_ALLOWED = {"decision/contracts.py", "decision/policy_publish.py", "decision/hub.py"}  # hub 只做白名單檢查
_PLAN_NAMES = {"plan", "advice", "decision_plan"}


def _is_plan_expr(node: ast.AST) -> bool:
    """節點是否像 plan 物件：名稱為 plan 類，或屬性鏈末端名為 plan／advice。"""
    if isinstance(node, ast.Name):
        return node.id in _PLAN_NAMES or node.id.endswith("_plan")
    if isinstance(node, ast.Attribute):
        return node.attr in _PLAN_NAMES
    return False


def _is_plan_dict_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        return name == "plan_to_dict"
    return isinstance(node, ast.Name) and "plan" in node.id


def _is_policy_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "policy"


def _violations(source: str, *, may_name_type: bool, may_read: bool) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        if not may_name_type:
            if isinstance(node, ast.Name) and node.id == "DecisionPolicy" or isinstance(node, ast.Attribute) and node.attr == "DecisionPolicy":
                out.append(f"{line}: DecisionPolicy")
            elif isinstance(node, ast.alias) and node.name.split(".")[-1] == "DecisionPolicy":
                out.append(f"{line}: import DecisionPolicy")
        if may_read:
            continue
        if isinstance(node, ast.Attribute) and node.attr == "policy" and _is_plan_expr(node.value):
            out.append(f"{line}: plan.policy")
        elif isinstance(node, ast.Subscript) and _is_policy_const(node.slice) and _is_plan_dict_expr(node.value):
            out.append(f"{line}: plan dict['policy']")
        elif (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and _is_policy_const(node.args[0]) and _is_plan_dict_expr(node.func.value)
        ):
            out.append(f"{line}: plan dict.get('policy')")
        elif isinstance(node, ast.Call) and any(k.arg == "policy" for k in node.keywords):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "DecisionPlan":
                out.append(f"{line}: DecisionPlan(policy=...)")
    return out


def test_ast_lock_catches_violations():
    bad = (
        "from uep.decision.contracts import DecisionPolicy\n"
        "def f(result, plan):\n"
        "    a = result.plan.policy\n"
        "    b = plan.policy\n"
        "    c = plan_to_dict(plan)['policy']\n"
        "    d = plan_to_dict(plan).get('policy')\n"
        "    e = DecisionPlan(policy=None)\n"
    )
    found = _violations(bad, may_name_type=False, may_read=False)
    for tag in ("import DecisionPolicy", "plan.policy", "plan dict['policy']", "plan dict.get('policy')", "DecisionPlan(policy"):
        assert any(tag in v for v in found), (tag, found)
    assert len(found) == 6, found


def test_ast_lock_ignores_unrelated_policy_attrs():
    ok = "def f(host, source):\n    return host.policy, source.policy.current(), {'policy': 1}['policy']\n"
    assert _violations(ok, may_name_type=False, may_read=False) == []


def test_no_new_policy_section_consumers_in_uep():
    problems: list[str] = []
    for path in sorted(_UEP.rglob("*.py")):
        rel = path.relative_to(_UEP).as_posix()
        found = _violations(
            path.read_text(encoding="utf-8"), may_name_type=rel in _TYPE_ALLOWED, may_read=rel in _READ_ALLOWED
        )
        problems.extend(f"{rel}:{v}" for v in found)
    assert problems == [], "P2 之前不得新增 DecisionPlan.policy 的消費者：" + "; ".join(problems)
