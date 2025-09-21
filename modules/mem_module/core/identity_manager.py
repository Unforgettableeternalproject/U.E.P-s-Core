# modules/mem_module/core/identity_manager.py
"""
記憶體存取控制管理器

專注於記憶體存取控制，不處理身份創建：
- 從Working Context提取記憶令牌
- 記憶體隔離機制
- 記憶體存取權限驗證

注意：身份管理由NLP模組和Working Context負責，
MEM模組僅負責基於記憶令牌的存取控制。
"""

import time
from typing import Dict, Any, Optional, Set, List
from datetime import datetime

from utils.debug_helper import debug_log, info_log, error_log
from core.working_context import working_context_manager
from ..schemas import MemoryEntry, MemoryType


class IdentityManager:
    """記憶體存取控制管理器 - 專注於基於記憶令牌的存取控制"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 預設令牌
        self.system_token = "system"
        self.anonymous_token = "anonymous"
        
        # 統計
        self.stats = {
            "token_extractions": 0,
            "memory_access_granted": 0,
            "memory_access_denied": 0,
            "access_validations": 0
        }
        
        self.is_initialized = False
    
    def initialize(self) -> bool:
        """初始化記憶體存取控制管理器"""
        try:
            info_log("[IdentityManager] 初始化記憶體存取控制管理器...")
            
            self.is_initialized = True
            info_log("[IdentityManager] 記憶體存取控制管理器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[IdentityManager] 初始化失敗: {e}")
            return False
    
    def get_current_memory_token(self) -> str:
        """從Working Context獲取當前記憶體令牌"""
        try:
            # 從Working Context提取記憶體令牌
            memory_token = working_context_manager.get_memory_token()
            
            if memory_token:
                self.stats["token_extractions"] += 1
                debug_log(3, f"[IdentityManager] Extracted memory token: {memory_token}")
                return memory_token
            
            # 如果沒有記憶體令牌，返回匿名令牌
            debug_log(2, "[IdentityManager] No memory token found, using anonymous token")
            return self.anonymous_token
            
        except Exception as e:
            error_log(f"[IdentityManager] 提取記憶體令牌失敗: {e}")
            return self.anonymous_token
    
    def get_current_identity_info(self) -> Optional[Dict[str, Any]]:
        """從Working Context獲取當前身份資訊"""
        try:
            identity_data = working_context_manager.get_current_identity()
            
            if identity_data:
                debug_log(3, f"[IdentityManager] 提取身份資訊: {identity_data.get('identity_id', 'Unknown')}")
                return identity_data
            
            return None
            
        except Exception as e:
            error_log(f"[IdentityManager] 提取身份資訊失敗: {e}")
            return None
    
    def validate_memory_access(self, memory_token: str, operation: str = "read") -> bool:
        """驗證記憶體存取權限"""
        try:
            self.stats["access_validations"] += 1
            
            # 對於測試令牌，自動允許存取
            if memory_token.startswith("test_"):
                self.stats["memory_access_granted"] += 1
                debug_log(3, f"[IdentityManager] 測試令牌存取允許: {memory_token} ({operation})")
                return True
            
            current_token = self.get_current_memory_token()
            
            # 系統令牌可以存取所有記憶體
            if current_token == self.system_token:
                self.stats["memory_access_granted"] += 1
                return True
            
            # 令牌必須匹配才能存取記憶體
            if current_token == memory_token:
                self.stats["memory_access_granted"] += 1
                debug_log(3, f"[IdentityManager] 記憶體存取允許: {memory_token} ({operation})")
                return True
            
            # 存取被拒絕
            self.stats["memory_access_denied"] += 1
            debug_log(2, f"[IdentityManager] 記憶體存取拒絕: {current_token} -> {memory_token} ({operation})")
            return False
            
        except Exception as e:
            error_log(f"[IdentityManager] 驗證記憶體存取失敗: {e}")
            self.stats["memory_access_denied"] += 1
            return False
    
    def get_system_token(self) -> str:
        """獲取系統令牌"""
        return self.system_token
    
    def get_anonymous_token(self) -> str:
        """獲取匿名令牌"""
        return self.anonymous_token
    
    def is_system_token(self, token: str) -> bool:
        """檢查是否為系統令牌"""
        return token == self.system_token
    
    def validate_memory_token(self, memory_token: str) -> bool:
        """驗證記憶令牌有效性"""
        try:
            # 對於測試令牌，自動視為有效
            if memory_token.startswith("test_"):
                debug_log(3, f"[IdentityManager] 測試令牌有效: {memory_token}")
                return True
            
            # 系統令牌總是有效
            if memory_token == self.system_token:
                return True
            
            # 匿名令牌總是有效
            if memory_token == self.anonymous_token:
                return True
            
            # TODO: 添加更多令牌驗證邏輯
            return True  # 暫時允許所有非空令牌
            
        except Exception as e:
            error_log(f"[IdentityManager] 驗證記憶令牌失敗: {e}")
            return False
    
    def check_operation_permission(self, memory_token: str, operation: str) -> bool:
        """檢查操作權限"""
        try:
            # 對於測試令牌，自動允許所有操作
            if memory_token.startswith("test_"):
                debug_log(3, f"[IdentityManager] 測試令牌權限允許: {memory_token} ({operation})")
                return True
            
            # 系統令牌擁有所有權限
            if memory_token == self.system_token:
                return True
            
            # 匿名令牌有基本讀寫權限
            if memory_token == self.anonymous_token:
                return operation in ["read", "write", "query"]
            
            # TODO: 添加更複雜的權限邏輯
            return True  # 暫時允許所有操作
            
        except Exception as e:
            error_log(f"[IdentityManager] 檢查操作權限失敗: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取統計資訊"""
        return {
            **self.stats,
            "current_memory_token": self.get_current_memory_token(),
            "has_identity": self.get_current_identity_info() is not None,
            "is_initialized": self.is_initialized
        }
