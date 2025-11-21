#!/usr/bin/env python3
"""
意圖分段後處理器
處理 BIO Tagger 初次分段結果，進行合併與優化
"""

from typing import List, Dict, Any
from utils.debug_helper import debug_log, info_log


class IntentPostProcessor:
    """意圖分段後處理器"""
    
    # 連接詞和標點符號（作為分段邊界）
    BOUNDARY_MARKERS = {',', '.', '!', '?', ';', 'and', 'then', 'but', 'or', 'so'}
    
    # 問候詞（應歸類為 CALL）
    GREETING_KEYWORDS = {'hello', 'hi', 'hey', 'greetings', 'good morning', 'good afternoon', 'good evening'}
    
    def __init__(self):
        """初始化後處理器"""
        info_log("[IntentPostProcessor] 初始化意圖後處理器")
    
    def process(self, raw_segments: List[Dict[str, Any]], original_text: str) -> List[Dict[str, Any]]:
        """
        處理原始分段結果
        
        Args:
            raw_segments: BIO Tagger 輸出的原始分段
            original_text: 原始輸入文本
        
        Returns:
            處理後的分段列表
        """
        if not raw_segments:
            return raw_segments
        
        debug_log(2, f"[IntentPostProcessor] 開始後處理 {len(raw_segments)} 個分段")
        
        # Step 1: 處理短分段（< 3 字元）
        segments = self._handle_short_segments(raw_segments)
        
        # Step 2: 合併相鄰分段
        segments = self._merge_segments(segments, original_text)
        
        debug_log(2, f"[IntentPostProcessor] 後處理完成，剩餘 {len(segments)} 個分段")
        return segments
    
    def _handle_short_segments(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        處理短分段（< 3 字元）
        
        規則：
        1. 如果是問候詞 → 歸類為 CALL
        2. 否則 → 歸類為 UNKNOWN（後續可能被合併）
        """
        processed = []
        
        for seg in segments:
            seg_text = seg['text'].strip()
            seg_length = len(seg_text)
            
            if seg_length < 3:
                # 檢查是否為問候詞
                text_lower = seg_text.lower()
                if text_lower in self.GREETING_KEYWORDS:
                    seg['intent'] = 'call'
                    debug_log(3, f"[IntentPostProcessor] 短分段 '{seg_text}' 識別為問候 → CALL")
                else:
                    seg['intent'] = 'unknown'
                    debug_log(3, f"[IntentPostProcessor] 短分段 '{seg_text}' 歸類為 UNKNOWN")
            
            processed.append(seg)
        
        return processed
    
    def _merge_segments(self, segments: List[Dict[str, Any]], original_text: str) -> List[Dict[str, Any]]:
        """
        合併相鄰分段
        
        合併規則：
        1. 相同意圖 → 直接合併
        2. 不同意圖但中間有 UNKNOWN → 合併並使用主導意圖
        3. 主導意圖 = 出現次數最多 + 優先取第一個
        """
        if len(segments) <= 1:
            return segments
        
        merged = []
        i = 0
        
        while i < len(segments):
            current_segment = segments[i]
            merge_group = [current_segment]
            j = i + 1
            
            # 嘗試向後合併
            while j < len(segments):
                next_segment = segments[j]
                
                # 檢查是否應該合併
                should_merge = self._should_merge(merge_group, next_segment, original_text)
                
                if should_merge:
                    merge_group.append(next_segment)
                    j += 1
                else:
                    break
            
            # 如果有合併，創建新分段
            if len(merge_group) > 1:
                merged_segment = self._create_merged_segment(merge_group, original_text)
                merged.append(merged_segment)
                debug_log(3, f"[IntentPostProcessor] 合併 {len(merge_group)} 個分段 → '{merged_segment['text']}' ({merged_segment['intent']})")
            else:
                merged.append(current_segment)
            
            i = j
        
        return merged
    
    def _should_merge(self, merge_group: List[Dict[str, Any]], next_segment: Dict[str, Any], original_text: str) -> bool:
        """
        判斷是否應該合併下一個分段
        
        合併條件：
        1. 相同意圖
        2. 下一個分段是 UNKNOWN（可能是連接詞）
        3. 分段之間沒有強邊界標記（., !, ?）
        """
        last_segment = merge_group[-1]
        last_intent = last_segment['intent']
        next_intent = next_segment['intent']
        
        # 條件 1: 相同意圖 → 合併
        if last_intent == next_intent:
            return True
        
        # 條件 2: 下一個是 UNKNOWN → 可能是連接詞，合併
        if next_intent == 'unknown':
            return True
        
        # 條件 3: 檢查分段之間是否有強邊界
        between_start = last_segment['end_pos']
        between_end = next_segment['start_pos']
        
        if between_start < between_end:
            between_text = original_text[between_start:between_end].strip()
            # 如果中間有強標點，不合併
            if between_text in {'.', '!', '?', ';'}:
                return False
        
        # 條件 4: 如果當前組中已有多種意圖，可以繼續合併（將用主導意圖）
        intents_in_group = set(seg['intent'] for seg in merge_group)
        if len(intents_in_group) > 1:
            return True
        
        return False
    
    def _create_merged_segment(self, segments: List[Dict[str, Any]], original_text: str) -> Dict[str, Any]:
        """
        創建合併後的分段
        
        決定主導意圖：
        1. 統計各意圖出現次數
        2. 選擇次數最多的意圖
        3. 如果平手，選擇第一個出現的意圖
        """
        # 統計意圖
        intent_counts = {}
        intent_first_appearance = {}
        
        for idx, seg in enumerate(segments):
            intent = seg['intent']
            if intent not in intent_counts:
                intent_counts[intent] = 0
                intent_first_appearance[intent] = idx
            intent_counts[intent] += 1
        
        # 找出主導意圖
        max_count = max(intent_counts.values())
        candidates = [intent for intent, count in intent_counts.items() if count == max_count]
        
        # 如果有多個候選，選擇第一個出現的
        if len(candidates) > 1:
            dominant_intent = min(candidates, key=lambda x: intent_first_appearance[x])
        else:
            dominant_intent = candidates[0]
        
        # 計算合併後的 confidence（取平均）
        avg_confidence = sum(seg['confidence'] for seg in segments) / len(segments)
        
        # 創建合併分段
        merged = {
            'text': original_text[segments[0]['start_pos']:segments[-1]['end_pos']],
            'intent': dominant_intent,
            'start_pos': segments[0]['start_pos'],
            'end_pos': segments[-1]['end_pos'],
            'confidence': round(avg_confidence, 3)
        }
        
        return merged
