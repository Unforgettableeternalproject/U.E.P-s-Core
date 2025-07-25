"""
modules/stt_module/smart_activation.py
智能啟動檢測器 - 分析上下文決定是否需要啟動 UEP
"""

import re
import time
from typing import List, Dict, Optional, Callable
from utils.debug_helper import debug_log, info_log, error_log

class SmartActivationDetector:
    """智能啟動檢測器"""
    
    def __init__(self, config: dict):
        self.config = config
        self.smart_config = config.get("smart_activation", {})
        
        # 配置參數
        self.enabled = self.smart_config.get("enabled", True)
        self.context_keywords = self.smart_config.get("context_keywords", [])
        self.conversation_mode = self.smart_config.get("conversation_mode", True)
        self.activation_confidence = self.smart_config.get("activation_confidence", 0.3)  # 降低到 0.3
        
        # 對話上下文追蹤
        self.conversation_context = []
        self.last_interaction_time = None
        self.conversation_active = False
        
        # 啟動模式
        self.activation_patterns = self._initialize_patterns()
        
        info_log(f"[SmartActivation] 初始化完成，關鍵字數量: {len(self.context_keywords)}")
        
    def _initialize_patterns(self) -> Dict[str, List[str]]:
        """初始化啟動模式 - 以英文為主"""
        patterns = {
            # 直接呼叫模式
            "direct_call": [
                r"\bUEP\b|hey UEP|hello UEP",
                r"\bhelp me\b|\bhelp\b|\bassist\b",
                r"\bplease\b.*\bdo\b|\bplease\b.*\bhelp\b|\bplease\b.*\bcan\b"
            ],
            
            # 問題模式
            "question": [
                r"what is.*\?|what are.*\?|what does.*\?",
                r"how to.*\?|how do.*\?|how can.*\?",
                r"why is.*\?|why does.*\?|why do.*\?",
                r"where is.*\?|where can.*\?|when is.*\?",
                r"which.*\?|who is.*\?"
            ],
            
            # 需求表達模式
            "need_expression": [
                r"\bI need\b.*|\bI want\b.*|\bI would like\b.*",
                r"\bcan you\b.*|\bcould you\b.*|\bwould you\b.*",
                r"\bis there\b.*|\bdo you have\b.*|\bcan I\b.*"
            ],
            
            # 命令模式
            "command": [
                r"\bshow me\b.*|\btell me\b.*|\bexplain\b.*",
                r"\bopen\b.*|\bclose\b.*|\bstart\b.*|\bstop\b.*",
                r"\bcreate\b.*|\bmake\b.*|\bgenerate\b.*",
                r"\bfind\b.*|\bsearch\b.*|\blook for\b.*"
            ],
            
            # 工作相關模式
            "work_related": [
                r"\bfile\b|\bfiles\b|\bfolder\b|\bdocument\b|\bdocuments\b",
                r"\bcode\b|\bprogram\b|\bsoftware\b|\bapplication\b",
                r"\bmeeting\b|\breport\b|\bpresentation\b|\bproject\b",
                r"\bemail\b|\bmessage\b|\btext\b|\bnote\b"
            ],
            
            # 困難求助模式
            "help_seeking": [
                r"\bI don't know\b|\bI'm confused\b|\bI'm stuck\b",
                r"\btrouble\b|\bproblem\b|\bissue\b|\bdifficulty\b",
                r"\bcan't figure out\b|\bdon't understand\b"
            ]
        }
        
        # 編譯正則表達式
        compiled_patterns = {}
        for category, pattern_list in patterns.items():
            compiled_patterns[category] = [re.compile(pattern, re.IGNORECASE) for pattern in pattern_list]
            
        return compiled_patterns
        
    def should_activate(self, text: str, context: Optional[Dict] = None) -> Dict[str, any]:
        """
        判斷是否應該啟動 UEP
        
        Args:
            text: 識別到的文字
            context: 額外上下文信息
            
        Returns:
            dict: 啟動決策結果
        """
        result = {
            "should_activate": False,
            "confidence": 0.0,
            "reason": "",
            "category": None,
            "keywords_found": []
        }
        
        if not self.enabled or not text.strip():
            return result
            
        # 1. 關鍵字檢測
        keyword_score = self._check_keywords(text)
        
        # 2. 模式匹配
        pattern_result = self._check_patterns(text)
        
        # 3. 對話上下文分析
        context_score = self._analyze_conversation_context(text)
        
        # 4. 綜合評分
        total_confidence = (keyword_score * 0.4 + 
                          pattern_result["confidence"] * 0.4 + 
                          context_score * 0.2)
        
        # 收集發現的關鍵字
        keywords_found = self._find_keywords_in_text(text)
        
        # 決策
        if total_confidence >= self.activation_confidence:
            result.update({
                "should_activate": True,
                "confidence": total_confidence,
                "reason": f"匹配模式: {pattern_result['category'] or '關鍵字'}, 信心度: {total_confidence:.2f}",
                "category": pattern_result["category"],
                "keywords_found": keywords_found
            })
            
            # 更新對話狀態
            self._update_conversation_state(text, activated=True)
            
            info_log(f"[SmartActivation] 啟動觸發: '{text}' (信心度: {total_confidence:.2f})")
            
        else:
            result.update({
                "confidence": total_confidence,
                "reason": f"信心度不足: {total_confidence:.2f} < {self.activation_confidence}",
                "keywords_found": keywords_found
            })
            
            debug_log(2, f"[SmartActivation] 未啟動: '{text}' (信心度: {total_confidence:.2f})")
            
        return result
        
    def _check_keywords(self, text: str) -> float:
        """檢查關鍵字匹配"""
        if not self.context_keywords:
            return 0.0
            
        found_keywords = 0
        text_lower = text.lower()
        
        for keyword in self.context_keywords:
            if keyword.lower() in text_lower:
                found_keywords += 1
                
        return min(found_keywords / len(self.context_keywords), 1.0)
        
    def _check_patterns(self, text: str) -> Dict[str, any]:
        """檢查模式匹配"""
        best_match = {"category": None, "confidence": 0.0}
        
        for category, patterns in self.activation_patterns.items():
            for pattern in patterns:
                if pattern.search(text):
                    # 簡單的信心度計算（基於匹配長度）
                    match_confidence = min(len(text) / 50.0, 1.0)  # 較長的文字可能表達更明確的意圖
                    
                    if match_confidence > best_match["confidence"]:
                        best_match = {
                            "category": category,
                            "confidence": match_confidence
                        }
                        
        return best_match
        
    def _analyze_conversation_context(self, text: str) -> float:
        """分析對話上下文"""
        current_time = time.time()
        
        # 檢查是否在對話中
        if self.last_interaction_time and (current_time - self.last_interaction_time) < 30:
            # 在對話中，較容易啟動
            context_score = 0.6
        else:
            # 新對話開始
            context_score = 0.2
            
        # 更新對話歷史
        self._update_conversation_history(text)
        
        return context_score
        
    def _find_keywords_in_text(self, text: str) -> List[str]:
        """在文字中查找關鍵字"""
        found = []
        text_lower = text.lower()
        
        for keyword in self.context_keywords:
            if keyword.lower() in text_lower:
                found.append(keyword)
                
        return found
        
    def _update_conversation_state(self, text: str, activated: bool = False):
        """更新對話狀態"""
        current_time = time.time()
        self.last_interaction_time = current_time
        
        if activated:
            self.conversation_active = True
            
    def _update_conversation_history(self, text: str):
        """更新對話歷史"""
        current_time = time.time()
        
        # 添加到歷史
        self.conversation_context.append({
            "text": text,
            "timestamp": current_time
        })
        
        # 限制歷史大小
        if len(self.conversation_context) > 10:
            self.conversation_context.pop(0)
            
    def set_conversation_mode(self, active: bool):
        """設置對話模式"""
        self.conversation_active = active
        if active:
            self.last_interaction_time = time.time()
        info_log(f"[SmartActivation] 對話模式: {'啟用' if active else '停用'}")
        
    def add_context_keyword(self, keyword: str):
        """添加上下文關鍵字"""
        if keyword not in self.context_keywords:
            self.context_keywords.append(keyword)
            info_log(f"[SmartActivation] 添加關鍵字: {keyword}")
            
    def get_activation_stats(self) -> Dict[str, any]:
        """獲取啟動統計信息"""
        return {
            "enabled": self.enabled,
            "keywords_count": len(self.context_keywords),
            "conversation_active": self.conversation_active,
            "last_interaction": self.last_interaction_time,
            "context_history_size": len(self.conversation_context)
        }
