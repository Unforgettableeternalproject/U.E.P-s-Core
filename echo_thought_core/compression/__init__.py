"""Compression — 四層漸進式壓縮引擎

Layer 1: MicroPruner      — 規則式輕量清理（工具輸出剪裁、短訊息合併）
Layer 2: RollingSummarizer — 增量摘要（LLM 摘要最舊 N 個 turn）
Layer 3: FullCompactor     — 完整壓縮（結構化九段式摘要 + boundary 模式）
Layer 4: EmergencyCompactor — 緊急截斷（只保留最後 N 條 + 神聖段落）

輔助模組：
- tool_pairs  — Tool Pair 完整性保護（邊界對齊 + 孤立修復）
- sacred      — 神聖段落管理
"""

from echo_thought_core.compression.micro_pruning import MicroPruner
from echo_thought_core.compression.rolling_summary import RollingSummarizer, SUMMARY_PREFIX
from echo_thought_core.compression.full_compaction import FullCompactor, COMPACTION_PROMPT
from echo_thought_core.compression.emergency import EmergencyCompactor
from echo_thought_core.compression.sacred import SacredManager
from echo_thought_core.compression.tool_pairs import (
    PRUNED_TOOL_PLACEHOLDER,
    align_boundary_forward,
    align_boundary_backward,
    sanitize_tool_pairs,
    find_last_complete_tool_group_end,
)

__all__ = [
    # 壓縮層
    "MicroPruner",
    "RollingSummarizer",
    "FullCompactor",
    "EmergencyCompactor",
    "SacredManager",
    # 工具配對保護
    "align_boundary_forward",
    "align_boundary_backward",
    "sanitize_tool_pairs",
    "find_last_complete_tool_group_end",
    # 常數
    "PRUNED_TOOL_PLACEHOLDER",
    "SUMMARY_PREFIX",
    "COMPACTION_PROMPT",
]
