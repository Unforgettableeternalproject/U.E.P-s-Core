# modules/mem_module/core/identity_manager.py
"""
身份管理器 - 處理記憶體隔離的身份令牌管理

注意：身份的創建與管理在NLP模組中已經處理了，
MEM模組只需要從Working Context中把身份給擷取下來就可以了

功能：
- 從Working Context提取身份令牌
- 記憶體隔離機制
- 記憶體存取控制
"""

import time
from typing import Dict, Any, Optional, Set, List
from datetime import datetime

from utils.debug_helper import debug_log, info_log, error_log
from core.working_context import working_context_manager
from ..schemas import MemoryEntry, MemoryType, IdentityToken


class IdentityManager:
    """身份管理器 - 專注於記憶體隔離的令牌管理"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 預設令牌
        self.system_token = "system"
        self.anonymous_token = "anonymous"
        
        # Identity Token 快取
        self.identity_tokens: Dict[str, IdentityToken] = {}
        
        # 統計
        self.stats = {
            "token_extractions": 0,
            "memory_access_granted": 0,
            "memory_access_denied": 0,
            "tokens_created": 0
        }
        
        self.is_initialized = False
    
    def initialize(self) -> bool:
        """初始化身份管理器"""
        try:
            info_log("[IdentityManager] 初始化記憶體隔離令牌管理器...")
            
            self.is_initialized = True
            info_log("[IdentityManager] 記憶體隔離令牌管理器初始化完成")
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
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取統計資訊"""
        return {
            **self.stats,
            "current_memory_token": self.get_current_memory_token(),
            "has_identity": self.get_current_identity_info() is not None,
            "cached_tokens": len(self.identity_tokens)
        }
    
    def create_identity_token_from_nlp(self, user_profile_data: Dict[str, Any]) -> Optional[IdentityToken]:
        """從 NLP UserProfile 數據創建 IdentityToken"""
        try:
            identity_token = IdentityToken(
                memory_token=user_profile_data.get("memory_token", ""),
                identity_id=user_profile_data.get("identity_id", ""),
                speaker_id=user_profile_data.get("speaker_id"),
                display_name=user_profile_data.get("display_name"),
                preferences=user_profile_data.get("preferences", {}),
                voice_preferences=user_profile_data.get("voice_preferences", {}),
                conversation_style=user_profile_data.get("conversation_style", {}),
                total_interactions=user_profile_data.get("total_interactions", 0),
                last_interaction=user_profile_data.get("last_interaction"),
                created_at=user_profile_data.get("created_at") or datetime.now()
            )
            
            # 快取令牌
            self.identity_tokens[identity_token.identity_id] = identity_token
            self.stats["tokens_created"] += 1
            
            info_log(f"[IdentityManager] 創建身份令牌: {identity_token.identity_id}")
            return identity_token
            
        except Exception as e:
            error_log(f"[IdentityManager] 創建身份令牌失敗: {e}")
            return None
    
    def get_identity_token(self, identity_id: str) -> Optional[IdentityToken]:
        """獲取身份令牌"""
        return self.identity_tokens.get(identity_id)
    
    def update_identity_token_usage(self, identity_id: str):
        """更新身份令牌使用時間"""
        if identity_id in self.identity_tokens:
            self.identity_tokens[identity_id].last_used = datetime.now()
            self.identity_tokens[identity_id].total_interactions += 1
