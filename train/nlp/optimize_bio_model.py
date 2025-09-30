#!/usr/bin/env python3
"""
BIO模型後處理優化腳本
改善分段邊界和意圖分類精確度
"""

import sys
import re
from pathlib import Path
from typing import List, Dict, Any, Tuple

# 添加項目根目錄到路徑
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from modules.nlp_module.bio_tagger import BIOTagger
from utils.debug_helper import debug_log, info_log, error_log

class BIOModelOptimizer:
    """BIO模型後處理優化器"""
    
    def __init__(self, model_path: str):
        self.model_path = model_path
        self.bio_tagger = BIOTagger()
        
        # 意圖關鍵詞字典
        self.intent_keywords = {
            'call': [
                'hello', 'hi', 'hey', 'system', 'uep', 'assistant', 
                'wake', 'attention', 'are you there', 'listening'
            ],
            'chat': [
                'weather', 'feeling', 'day', 'good', 'bad', 'interesting',
                'movie', 'book', 'think', 'believe', 'amazing', 'beautiful',
                'ok', 'thanks', 'thank you', 'great', 'wonderful'
            ],
            'command': [
                'set', 'open', 'save', 'turn', 'play', 'stop', 'start',
                'help', 'organize', 'create', 'delete', 'move', 'copy',
                'remind', 'calendar', 'schedule', 'timer', 'alarm'
            ]
        }
    
    def load_model(self) -> bool:
        """載入模型"""
        return self.bio_tagger.load_model(self.model_path)
    
    def predict_with_optimization(self, text: str) -> List[Dict[str, Any]]:
        """帶優化的預測"""
        # 原始預測
        segments = self.bio_tagger.predict(text)
        
        # 後處理優化
        optimized_segments = self._post_process_segments(text, segments)
        
        return optimized_segments
    
    def _post_process_segments(self, text: str, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """後處理分段優化"""
        if not segments:
            return segments
        
        optimized = []
        
        for segment in segments:
            # 1. 修正意圖分類
            corrected_intent = self._correct_intent(segment['text'], segment['intent'])
            
            # 2. 合併過度分割的分段
            if optimized and self._should_merge_segments(optimized[-1], segment):
                # 合併到前一個分段
                last_segment = optimized[-1]
                merged_text = f"{last_segment['text']} {segment['text']}"
                merged_intent = self._decide_merged_intent(last_segment['intent'], corrected_intent)
                
                optimized[-1] = {
                    'text': merged_text,
                    'intent': merged_intent,
                    'start_pos': last_segment['start_pos'],
                    'end_pos': segment['end_pos'],
                    'confidence': min(last_segment['confidence'], segment['confidence'])
                }
            else:
                # 添加新分段
                optimized.append({
                    'text': segment['text'],
                    'intent': corrected_intent,
                    'start_pos': segment['start_pos'],
                    'end_pos': segment['end_pos'],
                    'confidence': segment['confidence']
                })
        
        # 3. 分割過長的分段
        final_segments = []
        for segment in optimized:
            split_segments = self._split_long_segment(segment)
            final_segments.extend(split_segments)
        
        return final_segments
    
    def _correct_intent(self, text: str, original_intent: str) -> str:
        """修正意圖分類"""
        text_lower = text.lower()
        
        # 計算每個意圖的權重
        intent_scores = {
            'call': 0,
            'chat': 0,
            'command': 0
        }
        
        for intent, keywords in self.intent_keywords.items():
            for keyword in keywords:
                if keyword in text_lower:
                    intent_scores[intent] += 1
        
        # 特殊規則
        # 1. 短詞特殊處理
        if len(text.split()) <= 2:
            if any(word in text_lower for word in ['hi', 'hello', 'hey', 'uep', 'system']):
                return 'call'
            elif any(word in text_lower for word in ['ok', 'thanks', 'good', 'great']):
                return 'chat'
            elif any(word in text_lower for word in ['set', 'open', 'save', 'help']):
                return 'command'
        
        # 2. 問句處理
        if text.strip().endswith('?'):
            if any(word in text_lower for word in ['are you', 'can you', 'could you']):
                if 'there' in text_lower or 'listening' in text_lower:
                    return 'call'
                else:
                    return 'command'
        
        # 3. 祈使句處理
        command_starters = ['set', 'open', 'save', 'turn', 'play', 'stop', 'help', 'create']
        if any(text_lower.startswith(starter) for starter in command_starters):
            return 'command'
        
        # 如果有明顯的最高分意圖，使用它
        max_score = max(intent_scores.values())
        if max_score > 0:
            best_intent = max(intent_scores, key=intent_scores.get)
            # 只有當新意圖的分數明顯更高時才修正
            if intent_scores[best_intent] > intent_scores.get(original_intent, 0):
                return best_intent
        
        return original_intent
    
    def _should_merge_segments(self, seg1: Dict[str, Any], seg2: Dict[str, Any]) -> bool:
        """判斷是否應該合併兩個分段"""
        # 1. 如果兩個分段都很短且意圖相同
        if (len(seg1['text'].split()) <= 2 and 
            len(seg2['text'].split()) <= 2 and 
            seg1['intent'] == seg2['intent']):
            return True
        
        # 2. 如果第一個分段是單個詞的呼叫，第二個是相關動作
        if (len(seg1['text'].split()) == 1 and 
            seg1['intent'] == 'call' and 
            seg2['intent'] == 'command'):
            return False  # 保持分離
        
        # 3. 如果分段之間沒有明顯的停頓標記
        text_between = seg2['text']
        if not any(punct in text_between for punct in ['.', '!', '?', ';']):
            # 檢查是否是連續的短語
            combined_text = f"{seg1['text']} {seg2['text']}"
            if len(combined_text.split()) <= 6:  # 總長度不超過6個詞
                return True
        
        return False
    
    def _decide_merged_intent(self, intent1: str, intent2: str) -> str:
        """決定合併後的意圖"""
        # 優先級：command > call > chat
        priority = {'command': 3, 'call': 2, 'chat': 1}
        
        if priority.get(intent1, 0) >= priority.get(intent2, 0):
            return intent1
        else:
            return intent2
    
    def _split_long_segment(self, segment: Dict[str, Any]) -> List[Dict[str, Any]]:
        """分割過長的分段"""
        text = segment['text']
        words = text.split()
        
        # 如果分段不長，直接返回
        if len(words) <= 15:
            return [segment]
        
        # 尋找自然分割點
        split_points = []
        for i, word in enumerate(words):
            if word.endswith(('.', '!', '?', ';', ',')):
                split_points.append(i + 1)
        
        if not split_points:
            return [segment]
        
        # 分割為多個分段
        segments = []
        start_idx = 0
        char_pos = segment['start_pos']
        
        for split_idx in split_points:
            if split_idx > start_idx:
                sub_words = words[start_idx:split_idx]
                sub_text = ' '.join(sub_words)
                
                # 計算字符位置
                sub_start = char_pos
                sub_end = char_pos + len(sub_text)
                
                # 決定子分段的意圖
                sub_intent = self._correct_intent(sub_text, segment['intent'])
                
                segments.append({
                    'text': sub_text,
                    'intent': sub_intent,
                    'start_pos': sub_start,
                    'end_pos': sub_end,
                    'confidence': segment['confidence'] * 0.9  # 略微降低信心度
                })
                
                char_pos = sub_end + 1  # +1 for space
                start_idx = split_idx
        
        # 添加剩餘部分
        if start_idx < len(words):
            sub_words = words[start_idx:]
            sub_text = ' '.join(sub_words)
            sub_intent = self._correct_intent(sub_text, segment['intent'])
            
            segments.append({
                'text': sub_text,
                'intent': sub_intent,
                'start_pos': char_pos,
                'end_pos': segment['end_pos'],
                'confidence': segment['confidence'] * 0.9
            })
        
        return segments

def test_optimized_model():
    """測試優化後的模型"""
    model_path = "../../models/nlp/bio_tagger"
    optimizer = BIOModelOptimizer(model_path)
    
    if not optimizer.load_model():
        error_log("載入模型失敗")
        return
    
    # 測試案例（之前失敗的）
    test_cases = [
        "Are you there?",
        "That's interesting",
        "OK",
        "Hello! Can you help me? Thanks!",
        "@UEP #help $save",
        "UEP, save file_123.txt"
    ]
    
    info_log("🔧 測試優化後的模型...")
    
    for text in test_cases:
        info_log(f"\n測試: '{text}'")
        
        # 原始預測
        original = optimizer.bio_tagger.predict(text)
        info_log(f"原始: {original}")
        
        # 優化預測
        optimized = optimizer.predict_with_optimization(text)
        info_log(f"優化: {optimized}")

if __name__ == "__main__":
    test_optimized_model()
