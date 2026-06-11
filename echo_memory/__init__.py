"""
Echo Memory System - 自主記憶子系統

受 OpenSoul 啟發的動態記憶引擎，實現 Form-Function-Dynamic 模型。

核心模組：
- storage: 三層記憶（Episodic/Semantic/Working）
- retrieval: EcphoryRAG 聯想檢索
- consolidation: Dream Engine 離線鞏固
- schemas: 資料結構定義

Example:
    >>> from echo_memory import MemoryEngine
    >>> memory = MemoryEngine(storage_path="./data")
    >>> context = memory.retrieve(query="今天天氣", dopamine=0.5, serotonin=0.5)
    >>> print(context.to_text())
"""

__version__ = "0.1.0"
__author__ = "U.E.P Team"

from echo_memory.engine import MemoryEngine
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.episode import Episode
from echo_memory.schemas.memory_context import MemoryContext

__all__ = [
    "MemoryEngine",
    "MemoryContext",
    "Episode",
    "Concept",
]
