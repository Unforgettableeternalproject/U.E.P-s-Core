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
    """智能關鍵詞檢測器
    
    內建關鍵詞邏輯，不依賴外部配置文件
    """
    
    def __init__(self, config: Optional[Dict] = None):
        """初始化檢測器
        
        Args:
            config: 可選配置字典，包含閾值等設置
        """
        # 從配置中獲取閾值，如果沒有則使用默認值
        smart_config = config.get('smart_activation', {}) if config else {}
        self.default_threshold = smart_config.get('activation_threshold', 0.6)
        self.wake_confidence_threshold = smart_config.get('wake_confidence_threshold', 0.8)
        
        # UEP 相關的各種變體 (英文環境)
        self.uep_variants = [
            "uep", "u.e.p", "u e p", "yep", "yup", "yuep", "uap", "ueb", "uip",
            "eternal project", "unforgettable eternal project"
        ]
        
        # 需要幫助的表達方式 (英文)
        self.help_expressions = [
            "help", "can you help", "could you help", "i need help", 
            "assist", "assistance", "support"
        ]
        
        # 問題/疑問的表達 (英文)
        self.question_patterns = [
            r"what.*is", r"how.*do", r"why.*", r"when.*", r"where.*",
            r"can.*you", r"could.*you", r"would.*you", r"will.*you"
        ]
        
        # 呼叫/招呼的表達 (英文)
        self.calling_expressions = [
            "hey", "hello", "hi", "yo", "excuse me"
        ]
        
        # 任務相關的關鍵詞 (英文)
        self.task_keywords = [
            "execute", "run", "start", "begin", 
            "play", "stop", "pause", "continue",
            "search", "find", "look for"
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
        
        # 檢查問號 (英文)
        if '?' in text:
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
    
    def should_activate(self, text: str, threshold: Optional[float] = None) -> Tuple[bool, List[KeywordMatch]]:
        """判斷是否應該啟動
        
        Args:
            text: 輸入文本
            threshold: 啟動閾值，如果不提供則使用默認值
            
        Returns:
            (should_activate, matches_found)
        """
        if not text or len(text.strip()) < 2:
            return False, []
        
        # 使用提供的閾值或默認值
        activation_threshold = threshold if threshold is not None else self.default_threshold
        
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
        if uep_match.matched and uep_match.confidence >= self.wake_confidence_threshold:
            should_activate = True
        elif help_match.matched and help_match.confidence >= self.wake_confidence_threshold:
            should_activate = True
        elif total_confidence >= activation_threshold * 1.5:  # 多個條件組合
            should_activate = True
        
        # 增強啟動條件
        if len(matches) >= 2 and total_confidence >= activation_threshold:
            should_activate = True
        
        return should_activate, matches
    
    def get_activation_reason(self, matches: List[KeywordMatch]) -> str:
        """獲取啟動原因說明 (英文)"""
        if not matches:
            return "No matches"
        
        reasons = []
        for match in matches:
            if match.match_type == "exact":
                reasons.append(f"Keyword detected: {match.keyword}")
            elif match.match_type == "fuzzy":
                reasons.append(f"Fuzzy match: {match.keyword}")
            elif match.match_type == "semantic":
                reasons.append(f"Semantic detection: {match.keyword}")
        
        return "; ".join(reasons)
