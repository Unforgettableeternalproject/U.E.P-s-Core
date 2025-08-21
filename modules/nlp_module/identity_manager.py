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
from typing import Dict, Optional, List, Any
from datetime import datetime
from pathlib import Path

from .schemas import (
    UserProfile, IdentityStatus, IdentityDecision, 
    NLPDecisionPackage
)
from core.working_context import ContextType, WorkingContext
from utils.debug_helper import debug_log, info_log, error_log


class IdentityDecisionHandler:
    """身份決策處理器 - 實現Working Context的決策處理協議"""
    
    def can_handle(self, context_type: ContextType) -> bool:
        """檢查是否可以處理指定類型的上下文"""
        return context_type == ContextType.SPEAKER_ACCUMULATION
    
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
        """應用身份決策結果"""
        try:
            action = decision.get("action")
            speaker_id = decision.get("speaker_id")
            
            if action == "create_identity":
                # 創建新的使用者身份
                identity_id = f"user_{int(time.time())}"
                info_log(f"[IdentityDecisionHandler] 為語者 {speaker_id} 創建身份 {identity_id}")
                
                # 這裡可以觸發身份創建流程
                # 實際實現會調用 IdentityManager.create_identity
                
            elif action == "continue_accumulation":
                info_log(f"[IdentityDecisionHandler] 語者 {speaker_id} 繼續樣本累積")
                
            elif action == "reset_accumulation":
                info_log(f"[IdentityDecisionHandler] 語者 {speaker_id} 重置樣本累積", "WARNING")
                
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
    
    def __init__(self, storage_path: str = "memory/identities"):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
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
        
        profile = UserProfile(
            identity_id=identity_id,
            speaker_id=speaker_id,
            display_name=display_name or f"User-{identity_id[:8]}",
            status=IdentityStatus.CONFIRMED,
            total_interactions=0,
            created_at=datetime.now()
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
            
            # 更新習慣和偏好（簡化版）
            if "command_type" in interaction_data:
                cmd_type = interaction_data["command_type"]
                if "system_habits" not in profile.system_habits:
                    profile.system_habits["command_usage"] = {}
                profile.system_habits["command_usage"][cmd_type] = \
                    profile.system_habits["command_usage"].get(cmd_type, 0) + 1
            
            # 保存更新
            self._save_identity(profile)
            
            debug_log(3, f"[IdentityManager] 更新身份 {identity_id} 互動記錄")
    
    def process_speaker_identification(self, speaker_id: str, speaker_status: str, 
                                     confidence: float) -> tuple[Optional[UserProfile], str]:
        """處理語者識別結果"""
        
        if speaker_status == "existing" and confidence > 0.8:
            # 已知語者，直接載入身份
            identity = self.get_identity_by_speaker(speaker_id)
            if identity:
                debug_log(2, f"[IdentityManager] 載入已知身份：{identity.identity_id}")
                return identity, "loaded"
            
        elif speaker_status == "new" or (speaker_status == "existing" and confidence <= 0.8):
            # 新語者或不確定的語者，需要累積樣本
            debug_log(2, f"[IdentityManager] 語者 {speaker_id} 需要樣本累積")
            return None, "accumulating"
            
        # 未知情況
        debug_log(2, f"[IdentityManager] 語者 {speaker_id} 狀態未知")
        return None, "unknown"
    
    def get_decision_handler(self) -> IdentityDecisionHandler:
        """獲取決策處理器"""
        return self.decision_handler
    
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
