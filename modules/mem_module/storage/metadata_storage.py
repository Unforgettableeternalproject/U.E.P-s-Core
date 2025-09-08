# modules/mem_module/storage/metadata_storage.py
"""
元資料存儲管理器 - JSON檔案的記憶元資料管理

功能：
- 記憶條目元資料的儲存與載入
- 記憶的增刪改查操作
- 身份隔離的資料過濾
- 自動備份與恢復
"""

import json
import os
import threading
import shutil
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path

from utils.debug_helper import debug_log, info_log, error_log
from ..schemas import MemoryEntry, MemoryType, MemoryImportance


class MetadataStorageManager:
    """元資料存儲管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 基本配置
        self.metadata_file = config.get("metadata_file", "memory/mem_metadata.json")
        self.backup_file = f"{self.metadata_file}.backup"
        self.temp_file = f"{self.metadata_file}.tmp"
        
        # 備份配置
        self.auto_backup = config.get("auto_backup", True)
        self.backup_interval = config.get("backup_interval", 3600)  # 1小時
        self.max_backups = config.get("max_backups", 5)
        
        # 記憶體快取
        self.metadata_cache: List[Dict[str, Any]] = []
        self.cache_by_id: Dict[str, Dict[str, Any]] = {}
        self.cache_by_token: Dict[str, List[Dict[str, Any]]] = {}
        
        # 線程安全
        self._lock = threading.RLock()
        
        # 狀態追蹤
        self.is_initialized = False
        self.last_backup_time = None
        self._dirty = False  # 資料是否需要儲存
        
    def initialize(self) -> bool:
        """初始化元資料存儲"""
        try:
            with self._lock:
                info_log("[MetadataStorage] 初始化元資料存儲管理器...")
                
                # 確保目錄存在
                metadata_dir = Path(self.metadata_file).parent
                metadata_dir.mkdir(parents=True, exist_ok=True)
                
                # 載入現有資料
                if self._metadata_file_exists():
                    if self._load_metadata():
                        info_log(f"[MetadataStorage] 載入元資料成功，條目數: {len(self.metadata_cache)}")
                    else:
                        info_log("WARNING", "[MetadataStorage] 載入元資料失敗，創建新檔案")
                        self._create_empty_metadata()
                else:
                    info_log("[MetadataStorage] 元資料檔案不存在，創建新檔案")
                    self._create_empty_metadata()
                
                # 重建快取索引
                self._rebuild_cache_indexes()
                
                self.is_initialized = True
                info_log(f"[MetadataStorage] 元資料存儲初始化完成")
                return True
                
        except Exception as e:
            error_log(f"[MetadataStorage] 初始化失敗: {e}")
            return False
    
    def _metadata_file_exists(self) -> bool:
        """檢查元資料檔案是否存在"""
        return os.path.exists(self.metadata_file)
    
    def _create_empty_metadata(self) -> bool:
        """創建空的元資料檔案"""
        try:
            self.metadata_cache = []
            self.cache_by_id = {}
            self.cache_by_token = {}
            return self._save_metadata()
        except Exception as e:
            error_log(f"[MetadataStorage] 創建空元資料失敗: {e}")
            return False
    
    def _load_metadata(self) -> bool:
        """載入元資料檔案"""
        try:
            debug_log(2, f"[MetadataStorage] 載入元資料檔案: {self.metadata_file}")
            
            with open(self.metadata_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 處理不同格式的資料
            if isinstance(data, list):
                self.metadata_cache = data
            elif isinstance(data, dict) and 'memories' in data:
                self.metadata_cache = data['memories']
            else:
                info_log("WARNING", "[MetadataStorage] 未知的元資料格式，創建新檔案")
                return False
            
            debug_log(3, f"[MetadataStorage] 元資料載入成功，條目數: {len(self.metadata_cache)}")
            return True
            
        except json.JSONDecodeError as e:
            error_log(f"[MetadataStorage] JSON解析失敗: {e}")
            return False
        except Exception as e:
            error_log(f"[MetadataStorage] 載入元資料失敗: {e}")
            return False
    
    def _save_metadata(self) -> bool:
        """儲存元資料到檔案"""
        try:
            with self._lock:
                debug_log(2, f"[MetadataStorage] 儲存元資料到: {self.metadata_file}")
                
                # 準備資料
                save_data = {
                    "version": "2.0",
                    "created_at": datetime.now().isoformat(),
                    "total_memories": len(self.metadata_cache),
                    "memories": self.metadata_cache
                }
                
                # 寫入臨時檔案
                with open(self.temp_file, 'w', encoding='utf-8') as f:
                    json.dump(save_data, f, indent=2, ensure_ascii=False, default=str)
                
                # 創建備份
                if os.path.exists(self.metadata_file):
                    shutil.copy2(self.metadata_file, self.backup_file)
                
                # 原子性替換
                shutil.move(self.temp_file, self.metadata_file)
                
                self._dirty = False
                debug_log(3, f"[MetadataStorage] 元資料儲存成功，條目數: {len(self.metadata_cache)}")
                return True
                
        except Exception as e:
            error_log(f"[MetadataStorage] 儲存元資料失敗: {e}")
            
            # 清理臨時檔案
            if os.path.exists(self.temp_file):
                os.remove(self.temp_file)
            
            return False
    
    def _rebuild_cache_indexes(self):
        """重建快取索引"""
        try:
            self.cache_by_id = {}
            self.cache_by_token = {}
            
            for memory_data in self.metadata_cache:
                memory_id = memory_data.get('memory_id')
                identity_token = memory_data.get('identity_token')
                
                if memory_id:
                    self.cache_by_id[memory_id] = memory_data
                
                if identity_token:
                    if identity_token not in self.cache_by_token:
                        self.cache_by_token[identity_token] = []
                    self.cache_by_token[identity_token].append(memory_data)
            
            debug_log(3, f"[MetadataStorage] 快取索引重建完成，ID索引: {len(self.cache_by_id)}, Token索引: {len(self.cache_by_token)}")
            
        except Exception as e:
            error_log(f"[MetadataStorage] 重建快取索引失敗: {e}")
    
    def add_memory(self, memory_entry: MemoryEntry) -> bool:
        """添加記憶條目"""
        try:
            with self._lock:
                # 轉換為字典格式
                memory_data = memory_entry.dict()
                
                # 檢查是否已存在
                if memory_entry.memory_id in self.cache_by_id:
                    info_log("WARNING", f"[MetadataStorage] 記憶條目已存在: {memory_entry.memory_id}")
                    return False
                
                # 添加到快取
                self.metadata_cache.append(memory_data)
                self.cache_by_id[memory_entry.memory_id] = memory_data
                
                # 更新身份令牌索引
                if memory_entry.identity_token not in self.cache_by_token:
                    self.cache_by_token[memory_entry.identity_token] = []
                self.cache_by_token[memory_entry.identity_token].append(memory_data)
                
                self._dirty = True
                debug_log(3, f"[MetadataStorage] 添加記憶條目: {memory_entry.memory_id}")
                
                # 自動儲存
                if self.auto_backup:
                    self._save_metadata()
                
                return True
                
        except Exception as e:
            error_log(f"[MetadataStorage] 添加記憶條目失敗: {e}")
            return False
    
    def get_memory_by_id(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """根據ID獲取記憶條目"""
        try:
            with self._lock:
                return self.cache_by_id.get(memory_id)
        except Exception as e:
            error_log(f"[MetadataStorage] 獲取記憶條目失敗: {e}")
            return None
    
    def get_memories_by_token(self, identity_token: str) -> List[Dict[str, Any]]:
        """根據身份令牌獲取記憶條目"""
        try:
            with self._lock:
                return self.cache_by_token.get(identity_token, []).copy()
        except Exception as e:
            error_log(f"[MetadataStorage] 獲取身份記憶失敗: {e}")
            return []
    
    def update_memory(self, memory_id: str, updates: Dict[str, Any]) -> bool:
        """更新記憶條目"""
        try:
            with self._lock:
                if memory_id not in self.cache_by_id:
                    info_log("WARNING", f"[MetadataStorage] 記憶條目不存在: {memory_id}")
                    return False
                
                # 更新資料
                memory_data = self.cache_by_id[memory_id]
                memory_data.update(updates)
                memory_data['updated_at'] = datetime.now().isoformat()
                
                self._dirty = True
                debug_log(3, f"[MetadataStorage] 更新記憶條目: {memory_id}")
                
                # 自動儲存
                if self.auto_backup:
                    self._save_metadata()
                
                return True
                
        except Exception as e:
            error_log(f"[MetadataStorage] 更新記憶條目失敗: {e}")
            return False
    
    def delete_memory(self, memory_id: str) -> bool:
        """刪除記憶條目"""
        try:
            with self._lock:
                if memory_id not in self.cache_by_id:
                    info_log("WARNING", f"[MetadataStorage] 記憶條目不存在: {memory_id}")
                    return False
                
                memory_data = self.cache_by_id[memory_id]
                identity_token = memory_data.get('identity_token')
                
                # 從快取中移除
                self.metadata_cache = [m for m in self.metadata_cache if m.get('memory_id') != memory_id]
                del self.cache_by_id[memory_id]
                
                # 更新身份令牌索引
                if identity_token and identity_token in self.cache_by_token:
                    self.cache_by_token[identity_token] = [
                        m for m in self.cache_by_token[identity_token] 
                        if m.get('memory_id') != memory_id
                    ]
                
                self._dirty = True
                debug_log(3, f"[MetadataStorage] 刪除記憶條目: {memory_id}")
                
                # 自動儲存
                if self.auto_backup:
                    self._save_metadata()
                
                return True
                
        except Exception as e:
            error_log(f"[MetadataStorage] 刪除記憶條目失敗: {e}")
            return False
    
    def search_memories(self, identity_token: str, filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """搜索記憶條目"""
        try:
            with self._lock:
                # 獲取該身份的所有記憶
                memories = self.get_memories_by_token(identity_token)
                
                if not filters:
                    return memories
                
                # 應用過濾器
                filtered_memories = []
                
                for memory in memories:
                    # 記憶類型過濾
                    if 'memory_types' in filters:
                        if memory.get('memory_type') not in filters['memory_types']:
                            continue
                    
                    # 主題過濾
                    if 'topic_filter' in filters:
                        topic = memory.get('topic', '')
                        if filters['topic_filter'].lower() not in topic.lower():
                            continue
                    
                    # 重要性過濾
                    if 'importance_filter' in filters:
                        if memory.get('importance') not in filters['importance_filter']:
                            continue
                    
                    # 時間範圍過濾
                    if 'time_range' in filters:
                        created_at = memory.get('created_at')
                        if created_at:
                            # 這裡可以添加時間範圍檢查邏輯
                            pass
                    
                    # 歸檔狀態過濾
                    if 'include_archived' in filters:
                        is_archived = memory.get('is_archived', False)
                        if not filters['include_archived'] and is_archived:
                            continue
                    
                    filtered_memories.append(memory)
                
                debug_log(3, f"[MetadataStorage] 搜索完成，找到 {len(filtered_memories)} 個結果")
                return filtered_memories
                
        except Exception as e:
            error_log(f"[MetadataStorage] 搜索記憶失敗: {e}")
            return []
    
    def get_memory_stats(self, identity_token: str = None) -> Dict[str, Any]:
        """獲取記憶統計資訊"""
        try:
            with self._lock:
                if identity_token:
                    memories = self.get_memories_by_token(identity_token)
                else:
                    memories = self.metadata_cache
                
                stats = {
                    "total_memories": len(memories),
                    "memories_by_type": {},
                    "memories_by_importance": {},
                    "active_snapshots": 0,
                    "archived_snapshots": 0
                }
                
                for memory in memories:
                    # 按類型統計
                    memory_type = memory.get('memory_type', 'unknown')
                    stats["memories_by_type"][memory_type] = stats["memories_by_type"].get(memory_type, 0) + 1
                    
                    # 按重要性統計
                    importance = memory.get('importance', 'medium')
                    stats["memories_by_importance"][importance] = stats["memories_by_importance"].get(importance, 0) + 1
                    
                    # 快照統計
                    if memory_type == MemoryType.SNAPSHOT:
                        if memory.get('is_archived', False):
                            stats["archived_snapshots"] += 1
                        else:
                            stats["active_snapshots"] += 1
                
                return stats
                
        except Exception as e:
            error_log(f"[MetadataStorage] 獲取統計資訊失敗: {e}")
            return {}
    
    def force_save(self) -> bool:
        """強制儲存元資料"""
        return self._save_metadata()
    
    def clear_all_memories(self, identity_token: str = None) -> bool:
        """清空記憶（可指定身份）"""
        try:
            with self._lock:
                if identity_token:
                    # 只清空指定身份的記憶
                    self.metadata_cache = [
                        m for m in self.metadata_cache 
                        if m.get('identity_token') != identity_token
                    ]
                    if identity_token in self.cache_by_token:
                        del self.cache_by_token[identity_token]
                    
                    # 重建ID索引
                    self.cache_by_id = {
                        m.get('memory_id'): m for m in self.metadata_cache 
                        if m.get('memory_id')
                    }
                else:
                    # 清空所有記憶
                    self.metadata_cache = []
                    self.cache_by_id = {}
                    self.cache_by_token = {}
                
                self._dirty = True
                
                # 自動儲存
                if self.auto_backup:
                    self._save_metadata()
                
                info_log(f"[MetadataStorage] 清空記憶完成，身份: {identity_token or '全部'}")
                return True
                
        except Exception as e:
            error_log(f"[MetadataStorage] 清空記憶失敗: {e}")
            return False
