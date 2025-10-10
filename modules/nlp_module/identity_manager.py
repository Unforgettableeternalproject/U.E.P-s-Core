# modules/nlp_module/identity_manager.py
"""
語者身份管理器 - 管理使用者身份與Working Context整合

這個模組負責：
1. 管理語者ID到使用者身份的映射
2. 處理語者樣本累積決策
3. 整合Working Context進行身份決策
4. 維護使用者檔案和偏好設定
"""

import json
import os
import time
import uuid
from typing import Dict, Optional, List, Any
from datetime import datetime
from pathlib import Path

from .schemas import (
    UserProfile, IdentityStatus, IdentityDecision, 
    NLPDecisionPackage
)
from core.working_context import ContextType as WorkingContextType, WorkingContext
from utils.debug_helper import debug_log, info_log, error_log


class IdentityDecisionHandler:
    """身份決策處理器 - 實現Working Context的決策處理協議"""
    
    def can_handle(self, context_type: WorkingContextType) -> bool:
        """檢查是否可以處理指定類型的上下文"""
        return context_type == WorkingContextType.SPEAKER_ACCUMULATION
    
    def make_decision(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """進行語者身份決策"""
        speaker_samples = context_data.get("data", [])
        metadata = context_data.get("metadata", {})
        
        # 分析語者樣本的一致性
        consistency_score = self._analyze_speaker_consistency(speaker_samples)
        
        if consistency_score > 0.8:
            decision = {
                "action": "create_identity",
                "confidence": consistency_score,
                "speaker_id": metadata.get("speaker_id"),
                "sample_count": len(speaker_samples),
                "reasoning": f"語者樣本一致性達到 {consistency_score:.2f}，建議創建新身份"
            }
        elif consistency_score > 0.6:
            decision = {
                "action": "continue_accumulation",
                "confidence": consistency_score,
                "speaker_id": metadata.get("speaker_id"),
                "sample_count": len(speaker_samples),
                "reasoning": f"語者樣本一致性 {consistency_score:.2f}，建議繼續累積"
            }
        else:
            decision = {
                "action": "reset_accumulation",
                "confidence": consistency_score,
                "speaker_id": metadata.get("speaker_id"),
                "sample_count": len(speaker_samples),
                "reasoning": f"語者樣本一致性過低 {consistency_score:.2f}，建議重置累積"
            }
        
        debug_log(2, f"[IdentityDecisionHandler] 身份決策：{decision}")
        return decision
    
    def apply_decision(self, context_data: Dict[str, Any], decision: Dict[str, Any]) -> bool:
        """應用身份決策結果
        
        注意: 這個方法只負責記錄決策結果，實際的身份創建由 IdentityManager 自行處理
        """
        try:
            action = decision.get("action")
            speaker_id = decision.get("speaker_id")
            
            if action == "create_identity":
                info_log(f"[IdentityDecisionHandler] 建議為語者 {speaker_id} 創建身份")
                
            elif action == "continue_accumulation":
                info_log(f"[IdentityDecisionHandler] 語者 {speaker_id} 繼續樣本累積")
                
            elif action == "reset_accumulation":
                info_log(f"[IdentityDecisionHandler] 語者 {speaker_id} 樣本累積將重置")
                
            return True
            
        except Exception as e:
            error_log(f"[IdentityDecisionHandler] 應用決策失敗：{e}")
            return False
    
    def _analyze_speaker_consistency(self, samples: List[Any]) -> float:
        """分析語者樣本的一致性"""
        if not samples or len(samples) < 2:
            return 0.0
        
        # 這裡應該實現真正的語者一致性分析
        # 目前使用簡化的邏輯
        base_score = min(0.9, 0.5 + len(samples) * 0.1)
        return base_score


class IdentityManager:
    """語者身份管理器"""
    
    def __init__(self, storage_path: str = "memory/identities", config: Optional[Dict[str, Any]] = None):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # 模組設定
        self.config = config or {
            "sample_threshold": 15,  # 身份確認所需樣本數量
            "confirmation_threshold": 0.8  # 身份確認閾值
        }
        
        # 身份資料庫
        self.identities: Dict[str, UserProfile] = {}
        self.speaker_to_identity: Dict[str, str] = {}  # speaker_id -> identity_id
        
        # 決策處理器
        self.decision_handler = IdentityDecisionHandler()
        
        # 載入現有身份
        self._load_identities()
        
        info_log(f"[IdentityManager] 初始化完成，載入 {len(self.identities)} 個身份")
    
    def get_identity_by_speaker(self, speaker_id: str) -> Optional[UserProfile]:
        """根據語者ID獲取使用者身份"""
        identity_id = self.speaker_to_identity.get(speaker_id)
        if identity_id:
            return self.identities.get(identity_id)
        return None
    
    def create_identity(self, speaker_id: str, display_name: Optional[str] = None) -> UserProfile:
        """為語者創建新的使用者身份"""
        identity_id = f"user_{int(time.time())}_{speaker_id[:8]}"
        
        # 生成記憶存取令牌 - 用於MEM模組
        memory_token = f"mem_{identity_id}_{uuid.uuid4().hex[:8]}"
        
        profile = UserProfile(
            identity_id=identity_id,
            speaker_id=speaker_id,
            display_name=display_name or f"User-{identity_id[:8]}",
            status=IdentityStatus.CONFIRMED,
            total_interactions=0,
            created_at=datetime.now(),
            last_interaction=datetime.now(),
            
            # 添加記憶令牌
            memory_token=memory_token,
            
            # 初始化各模組偏好
            voice_preferences={
                "default_mood": "neutral",
                "speed": 1.0,
                "pitch": 1.0
            },
            conversation_style={
                "formality": "neutral",
                "verbosity": "moderate",
                "personality": "balanced"
            }
        )
        
        # 保存身份
        self.identities[identity_id] = profile
        self.speaker_to_identity[speaker_id] = identity_id
        
        # 持久化
        self._save_identity(profile)
        self._save_mapping()
        
        info_log(f"[IdentityManager] 創建新身份：{identity_id} (語者：{speaker_id})")
        return profile
    
    def update_identity_interaction(self, identity_id: str, interaction_data: Dict[str, Any]):
        """更新使用者互動記錄"""
        if identity_id in self.identities:
            profile = self.identities[identity_id]
            profile.total_interactions += 1
            profile.last_interaction = datetime.now()
            
            # 處理不同模組的交互記錄
            module = interaction_data.get("module", "")
            
            # SYS 模組 - 系統功能使用習慣
            if module == "sys" or "command_type" in interaction_data:
                cmd_type = interaction_data.get("command_type")
                if cmd_type:
                    if "command_usage" not in profile.system_habits:
                        profile.system_habits["command_usage"] = {}
                    profile.system_habits["command_usage"][cmd_type] = \
                        profile.system_habits["command_usage"].get(cmd_type, 0) + 1
            
            # LLM 模組 - 對話風格偏好
            elif module == "llm":
                if "conversation_feedback" in interaction_data:
                    feedback = interaction_data["conversation_feedback"]
                    if feedback == "positive":
                        # 記錄正面反饋的對話風格參數
                        style_params = interaction_data.get("style_params", {})
                        if style_params:
                            profile.conversation_style.update(style_params)
                            debug_log(2, f"[IdentityManager] 更新 {identity_id} 的對話風格偏好")
            
            # TTS 模組 - 語音偏好
            elif module == "tts":
                if "voice_feedback" in interaction_data:
                    feedback = interaction_data["voice_feedback"]
                    if feedback == "positive":
                        # 記錄正面反饋的語音參數
                        voice_params = interaction_data.get("voice_params", {})
                        if voice_params:
                            profile.voice_preferences.update(voice_params)
                            debug_log(2, f"[IdentityManager] 更新 {identity_id} 的語音風格偏好")
            
            # 保存更新
            self._save_identity(profile)
            
            debug_log(3, f"[IdentityManager] 更新身份 {identity_id} 互動記錄")
    
    def process_speaker_identification(self, speaker_id: str, speaker_status: str, 
                                     confidence: float) -> tuple[Optional[UserProfile], str]:
        """處理語者識別結果，並根據需要添加樣本到Working Context
        
        Args:
            speaker_id: 語者ID
            speaker_status: 語者狀態 (new/existing/unknown)
            confidence: 語者識別信心度
            
        Returns:
            tuple: (使用者檔案, 處理動作)
        """
        from core.working_context import WorkingContext, ContextType
        
        if speaker_status == "existing" and confidence > 0.8:
            # 已知語者，直接載入身份
            identity = self.get_identity_by_speaker(speaker_id)
            if identity:
                debug_log(2, f"[IdentityManager] 載入已知身份：{identity.identity_id}")
                
                # 更新身份的最後活動時間
                identity.last_interaction = datetime.now()
                self._save_identity(identity)
                
                return identity, "loaded"
            
        elif speaker_status == "new" or (speaker_status == "existing" and confidence <= 0.8):
            # 新語者或不確定的語者，需要累積樣本
            # 實際的樣本累積由 NLP 模組主程式透過 Working Context 處理
            debug_log(2, f"[IdentityManager] 語者 {speaker_id} 需要樣本累積 (由 NLP 模組處理)")
            return None, "accumulating"
            
        # 未知情況
        debug_log(2, f"[IdentityManager] 語者 {speaker_id} 狀態未知")
        return None, "unknown"
    
    def get_decision_handler(self) -> IdentityDecisionHandler:
        """獲取決策處理器"""
        return self.decision_handler
    
    def get_memory_token(self, identity_id: str) -> Optional[str]:
        """獲取身份的記憶庫存取令牌"""
        if identity_id in self.identities:
            return self.identities[identity_id].memory_token
        return None
    
    def verify_memory_access(self, memory_token: str) -> Optional[str]:
        """驗證記憶庫存取令牌，返回對應的身份ID"""
        for identity_id, profile in self.identities.items():
            if profile.memory_token == memory_token:
                return identity_id
        return None
    
    def get_voice_preferences(self, identity_id: str) -> Dict[str, Any]:
        """獲取使用者的語音風格偏好"""
        if identity_id in self.identities:
            return self.identities[identity_id].voice_preferences
        return {}
    
    def get_conversation_style(self, identity_id: str) -> Dict[str, Any]:
        """獲取使用者的對話風格偏好"""
        if identity_id in self.identities:
            return self.identities[identity_id].conversation_style
        return {}
    
    def update_user_preferences(self, identity_id: str, preference_type: str, preferences: Dict[str, Any]) -> bool:
        """更新使用者偏好設定
        
        Args:
            identity_id: 身份ID
            preference_type: 偏好類型 (voice, conversation, system)
            preferences: 偏好設定
            
        Returns:
            bool: 是否更新成功
        """
        if identity_id not in self.identities:
            return False
            
        profile = self.identities[identity_id]
        
        try:
            if preference_type == "voice":
                profile.voice_preferences.update(preferences)
            elif preference_type == "conversation":
                profile.conversation_style.update(preferences)
            elif preference_type == "system":
                if "habits" in preferences:
                    profile.system_habits.update(preferences["habits"])
                else:
                    profile.preferences.update(preferences)
            
            # 保存更新
            self._save_identity(profile)
            debug_log(2, f"[IdentityManager] 已更新 {identity_id} 的 {preference_type} 偏好設定")
            return True
            
        except Exception as e:
            error_log(f"[IdentityManager] 更新 {identity_id} 偏好設定失敗: {e}")
            return False
            
    def inject_identity_to_working_context(self, identity_id: str) -> Dict[str, Any]:
        """將身份資料注入到Working Context
        
        Args:
            identity_id: 身份ID
            
        Returns:
            Dict[str, Any]: 身份上下文資料
        """
        if identity_id not in self.identities:
            return {}
            
        profile = self.identities[identity_id]
        
        # 創建要注入的身份上下文
        identity_context = {
            "identity": {
                "id": profile.identity_id,
                "name": profile.display_name,
                "speaker_id": profile.speaker_id,
            },
            "preferences": {
                "voice": profile.voice_preferences,
                "conversation": profile.conversation_style,
                "system": profile.system_habits
            },
            "memory": {
                "token": profile.memory_token,
                "total_interactions": profile.total_interactions
            }
        }
        
        debug_log(2, f"[IdentityManager] 為身份 {identity_id} 注入工作上下文")
        return identity_context
        
    def extract_identity_from_context(self, context_data: Dict[str, Any]) -> Optional[str]:
        """從工作上下文中提取身份ID
        
        Args:
            context_data: 工作上下文資料
            
        Returns:
            Optional[str]: 身份ID，如果不存在則返回None
        """
        try:
            identity_data = context_data.get("identity", {})
            if identity_data and "id" in identity_data:
                return identity_data["id"]
                
            # 嘗試提取speaker_id並查找對應身份
            if "speaker_id" in identity_data:
                speaker_id = identity_data["speaker_id"]
                identity = self.get_identity_by_speaker(speaker_id)
                if identity:
                    return identity.identity_id
                    
            return None
            
        except Exception as e:
            error_log(f"[IdentityManager] 從上下文提取身份失敗: {e}")
            return None
    
    def _load_identities(self):
        """載入身份資料"""
        try:
            # 載入身份檔案
            identities_file = self.storage_path / "identities.json"
            if identities_file.exists():
                with open(identities_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for identity_data in data:
                        profile = UserProfile(**identity_data)
                        self.identities[profile.identity_id] = profile
            
            # 載入映射檔案
            mapping_file = self.storage_path / "speaker_mapping.json"
            if mapping_file.exists():
                with open(mapping_file, 'r', encoding='utf-8') as f:
                    self.speaker_to_identity = json.load(f)
                    
        except Exception as e:
            error_log(f"[IdentityManager] 載入身份資料失敗：{e}")
    
    def _save_identity(self, profile: UserProfile):
        """保存單個身份"""
        try:
            identities_file = self.storage_path / "identities.json"
            
            # 載入現有資料
            all_identities = []
            if identities_file.exists():
                with open(identities_file, 'r', encoding='utf-8') as f:
                    all_identities = json.load(f)
            
            # 更新或添加
            found = False
            for i, identity_data in enumerate(all_identities):
                if identity_data["identity_id"] == profile.identity_id:
                    all_identities[i] = profile.dict()
                    found = True
                    break
            
            if not found:
                all_identities.append(profile.dict())
            
            # 保存
            with open(identities_file, 'w', encoding='utf-8') as f:
                json.dump(all_identities, f, ensure_ascii=False, indent=2, default=str)
                
        except Exception as e:
            error_log(f"[IdentityManager] 保存身份失敗：{e}")
    
    def _save_mapping(self):
        """保存語者到身份的映射"""
        try:
            mapping_file = self.storage_path / "speaker_mapping.json"
            with open(mapping_file, 'w', encoding='utf-8') as f:
                json.dump(self.speaker_to_identity, f, ensure_ascii=False, indent=2)
        except Exception as e:
            error_log(f"[IdentityManager] 保存映射失敗：{e}")
