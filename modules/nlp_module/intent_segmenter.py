"""
意圖分段器 - 整合 BIOS Tagger 實現多意圖分段

基於訓練好的 BIOS Tagger 模型，將使用者輸入分段為多個意圖片段。
支援 4 種意圖類型：CALL, CHAT, WORK (含 work_mode metadata), UNKNOWN
"""

from typing import List, Optional
from pathlib import Path
from modules.nlp_module.intent_types import IntentSegment, IntentType
from modules.nlp_module.bio_tagger import BIOTagger
from modules.nlp_module.intent_post_processor import IntentPostProcessor
from modules.nlp_module.workflow_validator import WorkflowValidator
from utils.debug_helper import debug_log, info_log, error_log


class IntentSegmenter:
    """
    意圖分段器 - 基於 BIOS Tagger 的多意圖分段實現
    """
    
    def __init__(self, model_path: Optional[str] = None):
        """
        初始化意圖分段器
        
        Args:
            model_path: BIOS Tagger 模型路徑，若為 None 則使用預設路徑
        """
        self.bio_tagger = BIOTagger()
        self.model_loaded = False
        self.confidence_threshold = 0.7  # 置信度閖值
        
        # 初始化後處理器和驗證器
        self.post_processor = IntentPostProcessor()
        self.workflow_validator = WorkflowValidator()
        
        # 預設模型路徑
        if model_path is None:
            model_path = str(Path(__file__).parent.parent.parent / "models" / "nlp" / "bio_tagger")
        
        # 嘗試載入模型
        if self.load_model(model_path):
            info_log("[IntentSegmenter] BIOS Tagger 模型載入成功")
        else:
            error_log("[IntentSegmenter] BIOS Tagger 模型載入失敗，將使用備用分段")
    
    def segment_intents(self, text: str) -> List[IntentSegment]:
        """
        將輸入文本分段為多個意圖
        
        Args:
            text: 使用者輸入文本
            
        Returns:
            List[IntentSegment]: 意圖分段列表
        """
        debug_log(3, f"[IntentSegmenter] 分段輸入: {text[:50]}...")
        
        # Step 1: 使用 BIOS Tagger 預測
        segments_raw = self.bio_tagger.predict(text)
        
        if not segments_raw:
            debug_log(2, "[IntentSegmenter] BIOS Tagger 未返回分段，使用備用方案")
            return self._fallback_segment(text)
        
        debug_log(3, f"[IntentSegmenter] BIO Tagger 初次分段: {len(segments_raw)} 個分段")
        
        # Step 2: 後處理 - 合併與交叉比對
        segments_processed = self.post_processor.process(segments_raw, text)
        debug_log(3, f"[IntentSegmenter] 後處理後: {len(segments_processed)} 個分段")
        
        # Step 3: WORK 意圖校驗
        segments_validated = self.workflow_validator.validate(segments_processed)
        debug_log(3, f"[IntentSegmenter] 校驗後: {len(segments_validated)} 個分段")
        
        # Step 4: 轉換為 IntentSegment 格式
        intent_segments = []
        
        # Intent 映射（將 direct_work/background_work 統一為 WORK）
        intent_mapping = {
            'call': IntentType.CALL,
            'chat': IntentType.CHAT,
            'direct_work': IntentType.WORK,
            'background_work': IntentType.WORK,
            'unknown': IntentType.UNKNOWN
        }
        
        # work_mode 映射（區分 WORK 的執行模式）
        work_mode_mapping = {
            'direct_work': 'direct',
            'background_work': 'background'
        }
        
        for seg in segments_validated:
            # 映射 intent 字符串到 IntentType
            intent_str = seg['intent'].lower()
            intent_type = intent_mapping.get(intent_str, IntentType.UNKNOWN)
            
            if intent_type == IntentType.UNKNOWN and intent_str not in intent_mapping:
                debug_log(2, f"[IntentSegmenter] 未知意圖類型: {intent_str}，使用 UNKNOWN")
            
            # 構建 metadata（保留 WorkflowValidator 添加的標記）
            metadata = seg.get('metadata', {}).copy()  # 保留現有 metadata
            metadata.update({
                'start_pos': seg.get('start_pos', 0),
                'end_pos': seg.get('end_pos', len(seg['text'])),
                'model': 'bio_tagger'
            })
            
            # 如果是 WORK 類型，添加 work_mode
            work_mode = work_mode_mapping.get(intent_str)
            if work_mode:
                metadata['work_mode'] = work_mode
            
            # 創建 IntentSegment
            intent_segment = IntentSegment(
                segment_text=seg['text'],
                intent_type=intent_type,
                confidence=seg.get('confidence', 0.9),
                metadata=metadata
            )
            
            intent_segments.append(intent_segment)
        
        debug_log(2, f"[IntentSegmenter] 識別到 {len(intent_segments)} 個意圖分段")
        
        # 注意：不再使用固定的 confidence_threshold 過濾
        # 因為 WorkflowValidator 已經進行過驗證和調整
        # 如果 validator 認為分段應該被轉為 CHAT（即使 confidence 降低），仍然應該保留
        
        if not intent_segments:
            debug_log(2, "[IntentSegmenter] 沒有識別到任何分段，使用備用方案")
            return self._fallback_segment(text)
        
        return intent_segments
    
    def _fallback_segment(self, text: str) -> List[IntentSegment]:
        """
        備用分段方案 - 當模型失敗時使用
        
        Args:
            text: 輸入文本
            
        Returns:
            List[IntentSegment]: 單一 UNKNOWN 分段
        """
        return [IntentSegment(
            segment_text=text,
            intent_type=IntentType.UNKNOWN,
            confidence=0.5,
            metadata={"fallback": True}
        )]
    
    def load_model(self, model_path: str) -> bool:
        """
        載入 BIOS Tagger 模型
        
        Args:
            model_path: 模型檔案路徑
            
        Returns:
            bool: 是否成功載入
        """
        debug_log(2, f"[IntentSegmenter] 載入模型: {model_path}")
        
        try:
            model_path_obj = Path(model_path)
            if not model_path_obj.exists():
                error_log(f"[IntentSegmenter] 模型路徑不存在: {model_path}")
                return False
            
            # 載入 BIOS Tagger 模型
            self.model_loaded = self.bio_tagger.load_model(model_path)
            
            if self.model_loaded:
                info_log(f"[IntentSegmenter] 模型載入成功: {model_path}")
            else:
                error_log(f"[IntentSegmenter] 模型載入失敗: {model_path}")
            
            return self.model_loaded
            
        except Exception as e:
            error_log(f"[IntentSegmenter] 模型載入異常: {e}")
            return False
        
    def update_confidence_threshold(self, threshold: float):
        """
        更新置信度閾值
        
        Args:
            threshold: 置信度閾值（0.0-1.0）
        """
        if 0.0 <= threshold <= 1.0:
            self.confidence_threshold = threshold
            debug_log(2, f"[IntentSegmenter] 置信度閾值更新為: {threshold}")
        else:
            error_log(f"[IntentSegmenter] 無效的置信度閾值: {threshold}")


# 全局實例（延遲初始化）
_intent_segmenter: Optional[IntentSegmenter] = None


def get_intent_segmenter() -> IntentSegmenter:
    """
    獲取全局 IntentSegmenter 實例（單例模式）
    
    Returns:
        IntentSegmenter: 全局意圖分段器實例
    """
    global _intent_segmenter
    if _intent_segmenter is None:
        _intent_segmenter = IntentSegmenter()
    return _intent_segmenter


def segment_user_input(text: str) -> List[IntentSegment]:
    """
    便利函數：分段使用者輸入
    
    Args:
        text: 使用者輸入文本
        
    Returns:
        List[IntentSegment]: 意圖分段列表
    """
    return get_intent_segmenter().segment_intents(text)

