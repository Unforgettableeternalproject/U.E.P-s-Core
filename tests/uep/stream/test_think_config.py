"""N3：Think adapter 的設定來源（UEP_LLM_*、舊名相容與警告一次、金鑰守門）；全程 fake，不打真實 API。"""
from __future__ import annotations

import inspect
import logging

import pytest

from uep import config as uep_config
from uep.config import UepSettings
from uep.stream.adapters import output_guard, think_config, think_llm
from uep.stream.adapters.think_config import ThinkConfig, ThinkConfigError
from uep.stream.adapters.think_llm import LLMThinkStage, build_backend
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import Utterance

LLM_ENV = (
    "UEP_LLM_PROVIDER", "UEP_LLM_MODEL", "UEP_LLM_API", "UEP_LLM_REASONING_EFFORT", "UEP_LLM_MAX_TOKENS",
    "UEP_LLM_TEMPERATURE", "UEP_LLM_API_KEY", "UEP_LLM_PROJECT", "UEP_LLM_ORGANIZATION", "UEP_LLM_BASE_URL",
    "UEP_LLM_PROMPT_CACHE_KEY", "UEP_LLM_OUTPUT_GUARD", "UEP_LLM_OUTPUT_BLOCKLIST_EXTRA", "UEP_INFER_LLM_PROVIDER",
    "UEP_INFER_LLM_MODEL", "ECHO_STREAM_LLM_PROVIDER", "ECHO_STREAM_LLM_MODEL", "ECHO_STREAM_LLM_API",
    "ECHO_STREAM_LLM_REASONING_EFFORT", "ECHO_STREAM_LLM_MAX_TOKENS", "ECHO_STREAM_LLM_TEMPERATURE",
    "ECHO_STREAM_LLM_PROMPT_CACHE_KEY", "ECHO_STREAM_OUTPUT_GUARD", "ECHO_STREAM_OUTPUT_BLOCKLIST_EXTRA",
    "OPENAI_API_KEY", "OPENAI_PROJECT", "OPENAI_ORG", "OPENAI_BASE_URL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in LLM_ENV:
        monkeypatch.delenv(name, raising=False)
    uep_config._LEGACY_WARNED.clear()


def settings() -> UepSettings:
    return UepSettings(_env_file=None)


def test_預設值與舊_ES_一致():
    cfg = settings().think_config()
    assert (cfg.provider, cfg.model, cfg.api, cfg.reasoning_effort, cfg.max_output_tokens) == (
        "openai", "gpt-5.6-luna", "chat", "high", 1024)
    assert cfg.api_key is None and cfg.output_guard_enabled and cfg.output_blocklist_extra == ()


def test_uep_llm_命名優先於舊名(monkeypatch):
    monkeypatch.setenv("UEP_LLM_MODEL", "new-model")
    monkeypatch.setenv("ECHO_STREAM_LLM_MODEL", "old-model")
    monkeypatch.setenv("UEP_LLM_API_KEY", "key-new")
    monkeypatch.setenv("OPENAI_API_KEY", "key-old")
    monkeypatch.setenv("UEP_LLM_OUTPUT_BLOCKLIST_EXTRA", "甲甲，乙乙")
    cfg = settings().think_config()
    assert cfg.model == "new-model" and cfg.api_key == "key-new" and cfg.output_blocklist_extra == ("甲甲", "乙乙")


def test_舊名仍可讀且每個舊名只警告一次(monkeypatch, caplog):
    monkeypatch.setenv("ECHO_STREAM_LLM_MODEL", "old-model")
    monkeypatch.setenv("ECHO_STREAM_LLM_MAX_TOKENS", "256")
    monkeypatch.setenv("OPENAI_API_KEY", "key-old")
    monkeypatch.setenv("ECHO_STREAM_OUTPUT_GUARD", "off")
    with caplog.at_level(logging.WARNING, logger="uep.config"):
        cfg = settings().think_config()
        settings().think_config()
    assert cfg.model == "old-model" and cfg.max_output_tokens == 256 and cfg.api_key == "key-old"
    assert cfg.output_guard_enabled is False
    msgs = [r.getMessage() for r in caplog.records]
    assert len([m for m in msgs if "OPENAI_API_KEY" in m]) == 1
    assert len([m for m in msgs if "ECHO_STREAM_LLM_MODEL" in m]) == 1
    assert all("key-old" not in m for m in msgs)


def test_infer_llm_命名仍生效(monkeypatch):
    monkeypatch.setenv("UEP_INFER_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("UEP_INFER_LLM_MODEL", "qwen")
    cfg = settings().think_config()
    assert (cfg.provider, cfg.model) == ("ollama", "qwen")


def test_金鑰不進_repr():
    assert "sk-secret" not in repr(ThinkConfig(api_key="sk-secret"))


def test_openai_缺金鑰明確報錯():
    with pytest.raises(ThinkConfigError, match="UEP_LLM_API_KEY"):
        build_backend(config=ThinkConfig())
    ThinkConfig(provider="ollama").require_credentials()  # 其他 provider 不在此守門


class _FakeRegistry:
    created: list[tuple[str, dict]] = []

    @classmethod
    def default(cls):
        return cls()

    def create(self, name, cfg):
        self.created.append((name, dict(cfg)))
        return "fake-backend"


def test_build_backend_只用注入設定(monkeypatch):
    from echo_thought_core.backends import registry

    _FakeRegistry.created.clear()
    monkeypatch.setattr(registry, "BackendRegistry", _FakeRegistry)
    cfg = ThinkConfig(api_key="k", model="m", temperature=0.3, base_url="http://x")
    assert build_backend(config=cfg, max_output_tokens=7) == "fake-backend"
    name, opts = _FakeRegistry.created[0]
    assert name == "openai" and opts["api_key"] == "k" and opts["model"] == "m" and opts["temperature"] == 0.3
    assert opts["max_output_tokens"] == 7 and opts["base_url"] == "http://x" and "project" not in opts


def test_think_部分不依賴過渡設定檔():
    for mod in (think_llm, output_guard, think_config):
        src = inspect.getsource(mod)
        assert "from . import config" not in src and "adapters.config" not in src


class _Backend:
    def __init__(self):
        self.requests = []

    async def stream_query(self, messages, system_instruction=None, **kwargs):
        self.requests.append(kwargs)
        for piece in ("你好呀，", "今天天氣不錯。"):
            yield piece


async def test_stage_以_config_決定快取鍵與防線():
    backend = _Backend()
    cfg = ThinkConfig(prompt_cache_key="core-voice", output_blocklist_extra=("天氣",))
    stage = LLMThinkStage(backend, system_prompt="人設", config=cfg)
    assert stage.prompt_cache_key == "core-voice"
    sentences = [s.text async for s in stage.stream(Utterance(text="嗨"), CancellationToken())]
    assert sentences and all("天氣" not in t for t in sentences)
