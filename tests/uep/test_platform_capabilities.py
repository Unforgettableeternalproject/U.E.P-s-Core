"""平台端點契約 E1：能力宣告與缺席端輸入的純資料型別（零行為變化）。

型別目前沒有任何 production 消費者；鎖測試保證它們只存在於 ``uep/contracts/platform.py``，
未被其他 production 程式 import（接線是 E5 之後的事）。
"""
from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from uep.contracts.platform import (
    AbsentInboundItem,
    InteractionCapability,
    MonitorCapability,
    MonitorSurface,
    PlatformCapabilities,
    PresenceRequest,
)

_UEP = Path(__file__).resolve().parents[2] / "uep"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _surface(**kw) -> MonitorSurface:
    base = {"surface_ref": "discord:chan_x", "audience_cap_claimed": "public", "operator_auth": "platform_identity"}
    return MonitorSurface(**{**base, **kw})


def _item(**kw) -> AbsentInboundItem:
    base = {
        "source_platform": "discord", "surface_ref": "discord:chan_x", "actor_ref": "discord:123",
        "actor_authenticated": True, "is_owner": True, "raw_ref": "blob:abc123", "length": 12, "received_at": NOW,
    }
    return AbsentInboundItem(**{**base, **kw})


def _request(**kw) -> PresenceRequest:
    base = {
        "request_id": "discord:chan_x:m1", "source_platform": "discord", "requester_ref": "discord:123",
        "actor_authenticated": True, "authority": "owner", "priority": 10, "target": "desktop",
    }
    return PresenceRequest(**{**base, **kw})


# ---- 能力宣告 ----

def test_capabilities_two_facets_declared_separately():
    caps = PlatformCapabilities(
        platform="discord", endpoint_instance_id="inst-1", protocol_version=1,
        monitor=MonitorCapability(frozenset({"status_view", "instruction_inbox"}), (_surface(),)),
        interaction=InteractionCapability(frozenset({"text", "audio_in"})),
    )
    assert caps.monitor is not None and caps.interaction is not None
    # 只填 monitor 的純 Web 管理頁、只填 interaction 的端點都合法
    PlatformCapabilities("website", "web-1", 1, monitor=MonitorCapability(frozenset({"status_view"})))
    PlatformCapabilities("desktop", "d-1", 1, interaction=InteractionCapability(frozenset({"text"})))
    with pytest.raises(FrozenInstanceError):
        caps.platform = "x"  # type: ignore[misc]


def test_capabilities_require_at_least_one_facet_surface():
    with pytest.raises(ValueError):
        PlatformCapabilities("discord", "inst-1", 1)


@pytest.mark.parametrize(
    "kw",
    [
        {"platform": "Discord"}, {"platform": ""}, {"endpoint_instance_id": "bad id"},
        {"protocol_version": 0}, {"protocol_version": True}, {"declaration_revision": -1},
    ],
)
def test_capabilities_field_validation(kw):
    base = {
        "platform": "discord", "endpoint_instance_id": "inst-1", "protocol_version": 1,
        "interaction": InteractionCapability(frozenset({"text"})),
    }
    with pytest.raises(ValueError):
        PlatformCapabilities(**{**base, **kw})


