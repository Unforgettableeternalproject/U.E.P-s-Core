"""
Dream 模組 - 離線記憶鞏固

包含：
  - DreamEngine: 主協調器
  - GraphPruning: 圖譜修剪
"""

from echo_memory.dream.engine import DreamEngine, DreamReport
from echo_memory.dream.pruning import GraphPruning, PruningReport

__all__ = [
    "DreamEngine",
    "DreamReport",
    "GraphPruning",
    "PruningReport",
]
