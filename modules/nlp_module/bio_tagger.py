#!/usr/bin/env python3
"""
基於Transformer的序列標註模型
實現BIO標記的多意圖分段識別
"""

import torch
import torch.nn as nn
from transformers import AutoTokenizer, AutoModelForTokenClassification
from transformers import TrainingArguments, Trainer
from transformers import DataCollatorForTokenClassification
from typing import List, Dict, Any, Tuple, Optional
import json
from pathlib import Path
import numpy as np
from utils.debug_helper import debug_log, info_log, error_log

class BIOTagger:
    """BIO標記的序列標註器"""
    
    # BIO標籤定義（Stage 4 更新）
    BIO_LABELS = [
        "O",                    # Outside
        "B-CALL",              # Begin Call
        "I-CALL",              # Inside Call  
        "B-CHAT",              # Begin Chat
        "I-CHAT",              # Inside Chat
        "B-DIRECT_WORK",       # Begin Direct Work (緊急工作)
        "I-DIRECT_WORK",       # Inside Direct Work
        "B-BACKGROUND_WORK",   # Begin Background Work (背景任務)
        "I-BACKGROUND_WORK",   # Inside Background Work
        "B-UNKNOWN",           # Begin Unknown (無法識別)
        "I-UNKNOWN"            # Inside Unknown
    ]
    
    def __init__(self, model_name: str = "distilbert-base-uncased"):
        """初始化BIO標註器"""
        self.model_name = model_name
        self.tokenizer = None
        self.model = None
        self.label2id = {label: i for i, label in enumerate(self.BIO_LABELS)}
        self.id2label = {i: label for i, label in enumerate(self.BIO_LABELS)}
        
        info_log(f"[BIOTagger] 初始化序列標註器: {model_name}")
    
    def load_model(self, model_path: Optional[str] = None):
        """載入模型"""
        try:
            if model_path and Path(model_path).exists():
                # 載入微調後的模型
                self.tokenizer = AutoTokenizer.from_pretrained(model_path)
                self.model = AutoModelForTokenClassification.from_pretrained(
                    model_path,
                    num_labels=len(self.BIO_LABELS),
                    id2label=self.id2label,
                    label2id=self.label2id
                )
                info_log(f"[BIOTagger] 載入微調模型: {model_path}")
            else:
                # 載入預訓練模型
                self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
                self.model = AutoModelForTokenClassification.from_pretrained(
                    self.model_name,
                    num_labels=len(self.BIO_LABELS),
                    id2label=self.id2label,
                    label2id=self.label2id
                )
                info_log(f"[BIOTagger] 載入預訓練模型: {self.model_name}")
                
        except Exception as e:
            error_log(f"[BIOTagger] 模型載入失敗: {e}")
            # 使用備用模式
            info_log("[BIOTagger] 啟用備用模式")
            self.tokenizer = None
            self.model = None
            return True
            
        return True
    
    def predict(self, text: str) -> List[Dict[str, Any]]:
        """預測文本的BIO標籤並返回分段結果"""
        if not self.model or not self.tokenizer:
            # 備用實現：簡單的規則分段
            return self._fallback_segmentation(text)
        
        try:
            # 分詞
            inputs = self.tokenizer(
                text, 
                return_tensors="pt",
                truncation=True,
                is_split_into_words=False,
                return_offsets_mapping=True
            )
            
            # 預測
            with torch.no_grad():
                outputs = self.model(**{k: v for k, v in inputs.items() if k != 'offset_mapping'})
                predictions = torch.argmax(outputs.logits, dim=-1)
                # 計算 softmax 機率作為 confidence
                probabilities = torch.softmax(outputs.logits, dim=-1)
                confidences = torch.max(probabilities, dim=-1).values
            
            # 獲取token到字符的映射
            offset_mapping = inputs['offset_mapping'][0]
            tokens = self.tokenizer.convert_ids_to_tokens(inputs['input_ids'][0])
            predicted_labels = [self.id2label[pred.item()] for pred in predictions[0]] # type: ignore
            token_confidences = confidences[0].tolist()
            
            # 將BIO標籤轉換為分段（傳遞 confidence 值）
            segments = self._bio_to_segments(text, tokens, predicted_labels, offset_mapping, token_confidences)
            
            debug_log(3, f"[BIOTagger] 識別到 {len(segments)} 個分段")
            return segments
            
        except Exception as e:
            error_log(f"[BIOTagger] 預測失敗: {e}")
            return self._fallback_segmentation(text)
    
    def _fallback_segmentation(self, text: str) -> List[Dict[str, Any]]:
        """備用分段實現 - 基於簡單規則"""
        segments = []
        
        # 簡單的關鍵詞檢測
        text_lower = text.lower()
        
        # 🔧 特殊處理：U.E.P 名字識別（提及名字視為聊天或呼叫）
        # 支援多種格式: u.e.p / uep / U.E.P / UEP
        uep_patterns = ['u.e.p', 'u e p', 'uep']
        contains_uep_name = any(pattern in text_lower for pattern in uep_patterns)
        
        # 檢測呼叫意圖
        call_keywords = ['hey', 'hello', 'hi', 'uep', 'system', 'wake up']
        chat_keywords = ['how are you', 'what do you think', 'tell me', 'story', 'chat', 'name', 'nickname', 'real name']
        direct_work_keywords = ['save', 'open', 'create', 'delete', 'show', 'search', 'find']
        background_work_keywords = ['play', 'sync', 'backup', 'download', 'install', 'update']
        
        # 🔧 如果提及 U.E.P 名字且有問題，視為聊天
        if contains_uep_name and ('?' in text or 'what' in text_lower or 'who' in text_lower or 'name' in text_lower):
            intent = 'chat'
        elif any(keyword in text_lower for keyword in call_keywords):
            intent = 'call'
        elif any(keyword in text_lower for keyword in direct_work_keywords):
            intent = 'direct_work'
        elif any(keyword in text_lower for keyword in background_work_keywords):
            intent = 'background_work'
        elif any(keyword in text_lower for keyword in chat_keywords):
            intent = 'chat'
        else:
            intent = 'unknown'  # 無法識別時返回 unknown
        
        # 創建單一分段（簡化版本）
        segment = {
            'text': text,
            'intent': intent,
            'start_pos': 0,
            'end_pos': len(text),
            'confidence': 0.7  # 較低的信心度表示這是備用實現
        }
        
        segments.append(segment)
        info_log(f"[BIOTagger] 備用分段: '{text}' -> {intent}")
        
        return segments
    
    def _bio_to_segments(self, text: str, tokens: List[str], labels: List[str], 
                        offset_mapping: torch.Tensor, token_confidences: Optional[List[float]] = None) -> List[Dict[str, Any]]:
        """將BIO標籤序列轉換為分段結果
        
        Args:
            text: 原始文本
            tokens: tokenizer 分詞結果
            labels: BIO 標籤序列
            offset_mapping: token 到字符的映射
            token_confidences: 每個 token 的預測信心度（可選）
        """
        segments = []
        current_segment = None
        segment_confidences = []  # 追蹤當前分段的所有 token confidence
        
        for i, (token, label, offset) in enumerate(zip(tokens, labels, offset_mapping)):
            confidence = token_confidences[i] if token_confidences else 0.9
            # 跳過特殊token
            if token in ['[CLS]', '[SEP]', '[PAD]']:
                continue
                
            start_pos, end_pos = offset.tolist()
            
            if label.startswith('B-'):
                # 開始新分段
                if current_segment:
                    # 計算當前分段的平均 confidence
                    avg_confidence = sum(segment_confidences) / len(segment_confidences) if segment_confidences else 0.9
                    current_segment['confidence'] = round(avg_confidence, 3)
                    segments.append(current_segment)
                
                intent_type = label[2:].lower()  # 移除 'B-' 前綴
                current_segment = {
                    'text': text[start_pos:end_pos],
                    'intent': intent_type,
                    'start_pos': start_pos,
                    'end_pos': end_pos
                }
                segment_confidences = [confidence]  # 重置並添加當前 token confidence
                
            elif label.startswith('I-'):
                intent_type = label[2:].lower()
                
                if current_segment and current_segment['intent'] == intent_type:
                    # 延續當前分段
                    current_segment['end_pos'] = end_pos
                    current_segment['text'] = text[current_segment['start_pos']:end_pos]
                    segment_confidences.append(confidence)  # 追蹤 confidence
                else:
                    # 沒有對應的 B- 標籤，或標籤不一致
                    # 將孤立的 I- 標籤視為 B- 開始新分段
                    if current_segment:
                        avg_confidence = sum(segment_confidences) / len(segment_confidences) if segment_confidences else 0.9
                        current_segment['confidence'] = round(avg_confidence, 3)
                        segments.append(current_segment)
                    
                    current_segment = {
                        'text': text[start_pos:end_pos],
                        'intent': intent_type,
                        'start_pos': start_pos,
                        'end_pos': end_pos
                    }
                    segment_confidences = [confidence]  # 重置
            
            elif label == 'O':
                # 結束當前分段
                if current_segment:
                    avg_confidence = sum(segment_confidences) / len(segment_confidences) if segment_confidences else 0.9
                    current_segment['confidence'] = round(avg_confidence, 3)
                    segments.append(current_segment)
                    current_segment = None
                    segment_confidences = []  # 重置
        
        # 添加最後一個分段
        if current_segment:
            avg_confidence = sum(segment_confidences) / len(segment_confidences) if segment_confidences else 0.9
            current_segment['confidence'] = round(avg_confidence, 3)
            segments.append(current_segment)
        
        return segments
    
    def prepare_training_data(self, annotated_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """準備訓練數據"""
        texts = []
        labels = []
        
        for example in annotated_data:
            text = example['text']
            segments = example['segments']  # [{'start': 0, 'end': 10, 'label': 'CALL'}, ...]
            
            # 轉換為BIO標籤
            bio_labels = self._segments_to_bio(text, segments)
            
            texts.append(text)
            labels.append(bio_labels)
        
        # 使用tokenizer處理
        tokenized_inputs = self.tokenizer(
            texts,
            truncation=True,
            is_split_into_words=False,
            return_offsets_mapping=True,
            padding=True
        ) # type: ignore
        
        # 對齊標籤
        aligned_labels = []
        for i, label_seq in enumerate(labels):
            word_ids = tokenized_inputs.word_ids(batch_index=i)
            aligned_label = self._align_labels_with_tokens(label_seq, word_ids)
            aligned_labels.append(aligned_label)
        
        return {
            'input_ids': tokenized_inputs['input_ids'],
            'attention_mask': tokenized_inputs['attention_mask'],
            'labels': aligned_labels
        }
    
    def _segments_to_bio(self, text: str, segments: List[Dict[str, Any]]) -> List[str]:
        """將分段標註轉換為BIO標籤序列"""
        # 這是一個簡化版本，實際實現需要更精確的token對齊
        bio_labels = ['O'] * len(text.split())
        
        for segment in segments:
            start = segment['start']
            end = segment['end']
            label = segment['label'].upper()
            
            # 簡化：假設每個詞一個token
            words = text.split()
            char_pos = 0
            
            for i, word in enumerate(words):
                word_start = char_pos
                word_end = char_pos + len(word)
                
                if word_start >= start and word_end <= end:
                    if bio_labels[i] == 'O':  # 第一個詞
                        bio_labels[i] = f'B-{label}'
                    else:  # 後續詞
                        bio_labels[i] = f'I-{label}'
                
                char_pos = word_end + 1  # +1 for space
        
        return bio_labels
    
    def _align_labels_with_tokens(self, labels: List[str], word_ids: List[Optional[int]]) -> List[int]:
        """對齊標籤與tokens"""
        aligned_labels = []
        previous_word_idx = None
        
        for word_idx in word_ids:
            if word_idx is None:
                # 特殊token
                aligned_labels.append(-100)
            elif word_idx != previous_word_idx:
                # 新詞的第一個subtoken
                aligned_labels.append(self.label2id[labels[word_idx]])
            else:
                # 同一詞的後續subtoken
                aligned_labels.append(-100)
            
            previous_word_idx = word_idx
        
        return aligned_labels


class EnhancedIntentAnalyzer:
    """增強的意圖分析器，整合BIO標註器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.bio_tagger = BIOTagger(config.get('model_name', 'distilbert-base-uncased'))
        self.fallback_analyzer = None  # 原有的意圖分析器作為後備
        
    def initialize(self):
        """初始化分析器"""
        try:
            model_path = self.config.get('bio_model_path')
            if not self.bio_tagger.load_model(model_path):
                error_log("[EnhancedIntentAnalyzer] BIO模型載入失敗，使用後備方案")
                return False
            
            info_log("[EnhancedIntentAnalyzer] 增強意圖分析器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[EnhancedIntentAnalyzer] 初始化失敗: {e}")
            return False
    
    def analyze_intent(self, text: str, enable_segmentation: bool = True, 
                      context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """分析意圖（增強版）"""
        
        result = {
            "primary_intent": "unknown",
            "intent_segments": [],
            "overall_confidence": 0.0,
            "segmentation_method": "bio_tagging"
        }
        
        try:
            # 使用BIO標註器進行分段
            segments = self.bio_tagger.predict(text)
            
            if segments:
                # 轉換為標準格式
                intent_segments = []
                confidences = []
                
                for segment in segments:
                    from modules.nlp_module.schemas import IntentSegment, IntentType
                    
                    # 映射intent類型（統一為 WORK，使用 work_mode 區分）
                    intent_mapping = {
                        'call': IntentType.CALL,
                        'chat': IntentType.CHAT,
                        'direct_work': IntentType.WORK,
                        'background_work': IntentType.WORK,
                        'unknown': IntentType.UNKNOWN
                    }
                    
                    # work_mode 映射（僅用於 WORK 類型）
                    work_mode_mapping = {
                        'direct_work': 'direct',
                        'background_work': 'background'
                    }
                    
                    intent_type = intent_mapping.get(segment['intent'], IntentType.UNKNOWN)
                    work_mode = work_mode_mapping.get(segment['intent'])
                    
                    # 構建 metadata
                    metadata = {}
                    if work_mode:
                        metadata['work_mode'] = work_mode
                    
                    intent_seg = IntentSegment(
                        text=segment['text'],
                        intent=intent_type,
                        confidence=segment['confidence'],
                        start_pos=segment['start_pos'],
                        end_pos=segment['end_pos'],
                        entities=[],  # 可以進一步整合實體識別
                        metadata=metadata
                    )
                    
                    intent_segments.append(intent_seg)
                    confidences.append(segment['confidence'])
                
                # 計算主要意圖
                primary_intent = self._determine_primary_intent(intent_segments)
                overall_confidence = np.mean(confidences) if confidences else 0.0
                
                result.update({
                    "primary_intent": primary_intent,
                    "intent_segments": intent_segments,
                    "overall_confidence": overall_confidence
                })
                
                info_log(f"[EnhancedIntentAnalyzer] BIO分段成功: {len(intent_segments)} 個片段")
                
            else:
                # BIO標註失敗，使用後備方案
                debug_log(2, "[EnhancedIntentAnalyzer] BIO標註無結果，使用後備分析")
                if self.fallback_analyzer:
                    return self.fallback_analyzer.analyze_intent(text, enable_segmentation, context)
                    
        except Exception as e:
            error_log(f"[EnhancedIntentAnalyzer] 分析失敗: {e}")
            
        return result
    
    def _determine_primary_intent(self, segments) -> str:
        """決定主要意圖"""
        if not segments:
            return "unknown"
            
        # 優先級規則（Stage 4 更新）
        priority = {
            "direct_work": 4,      # 最高優先級
            "call": 3,
            "background_work": 2,
            "chat": 1,
            "unknown": 0
        }
        
        # 選擇最高優先級的意圖
        best_intent = "unknown"
        best_priority = -1
        
        for segment in segments:
            intent_str = segment.intent.value if hasattr(segment.intent, 'value') else str(segment.intent)
            intent_priority = priority.get(intent_str, 0)
            
            if intent_priority > best_priority:
                best_priority = intent_priority
                best_intent = intent_str
                
        return best_intent
