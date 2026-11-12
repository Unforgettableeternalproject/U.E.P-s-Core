"""Module Coordinator PR-2：描述子、登記表與 handle Protocol。離線，不需 DB。"""
from __future__ import annotations

import pytest

from tests.uep.module_fakes import FakeHandle, FakeObserver
from uep.lifecycle.descriptor import ModuleDescriptor, canonical_name
from uep.lifecycle.handles import ModuleHandle, ObservationOnlyError
from uep.lifecycle.registry import ModuleRegistry, ModuleRegistryError


def desc(module_id: str, *deps: str, **over) -> ModuleDescriptor:
    return ModuleDescriptor(module_id, depends_on=tuple(deps), **over)


def test_descriptor_defaults_and_validation():
    d = ModuleDescriptor("lore")
    assert (d.kind, d.autoload, d.needs_warm, d.blocked) == ("service", False, False, False)
    assert ModuleDescriptor("gpu:indextts").module_id == "gpu:indextts"
    for bad in ("", "Lore", "a:b:c", "gpu:", ":x", "1abc", "has space"):
        with pytest.raises(ValueError):
            ModuleDescriptor(bad)
    with pytest.raises(ValueError):
        ModuleDescriptor("lore", depends_on=("lore",))
    with pytest.raises(ValueError):
        ModuleDescriptor("lore", depends_on=("a", "a"))
    with pytest.raises(ValueError):
        ModuleDescriptor("lore", load_timeout_s=0)
    with pytest.raises(ValueError):
        ModuleDescriptor("lore", blocked_reason="Not Configured")
    assert ModuleDescriptor("lore", blocked_reason="not_configured").blocked


def test_registry_rejects_duplicate_and_same_resource_ids():
    reg = ModuleRegistry()
    reg.register(desc("lore"), FakeHandle())
    with pytest.raises(ModuleRegistryError):
        reg.register(desc("lore"), FakeHandle())
    reg.register(desc("gpu:indextts"), FakeHandle())
    with pytest.raises(ModuleRegistryError):  # R9：同一資源只一個 canonical id
        reg.register(desc("tts:indextts"), FakeHandle())
    with pytest.raises(ModuleRegistryError):
        reg.register(desc("indextts"), FakeHandle())
    assert canonical_name("gpu:indextts") == canonical_name("indextts") == "indextts"
    assert reg.known_ids() == {"lore", "gpu:indextts"} and "lore" in reg and len(reg) == 2


def test_registry_rejects_bad_handles():
    reg = ModuleRegistry()
    with pytest.raises(ModuleRegistryError):
        reg.register(desc("lore"), object())  # type: ignore[arg-type]


def test_validate_missing_dependency_and_cycle_regardless_of_order():
    reg = ModuleRegistry()
    reg.register(desc("a", "b"), FakeHandle())  # 依賴者先登記是合法的
    with pytest.raises(ModuleRegistryError, match="未登記"):
        reg.validate()
    reg.register(desc("b"), FakeHandle())
    assert reg.validate() == ["b", "a"]  # 拓撲序：依賴在前

    cyc = ModuleRegistry()
    for mid, dep in (("x", "y"), ("y", "z"), ("z", "x")):
        cyc.register(desc(mid, dep), FakeHandle())
    with pytest.raises(ModuleRegistryError, match="循環"):
        cyc.validate()


def test_observation_only_handle_forbids_load_warm_unload():
    obs = FakeObserver()
    assert isinstance(obs, ModuleHandle) and (obs.can_load, obs.can_unload) == (False, False)
    for method in (obs.load, obs.warm, obs.unload):
        with pytest.raises(ObservationOnlyError):
            method()
    assert obs.probe() == "ok"
