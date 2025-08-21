# modules/nlp_module/intent_analyzer.py
"""
意圖分析器 - 支援分段標籤和複合指令分析

這個模組負責：
1. 將文本分段並分析各段意圖
2. 支援新的 call 類型意圖
3. 實體抽取和上下文分析
4. 系統狀態轉換建議
"""

import re
import torch
from typing import List, Dict, Any, Tuple, Optional
from transformers import (
    DistilBertForSequenceClassification, 
    DistilBertTokenizer,
    AutoTokenizer,
    AutoModelForTokenClassification,
    pipeline
)

from .schemas import (
    IntentType, IntentSegment, Entity, EntityType,
    SystemStateTransition
)
from utils.debug_helper import debug_log, info_log, error_log


class IntentAnalyzer:
    """意圖分析器 - 支援分段分析"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 意圖分類模型配置
        self.intent_model_dir = config.get("intent_model_dir", "./models/nlp/command_chat_classifier")
        self.entity_model_name = config.get("entity_model_name", "ckiplab/bert-base-chinese-ner")
        
        # 模型和分詞器
        self.intent_model = None
        self.intent_tokenizer = None
        self.entity_pipeline = None
        
        # 標籤映射 - 擴展支援call類型
        self.intent_mapping = {
            0: IntentType.COMMAND,
            1: IntentType.CHAT, 
            2: IntentType.NON_SENSE,
            3: IntentType.CALL      # 新增call類型
        }
        
        # 狀態轉換規則
        self.state_transition_rules = {
            IntentType.CALL: {"target": "IDLE", "reason": "使用者呼叫UEP"},
            IntentType.CHAT: {"target": "CHAT", "reason": "進入聊天模式"},
            IntentType.COMMAND: {"target": "WORK", "reason": "執行系統指令"},
            IntentType.COMPOUND: {"target": "WORK", "reason": "執行複合指令"}
        }
        
        # 文本分段模式
        self.segmentation_patterns = [
            r'[.!?。！？]+\s*',  # 句號分段
            r'[,，]\s*(?=(?:請|可以|幫我|然後))',  # 連接詞分段
            r'\s+(?=(?:接著|然後|還有|另外|最後))',  # 序列詞分段
        ]
    
    def initialize(self):
        """初始化分析器"""
        try:
            info_log("[IntentAnalyzer] 初始化意圖分析器...")
            
            # 載入意圖分類模型
            if os.path.exists(self.intent_model_dir):
                self.intent_model = DistilBertForSequenceClassification.from_pretrained(
                    self.intent_model_dir
                )
                self.intent_tokenizer = DistilBertTokenizer.from_pretrained(
                    self.intent_model_dir
                )
                info_log(f"[IntentAnalyzer] 載入意圖模型：{self.intent_model_dir}")
            else:
                error_log(f"[IntentAnalyzer] 意圖模型不存在：{self.intent_model_dir}")
                return False
            
            # 載入實體識別模型
            try:
                self.entity_pipeline = pipeline(
                    "ner",
                    model=self.entity_model_name,
                    tokenizer=self.entity_model_name,
                    aggregation_strategy="simple"
                )
                info_log(f"[IntentAnalyzer] 載入實體模型：{self.entity_model_name}")
            except Exception as e:
                info_log(f"[IntentAnalyzer] 實體模型載入失敗，將跳過實體識別：{e}", "WARNING")
                self.entity_pipeline = None
            
            info_log("[IntentAnalyzer] 初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[IntentAnalyzer] 初始化失敗：{e}")
            return False
    
    def analyze_intent(self, text: str, enable_segmentation: bool = True, 
                      context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """分析文本意圖"""
        
        result = {
            "primary_intent": IntentType.UNKNOWN,
            "intent_segments": [],
            "overall_confidence": 0.0,
            "entities": [],
            "state_transition": None
        }
        
        try:
            if enable_segmentation:
                # 分段分析
                segments = self._segment_text(text)
                debug_log(3, f"[IntentAnalyzer] 分段結果：{len(segments)} 個片段")
                
                intent_segments = []
                confidences = []
                
                for segment_text, start_pos, end_pos in segments:
                    if len(segment_text.strip()) > 0:
                        intent, confidence = self._classify_single_text(segment_text)
                        entities = self._extract_entities(segment_text, start_pos)
                        
                        segment = IntentSegment(
                            text=segment_text,
                            intent=intent,
                            confidence=confidence,
                            start_pos=start_pos,
                            end_pos=end_pos,
                            entities=entities
                        )
                        
                        intent_segments.append(segment)
                        confidences.append(confidence)
                
                result["intent_segments"] = intent_segments
                
                # 決定主要意圖
                if intent_segments:
                    result["primary_intent"] = self._determine_primary_intent(intent_segments)
                    result["overall_confidence"] = sum(confidences) / len(confidences)
                
            else:
                # 整體分析
                intent, confidence = self._classify_single_text(text)
                entities = self._extract_entities(text, 0)
                
                segment = IntentSegment(
                    text=text,
                    intent=intent,
                    confidence=confidence,
                    start_pos=0,
                    end_pos=len(text),
                    entities=entities
                )
                
                result["intent_segments"] = [segment]
                result["primary_intent"] = intent
                result["overall_confidence"] = confidence
            
            # 提取所有實體
            result["entities"] = self._merge_entities(result["intent_segments"])
            
            # 建議狀態轉換
            result["state_transition"] = self._suggest_state_transition(
                result["primary_intent"], 
                result["overall_confidence"],
                context
            )
            
            debug_log(2, f"[IntentAnalyzer] 分析完成：主要意圖={result['primary_intent']}, "
                       f"信心度={result['overall_confidence']:.3f}")
            
            return result
            
        except Exception as e:
            error_log(f"[IntentAnalyzer] 意圖分析失敗：{e}")
            return result
    
    def _segment_text(self, text: str) -> List[Tuple[str, int, int]]:
        """將文本分段"""
        segments = []
        current_pos = 0
        
        # 嘗試各種分段模式
        for pattern in self.segmentation_patterns:
            matches = list(re.finditer(pattern, text))
            if matches:
                last_end = 0
                for match in matches:
                    # 添加分隔符前的文本
                    if match.start() > last_end:
                        segment_text = text[last_end:match.start()].strip()
                        if segment_text:
                            segments.append((segment_text, last_end, match.start()))
                    last_end = match.end()
                
                # 添加最後一段
                if last_end < len(text):
                    segment_text = text[last_end:].strip()
                    if segment_text:
                        segments.append((segment_text, last_end, len(text)))
                
                # 如果找到有效分段，就使用這個模式
                if len(segments) > 1:
                    break
                else:
                    segments.clear()
        
        # 如果沒有找到合適的分段，就作為整體處理
        if not segments:
            segments = [(text.strip(), 0, len(text))]
        
        return segments
    
    def _classify_single_text(self, text: str) -> Tuple[IntentType, float]:
        """分類單個文本片段"""
        try:
            if not self.intent_model or not self.intent_tokenizer:
                return IntentType.UNKNOWN, 0.0
            
            # 檢查是否為call類型的常見模式
            call_patterns = [
                r'^(?:hi|hello|hey|你好|哈囉)\s*[!！]*$',
                r'.*(?:can you|could you|請|可以幫我).*\?*$',
                r'^(?:uep|UEP)\s*[!！]*$',
                r'.*(?:在嗎|聽得到嗎).*\?*$'
            ]
            
            for pattern in call_patterns:
                if re.match(pattern, text.strip(), re.IGNORECASE):
                    debug_log(3, f"[IntentAnalyzer] 檢測到call模式：{pattern}")
                    return IntentType.CALL, 0.9
            
            # 使用模型分類
            inputs = self.intent_tokenizer(text, return_tensors="pt", truncation=True)
            with torch.no_grad():
                outputs = self.intent_model(**inputs)
                probabilities = torch.softmax(outputs.logits, dim=1)
                prediction = torch.argmax(probabilities, dim=1).item()
                confidence = probabilities[0][prediction].item()
            
            intent = self.intent_mapping.get(prediction, IntentType.UNKNOWN)
            
            debug_log(3, f"[IntentAnalyzer] 文本 '{text[:20]}...' => {intent}, 信心度: {confidence:.3f}")
            
            return intent, confidence
            
        except Exception as e:
            error_log(f"[IntentAnalyzer] 文本分類失敗：{e}")
            return IntentType.UNKNOWN, 0.0
    
    def _extract_entities(self, text: str, offset: int = 0) -> List[Dict[str, Any]]:
        """抽取實體"""
        entities = []
        
        try:
            if self.entity_pipeline:
                # 使用NER模型
                ner_results = self.entity_pipeline(text)
                
                for result in ner_results:
                    entity = {
                        "text": result["word"],
                        "entity_type": self._map_entity_type(result["entity_group"]),
                        "confidence": result["score"],
                        "start_pos": offset + result["start"],
                        "end_pos": offset + result["end"]
                    }
                    entities.append(entity)
            
            # 添加規則式實體識別
            rule_entities = self._extract_rule_based_entities(text, offset)
            entities.extend(rule_entities)
            
        except Exception as e:
            debug_log(3, f"[IntentAnalyzer] 實體抽取失敗：{e}")
        
        return entities
    
    def _extract_rule_based_entities(self, text: str, offset: int = 0) -> List[Dict[str, Any]]:
        """基於規則的實體抽取"""
        entities = []
        
        # 檔案路徑模式
        file_patterns = [
            r'[A-Za-z]:\\[^\\/:*?"<>|\r\n]*',  # Windows路徑
            r'/[^/:*?"<>|\r\n]*',               # Unix路徑
            r'[^/\\:*?"<>|\r\n]*\.[a-zA-Z0-9]+' # 檔案名稱
        ]
        
        for pattern in file_patterns:
            for match in re.finditer(pattern, text):
                entities.append({
                    "text": match.group(),
                    "entity_type": "file_path",
                    "confidence": 0.8,
                    "start_pos": offset + match.start(),
                    "end_pos": offset + match.end()
                })
        
        return entities
    
    def _map_entity_type(self, ner_label: str) -> str:
        """映射NER標籤到我們的實體類型"""
        mapping = {
            "PER": "person",
            "LOC": "location", 
            "ORG": "organization",
            "MISC": "custom"
        }
        return mapping.get(ner_label, "custom")
    
    def _determine_primary_intent(self, segments: List[IntentSegment]) -> IntentType:
        """決定主要意圖"""
        if not segments:
            return IntentType.UNKNOWN
        
        # 統計各種意圖
        intent_counts = {}
        weighted_scores = {}
        
        for segment in segments:
            intent = segment.intent
            weight = segment.confidence * len(segment.text)
            
            intent_counts[intent] = intent_counts.get(intent, 0) + 1
            weighted_scores[intent] = weighted_scores.get(intent, 0) + weight
        
        # 優先級規則
        priority = {
            IntentType.COMMAND: 3,
            IntentType.COMPOUND: 3,
            IntentType.CALL: 2,
            IntentType.CHAT: 1,
            IntentType.NON_SENSE: 0,
            IntentType.UNKNOWN: 0
        }
        
        # 選擇最高優先級且得分最高的意圖
        best_intent = IntentType.UNKNOWN
        best_score = -1
        
        for intent, score in weighted_scores.items():
            combined_score = score * priority.get(intent, 0)
            if combined_score > best_score:
                best_score = combined_score
                best_intent = intent
        
        # 檢查是否為複合指令
        if len([s for s in segments if s.intent == IntentType.COMMAND]) > 1:
            return IntentType.COMPOUND
        
        return best_intent
    
    def _merge_entities(self, segments: List[IntentSegment]) -> List[Dict[str, Any]]:
        """合併所有片段的實體"""
        all_entities = []
        for segment in segments:
            all_entities.extend(segment.entities)
        
        # 去重和合併重疊實體
        merged_entities = []
        all_entities.sort(key=lambda x: x["start_pos"])
        
        for entity in all_entities:
            # 檢查是否與已有實體重疊
            overlapped = False
            for merged in merged_entities:
                if (entity["start_pos"] < merged["end_pos"] and 
                    entity["end_pos"] > merged["start_pos"]):
                    # 保留信心度更高的實體
                    if entity["confidence"] > merged["confidence"]:
                        merged_entities.remove(merged)
                        merged_entities.append(entity)
                    overlapped = True
                    break
            
            if not overlapped:
                merged_entities.append(entity)
        
        return merged_entities
    
    def _suggest_state_transition(self, primary_intent: IntentType, 
                                confidence: float, context: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """建議系統狀態轉換"""
        
        if confidence < 0.5:  # 信心度太低
            return None
        
        current_state = context.get("current_system_state", "IDLE") if context else "IDLE"
        
        if primary_intent in self.state_transition_rules:
            rule = self.state_transition_rules[primary_intent]
            target_state = rule["target"]
            
            # 檢查是否需要狀態轉換
            if current_state != target_state:
                return {
                    "from_state": current_state,
                    "to_state": target_state,
                    "reason": rule["reason"],
                    "confidence": confidence
                }
        
        return None


# 導入所需模組
import os
from utils.debug_helper import debug_log, info_log, error_log
