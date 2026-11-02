"""
Dream 模組 - 離線記憶鞏固

包含：
  - DreamEngine: 主協調器
  - GraphPruning: 圖譜修剪
  - LiDERReplay: LLM 驅動反思重播
  - KnowledgeDistillation: 知識蒸餾（Episode → Concept）
"""

from echo_memory.dream.distillation import DistillationReport, KnowledgeDistillation
from echo_memory.dream.engine import DreamEngine, DreamReport
from echo_memory.dream.pruning import GraphPruning, PruningReport
from echo_memory.dream.replay import LiDERReplay, ReplayReport

__all__ = [
    "DreamEngine",
    "DreamReport",
    "GraphPruning",
    "PruningReport",
    "LiDERReplay",
    "ReplayReport",
    "KnowledgeDistillation",
    "DistillationReport",
]
