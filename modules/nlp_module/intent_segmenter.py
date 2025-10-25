"""
意圖分段器 - 整合 BIOS Tagger 實現多意圖分段

基於訓練好的 BIOS Tagger 模型，將使用者輸入分段為多個意圖片段。
支援 5 種意圖類型：CALL, CHAT, DIRECT_WORK, BACKGROUND_WORK, UNKNOWN
"""

from typing import List, Optional
from pathlib import Path
from modules.nlp_module.intent_types import IntentSegment, IntentType
from modules.nlp_module.bio_tagger import BIOTagger
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
        self.confidence_threshold = 0.7  # 置信度閾值
        
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
        
        # 使用 BIOS Tagger 預測
        segments_raw = self.bio_tagger.predict(text)
        
        if not segments_raw:
            debug_log(2, "[IntentSegmenter] BIOS Tagger 未返回分段，使用備用方案")
            return self._fallback_segment(text)
        
        # 轉換為 IntentSegment 格式
        intent_segments = []
        for seg in segments_raw:
            # 映射 intent 字符串到 IntentType
            intent_str = seg['intent'].upper()
            try:
                intent_type = IntentType[intent_str]
            except KeyError:
                debug_log(2, f"[IntentSegmenter] 未知意圖類型: {intent_str}，使用 UNKNOWN")
                intent_type = IntentType.UNKNOWN
            
            # 創建 IntentSegment
            intent_segment = IntentSegment(
                segment_text=seg['text'],
                intent_type=intent_type,
                confidence=seg.get('confidence', 0.9),
                metadata={
                    'start_pos': seg.get('start_pos', 0),
                    'end_pos': seg.get('end_pos', len(seg['text'])),
                    'model': 'bio_tagger'
                }
            )
            
            intent_segments.append(intent_segment)
        
        debug_log(2, f"[IntentSegmenter] 識別到 {len(intent_segments)} 個意圖分段")
        
        # 過濾低置信度分段
        filtered_segments = [
            seg for seg in intent_segments 
            if seg.confidence >= self.confidence_threshold
        ]
        
        if not filtered_segments:
            debug_log(2, "[IntentSegmenter] 所有分段置信度過低，使用備用方案")
            return self._fallback_segment(text)
        
        return filtered_segments
    
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

