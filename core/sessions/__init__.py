# core/sessions/__init__.py
"""
會話管理模組

提供三種會話類型的統一管理：
- GeneralSession (GS): 基礎會話
- ChattingSession (CS): 對話會話
- WorkflowSession (WS): 工作流會話
"""

from .session_manager import (
    session_manager,
    unified_session_manager,
    SessionType,
    SessionRecordStatus,
    SessionRecord,
    UnifiedSessionManager
)

__all__ = [
    'session_manager',
    'unified_session_manager',
    'SessionType',
    'SessionRecordStatus',
    'SessionRecord',
    'UnifiedSessionManager'
]
