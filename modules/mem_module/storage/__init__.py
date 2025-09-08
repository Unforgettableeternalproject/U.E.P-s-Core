# modules/mem_module/storage/__init__.py
"""
MEM模組存儲層

包含：
- 向量索引管理 (FAISS)
- 元資料存儲
- 身份隔離管理
- 統一存儲介面
"""

from .vector_index import VectorIndexManager
from .metadata_storage import MetadataStorageManager
from .identity_isolation import IdentityIsolationManager
from .storage_manager import MemoryStorageManager

__all__ = [
    "VectorIndexManager",
    "MetadataStorageManager", 
    "IdentityIsolationManager",
    "MemoryStorageManager"
]
