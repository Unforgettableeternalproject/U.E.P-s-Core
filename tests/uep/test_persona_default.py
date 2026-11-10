"""預設人設：UEP_INFER_SYSTEM_PROMPT 留空用 repo 內人設檔，有值以 env 為準。"""
from __future__ import annotations

from uep.config import UepSettings
from uep.persona import default_system_prompt, load_persona


def test_人設檔載入_含價值觀指引且不含舊參數():
    data = load_persona()
    assert {"base_personality", "chat_mode", "work_mode", "system_values_guide"} <= set(data)
    assert set(data["system_values_guide"]) == {"mood_delta", "pride_delta", "helpfulness_delta", "boredom_delta"}
    assert not {"model", "temperature", "safety_settings"} & set(data)


def test_預設_prompt_由人設組成且無佔位():
    prompt = default_system_prompt()
    assert prompt.startswith("You're U.E.P. (Ultra Ethereal Presence)")
    assert "{system_values}" not in prompt
    assert "Just chat naturally" in prompt


def test_env_未設或留空_用人設檔(monkeypatch):
    monkeypatch.delenv("UEP_INFER_SYSTEM_PROMPT", raising=False)
    assert UepSettings(_env_file=None).infer_system_prompt == default_system_prompt()
    monkeypatch.setenv("UEP_INFER_SYSTEM_PROMPT", "  ")
    assert UepSettings(_env_file=None).infer_system_prompt == default_system_prompt()


def test_env_有值_以_env_為準(monkeypatch):
    monkeypatch.setenv("UEP_INFER_SYSTEM_PROMPT", "你是測試人設")
    assert UepSettings(_env_file=None).infer_system_prompt == "你是測試人設"
