"""PR-4 C2：觀測模組的 warming 語意（coordinator）與 M 群唯讀 handle（離線，不需 DB、不碰 GPU）。"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.uep.module_fakes import FakeHandle, FakeObserver
from tests.uep.test_module_coordinator import Rig, wait_until
from uep.echo.module_handles import (
    MODULE_LAYA,
    MODULE_NLP,
    MODULE_STT,
    MODULE_THINK,
    MODULE_TRANSLATE,
    MODULE_TTS,
    build_m_group_registrations,
)
from uep.lifecycle.coordinator import WARMING_POLL_S
from uep.lifecycle.handles import ObservationOnlyError


def settle(rig: Rig, module_id: str, state: str) -> None:
    rig.until(module_id, state)


def poll(rig: Rig, seconds: float = WARMING_POLL_S + 0.1) -> None:
    rig.clock.advance(seconds)
    rig.mc.tick()


def test_observer_warming_then_ready_without_burning_retries() -> None:
    rig = Rig()
    obs = rig.add("model:x", handle=FakeObserver("warming"), kind="model", pinned=True)
    rig.mc.start()
    try:
        settle(rig, "model:x", "warming")
        assert rig.rec.states("model:x")[-2:] == ["loading", "warming"] or "warming" in rig.rec.states("model:x")
        for _ in range(5):  # 持續 warming 多輪：不失敗、不累計失敗次數、不耗盡
            before = obs.probes
            poll(rig)
            wait_until(lambda b=before: obs.probes > b)
            assert rig.state("model:x") == "warming"
        st = rig.mc.status("model:x")
        assert st.state == "warming" and "failed" not in rig.rec.states("model:x")
        obs.result = "ok"
        poll(rig)
        settle(rig, "model:x", "ready")
        assert rig.rec.states("model:x")[-1] == "ready"
    finally:
        rig.mc.stop()


def test_observer_warming_then_degraded_cpu_fallback() -> None:
    rig = Rig()
    obs = rig.add("model:x", handle=FakeObserver("warming"), kind="model", pinned=True)
    rig.mc.start()
    try:
        settle(rig, "model:x", "warming")
        obs.result = "degraded"
        poll(rig)
        settle(rig, "model:x", "degraded")
        assert rig.rec.of("model:x")[-1]["reason_code"] == "probe_degraded"
        assert rig.rec.of("model:x")[-1]["readiness"] == "ready"
    finally:
        rig.mc.stop()


def test_observer_warming_then_dead_fails_after_threshold() -> None:
    rig = Rig()
    obs = rig.add("model:x", handle=FakeObserver("warming"), kind="model", pinned=True)
    rig.mc.start()
    try:
        settle(rig, "model:x", "warming")
        obs.result = "dead"
        for _ in range(3):
            before = obs.probes
            poll(rig)
            wait_until(lambda b=before: obs.probes > b)
            wait_until(lambda: not rig.mc._recs["model:x"].in_flight)  # noqa: SLF001  等這次探測結算
        settle(rig, "model:x", "failed")
    finally:
        rig.mc.stop()


def test_observer_dead_at_first_probe_is_failed_not_warming() -> None:
    rig = Rig()
    rig.add("model:x", handle=FakeObserver("dead"), kind="model", pinned=True)
    rig.mc.start()
    try:
        settle(rig, "model:x", "failed")
        assert "warming" not in rig.rec.states("model:x")
    finally:
        rig.mc.stop()


def test_ready_observer_reporting_warming_stays_ready() -> None:
    rig = Rig(probe_interval_s=10.0)
    obs = rig.add("model:x", handle=FakeObserver("ok"), kind="model", pinned=True)
    rig.mc.start()
    try:
        settle(rig, "model:x", "ready")
        obs.result = "warming"
        poll(rig, 10.5)
        wait_until(lambda: obs.probes >= 2)
        assert rig.state("model:x") == "ready"
    finally:
        rig.mc.stop()


def test_warming_probe_on_loadable_handle_counts_as_ok() -> None:
    rig = Rig(probe_interval_s=10.0)
    handle = rig.add("svc", handle=FakeHandle(), autoload=True)
    rig.mc.start()
    try:
        settle(rig, "svc", "ready")
        handle.probe_result = "warming"
        poll(rig, 10.5)
        wait_until(lambda: handle.calls.count("probe") >= 1)
        assert rig.state("svc") == "ready"
    finally:
        rig.mc.stop()


def test_stop_while_observer_warming_unloads_cleanly() -> None:
    rig = Rig()
    obs = rig.add("model:x", handle=FakeObserver("warming"), kind="model", pinned=True)
    rig.mc.start()
    settle(rig, "model:x", "warming")
    rig.mc.stop()
    assert rig.state("model:x") == "unloaded" and obs.probes >= 1


# --- M 群 handle ---


def readiness(**flags: Any) -> SimpleNamespace:
    return SimpleNamespace(ready=flags.get("ready"))


def holder(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def test_descriptors_are_pinned_observed_and_unmanaged() -> None:
    inference = holder(readiness={"input": readiness(), "speak": readiness(), "think": readiness()})
    regs = build_m_group_registrations(
        inference=inference, laya=holder(loaded=False), nlp=holder(loaded=False),
        translator=holder(available=False),
    )
    ids = {d.module_id for d, _ in regs}
    assert ids == {MODULE_STT, MODULE_TTS, MODULE_THINK, MODULE_LAYA, MODULE_NLP, MODULE_TRANSLATE}
    for desc, handle in regs:
        assert desc.pinned and desc.vram_mb is None and desc.kind in ("model", "stage")
        assert handle.can_load is False and handle.can_unload is False
        for method in (handle.load, handle.warm, handle.unload):
            with pytest.raises(ObservationOnlyError):
                method()
    assert build_m_group_registrations() == []  # 沒給持有者就不登記（embedding 本版不登記）


def test_stage_readiness_mapping() -> None:
    r = {"input": readiness(), "speak": readiness(ready=True), "think": readiness(ready=False)}
    handles = {d.module_id: h for d, h in build_m_group_registrations(inference=holder(readiness=r))}
    assert handles[MODULE_STT].probe() == "warming"  # ready is None
    assert handles[MODULE_TTS].probe() == "ok"
    assert handles[MODULE_THINK].probe() == "dead"
    r["input"].ready = True
    assert handles[MODULE_STT].probe() == "ok"


def test_laya_nlp_loading_vs_failed_vs_loaded() -> None:
    laya = holder(loaded=False, load_error=None)
    nlp = holder(loaded=False)  # 沒有 load_error 屬性也不爆：視為載入中
    handles = {d.module_id: h for d, h in build_m_group_registrations(laya=laya, nlp=nlp)}
    assert handles[MODULE_LAYA].probe() == "warming" and handles[MODULE_NLP].probe() == "warming"
    laya.load_error = "RuntimeError"
    assert handles[MODULE_LAYA].probe() == "dead"
    laya.loaded, laya.load_error = True, None
    assert handles[MODULE_LAYA].probe() == "ok"


def test_translator_cpu_fallback_is_degraded() -> None:
    translator = holder(available=False, load_error=None, cpu_fallback_used=False)
    handle = build_m_group_registrations(translator=translator)[0][1]
    assert handle.probe() == "warming"
    translator.available, translator.cpu_fallback_used = True, True
    assert handle.probe() == "degraded"
    translator.cpu_fallback_used = False
    assert handle.probe() == "ok"
    translator.available, translator.load_error = False, "RuntimeError"
    assert handle.probe() == "dead"


def test_real_wiring_classes_expose_load_error() -> None:
    from uep.echo.laya_wiring import LayaProposer
    from uep.echo.nlp_wiring import NlpLegacyProposer
    from uep.echo.translate_wiring import BoundaryTranslator

    for cls in (LayaProposer, NlpLegacyProposer, BoundaryTranslator):
        assert isinstance(getattr(cls, "load_error"), property)
    assert isinstance(BoundaryTranslator.cpu_fallback_used, property)
