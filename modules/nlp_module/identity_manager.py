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
    
    def get_identity_by_name(self, display_name: str) -> Optional[UserProfile]:
        """根據顯示名稱獲取使用者身份"""
        for profile in self.identities.values():
            if profile.display_name == display_name:
                return profile
        return None
    
    def create_identity(self, speaker_id: str, display_name: Optional[str] = None, 
                       force_new: bool = False) -> UserProfile:
        """為語者創建新的使用者身份
        
        Args:
            speaker_id: 語者ID
            display_name: 顯示名稱（可選）
            force_new: 是否強制創建新身份（即使名稱已存在）
        
        Returns:
            UserProfile: 創建的身份檔案
        
        注意：
            - 如果 speaker_id 已關聯到現有身份，會返回該身份
            - 如果 display_name 已存在且 force_new=False，會記錄警告但仍創建新身份
        """
        # 檢查該 speaker_id 是否已經有關聯的身份
        existing_identity = self.get_identity_by_speaker(speaker_id)
        if existing_identity:
            info_log(f"[IdentityManager] Speaker {speaker_id} 已關聯到身份 {existing_identity.identity_id}")
            return existing_identity
        
        # 檢查 display_name 是否已存在
        if display_name and not force_new:
            existing_by_name = self.get_identity_by_name(display_name)
            if existing_by_name:
                info_log(f"[IdentityManager] ⚠️  顯示名稱 '{display_name}' 已被 {existing_by_name.identity_id} 使用")
                info_log(f"[IdentityManager] 建議使用 get_or_create_identity() 或設置 force_new=True")
        
        # 使用 UUID 確保唯一性，避免不同 speaker_id 產生相同的 identity_id
        unique_id = uuid.uuid4().hex[:8]
        identity_id = f"user_{int(time.time())}_{unique_id}"
        
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
    
    def get_or_create_identity(self, speaker_id: str, display_name: Optional[str] = None) -> UserProfile:
        """獲取或創建使用者身份（智能處理重複）
        
        優先級：
        1. 如果 speaker_id 已關聯身份 → 返回該身份
        2. 如果 display_name 已存在 → 關聯到該身份並返回
        3. 否則 → 創建新身份
        
        Args:
            speaker_id: 語者ID
            display_name: 顯示名稱（可選）
        
        Returns:
            UserProfile: 身份檔案
        """
        # 1. 檢查 speaker_id 是否已關聯
        existing_by_speaker = self.get_identity_by_speaker(speaker_id)
        if existing_by_speaker:
            debug_log(2, f"[IdentityManager] Speaker {speaker_id} 已關聯到 {existing_by_speaker.identity_id}")
            return existing_by_speaker
        
        # 2. 檢查 display_name 是否已存在
        if display_name:
            existing_by_name = self.get_identity_by_name(display_name)
            if existing_by_name:
                # 將此 speaker 關聯到現有身份
                info_log(f"[IdentityManager] 將 Speaker {speaker_id} 關聯到現有身份 '{display_name}'")
                self.speaker_to_identity[speaker_id] = existing_by_name.identity_id
                
                # 如果該身份還沒有 speaker_id，設置它
                if not existing_by_name.speaker_id or existing_by_name.speaker_id == speaker_id:
                    existing_by_name.speaker_id = speaker_id
                    self._save_identity(existing_by_name)
                
                self._save_mapping()
                return existing_by_name
        
        # 3. 創建新身份
        return self.create_identity(speaker_id, display_name, force_new=True)
    
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
    
    # 🆕 Speaker 管理方法（Identity 為主，Speaker 為輔）
    
    def add_speaker_sample(self, identity_id: str, embedding: List[float], 
                          confidence: float, audio_duration: Optional[float] = None,
                          metadata: Optional[Dict[str, Any]] = None) -> bool:
        """添加語音樣本到指定 Identity
        
        Args:
            identity_id: Identity ID
            embedding: 語音特徵向量
            confidence: 樣本信心度
            audio_duration: 音頻長度（秒）
            metadata: 額外元數據
            
        Returns:
            bool: 是否添加成功
        """
        if identity_id not in self.identities:
            error_log(f"[IdentityManager] Identity {identity_id} 不存在")
            return False
        
        try:
            from .schemas import SpeakerSample
            profile = self.identities[identity_id]
            
            # 創建新樣本
            sample = SpeakerSample(
                embedding=embedding,
                confidence=confidence,
                audio_duration=audio_duration,
                metadata=metadata or {}
            )
            
            # 添加到 Identity 的 speaker_accumulation
            profile.speaker_accumulation.samples.append(sample)
            profile.speaker_accumulation.total_samples += 1
            profile.speaker_accumulation.last_updated = datetime.now()
            
            # 檢查是否達到確認閾值
            if (profile.speaker_accumulation.total_samples >= 
                profile.speaker_accumulation.min_samples_threshold):
                profile.speaker_accumulation.is_confirmed = True
                info_log(f"[IdentityManager] Identity {identity_id} 的 Speaker 已達到確認閾值")
            
            # 保存更新
            self._save_identity(profile)
            
            debug_log(3, f"[IdentityManager] 已添加 Speaker 樣本到 Identity {identity_id} "
                        f"(總數: {profile.speaker_accumulation.total_samples})")
            return True
            
        except Exception as e:
            error_log(f"[IdentityManager] 添加 Speaker 樣本失敗: {e}")
            return False
    
    def get_speaker_accumulation(self, identity_id: str) -> Optional[Dict[str, Any]]:
        """獲取 Identity 的 Speaker 累積數據
        
        Args:
            identity_id: Identity ID
            
        Returns:
            Optional[Dict]: Speaker 累積數據，包含樣本列表和統計信息
        """
        if identity_id not in self.identities:
            return None
        
        profile = self.identities[identity_id]
        accumulation = profile.speaker_accumulation
        
        return {
            "total_samples": accumulation.total_samples,
            "min_samples_threshold": accumulation.min_samples_threshold,
            "is_confirmed": accumulation.is_confirmed,
            "model_trained": accumulation.model_trained,
            "last_updated": accumulation.last_updated.isoformat() if accumulation.last_updated else None,
            "samples": [
                {
                    "confidence": s.confidence,
                    "timestamp": s.timestamp.isoformat(),
                    "audio_duration": s.audio_duration
                }
                for s in accumulation.samples
            ]
        }
    
    def update_speaker_model(self, identity_id: str, model_data: Dict[str, Any]) -> bool:
        """更新 Identity 的 Speaker 模型數據
        
        Args:
            identity_id: Identity ID
            model_data: 模型數據（如平均 embedding、協方差矩陣等）
            
        Returns:
            bool: 是否更新成功
        """
        if identity_id not in self.identities:
            return False
        
        try:
            profile = self.identities[identity_id]
            profile.speaker_accumulation.speaker_model = model_data
            profile.speaker_accumulation.model_trained = True
            profile.speaker_accumulation.last_updated = datetime.now()
            
            self._save_identity(profile)
            info_log(f"[IdentityManager] 已更新 Identity {identity_id} 的 Speaker 模型")
            return True
            
        except Exception as e:
            error_log(f"[IdentityManager] 更新 Speaker 模型失敗: {e}")
            return False
    
    def associate_speaker_to_identity(self, speaker_id: str, identity_id: str) -> bool:
        """將 Speaker ID 關聯到指定 Identity（用於主動聲明場景）
        
        Args:
            speaker_id: Speaker ID
            identity_id: Identity ID
            
        Returns:
            bool: 是否關聯成功
        """
        if identity_id not in self.identities:
            error_log(f"[IdentityManager] Identity {identity_id} 不存在")
            return False
        
        try:
            # 更新映射
            self.speaker_to_identity[speaker_id] = identity_id
            
            # 更新 Identity 的 speaker_id（向後兼容）
            profile = self.identities[identity_id]
            if not profile.speaker_id:
                profile.speaker_id = speaker_id
                self._save_identity(profile)
            
            # 保存映射
            self._save_mapping()
            
            info_log(f"[IdentityManager] 已關聯 Speaker {speaker_id} 到 Identity {identity_id}")
            return True
            
        except Exception as e:
            error_log(f"[IdentityManager] 關聯 Speaker 失敗: {e}")
            return False
    
    def get_identity_by_id(self, identity_id: str) -> Optional[UserProfile]:
        """根據 Identity ID 獲取用戶檔案
        
        Args:
            identity_id: Identity ID
            
        Returns:
            Optional[UserProfile]: 用戶檔案
        """
        return self.identities.get(identity_id)
    
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
