# modules/mem_module/storage/identity_isolation.py
"""
身份隔離管理器 - 簡化版本，基於 Working Context
移除獨立的 Identity Token 存儲，只保留 Identity 和其內部的 memory_token
"""

import os
import json
import hashlib
import uuid
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Any
from pathlib import Path

from utils.debug_helper import debug_log, info_log, error_log


class IdentityIsolationManager:
    """身份隔離管理器 - 基於 Working Context 的身份管理"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 基本配置
        self.token_expiry_days = config.get("token_expiry_days", 30)
        self.enable_permissions = config.get("enable_permissions", True)
        
        # 記憶體快取（簡化，主要依賴 Working Context）
        self.memory_token_cache: Dict[str, str] = {}  # memory_token -> identity_id (緩存)
        self.access_permissions: Dict[str, Set[str]] = {}  # memory_token -> permissions
        
        # 權限快取
        self.token_validation_cache: Dict[str, bool] = {}
        self.cache_timeout = config.get("cache_timeout", 300)  # 5分鐘
        self.cache_timestamps: Dict[str, datetime] = {}
        
        # 線程安全
        self._lock = threading.RLock()
        
        # 預設權限
        self.default_permissions = {"read", "write", "query", "create", "update"}
        
        # 統計資訊
        self.stats = {
            "tokens_validated": 0,
            "cache_hits": 0,
            "cache_misses": 0
        }
        
        self.is_initialized = False

    def initialize(self) -> bool:
        """初始化身份隔離管理器 - 基於 Working Context"""
        try:
            with self._lock:
                info_log("[IdentityIsolation] 初始化身份隔離管理器...")
                
                # 設置預設權限
                self._setup_default_permissions()
                
                # 清理過期緩存
                self._cleanup_expired_cache()
                
                self.is_initialized = True
                info_log("[IdentityIsolation] 身份隔離管理器初始化完成")
                return True
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 初始化失敗: {e}")
            return False
    
    def _setup_default_permissions(self):
        """設置預設權限"""
        # 基本權限已在 __init__ 中設置
        debug_log(3, "[IdentityIsolation] 預設權限設置完成")
    
    def _cleanup_expired_cache(self):
        """清理過期的緩存"""
        try:
            current_time = datetime.now()
            expired_tokens = []
            
            for token, timestamp in self.cache_timestamps.items():
                if (current_time - timestamp).seconds > self.cache_timeout:
                    expired_tokens.append(token)
            
            for token in expired_tokens:
                self.token_validation_cache.pop(token, None)
                self.cache_timestamps.pop(token, None)
                
            if expired_tokens:
                debug_log(3, f"[IdentityIsolation] 清理了 {len(expired_tokens)} 個過期緩存")
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 清理緩存失敗: {e}")
    
    def validate_memory_token(self, memory_token: str) -> bool:
        """驗證記憶令牌 - 基於 Working Context"""
        try:
            if not memory_token:
                return False
            
            self.stats["tokens_validated"] += 1
            
            # 檢查緩存
            if memory_token in self.token_validation_cache:
                timestamp = self.cache_timestamps.get(memory_token)
                if timestamp and (datetime.now() - timestamp).seconds < self.cache_timeout:
                    self.stats["cache_hits"] += 1
                    return self.token_validation_cache[memory_token]
            
            self.stats["cache_misses"] += 1
            
            # 特殊令牌處理
            if memory_token.startswith(("test_", "system_", "anonymous_", "legacy_")):
                result = True
            else:
                # 其他令牌暫時都允許（待實作更複雜的驗證邏輯）
                result = True
            
            # 更新緩存
            self.token_validation_cache[memory_token] = result
            self.cache_timestamps[memory_token] = datetime.now()
            
            return result
            
        except Exception as e:
            error_log(f"[IdentityIsolation] 驗證記憶令牌失敗: {e}")
            return False
    
    def get_identity_by_token(self, memory_token: str) -> Optional[str]:
        """根據記憶令牌獲取身份ID - 通過 Working Context"""
        try:
            # 從 Working Context 獲取當前身份
            from core.working_context import working_context_manager
            
            current_identity = working_context_manager.get_current_identity()
            current_token = working_context_manager.get_memory_token()
            
            if current_token == memory_token and current_identity:
                return current_identity.get('identity_id')
            
            # 如果不匹配，檢查緩存
            if memory_token in self.memory_token_cache:
                return self.memory_token_cache[memory_token]
            
            return None
            
        except Exception as e:
            error_log(f"[IdentityIsolation] 獲取身份失敗: {e}")
            return None
    
    def register_identity(self, identity_id: str, memory_token: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """註冊身份 - 更新緩存"""
        try:
            with self._lock:
                # 更新緩存
                self.memory_token_cache[memory_token] = identity_id
                
                # 設置預設權限
                if self.enable_permissions:
                    self.access_permissions[memory_token] = self.default_permissions.copy()
                
                debug_log(3, f"[IdentityIsolation] 註冊身份: {identity_id} -> {memory_token}")
                return True
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 註冊身份失敗: {e}")
            return False
    
    def check_access_permission(self, memory_token: str, operation: str) -> bool:
        """檢查存取權限"""
        try:
            # 特殊令牌處理
            if memory_token.startswith(("test_", "system_", "anonymous_", "legacy_")):
                return True
            
            # 檢查權限
            if not self.enable_permissions:
                return True
            
            permissions = self.access_permissions.get(memory_token, set())
            return operation in permissions
            
        except Exception as e:
            error_log(f"[IdentityIsolation] 檢查權限失敗: {e}")
            return False
    
    def check_operation_permission(self, memory_token: str, operation: str) -> bool:
        """檢查操作權限 - 與 check_access_permission 相同，提供相容性"""
        return self.check_access_permission(memory_token, operation)
    
    def generate_memory_token(self, identity_id: str) -> str:
        """為身份ID生成記憶令牌"""
        try:
            # 生成新令牌
            token_seed = f"{identity_id}_{uuid.uuid4().hex}_{datetime.now().timestamp()}"
            memory_token = hashlib.sha256(token_seed.encode()).hexdigest()[:32]
            
            # 註冊到緩存
            self.register_identity(identity_id, memory_token)
            
            debug_log(3, f"[IdentityIsolation] 生成記憶令牌: {identity_id} -> {memory_token}")
            return memory_token
            
        except Exception as e:
            error_log(f"[IdentityIsolation] 生成記憶令牌失敗: {e}")
            return ""
    
    def revoke_memory_token(self, memory_token: str) -> bool:
        """撤銷記憶令牌"""
        try:
            with self._lock:
                # 從緩存移除
                identity_id = self.memory_token_cache.pop(memory_token, None)
                self.access_permissions.pop(memory_token, None)
                self.token_validation_cache.pop(memory_token, None)
                self.cache_timestamps.pop(memory_token, None)
                
                if identity_id:
                    debug_log(3, f"[IdentityIsolation] 撤銷記憶令牌: {identity_id}")
                    return True
                
                return False
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 撤銷記憶令牌失敗: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取統計資訊"""
        return {
            **self.stats,
            "active_tokens": len(self.memory_token_cache),
            "cache_entries": len(self.token_validation_cache),
            "permissions_entries": len(self.access_permissions),
            "is_initialized": self.is_initialized
        }
    
    def shutdown(self):
        """關閉管理器"""
        try:
            with self._lock:
                # 清理緩存
                self.memory_token_cache.clear()
                self.access_permissions.clear()
                self.token_validation_cache.clear()
                self.cache_timestamps.clear()
                
                self.is_initialized = False
                info_log("[IdentityIsolation] 身份隔離管理器已關閉")
                
        except Exception as e:
            error_log(f"[IdentityIsolation] 關閉失敗: {e}")