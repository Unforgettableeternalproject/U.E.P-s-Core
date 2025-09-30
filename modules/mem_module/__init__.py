# modules/mem_module/__init__.py
"""
MEM模組 - 記憶與知識庫模組 (重構版本)

功能:
- 長短期記憶管理
- 知識儲存與檢索
- 上下文管理
- Identity嵌入和快照功能
- 增強RAG算法

子模組結構:
- core/: 核心功能 (IdentityManager, SnapshotManager)
- retrieval/: 檢索功能 (SemanticRetriever)
- analysis/: 分析功能 (MemoryAnalyzer)
- storage/: 存儲功能 (StorageManager, VectorIndex等)
"""

from .mem_module import MEMModule
from .memory_manager import MemoryManager
from .schemas import *
from configs.config_loader import load_module_config

# 子模組導入
from .core import IdentityManager, SnapshotManager
from .retrieval import SemanticRetriever
from .analysis import MemoryAnalyzer


def register():
    """註冊MEM模組"""
    try:
        config = load_module_config("mem_module")
        instance = MEMModule(config=config)
        instance.initialize()
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[MEM] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = [
    "MEMModule",
    "MemoryManagerV2",
    "IdentityManager",
    "SnapshotManager", 
    "SemanticRetriever",
    "MemoryAnalyzer",
    "register"
]
