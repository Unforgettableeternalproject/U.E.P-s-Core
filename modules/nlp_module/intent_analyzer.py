#!/usr/bin/env python3
"""
意圖分析器
集成BIO標註器和多意圖上下文管理
"""
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

# 添加項目根目錄到路徑
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from modules.nlp_module.bio_tagger import BIOTagger
from modules.nlp_module.multi_intent_context import (
    get_multi_intent_context_manager, IntentContext, ContextType
)
from modules.nlp_module.schemas import IntentType, IntentSegment
from utils.debug_helper import debug_log, info_log, error_log

class IntentAnalyzer:
    """意圖分析器 - 基於BIO標註和上下文管理"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # BIO標註器
        self.bio_tagger = BIOTagger()
        self.bio_model_path = config.get('bio_model_path', './models/nlp/bio_tagger')
        
        # 多意圖上下文管理器
        self.context_manager = get_multi_intent_context_manager()
        
        # 配置選項
        self.enable_segmentation = config.get('enable_segmentation', True)
        self.max_segments = config.get('max_segments', 5)
        self.min_segment_length = config.get('min_segment_length', 3)
        
        # 意圖映射
        self.bio_to_intent_map = {
            'CALL': IntentType.CALL,
            'CHAT': IntentType.CHAT,
            'COMMAND': IntentType.COMMAND
        }
        
        info_log("[IntentAnalyzer] 意圖分析器初始化")
    
    def initialize(self) -> bool:
        """初始化分析器"""
        try:
            info_log("[IntentAnalyzer] 初始化中...")
            
            # 載入BIO標註器
            if not self.bio_tagger.load_model(self.bio_model_path):
                error_log(f"[IntentAnalyzer] BIO模型載入失敗: {self.bio_model_path}")
                return False
            
            info_log("[IntentAnalyzer] BIO標註器載入成功")
            return True
            
        except Exception as e:
            error_log(f"[IntentAnalyzer] 初始化失敗: {e}")
            return False
    
    def analyze_intent(self, text: str, enable_segmentation: bool = True,
                      context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """分析文本意圖"""
        result = {
            "primary_intent": IntentType.UNKNOWN,
            "intent_segments": [],
            "overall_confidence": 0.0,
            "entities": [],
            "state_transition": None,
            "context_ids": [],  # 新增：關聯的上下文ID
            "execution_plan": []  # 新增：執行計劃
        }
        
        try:
            debug_log(2, f"[IntentAnalyzer] 分析文本: '{text[:50]}...'")
            
            # 使用BIO標註器進行分段和意圖識別
            bio_segments = self.bio_tagger.predict(text)
            
            if not bio_segments:
                debug_log(2, "[IntentAnalyzer] BIO標註器未返回分段結果")
                return result
            
            # 轉換BIO結果為IntentSegment格式
            intent_segments = self._convert_bio_to_intent_segments(bio_segments, text)
            
            # 後處理優化分段結果
            intent_segments = self._post_process_segments(intent_segments, text)
            
            result["intent_segments"] = intent_segments
            
            # 決定主要意圖
            result["primary_intent"] = self._determine_primary_intent(intent_segments)
            result["overall_confidence"] = self._calculate_overall_confidence(intent_segments)
            
            # 創建多意圖上下文
            contexts = self.context_manager.create_contexts_from_segments(
                [self._segment_to_dict(seg) for seg in intent_segments], text
            )
            
            # 添加上下文到佇列
            context_ids = self.context_manager.add_contexts_to_queue(contexts)
            result["context_ids"] = context_ids
            
            # 生成執行計劃
            result["execution_plan"] = self._generate_execution_plan(contexts)
            
            # 建議狀態轉換（基於下一個可執行的上下文）
            next_context = self.context_manager.get_next_executable_context()
            if next_context:
                result["state_transition"] = {
                    "from_state": "processing",
                    "to_state": "waiting_for_execution",
                    "reason": "有待執行的意圖上下文",
                    "confidence": result["overall_confidence"],
                    "context_id": next_context[1].context_id
                }
            
            info_log(f"[IntentAnalyzer] 分析完成: 主要意圖={result['primary_intent']}, "
                    f"分段數={len(intent_segments)}, 上下文數={len(contexts)}")
            
            return result
            
        except Exception as e:
            error_log(f"[IntentAnalyzer] 意圖分析失敗: {e}")
            return result
    
    def _convert_bio_to_intent_segments(self, bio_segments: List[Dict[str, Any]], 
                                      original_text: str) -> List[IntentSegment]:
        """將BIO分段結果轉換為IntentSegment格式"""
        intent_segments = []
        
        for segment in bio_segments:
            intent_type = self.bio_to_intent_map.get(
                segment['intent'].upper(), 
                IntentType.UNKNOWN
            )
            
            # 創建IntentSegment
            intent_segment = IntentSegment(
                text=segment['text'],
                intent=intent_type,
                confidence=segment.get('confidence', 0.9),
                start_pos=segment['start_pos'],
                end_pos=segment['end_pos'],
                entities=[]
            )
            
            intent_segments.append(intent_segment)
        
        return intent_segments
    
    def _segment_to_dict(self, segment: IntentSegment) -> Dict[str, Any]:
        """將IntentSegment轉換為字典格式"""
        return {
            'text': segment.text,
            'intent': segment.intent.value if hasattr(segment.intent, 'value') else str(segment.intent),
            'start': segment.start_pos,
            'end': segment.end_pos,
            'confidence': segment.confidence
        }
    
    def _determine_primary_intent(self, segments: List[IntentSegment]) -> IntentType:
        """決定主要意圖"""
        if not segments:
            return IntentType.UNKNOWN
        
        # 統計各意圖類型
        intent_counts = {}
        intent_confidences = {}
        
        for segment in segments:
            intent = segment.intent
            if intent not in intent_counts:
                intent_counts[intent] = 0
                intent_confidences[intent] = []
            
            intent_counts[intent] += 1
            intent_confidences[intent].append(segment.confidence)
        
        # 如果有多種意圖，標記為複合意圖
        if len(intent_counts) > 1:
            return IntentType.COMPOUND
        
        # 如果只有一種意圖，返回該意圖
        if len(intent_counts) == 1:
            return list(intent_counts.keys())[0]
        
        return IntentType.UNKNOWN
    
    def _calculate_overall_confidence(self, segments: List[IntentSegment]) -> float:
        """計算整體信心度"""
        if not segments:
            return 0.0
        
        confidences = [seg.confidence for seg in segments]
        return sum(confidences) / len(confidences)
    
    def _generate_execution_plan(self, contexts: List[IntentContext]) -> List[Dict[str, Any]]:
        """生成執行計劃"""
        plan = []
        
        # 按優先級排序
        sorted_contexts = sorted(contexts, key=lambda c: c.priority)
        
        for i, context in enumerate(sorted_contexts):
            plan_item = {
                "step": i + 1,
                "context_id": context.context_id,
                "action_type": context.context_type.value,
                "description": context.task_description or context.conversation_topic,
                "priority": context.priority,
                "dependencies": list(context.depends_on),
                "estimated_execution_order": self._estimate_execution_order(context, sorted_contexts)
            }
            plan.append(plan_item)
        
        return plan
    
    def _estimate_execution_order(self, context: IntentContext, 
                                all_contexts: List[IntentContext]) -> int:
        """估計執行順序"""
        order = 1
        
        # 計算有多少個上下文會在這個之前執行
        for other_context in all_contexts:
            if (other_context.priority < context.priority or 
                context.context_id in other_context.blocks):
                order += 1
        
        return order
    
    def _post_process_segments(self, segments: List[IntentSegment], 
                              original_text: str) -> List[IntentSegment]:
        """後處理分段結果以提高準確率"""
        if not segments:
            return segments
            
        debug_log(2, f"[PostProcess] 原始分段數: {len(segments)}")
        
        # 連接詞列表
        connective_words = {
            'then', 'and', 'also', 'plus', 'after', 'before', 
            'next', 'finally', 'additionally', 'furthermore'
        }
        
        # 應用改進規則
        improved_segments = segments.copy()
        improved_segments = self._merge_connectives(improved_segments, connective_words)
        improved_segments = self._merge_short_segments(improved_segments)
        improved_segments = self._merge_context_related(improved_segments)
        
        debug_log(2, f"[PostProcess] 改進後分段數: {len(improved_segments)}")
        
        return improved_segments
    
    def _merge_connectives(self, segments: List[IntentSegment], 
                          connective_words: set) -> List[IntentSegment]:
        """合併連接詞到前一個分段"""
        if len(segments) <= 1:
            return segments
            
        result = []
        
        for current in segments:
            # 檢查是否是連接詞
            if (current.text.strip().lower() in connective_words and 
                len(result) > 0 and 
                result[-1].intent == IntentType.COMMAND):
                
                # 合併到前一個分段
                last = result[-1]
                merged_text = f"{last.text} {current.text}"
                merged_segment = IntentSegment(
                    text=merged_text,
                    intent=last.intent,
                    confidence=(last.confidence + current.confidence) / 2,
                    start_pos=last.start_pos,
                    end_pos=current.end_pos,
                    entities=last.entities + current.entities
                )
                result[-1] = merged_segment
                debug_log(3, f"[PostProcess] 合併連接詞: '{current.text}' -> '{merged_text}'")
            else:
                result.append(current)
        
        return result
    
    def _merge_short_segments(self, segments: List[IntentSegment]) -> List[IntentSegment]:
        """合併過短的分段"""
        min_segment_length = 3
        if len(segments) <= 1:
            return segments
            
        result = []
        
        for segment in segments:
            if (len(segment.text.strip()) < min_segment_length and 
                len(result) > 0 and 
                result[-1].intent == segment.intent):
                
                # 合併到前一個分段
                last = result[-1]
                merged_text = f"{last.text} {segment.text}"
                merged_segment = IntentSegment(
                    text=merged_text,
                    intent=last.intent,
                    confidence=(last.confidence + segment.confidence) / 2,
                    start_pos=last.start_pos,
                    end_pos=segment.end_pos,
                    entities=last.entities + segment.entities
                )
                result[-1] = merged_segment
                debug_log(3, f"[PostProcess] 合併短分段: '{segment.text}' -> '{merged_text}'")
            else:
                result.append(segment)
        
        return result
    
    def _merge_context_related(self, segments: List[IntentSegment]) -> List[IntentSegment]:
        """合併上下文相關的分段"""
        if len(segments) <= 1:
            return segments
            
        result = []
        
        for segment in segments:
            should_merge = False
            
            if (len(result) > 0 and 
                segment.intent == result[-1].intent and
                segment.intent == IntentType.COMMAND):
                
                # 檢查是否是相關的命令片段
                should_merge = True
            
            if should_merge:
                last = result[-1]
                merged_text = f"{last.text} {segment.text}"
                merged_segment = IntentSegment(
                    text=merged_text,
                    intent=last.intent,
                    confidence=(last.confidence + segment.confidence) / 2,
                    start_pos=last.start_pos,
                    end_pos=segment.end_pos,
                    entities=last.entities + segment.entities
                )
                result[-1] = merged_segment
                debug_log(3, f"[PostProcess] 合併相關分段: '{segment.text}' -> '{merged_text}'")
            else:
                result.append(segment)
        
        return result
    
    def get_context_summary(self) -> Dict[str, Any]:
        """獲取上下文管理摘要"""
        return self.context_manager.get_context_summary()
    
    def mark_context_completed(self, context_id: str, success: bool = True):
        """標記上下文完成"""
        self.context_manager.mark_context_completed(context_id, success)
    
    def get_next_context(self) -> Optional[Tuple[Any, IntentContext]]:
        """獲取下一個可執行的上下文"""
        return self.context_manager.get_next_executable_context()

def test_intent_analyzer():
    """測試意圖分析器"""
    config = {
        'bio_model_path': '../../models/nlp/bio_tagger',
        'enable_segmentation': True,
        'max_segments': 5
    }
    
    analyzer = IntentAnalyzer(config)
    
    if not analyzer.initialize():
        error_log("分析器初始化失敗")
        return
    
    # 測試案例
    test_cases = [
        "Hello UEP, how's the weather today?",
        "I had a great day. Can you help me organize my photos?",
        "System wake up, the movie was interesting, please save my work",
        "Set a reminder for tomorrow and play some music"
    ]
    
    for text in test_cases:
        info_log(f"\n測試: '{text}'")
        result = analyzer.analyze_intent(text)
        
        info_log(f"主要意圖: {result['primary_intent']}")
        info_log(f"信心度: {result['overall_confidence']:.3f}")
        info_log(f"分段數: {len(result['intent_segments'])}")
        info_log(f"上下文數: {len(result['context_ids'])}")
        
        for i, segment in enumerate(result['intent_segments'], 1):
            info_log(f"  分段{i}: '{segment.text}' -> {segment.intent}")
        
        if result['execution_plan']:
            info_log("執行計劃:")
            for plan_item in result['execution_plan']:
                info_log(f"  步驟{plan_item['step']}: {plan_item['description']}")

if __name__ == "__main__":
    test_intent_analyzer()
