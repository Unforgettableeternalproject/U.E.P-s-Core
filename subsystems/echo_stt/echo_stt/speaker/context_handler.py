"""
語者識別上下文決策處理器

從 U.E.P Core 遷出：
- 移除 from core.working_context import DecisionHandler, ContextType
- 本地定義 ContextType enum
- 替換 debug_helper → logging
- __init__ 直接接收 speaker_identification 參數（不再依賴 stt_module）
"""

import logging
import time
from enum import Enum
from typing import Dict, Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  本地 Protocol 與 Enum（替代 core.working_context）
# ──────────────────────────────────────────────

class ContextType(Enum):
    """上下文類型（本地精簡版）"""
    SPEAKER_ACCUMULATION = "speaker_accumulation"


@runtime_checkable
class ContextManagerProtocol(Protocol):
    """Working Context Manager 的最小介面契約。

    讓 speaker 模組可在不依賴 Core 的情況下
    可選地注入 context manager。
    """

    def add_data_to_context(
        self,
        context_type: Any,
        data_item: Any,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]: ...

    def get_context_status(self, context_id: str) -> Optional[Dict[str, Any]]: ...

    def get_all_contexts_info(self) -> list: ...


# ──────────────────────────────────────────────
#  SpeakerContextHandler
# ──────────────────────────────────────────────

class SpeakerContextHandler:
    """語者識別上下文決策處理器"""

    def __init__(self, speaker_identification: Any):
        """初始化處理器

        Args:
            speaker_identification: SpeakerIdentification 實例
        """
        self.speaker_identification = speaker_identification
        self.min_samples_for_speaker = 15

    def can_handle(self, context_type: ContextType) -> bool:
        """檢查是否可以處理指定類型的上下文"""
        return context_type == ContextType.SPEAKER_ACCUMULATION

    def is_context_worth_keeping(self, context_data: Dict[str, Any]) -> bool:
        """評估上下文是否值得保留"""
        embeddings = context_data.get('data', [])
        return len(embeddings) >= self.min_samples_for_speaker

    def make_decision(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """進行語者識別決策

        Args:
            context_data: 上下文數據包

        Returns:
            決策結果字典
        """
        try:
            embeddings = context_data.get('data', [])
            context_id = context_data.get('context_id')

            logger.debug(
                "[SpeakerContextHandler] 開始語者決策: %s, 樣本數: %d",
                context_id, len(embeddings),
            )

            best_match = self.speaker_identification._find_best_speaker_match(embeddings)

            if best_match:
                speaker_id, similarity = best_match

                if similarity > 0.8:
                    return {
                        'success': True,
                        'decision_type': 'auto_assign',
                        'speaker_id': speaker_id,
                        'similarity': similarity,
                        'result_id': speaker_id,
                        'message': f'自動歸類到說話人 {speaker_id} (相似度: {similarity:.3f})',
                    }
                else:
                    return {
                        'success': False,
                        'decision_type': 'confirm_needed',
                        'speaker_id': speaker_id,
                        'similarity': similarity,
                        'message': f'檢測到可能匹配的說話人 {speaker_id} (相似度: {similarity:.3f})，請確認',
                        'options': [
                            f'是，歸類到 {speaker_id}',
                            '不是，創建新說話人',
                        ],
                    }
            else:
                return {
                    'success': False,
                    'decision_type': 'new_speaker_suggested',
                    'message': '未找到匹配的現有說話人，建議創建新說話人',
                    'options': ['創建新說話人', '手動指定現有說話人'],
                }

        except Exception as e:
            logger.error("[SpeakerContextHandler] 決策失敗: %s", e)
            return {
                'success': False,
                'decision_type': 'error',
                'message': f'決策處理錯誤: {e}',
            }

    def apply_decision(self, context_data: Dict[str, Any],
                       decision: Dict[str, Any]) -> bool:
        """應用決策結果

        Args:
            context_data: 上下文數據包
            decision: 決策結果或用戶回應

        Returns:
            是否成功應用決策
        """
        try:
            embeddings = context_data.get('data', [])

            if decision.get('decision_type') == 'auto_assign':
                speaker_id = decision.get('speaker_id')
                return self._assign_to_speaker(embeddings, speaker_id)  # type: ignore

            user_response = decision.get('user_response', decision)
            response_text = str(user_response).lower()

            if '創建新說話人' in str(user_response) or 'new' in response_text:
                return self._create_new_speaker(embeddings)
            elif '歸類' in str(user_response) or 'assign' in response_text:
                speaker_id = decision.get('speaker_id')
                if speaker_id:
                    return self._assign_to_speaker(embeddings, speaker_id)

            return self._create_new_speaker(embeddings)

        except Exception as e:
            logger.error("[SpeakerContextHandler] 應用決策失敗: %s", e)
            return False

    def _assign_to_speaker(self, embeddings: list, speaker_id: str) -> bool:
        """將嵌入分配給現有說話人"""
        try:
            if speaker_id in self.speaker_identification.speaker_database:
                self.speaker_identification.speaker_database[speaker_id]['embeddings'].extend(embeddings)
                self.speaker_identification.speaker_database[speaker_id]['metadata']['last_seen'] = time.time()
                self.speaker_identification.speaker_database[speaker_id]['metadata']['sample_count'] += len(embeddings)
                self.speaker_identification._save_speaker_database()
                logger.info("[SpeakerContextHandler] 樣本已分配給說話人: %s", speaker_id)
                return True
            else:
                logger.error("[SpeakerContextHandler] 說話人不存在: %s", speaker_id)
                return False

        except Exception as e:
            logger.error("[SpeakerContextHandler] 分配失敗: %s", e)
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
                    'method': 'context_decision',
                },
            }
            self.speaker_identification._save_speaker_database()
            logger.info("[SpeakerContextHandler] 創建新說話人: %s", new_speaker_id)
            return True

        except Exception as e:
            logger.error("[SpeakerContextHandler] 創建新說話人失敗: %s", e)
            return False


def create_speaker_context_handler(speaker_identification: Any) -> SpeakerContextHandler:
    """工廠函數：創建語者上下文處理器"""
    return SpeakerContextHandler(speaker_identification)
