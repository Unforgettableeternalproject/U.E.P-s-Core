"""S 群模組 handle 與描述子預設表（PR-4 C1）：只驗設定、零網路、token 不外洩（離線，不需 DB）。"""
from __future__ import annotations

import socket
from typing import Any

import pytest
from pydantic import SecretStr

from tests.uep.module_fakes import FakeClock
from tests.uep.test_module_coordinator import Recorder, wait_until
from uep.config import UepSettings
from uep.lifecycle.coordinator import ModuleCoordinator
from uep.lifecycle.handles import ModuleHandle
from uep.tools.module_handles import (
    S_GROUP_MODULES,
    ModuleConfigError,
    build_s_group_registrations,
)
from uep.tools.web_image import image_read_available, web_search_available

SECRET = "tok-SECRET-123"
_ENV_KEYS = (
    "UEP_LLM_API_KEY", "OPENAI_API_KEY", "UEP_LORE_URL", "UEP_LORE_TOKEN", "UEP_CHATROOM_URL", "UEP_CHATROOM_SESSION_KEY",
    "UEP_CHATROOM_TOKEN", "UEP_CHATROOM_TOKEN_FILE", "UEP_CHATROOM_ROOMS", "UEP_WEB_SEARCH_PROVIDERS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("S 群 handle 不得打網路")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def boom_transport(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("transport 不得被呼叫")


def full_settings(**kw: Any) -> UepSettings:
    base: dict[str, Any] = {
        "lore_url": "http://lore.invalid", "lore_token": SecretStr(SECRET),
        "chatroom_url": "http://hub.invalid", "chatroom_session_key": "k", "chatroom_token": SecretStr(SECRET),
        "chatroom_rooms": "room-1", "web_search_providers": "duckduckgo", "web_fetch_enabled": True,
    }
    base.update(kw)
    return UepSettings(_env_file=None, **base)


def table(settings: UepSettings, **kw: Any) -> dict[str, tuple[Any, ModuleHandle]]:
    return {d.module_id: (d, h) for d, h in build_s_group_registrations(settings, **kw)}


def test_descriptor_defaults() -> None:
    regs = table(full_settings(), transport=boom_transport)
    assert set(regs) == set(S_GROUP_MODULES) == {"lore", "lore_delete", "chatroom", "web_search", "web_fetch", "image_read"}
    for module_id, (desc, handle) in regs.items():
        assert desc.kind == "service" and desc.autoload and not desc.needs_warm and desc.priority == "on_demand"
        assert not desc.uses_gpu and desc.vram_mb is None and not desc.blocked
        assert handle.can_load and handle.can_unload is False, module_id
    assert regs["lore_delete"][0].depends_on == ("lore",)
    assert all(not d.depends_on for m, (d, _) in regs.items() if m != "lore_delete")


def test_all_configured_probe_ok_and_load_ok(no_network: None) -> None:
    fake_vision = lambda timeout: object()  # noqa: E731  有視覺模型即算可用，不建立真 client
    for module_id, (_, handle) in table(full_settings(), transport=boom_transport, client_factory=fake_vision).items():
        handle.load()
        handle.warm()
        handle.unload()
        assert handle.probe() == "ok", module_id


def test_unconfigured_probe_dead_and_load_raises_short_code(no_network: None) -> None:
    settings = UepSettings(_env_file=None, web_search_providers="", web_search_openai_fallback=False, web_fetch_enabled=False,
                           lore_delete_enabled=False)
    for module_id, (_, handle) in table(settings, transport=boom_transport).items():
        assert handle.probe() == "dead", module_id
        with pytest.raises(ModuleConfigError) as info:
            handle.load()
        assert str(info.value) == "not_configured"


def test_partial_config_each_module_independent(no_network: None) -> None:
    fake_vision = lambda timeout: object()  # noqa: E731
    regs = table(full_settings(lore_url="", chatroom_rooms="", lore_delete_enabled=False, web_fetch_enabled=False),
                 client_factory=fake_vision)
    assert regs["lore"][1].probe() == "dead" and regs["chatroom"][1].probe() == "dead"
    assert regs["lore_delete"][1].probe() == "dead" and regs["web_fetch"][1].probe() == "dead"
    assert regs["web_search"][1].probe() == "ok" and regs["image_read"][1].probe() == "ok"


def test_health_callable_injected(no_network: None) -> None:
    calls: list[str] = []

    def fail() -> bool:
        calls.append("lore")
        return False

    def boom() -> bool:
        raise RuntimeError(SECRET)

    regs = table(full_settings(), health={"lore": fail, "chatroom": boom, "web_search": lambda: "degraded"})
    assert regs["lore"][1].probe() == "dead" and calls == ["lore"]
    assert regs["chatroom"][1].probe() == "dead"
    assert regs["web_search"][1].probe() == "degraded"
    assert table(full_settings(), health={"lore": lambda: "warming"})["lore"][1].probe() == "warming"
    assert regs["web_fetch"][1].probe() == "ok"  # 未注入 => 純設定判定
    with pytest.raises(ValueError):
        build_s_group_registrations(full_settings(), health={"nope": lambda: True})


def test_zero_network_even_with_exploding_transport(no_network: None) -> None:
    regs = table(full_settings(), transport=boom_transport, resolver=boom_transport)
    for _, handle in regs.values():
        handle.probe()
        if handle.probe() == "ok":
            handle.load()


def test_token_never_leaks(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    regs = table(full_settings())
    texts = [repr(h) for _, h in regs.values()] + [repr(d) for d, _ in regs.values()]
    closed = UepSettings(_env_file=None, web_search_providers="", web_search_openai_fallback=False, web_fetch_enabled=False,
                         lore_delete_enabled=False)
    for _, handle in table(closed).values():
        with pytest.raises(ModuleConfigError) as info:
            handle.load()
        texts += [str(info.value), repr(info.value)]

    def leaky() -> bool:
        raise RuntimeError(SECRET)

    table(full_settings(), health={"lore": leaky})["lore"][1].probe()
    texts.append(caplog.text)
    assert all(SECRET not in text for text in texts)


def test_shared_availability_functions_match_gate_behavior() -> None:
    off = UepSettings(_env_file=None, web_search_providers="", web_search_openai_fallback=False)
    assert web_search_available(off) is False and image_read_available(off) is False
    on = UepSettings(_env_file=None, web_search_providers="duckduckgo", llm_api_key=SecretStr(SECRET))
    assert web_search_available(on) is True and image_read_available(on) is True
    assert image_read_available(off, vision=object()) is True  # type: ignore[arg-type]  注入的視覺模型優先


def test_coordinator_runs_s_group_without_unload() -> None:
    rec = Recorder()
    mc = ModuleCoordinator(rec, epoch_source=lambda: 1, clock=FakeClock(), run_scheduler=False, resync_interval_s=0)
    for desc, handle in build_s_group_registrations(full_settings(), client_factory=lambda timeout: object()):
        mc.register(desc, handle)
    try:
        mc.start()
        wait_until(lambda: all(s.state == "ready" for s in mc.snapshot().values()))
        mc.request_unload("lore")
        assert mc.status("lore").state == "ready"  # can_unload=False：不會被卸載
    finally:
        mc.stop()
