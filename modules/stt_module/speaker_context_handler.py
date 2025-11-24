# modules/stt_module/speaker_context_handler.py
"""
語者識別上下文決策處理器
實現 DecisionHandler 協議，專門處理語者識別相關的上下文決策
"""

from typing import Dict, Any, Optional, Tuple
from core.working_context import DecisionHandler, ContextType
from utils.debug_helper import debug_log, info_log, error_log
import time


class SpeakerContextHandler:
    """語者識別上下文決策處理器"""
    
    def __init__(self, stt_module):
        """
        初始化處理器
        
        Args:
            stt_module: STT 模組實例
        """
        self.stt_module = stt_module
        self.speaker_identification = stt_module.speaker_module
        self.min_samples_for_speaker = 15  # 建立說話人所需的最小樣本數
    
    def can_handle(self, context_type: ContextType) -> bool:
        """檢查是否可以處理指定類型的上下文"""
        return context_type == ContextType.SPEAKER_ACCUMULATION
    
    def is_context_worth_keeping(self, context_data: Dict[str, Any]) -> bool:
        """
        評估上下文是否值得保留
        
        Args:
            context_data: 上下文數據
            
        Returns:
            如果樣本數足夠建立可信的說話人，返回 True
        """
        embeddings = context_data.get('data', [])
        return len(embeddings) >= self.min_samples_for_speaker
    
    def make_decision(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        進行語者識別決策
        
        Args:
            context_data: 上下文數據包
            
        Returns:
            決策結果字典
        """
        try:
            embeddings = context_data.get('data', [])
            context_id = context_data.get('context_id')
            
            debug_log(3, f"[SpeakerContextHandler] 開始語者決策: {context_id}, 樣本數: {len(embeddings)}")
            
            # 嘗試找到最佳匹配的說話人
            best_match = self.speaker_identification._find_best_speaker_match(embeddings)
            
            if best_match:
                speaker_id, similarity = best_match
                
                # 檢查相似度是否足夠高
                if similarity > 0.8:  # 高信心度閾值
                    return {
                        'success': True,
                        'decision_type': 'auto_assign',
                        'speaker_id': speaker_id,
                        'similarity': similarity,
                        'result_id': speaker_id,
                        'message': f'自動歸類到說話人 {speaker_id} (相似度: {similarity:.3f})'
                    }
                else:
                    # 中等信心度，需要確認
                    return {
                        'success': False,
                        'decision_type': 'confirm_needed',
                        'speaker_id': speaker_id,
                        'similarity': similarity,
                        'message': f'檢測到可能匹配的說話人 {speaker_id} (相似度: {similarity:.3f})，請確認',
                        'options': [
                            f'是，歸類到 {speaker_id}',
                            '不是，創建新說話人'
                        ]
                    }
            else:
                # 沒有找到匹配，建議創建新說話人
                return {
                    'success': False,
                    'decision_type': 'new_speaker_suggested',
                    'message': '未找到匹配的現有說話人，建議創建新說話人',
                    'options': [
                        '創建新說話人',
                        '手動指定現有說話人'
                    ]
                }
                
        except Exception as e:
            error_log(f"[SpeakerContextHandler] 決策失敗: {e}")
            return {
                'success': False,
                'decision_type': 'error',
                'message': f'決策處理錯誤: {str(e)}'
            }
    
    def apply_decision(self, context_data: Dict[str, Any], decision: Dict[str, Any]) -> bool:
        """
        應用決策結果
        
        Args:
            context_data: 上下文數據包
            decision: 決策結果或用戶回應
            
        Returns:
            是否成功應用決策
        """
        try:
            embeddings = context_data.get('data', [])
            context_id = context_data.get('context_id')
            
            # 處理自動決策
            if decision.get('decision_type') == 'auto_assign':
                speaker_id = decision.get('speaker_id')
                return self._assign_to_speaker(embeddings, speaker_id)  # type: ignore
            
            # 處理用戶回應
            user_response = decision.get('user_response', decision)
            response_text = str(user_response).lower()
            
            if '創建新說話人' in str(user_response) or 'new' in response_text:
                return self._create_new_speaker(embeddings)
            elif '歸類' in str(user_response) or 'assign' in response_text:
                # 從決策中提取說話人 ID
                speaker_id = decision.get('speaker_id')
                if speaker_id:
                    return self._assign_to_speaker(embeddings, speaker_id)
            
            # 如果無法確定，創建新說話人
            return self._create_new_speaker(embeddings)
            
        except Exception as e:
            error_log(f"[SpeakerContextHandler] 應用決策失敗: {e}")
            return False
    
    def _assign_to_speaker(self, embeddings: list, speaker_id: str) -> bool:
        """將嵌入分配給現有說話人"""
        try:
            if speaker_id in self.speaker_identification.speaker_database:
                # 添加到現有說話人
                self.speaker_identification.speaker_database[speaker_id]['embeddings'].extend(embeddings)
                self.speaker_identification.speaker_database[speaker_id]['metadata']['last_seen'] = time.time()
                self.speaker_identification.speaker_database[speaker_id]['metadata']['sample_count'] += len(embeddings)
                
                # 保存資料庫
                self.speaker_identification._save_speaker_database()
                
                info_log(f"[SpeakerContextHandler] 樣本已分配給說話人: {speaker_id}")
                return True
            else:
                error_log(f"[SpeakerContextHandler] 說話人不存在: {speaker_id}")
                return False
                
        except Exception as e:
            error_log(f"[SpeakerContextHandler] 分配失敗: {e}")
            return False
    
    def _create_new_speaker(self, embeddings: list) -> bool:
        """創建新說話人"""
        try:
            new_speaker_id = f"speaker_{self.speaker_identification.speaker_counter:03d}"
            self.speaker_identification.speaker_counter += 1
            
            self.speaker_identification.speaker_database[new_speaker_id] = {
                'embeddings': embeddings,
                'metadata': {
                    'created_at': time.time(),
                    'last_seen': time.time(),
                    'sample_count': len(embeddings),
                    'method': 'context_decision'
                }
            }
            
            # 保存資料庫
            self.speaker_identification._save_speaker_database()
            
            info_log(f"[SpeakerContextHandler] 創建新說話人: {new_speaker_id}")
            return True
            
        except Exception as e:
            error_log(f"[SpeakerContextHandler] 創建新說話人失敗: {e}")
            return False


def create_speaker_context_handler(speaker_module) -> SpeakerContextHandler:
    """工廠函數：創建語者上下文處理器"""
    return SpeakerContextHandler(speaker_module)
