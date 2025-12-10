"""
MISCHIEF Actions 模組初始化

自動導入所有行為實現，確保它們被註冊到 registry。
"""

from . import (
    MischiefAction,
    MoodContext,
    MischiefActionRegistry
)

# 創建全局註冊器實例
mischief_registry = MischiefActionRegistry()

from .executor import mischief_executor, MischiefExecutor

# 導入所有行為
try:
    from .move_window import MoveWindowAction
    from .create_file import CreateTextFileAction
    from .click_shortcut import ClickShortcutAction
    from .move_mouse import MoveMouseAction
    from .speak import SpeakAction
except ImportError as e:
    import sys
    from pathlib import Path
    # 確保可以導入
    sys.path.insert(0, str(Path(__file__).parents[3]))
    
    from .move_window import MoveWindowAction
    from .create_file import CreateTextFileAction
    from .click_shortcut import ClickShortcutAction
    from .move_mouse import MoveMouseAction
    from .speak import SpeakAction

# 手動註冊所有行為
mischief_registry.register(MoveWindowAction())
mischief_registry.register(CreateTextFileAction())
mischief_registry.register(ClickShortcutAction())
mischief_registry.register(MoveMouseAction())
mischief_registry.register(SpeakAction())


__all__ = [
    'MischiefAction',
    'MoodContext',
    'MischiefActionRegistry',
    'mischief_registry',
    'mischief_executor',
    'MischiefExecutor',
    # Actions
    'MoveWindowAction',
    'CreateTextFileAction',
    'ClickShortcutAction',
    'MoveMouseAction',
    'SpeakAction',
]
