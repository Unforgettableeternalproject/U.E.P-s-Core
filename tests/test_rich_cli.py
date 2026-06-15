"""
Test Rich CLI — Entry.py 的單元測試

測試 Rich Panel 渲染、指令處理、壓縮觸發。
不測試真實 LLM 呼叫（mock backend）。
"""

from unittest.mock import MagicMock, patch

import pytest

from echo_thought_core import CompactionConfig, ContextManager, PersonaContext
from echo_thought_core.schemas.session import CompressionTier

# 避免 Entry.py 在 import 時因為 Rich 不在 test 環境而失敗
# U.E.P Core 環境有 Rich，這裡只做防禦性處理
try:
    from Entry import (
        SessionControlCLI,
        render_status_panel,
        render_model_panel,
        render_history_panel,
        render_sawtooth_panel,
        show_dashboard,
        _usage_color,
        TIER_STYLES,
    )
    _HAS_ENTRY = True
except ImportError:
    _HAS_ENTRY = False


pytestmark = pytest.mark.skipif(not _HAS_ENTRY, reason="Rich or Entry not available")


# ── 輔助 fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def test_manager():
    """建立小 window 的 ContextManager（無 backend）"""
    config = CompactionConfig(
        backend="gemini",
        model="gemini-2.5-flash-lite",
        context_window_size=5000,
        compaction_cooldown_seconds=0,
    )
    persona = PersonaContext(
        name="U.E.P",
        traits=["cheerful"],
    )
    return ContextManager(config=config, persona=persona, backend=None)


@pytest.fixture
def filled_manager(test_manager):
    """灌入一些訊息的 ContextManager"""
    for i in range(10):
        test_manager.add_message("user", f"Test message {i}: hello world")
        test_manager.add_message("assistant", f"Reply {i}: nice to meet you")
    return test_manager


# ═══════════════════════════════════════════════════════════════════════════
# Panel 渲染測試
# ═══════════════════════════════════════════════════════════════════════════


class TestPanelRendering:
    """各個 Rich Panel 可正常渲染（不拋錯）"""

    def test_render_status_panel(self, filled_manager):
        panel = render_status_panel(filled_manager)
        assert panel is not None
        assert panel.title is not None

    def test_render_model_panel(self, filled_manager):
        panel = render_model_panel(filled_manager)
        assert panel is not None

    def test_render_history_panel(self, filled_manager):
        panel = render_history_panel(filled_manager)
        assert panel is not None

    def test_render_history_empty(self, test_manager):
        panel = render_history_panel(test_manager)
        assert panel is not None

    def test_render_sawtooth_panel(self, filled_manager):
        panel = render_sawtooth_panel(filled_manager)
        assert panel is not None

    def test_render_sawtooth_empty(self, test_manager):
        panel = render_sawtooth_panel(test_manager)
        assert panel is not None

    def test_show_dashboard_no_crash(self, filled_manager):
        """show_dashboard 完整渲染不拋錯"""
        from rich.console import Console
        console = Console(file=MagicMock())
        show_dashboard(filled_manager, console)


# ═══════════════════════════════════════════════════════════════════════════
# 使用率配色
# ═══════════════════════════════════════════════════════════════════════════


class TestUsageColor:
    def test_green_low_usage(self):
        assert _usage_color(0.3) == "green"

    def test_yellow_medium_usage(self):
        assert _usage_color(0.65) == "yellow"

    def test_orange_high_usage(self):
        assert _usage_color(0.85) == "dark_orange"

    def test_red_critical_usage(self):
        assert _usage_color(0.95) == "red"


# ═══════════════════════════════════════════════════════════════════════════
# Tier 配色
# ═══════════════════════════════════════════════════════════════════════════


class TestTierStyles:
    def test_all_tiers_have_styles(self):
        for tier in CompressionTier:
            assert tier in TIER_STYLES
            dot, color, label = TIER_STYLES[tier]
            assert isinstance(dot, str)
            assert isinstance(color, str)
            assert isinstance(label, str)


# ═══════════════════════════════════════════════════════════════════════════
# CLI 初始化
# ═══════════════════════════════════════════════════════════════════════════


class TestCLIInit:
    def test_cli_creation(self):
        cli = SessionControlCLI()
        assert cli.mgr is None
        assert cli.console is not None

    @patch("Entry.create_context_manager")
    def test_initialize_offline_mode(self, mock_create):
        """Backend 初始化失敗時降級為 offline 模式"""
        mock_create.side_effect = Exception("No API key")

        cli = SessionControlCLI()
        cli.console = MagicMock()
        result = cli.initialize()

        assert result is True
        assert cli.mgr is not None
        assert cli.mgr._backend is None


# ═══════════════════════════════════════════════════════════════════════════
# 指令處理
# ═══════════════════════════════════════════════════════════════════════════


class TestCommands:
    def test_fill_command(self, test_manager):
        """fill 指令灌入訊息"""
        cli = SessionControlCLI()
        cli.mgr = test_manager
        cli.console = MagicMock()

        cli._cmd_fill("fill 10")
        assert test_manager.state.message_count == 20  # 10 pairs

    def test_fill_default(self, test_manager):
        """fill 不帶數字時使用預設值"""
        cli = SessionControlCLI()
        cli.mgr = test_manager
        cli.console = MagicMock()

        cli._cmd_fill("fill")
        assert test_manager.state.message_count == 100  # 50 pairs

    def test_compact_empty(self, test_manager):
        """空歷史的 compact 不拋錯"""
        cli = SessionControlCLI()
        cli.mgr = test_manager
        cli.console = MagicMock()

        cli._cmd_compact()
        # 應該輸出 "Nothing to compact"

    def test_usage_command(self, filled_manager):
        """usage 指令不拋錯"""
        cli = SessionControlCLI()
        cli.mgr = filled_manager
        cli.console = MagicMock()

        cli._cmd_usage()
