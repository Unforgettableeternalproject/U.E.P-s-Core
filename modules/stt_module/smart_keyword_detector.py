#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能關鍵詞檢測模組 (Smart Keyword Detection)
更自然的關鍵詞檢測，不需要明顯的啟動詞
"""

import re
import time
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

@dataclass
class KeywordMatch:
    """關鍵詞匹配結果"""
    matched: bool
    keyword: str
    confidence: float
    match_type: str  # "exact", "fuzzy", "semantic"
    position: Tuple[int, int]  # 在文本中的位置

class SmartKeywordDetector:
    """智能關鍵詞檢測器"""
    
    def __init__(self):
        # UEP 相關的各種變體
        self.uep_variants = [
            "uep", "u.e.p", "u e p", "yep", "yup", "yuep", "uap", "ueb", "uip",
            "永恆計畫", "永恒计划", "eternal project", "unforgettable eternal project"
        ]
        
        # 需要幫助的表達方式
        self.help_expressions = [
            "help", "幫助", "幫忙", "協助", "求助", "需要幫助", "can you help",
            "could you help", "i need help", "請幫忙", "可以幫我嗎",
            "assist", "assistance", "support", "救命"
        ]
        
        # 問題/疑問的表達
        self.question_patterns = [
            r"what.*is", r"how.*do", r"why.*", r"when.*", r"where.*",
            r"can.*you", r"could.*you", r"would.*you", r"will.*you",
            r"什麼是", r"怎麼", r"為什麼", r"什麼時候", r"在哪裡",
            r"可以.*嗎", r"能不能", r"會不會"
        ]
        
        # 呼叫/招呼的表達
        self.calling_expressions = [
            "hey", "hello", "hi", "yo", "excuse me", "嘿", "哈囉", "你好",
            "不好意思", "請問", "麻煩"
        ]
        
        # 任務相關的關鍵詞
        self.task_keywords = [
            "execute", "run", "start", "begin", "開始", "執行", "運行",
            "play", "stop", "pause", "continue", "播放", "停止", "暫停", "繼續",
            "search", "find", "look for", "搜尋", "查找", "尋找"
        ]
    
    def normalize_text(self, text: str) -> str:
        """正規化文本"""
        # 轉為小寫
        text = text.lower().strip()
        
        # 移除標點符號
        text = re.sub(r'[^\w\s]', ' ', text)
        
        # 正規化空白
        text = re.sub(r'\s+', ' ', text)
        
        return text
    
    def check_uep_mention(self, text: str) -> KeywordMatch:
        """檢查是否提到 UEP"""
        normalized = self.normalize_text(text)
        
        for variant in self.uep_variants:
            variant_norm = self.normalize_text(variant)
            if variant_norm in normalized:
                start_pos = normalized.find(variant_norm)
                end_pos = start_pos + len(variant_norm)
                
                return KeywordMatch(
                    matched=True,
                    keyword=variant,
                    confidence=1.0,
                    match_type="exact",
                    position=(start_pos, end_pos)
                )
        
        # 模糊匹配 UEP
        uep_fuzzy_patterns = [
            r'\bu\s*e\s*p\b',
            r'\byou\s*e\s*p\b',
            r'\byep\b',
            r'\byup\b'
        ]
        
        for pattern in uep_fuzzy_patterns:
            match = re.search(pattern, normalized)
            if match:
                return KeywordMatch(
                    matched=True,
                    keyword="UEP",
                    confidence=0.8,
                    match_type="fuzzy",
                    position=(match.start(), match.end())
                )
        
        return KeywordMatch(False, "", 0.0, "", (0, 0))
    
    def check_help_request(self, text: str) -> KeywordMatch:
        """檢查是否請求幫助"""
        normalized = self.normalize_text(text)
        
        for expr in self.help_expressions:
            expr_norm = self.normalize_text(expr)
            if expr_norm in normalized:
                start_pos = normalized.find(expr_norm)
                end_pos = start_pos + len(expr_norm)
                
                return KeywordMatch(
                    matched=True,
                    keyword=expr,
                    confidence=0.9,
                    match_type="exact",
                    position=(start_pos, end_pos)
                )
        
        return KeywordMatch(False, "", 0.0, "", (0, 0))
    
    def check_question_pattern(self, text: str) -> KeywordMatch:
        """檢查是否為問句"""
        normalized = self.normalize_text(text)
        
        for pattern in self.question_patterns:
            match = re.search(pattern, normalized)
            if match:
                return KeywordMatch(
                    matched=True,
                    keyword=f"question_pattern: {pattern}",
                    confidence=0.7,
                    match_type="semantic",
                    position=(match.start(), match.end())
                )
        
        # 檢查問號
        if '?' in text or '？' in text:
            return KeywordMatch(
                matched=True,
                keyword="question_mark",
                confidence=0.6,
                match_type="semantic",
                position=(0, len(text))
            )
        
        return KeywordMatch(False, "", 0.0, "", (0, 0))
    
    def check_calling_expression(self, text: str) -> KeywordMatch:
        """檢查是否為呼叫表達"""
        normalized = self.normalize_text(text)
        
        for expr in self.calling_expressions:
            expr_norm = self.normalize_text(expr)
            if normalized.startswith(expr_norm):  # 通常在句首
                return KeywordMatch(
                    matched=True,
                    keyword=expr,
                    confidence=0.5,
                    match_type="exact",
                    position=(0, len(expr_norm))
                )
        
        return KeywordMatch(False, "", 0.0, "", (0, 0))
    
    def should_activate(self, text: str, threshold: float = 0.6) -> Tuple[bool, List[KeywordMatch]]:
        """判斷是否應該啟動
        
        Args:
            text: 輸入文本
            threshold: 啟動閾值
            
        Returns:
            (should_activate, matches_found)
        """
        if not text or len(text.strip()) < 2:
            return False, []
        
        matches = []
        total_confidence = 0.0
        
        # 1. 檢查 UEP 提及（最高優先級）
        uep_match = self.check_uep_mention(text)
        if uep_match.matched:
            matches.append(uep_match)
            total_confidence += uep_match.confidence * 1.5  # UEP 提及權重更高
        
        # 2. 檢查幫助請求
        help_match = self.check_help_request(text)
        if help_match.matched:
            matches.append(help_match)
            total_confidence += help_match.confidence
        
        # 3. 檢查問句模式
        question_match = self.check_question_pattern(text)
        if question_match.matched:
            matches.append(question_match)
            total_confidence += question_match.confidence * 0.8
        
        # 4. 檢查呼叫表達
        calling_match = self.check_calling_expression(text)
        if calling_match.matched:
            matches.append(calling_match)
            total_confidence += calling_match.confidence * 0.6
        
        # 組合邏輯
        should_activate = False
        
        # 直接啟動條件
        if uep_match.matched and uep_match.confidence >= 0.8:
            should_activate = True
        elif help_match.matched and help_match.confidence >= 0.8:
            should_activate = True
        elif total_confidence >= threshold * 1.5:  # 多個條件組合
            should_activate = True
        
        # 增強啟動條件
        if len(matches) >= 2 and total_confidence >= threshold:
            should_activate = True
        
        return should_activate, matches
    
    def get_activation_reason(self, matches: List[KeywordMatch]) -> str:
        """獲取啟動原因說明"""
        if not matches:
            return "無匹配"
        
        reasons = []
        for match in matches:
            if match.match_type == "exact":
                reasons.append(f"檢測到關鍵詞: {match.keyword}")
            elif match.match_type == "fuzzy":
                reasons.append(f"模糊匹配: {match.keyword}")
            elif match.match_type == "semantic":
                reasons.append(f"語意檢測: {match.keyword}")
        
        return "; ".join(reasons)
