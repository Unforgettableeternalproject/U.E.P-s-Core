"""
Test fixtures for Session Control
"""

import pytest
from session_control import ContextManager, CompactionConfig, PersonaContext


@pytest.fixture
def config():
    """基本測試設定"""
    return CompactionConfig(
        backend="anthropic",
        model="claude-sonnet-4-6",
        context_window_size=10000,  # 小 window 方便測試
        thresholds={
            "micro_prune": 0.60,
            "rolling_summary": 0.80,
            "full_compact": 0.92,
        },
    )


@pytest.fixture
def persona():
    """測試用人設"""
    return PersonaContext(
        name="TestBot",
        traits=["friendly", "concise"],
        speaking_style="casual",
        user_preferences={"language": "zh-TW"},
    )


@pytest.fixture
def manager(config, persona):
    """基本 ContextManager 實例"""
    return ContextManager(config=config, persona=persona)
