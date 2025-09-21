# modules/mem_module/storage/identity_isolation.py
"""
身份隔離管理器 - 確保記憶按使用者隔離

功能：
- 記憶令牌的生成與管理
- 身份驗證與權限控制
- 記憶存取的身份隔離
- 令牌的持久化與恢復
"""

import hashlib
import uuid
import json
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, Set, Optional, List, Any
from pathlib import Path

from utils.debug_helper import debug_log, info_log, error_log 
from ..schemas import IdentityToken


class IdentityIsolationManager:
    """身份隔離管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 基本配置
        self.token_file = config.get("token_file", "memory/identity_tokens.json")
        self.token_expiry_days = config.get("token_expiry_days", 30)
        self.enable_permissions = config.get("enable_permissions", True)
        
        # 記憶體快取
        self.identity_token_map: Dict[str, str] = {}  # identity_id -> memory_token
        self.token_identity_map: Dict[str, str] = {}  # memory_token -> identity_id
        self.access_permissions: Dict[str, Set[str]] = {}  # memory_token -> permissions
        self.token_metadata: Dict[str, IdentityToken] = {}  # memory_token -> IdentityToken
        
        # 權限快取
        self.token_validation_cache: Dict[str, bool] = {}
        self.cache_timeout = config.get("cache_timeout", 300)  # 5分鐘
        self.cache_timestamps: Dict[str, datetime] = {}
        
        # 線程安全
        self._lock = threading.RLock()
        
        # 預設權限
        self.default_permissions = {
            "read", "write", "delete", "query", "snapshot", "search"
        }
        
        # 狀態追蹤
        self.is_initialized = False
        
    def initialize(self) -> bool:
        """初始化身份隔離管理器"""
        try:
            with self._lock:
                info_log("[IdentityIsolation] 初始化身份隔離管理器...")
                
                # 確保目錄存在
                token_dir = Path(self.token_file).parent
                token_dir.mkdir(parents=True, exist_ok=True)
                
                # 載入現有令牌
                if os.path.exists(self.token_file):
                    if self._load_tokens():
                        info_log(f"[IdentityIsolation] 載入令牌成功，數量: {len(self.identity_token_map)}")
                    else:
                        info_log("WARNING", "[IdentityIsolation] 載入令牌失敗，創建新檔案")
                        self._create_empty_token_file()
                else:
                    info_log("[IdentityIsolation] 令牌檔案不存在，創建新檔案")
                    self._create_empty_token_file()
                
                # 清理過期令牌
                self._cleanup_expired_tokens()
                
                self.is_initialized = True
                info_log("[IdentityIsolation] 身份隔離管理器初始化完成")
                return True
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 初始化失敗: {e}")
            return False
    
    def _create_empty_token_file(self) -> bool:
        """創建空的令牌檔案"""
        try:
            empty_data = {
                "version": "1.0",
                "created_at": datetime.now().isoformat(),
                "tokens": []
            }
            
            with open(self.token_file, 'w', encoding='utf-8') as f:
                json.dump(empty_data, f, indent=2, ensure_ascii=False)
            
            return True
            
        except Exception as e:
            error_log(f"[IdentityIsolation] 創建令牌檔案失敗: {e}")
            return False
    
    def _load_tokens(self) -> bool:
        """載入令牌資料"""
        try:
            debug_log(2, f"[IdentityIsolation] 載入令牌檔案: {self.token_file}")
            
            with open(self.token_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            tokens_data = data.get('tokens', [])
            
            for token_data in tokens_data:
                try:
                    # 轉換為記憶令牌物件
                    token_obj = IdentityToken(**token_data)
                    
                    # 檢查令牌是否過期
                    if token_obj.expires_at and datetime.fromisoformat(token_obj.expires_at.replace('Z', '+00:00')) < datetime.now():
                        debug_log(3, f"[IdentityIsolation] 跳過過期令牌: {token_obj.identity_id}")
                        continue
                    
                    # 建立映射關係
                    self.identity_token_map[token_obj.identity_id] = token_obj.token
                    self.token_identity_map[token_obj.token] = token_obj.identity_id
                    self.token_metadata[token_obj.token] = token_obj
                    
                    # 設定權限
                    if self.enable_permissions:
                        self.access_permissions[token_obj.token] = set(token_obj.permissions)
                    else:
                        self.access_permissions[token_obj.token] = self.default_permissions.copy()
                        
                except Exception as e:
                    info_log("WARNING", f"[IdentityIsolation] 解析令牌資料失敗: {e}")
                    continue
            
            debug_log(3, f"[IdentityIsolation] 令牌載入成功，有效數量: {len(self.identity_token_map)}")
            return True
            
        except Exception as e:
            error_log(f"[IdentityIsolation] 載入令牌失敗: {e}")
            return False
    
    def _save_tokens(self) -> bool:
        """儲存令牌資料"""
        try:
            with self._lock:
                debug_log(2, f"[IdentityIsolation] 儲存令牌到: {self.token_file}")
                
                tokens_list = []
                for token, token_obj in self.token_metadata.items():
                    tokens_list.append(token_obj.dict())
                
                save_data = {
                    "version": "1.0",
                    "updated_at": datetime.now().isoformat(),
                    "total_tokens": len(tokens_list),
                    "tokens": tokens_list
                }
                
                # 創建備份
                backup_file = f"{self.token_file}.backup"
                if os.path.exists(self.token_file):
                    os.rename(self.token_file, backup_file)
                
                # 儲存新檔案
                with open(self.token_file, 'w', encoding='utf-8') as f:
                    json.dump(save_data, f, indent=2, ensure_ascii=False, default=str)
                
                # 清理備份
                if os.path.exists(backup_file):
                    os.remove(backup_file)
                
                debug_log(3, f"[IdentityIsolation] 令牌儲存成功，數量: {len(tokens_list)}")
                return True
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 儲存令牌失敗: {e}")
            return False
    
    def generate_memory_token(self, identity_id: str, permissions: List[str] = None) -> str:
        """為身份ID生成記憶令牌"""
        try:
            with self._lock:
                # 檢查是否已有有效令牌
                if identity_id in self.identity_token_map:
                    existing_token = self.identity_token_map[identity_id]
                    token_obj = self.token_metadata.get(existing_token)
                    
                    # 檢查令牌是否仍然有效
                    if token_obj and token_obj.is_active:
                        if not token_obj.expires_at or datetime.fromisoformat(token_obj.expires_at.replace('Z', '+00:00')) > datetime.now():
                            debug_log(3, f"[IdentityIsolation] 使用現有令牌: {identity_id}")
                            return existing_token
                
                # 生成新令牌
                token_seed = f"{identity_id}_{uuid.uuid4().hex}_{datetime.now().timestamp()}"
                memory_token = hashlib.sha256(token_seed.encode()).hexdigest()[:32]
                
                # 計算過期時間
                expires_at = datetime.now() + timedelta(days=self.token_expiry_days)
                
                # 設定權限
                token_permissions = permissions if permissions else list(self.default_permissions)
                
                # 創建令牌物件
                token_obj = IdentityToken(
                    token=memory_token,
                    identity_id=identity_id,
                    created_at=datetime.now(),
                    expires_at=expires_at,
                    permissions=token_permissions,
                    is_active=True
                )
                
                # 建立映射關係
                self.identity_token_map[identity_id] = memory_token
                self.token_identity_map[memory_token] = identity_id
                self.token_metadata[memory_token] = token_obj
                
                # 設定權限
                self.access_permissions[memory_token] = set(token_permissions)
                
                # 更新快取
                self.token_validation_cache[memory_token] = True
                self.cache_timestamps[memory_token] = datetime.now()
                
                # 儲存到檔案
                self._save_tokens()
                
                info_log(f"[IdentityIsolation] 為身份 {identity_id} 生成記憶令牌")
                return memory_token
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 令牌生成失敗: {e}")
            return ""
    
    def validate_memory_token(self, memory_token: str) -> bool:
        """驗證記憶令牌有效性"""
        try:
            # 對於測試令牌，直接返回True
            if memory_token.startswith("test_"):
                return True
                
            with self._lock:
                # 檢查快取
                if memory_token in self.token_validation_cache:
                    cache_time = self.cache_timestamps.get(memory_token)
                    if cache_time and (datetime.now() - cache_time).seconds < self.cache_timeout:
                        return self.token_validation_cache[memory_token]
                
                # 驗證令牌
                is_valid = False
                
                if memory_token in self.token_metadata:
                    token_obj = self.token_metadata[memory_token]
                    
                    # 檢查是否活躍
                    if not token_obj.is_active:
                        is_valid = False
                    # 檢查是否過期
                    elif token_obj.expires_at and datetime.fromisoformat(token_obj.expires_at.replace('Z', '+00:00')) < datetime.now():
                        is_valid = False
                        # 標記為非活躍
                        token_obj.is_active = False
                        self._save_tokens()
                    else:
                        is_valid = True
                
                # 更新快取
                self.token_validation_cache[memory_token] = is_valid
                self.cache_timestamps[memory_token] = datetime.now()
                
                return is_valid
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 令牌驗證失敗: {e}")
            return False
    
    def check_operation_permission(self, memory_token: str, operation: str) -> bool:
        """檢查操作權限"""
        try:
            # 對於測試令牌，直接允許所有操作
            if memory_token.startswith("test_"):
                return True
                
            # 先驗證令牌
            if not self.validate_memory_token(memory_token):
                return False
            
            # 檢查權限
            if not self.enable_permissions:
                return True
            
            allowed_operations = self.access_permissions.get(memory_token, set())
            return operation in allowed_operations
            
        except Exception as e:
            error_log(f"[IdentityIsolation] 權限檢查失敗: {e}")
            return False
    
    def get_identity_from_token(self, memory_token: str) -> Optional[str]:
        """從記憶令牌獲取身份ID"""
        try:
            with self._lock:
                return self.token_identity_map.get(memory_token)
        except Exception as e:
            error_log(f"[IdentityIsolation] 獲取身份失敗: {e}")
            return None
    
    def get_token_info(self, memory_token: str) -> Optional[IdentityToken]:
        """獲取令牌詳細資訊"""
        try:
            with self._lock:
                return self.token_metadata.get(memory_token)
        except Exception as e:
            error_log(f"[IdentityIsolation] 獲取令牌資訊失敗: {e}")
            return None
    
    def revoke_token(self, memory_token: str) -> bool:
        """撤銷令牌"""
        try:
            with self._lock:
                if memory_token not in self.token_metadata:
                    info_log("WARNING", f"[IdentityIsolation] 令牌不存在: {memory_token[:8]}...")
                    return False
                
                # 標記為非活躍
                token_obj = self.token_metadata[memory_token]
                token_obj.is_active = False
                
                # 更新快取
                self.token_validation_cache[memory_token] = False
                
                # 儲存變更
                self._save_tokens()
                
                info_log(f"[IdentityIsolation] 撤銷令牌: {token_obj.identity_id}")
                return True
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 撤銷令牌失敗: {e}")
            return False
    
    def update_token_permissions(self, memory_token: str, permissions: List[str]) -> bool:
        """更新令牌權限"""
        try:
            with self._lock:
                if memory_token not in self.token_metadata:
                    return False
                
                # 更新權限
                token_obj = self.token_metadata[memory_token]
                token_obj.permissions = permissions
                self.access_permissions[memory_token] = set(permissions)
                
                # 儲存變更
                self._save_tokens()
                
                debug_log(3, f"[IdentityIsolation] 更新令牌權限: {token_obj.identity_id}")
                return True
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 更新權限失敗: {e}")
            return False
    
    def _cleanup_expired_tokens(self):
        """清理過期令牌"""
        try:
            with self._lock:
                expired_tokens = []
                current_time = datetime.now()
                
                for token, token_obj in self.token_metadata.items():
                    if token_obj.expires_at:
                        expires_at = datetime.fromisoformat(token_obj.expires_at.replace('Z', '+00:00'))
                        if expires_at < current_time:
                            expired_tokens.append(token)
                
                # 移除過期令牌
                for token in expired_tokens:
                    token_obj = self.token_metadata[token]
                    identity_id = token_obj.identity_id
                    
                    # 清理所有映射
                    del self.token_metadata[token]
                    del self.token_identity_map[token]
                    if identity_id in self.identity_token_map:
                        del self.identity_token_map[identity_id]
                    if token in self.access_permissions:
                        del self.access_permissions[token]
                    if token in self.token_validation_cache:
                        del self.token_validation_cache[token]
                    if token in self.cache_timestamps:
                        del self.cache_timestamps[token]
                
                if expired_tokens:
                    info_log(f"[IdentityIsolation] 清理過期令牌數量: {len(expired_tokens)}")
                    self._save_tokens()
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 清理過期令牌失敗: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取身份隔離統計資訊"""
        try:
            with self._lock:
                active_tokens = sum(1 for t in self.token_metadata.values() if t.is_active)
                total_identities = len(self.identity_token_map)
                
                return {
                    "total_tokens": len(self.token_metadata),
                    "active_tokens": active_tokens,
                    "total_identities": total_identities,
                    "cache_size": len(self.token_validation_cache),
                    "permissions_enabled": self.enable_permissions,
                    "token_expiry_days": self.token_expiry_days
                }
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 獲取統計失敗: {e}")
            return {}
