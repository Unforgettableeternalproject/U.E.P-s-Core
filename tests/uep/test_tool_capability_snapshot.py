"""ToolRegistry.capability_snapshot：決策層讀取的能力快照（標籤 readiness、side_effect_class、版本）。"""

from __future__ import annotations

from tests.uep.test_tool_registry import make_entry
from uep.tools.registry import CapabilityStatus, ToolRegistry

MOD_A = "svc:a"
MOD_B = "svc:b"


def _event(registry: ToolRegistry, module: str, revision: int, readiness: str, **extra) -> None:
    payload = {"module_id": module, "revision": revision, "readiness": readiness, **extra}
    assert registry.apply_module_event("module.state_changed", payload)


def _registry() -> ToolRegistry:
    return ToolRegistry.build(
        [
            make_entry("read.a", spec={"capability": "memory.read"}, requires=(MOD_A,)),
            make_entry("read.b", spec={"capability": "memory.read"}, requires=(MOD_B,)),
            make_entry("write.a", spec={"capability": "memory.write", "side_effect_class": "reversible_local"},
                       requires=(MOD_A,)),
            make_entry("write.free", spec={"capability": "memory.write"}),
            make_entry("web.off", spec={"capability": "web.fetch"}, disabled=True),
        ],
        known_modules=[MOD_A, MOD_B],
    )


def _by_tag(registry: ToolRegistry) -> dict[str, CapabilityStatus]:
    return {s.tag: s for s in registry.capability_snapshot().tags}


def test_snapshot_lists_enabled_tags_sorted_and_skips_disabled():
    snap = _registry().capability_snapshot()
    assert snap.revision == 0
    assert [s.tag for s in snap.tags] == ["memory.read", "memory.write"]  # 停用的 web.fetch 不列
    assert all(s.readiness == "ready" for s in snap.tags)  # 首事件前、未宣告 warm_required 視為 ready


def test_readiness_takes_best_tool_and_class_follows_that_readiness():
    registry = _registry()
    _event(registry, MOD_A, 1, "ready")
    # memory.write：write.a（reversible_local, ready）與 write.free（internal_read, ready）同為 ready ⇒ 取最高一級
    assert _by_tag(registry)["memory.write"].side_effect_class == "reversible_local"
    _event(registry, MOD_A, 2, "unavailable", state="unloaded", reason_code="not_loaded")
    tags = _by_tag(registry)
    # write.a 不可用 ⇒ 只剩 write.free 達到 ready，類別隨之降為 internal_read
    assert tags["memory.write"] == CapabilityStatus("memory.write", "ready", "internal_read")
    # read.a 不可用，read.b 仍 ready ⇒ 標籤取最好的 ready
    assert tags["memory.read"].readiness == "ready"


def test_warming_and_unavailable_are_distinguished_and_revision_tracks_events():
    registry = _registry()
    _event(registry, MOD_A, 1, "warming", state="loading")
    _event(registry, MOD_B, 1, "unavailable", state="failed", reason_code="probe_dead")
    snap = registry.capability_snapshot()
    tags = {s.tag: s for s in snap.tags}
    assert tags["memory.read"].readiness == "warming"  # 最好的只到 warming，決策層可據此排除
    assert snap.revision == 2
    _event(registry, MOD_A, 2, "unavailable", state="failed", reason_code="probe_dead")
    tags = _by_tag(registry)
    assert tags["memory.read"].readiness == "unavailable"  # unavailable 的標籤也列出，不消失
    assert "memory.read" not in registry.enabled_tags()


def test_snapshot_matches_enabled_tags_scope():
    registry = _registry()
    _event(registry, MOD_A, 1, "unavailable", state="unloaded", reason_code="not_loaded")
    _event(registry, MOD_B, 1, "unavailable", state="unloaded", reason_code="not_loaded")
    available = {s.tag for s in registry.capability_snapshot().tags if s.readiness != "unavailable"}
    assert available == set(registry.enabled_tags())