def test_unknown_or_empty_facets_rejected_fail_closed():
    with pytest.raises(ValueError):
        MonitorCapability(frozenset({"status_view", "telepathy"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MonitorCapability(frozenset())
    with pytest.raises(ValueError):
        InteractionCapability(frozenset({"text", "teleport"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        InteractionCapability(frozenset())


def test_instruction_inbox_needs_a_surface_that_can_identify_operator():
    with pytest.raises(ValueError):
        MonitorCapability(frozenset({"instruction_inbox"}))
    with pytest.raises(ValueError):
        MonitorCapability(frozenset({"instruction_inbox"}), (_surface(operator_auth="none"),))
    MonitorCapability(frozenset({"instruction_inbox"}), (_surface(operator_auth="owner_channel"),))


def test_surface_validation_and_duplicates():
    for kw in ({"surface_ref": "Web Admin"}, {"audience_cap_claimed": "secret"}, {"operator_auth": "root"}):
        with pytest.raises(ValueError):
            _surface(**kw)
    with pytest.raises(ValueError):
        MonitorCapability(frozenset({"status_view"}), (_surface(), _surface()))


# ---- AbsentInboundItem ----

def test_absent_inbound_item_valid_and_frozen():
    item = _item()
    assert item.is_owner and item.actor_authenticated and item.length == 12
    with pytest.raises(FrozenInstanceError):
        item.length = 1  # type: ignore[misc]


def test_absent_inbound_item_cannot_carry_raw_text():
    # 原文（含空白、中文、大寫）塞不進 raw_ref；型別也沒有任何原文欄位
    for raw in ("請移到 desktop", "please move to desktop", "Blob:ABC", ""):
        with pytest.raises(ValueError):
            _item(raw_ref=raw)
    assert not {f for f in AbsentInboundItem.__dataclass_fields__} & {"text", "content", "body", "raw"}


def test_absent_inbound_item_rejects_display_name_as_actor():
    for actor in ("Bernie", "艾斯維爾", "bernie the owner", ""):
        with pytest.raises(ValueError):
            _item(actor_ref=actor, actor_authenticated=False, is_owner=False)


def test_unauthenticated_actor_is_never_owner():
    with pytest.raises(ValueError):
        _item(actor_authenticated=False, is_owner=True)
    ok = _item(actor_authenticated=False, is_owner=False)
    assert not ok.is_owner


def test_authenticated_actor_must_belong_to_source_namespace():
    with pytest.raises(ValueError):
        _item(source_platform="discord", actor_ref="desktop:owner")
    # 未認證者可以是任意（真實）ID，shadow 紀錄仍存其 ID
    _item(source_platform="discord", actor_ref="desktop:owner", actor_authenticated=False, is_owner=False)
    # owner 後台是獨立來源（Q5 A）
    _item(source_platform="owner_console", surface_ref="web:admin", actor_ref="owner_console:owner")


@pytest.mark.parametrize(
    "kw",
    [
        {"length": -1}, {"length": True}, {"received_at": datetime(2026, 11, 10)}, {"received_at": "2026-11-10"},
        {"actor_authenticated": 1}, {"is_owner": "yes"}, {"source_platform": "Discord"}, {"surface_ref": "a b"},
    ],
)
def test_absent_inbound_item_field_validation(kw):
    with pytest.raises(ValueError):
        _item(**kw)


# ---- PresenceRequest ----

def test_presence_request_valid_owner_and_system():
    req = _request()
    assert req.authority == "owner" and req.target == "desktop" and req.reason_ref is None
    _request(target="host", reason_ref="blob:why1")
    _request(source_platform="owner_console", requester_ref="owner_console:owner")
    _request(authority="system", requester_ref="system:scheduler", priority=100)
    with pytest.raises(FrozenInstanceError):
        req.priority = 0  # type: ignore[misc]


def test_presence_request_only_from_authenticated_actor():
    for value in (False, None, 1, "true"):
        with pytest.raises(ValueError):
            _request(actor_authenticated=value)


@pytest.mark.parametrize("authority", ["member", "guest", "admin", "", None])
def test_presence_request_first_phase_only_owner_or_system(authority):
    with pytest.raises(ValueError):
        _request(authority=authority)


def test_presence_request_owner_cannot_claim_other_namespace():
    with pytest.raises(ValueError):
        _request(source_platform="discord", requester_ref="desktop:owner")


@pytest.mark.parametrize(
    "kw",
    [
        {"request_id": ""}, {"request_id": "has space"}, {"requester_ref": "Bernie"}, {"priority": -1},
        {"priority": True}, {"target": "Desktop"}, {"target": ""}, {"reason_ref": "請移到 desktop"},
    ],
)
def test_presence_request_field_validation(kw):
    with pytest.raises(ValueError):
        _request(**kw)


# ---- 無消費者鎖（比照 test_decision_capability 的做法）----

_NEW_NAMES = {
    "PlatformCapabilities", "MonitorCapability", "MonitorSurface", "InteractionCapability",
    "AbsentInboundItem", "PresenceRequest", "SOURCE_OWNER_CONSOLE", "PRESENCE_REQUEST_AUTHORITIES",
}


_GATE_MODULE = "presence/gate.py"
_GATE_ALLOWED = {"PlatformCapabilities", "InteractionCapability"}
# 組裝層登記出站端點的 interaction 宣告（E3 resolver 接線）；只可引用這兩個型別
_ASSEMBLY_MODULE = "assembly.py"


def _referenced_names(path, names):
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
        if name in names:
            found.add(name)
        elif isinstance(node, ast.ImportFrom) and node.module == "uep.contracts.platform":
            found |= {a.name for a in node.names if a.name in names}
    return found


def test_capability_types_have_no_production_consumer():
    """除定義檔 ``contracts/platform.py`` 外，``uep/`` 內只有 E3 的 Output Gate（``presence/gate.py``）可以引用
    ``PlatformCapabilities``／``InteractionCapability``；其他型別與其他模組仍無消費者。
    E5 之後有更多消費者時，這個測試要連同契約一起改（刻意讓接線需要明示）。"""
    offenders: list[str] = []
    for path in sorted(_UEP.rglob("*.py")):
        rel = path.relative_to(_UEP).as_posix()
        if rel == "contracts/platform.py":
            continue
        if rel in (_GATE_MODULE, _ASSEMBLY_MODULE):
            extra = _referenced_names(path, _NEW_NAMES) - _GATE_ALLOWED
            offenders += [f"{rel} {n}" for n in sorted(extra)]
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if name in _NEW_NAMES:
                offenders.append(f"{rel}:{getattr(node, 'lineno', 0)} {name}")
            elif isinstance(node, ast.ImportFrom) and node.module == "uep.contracts.platform":
                offenders += [f"{rel}:{node.lineno} import {a.name}" for a in node.names if a.name in _NEW_NAMES]
    assert offenders == []


def test_output_gate_really_consumes_capability_types():
    """反向斷言：Gate 真的引用 PlatformCapabilities（E3 前置檢查），放寬不是空白支票。"""
    refs = _referenced_names(_UEP / "presence" / "gate.py", _NEW_NAMES)
    assert "PlatformCapabilities" in refs and refs <= _GATE_ALLOWED


def test_platform_contract_module_stays_a_leaf():
    """platform.py 只放純資料：不 import presence／tools／decision（依賴單向）。"""
    tree = ast.parse((_UEP / "contracts" / "platform.py").read_text(encoding="utf-8"))
    mods = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("uep")
    }
    assert mods <= {"uep.contracts.presence"}, sorted(mods)
