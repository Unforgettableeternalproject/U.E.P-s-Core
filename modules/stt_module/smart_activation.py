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
        self.activation_confidence = self.smart_config.get("activation_confidence", 0.3)  # 智能判斷啟動的信心度閾值
        
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
            ],
            
            # 短指令模式 - 專門處理簡短的指令
            "short_command": [
                r"\bhey\b|\bhi\b|\bhello\b|\byo\b",
                r"\byes\b|\bno\b|\bmaybe\b|\bok\b|\bokay\b",
                r"\bplease\b|\bthanks\b|\bthank you\b|\bsorry\b",
                r"\bhelp\b|\bstop\b|\bpause\b|\bresume\b|\bwait\b",
                r"\bwhat\b|\bwho\b|\bwhen\b|\bwhere\b|\bwhy\b|\bhow\b",
                r"\bshow\b|\btell\b|\bgive\b|\bfind\b|\bget\b",
                r"\bopen\b|\bclose\b|\bexit\b|\bquit\b|\brestart\b",
                r"\bnext\b|\bprevious\b|\bback\b|\bforward\b|\bagain\b",
                r"\bup\b|\bdown\b|\bleft\b|\bright\b|\bcenter\b",
                r"\bone\b|\btwo\b|\bthree\b|\bfour\b|\bfive\b"
            ]
        }
        
        # 編譯正則表達式
        compiled_patterns = {}
        for category, pattern_list in patterns.items():
            compiled_patterns[category] = [re.compile(pattern, re.IGNORECASE) for pattern in pattern_list]
            
        return compiled_patterns
        
    def contains_keywords(self, text: str) -> bool:
        """
        檢查文字是否包含任何關鍵字
        
        Args:
            text: 識別到的文字
            
        Returns:
            bool: 是否包含關鍵字
        """
        if not text or not self.context_keywords:
            return False
            
        text_lower = text.lower()
        for keyword in self.context_keywords:
            if keyword.lower() in text_lower:
                return True
                
        return False
    
    def should_activate(self, text: str, context: Optional[Dict] = None, threshold_reduction: float = 0.0) -> Dict[str, any]:
        """
        判斷是否應該啟動 UEP
        
        Args:
            text: 識別到的文字
            context: 額外上下文信息
            threshold_reduction: 閾值降低的幅度（用於短命令處理）
            
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
        
        # 檢測短指令
        is_short_text = len(text.split()) <= 3  # 不超過3個單詞視為短文本
            
        # 應用閾值調整（短指令時會自動降低門檻）
        if is_short_text:
            # 短指令使用更寬鬆的閾值
            base_reduction = 0.05  # 基礎降低
            adjusted_threshold = max(0.1, self.activation_confidence - base_reduction - threshold_reduction)
            debug_log(3, f"[SmartActivation] 短指令檢測: '{text}' (閾值降低: {base_reduction + threshold_reduction:.2f})")
        else:
            adjusted_threshold = max(0.1, self.activation_confidence - threshold_reduction)
            
        # 1. 關鍵字檢測
        keyword_score = self._check_keywords(text)
        
        # 2. 模式匹配
        pattern_result = self._check_patterns(text)
        
        # 3. 對話上下文分析
        context_score = self._analyze_conversation_context(text)
        
        # 4. 綜合評分 - 對短指令使用不同的權重
        if is_short_text:
            # 對短指令，提高關鍵字和上下文的權重
            total_confidence = (keyword_score * 0.4 + 
                              pattern_result["confidence"] * 0.4 + 
                              context_score * 0.2)
        else:
            # 標準權重
            total_confidence = (keyword_score * 0.3 + 
                              pattern_result["confidence"] * 0.5 + 
                              context_score * 0.2)
        
        # 額外獎勵：如果同時有關鍵字和模式匹配，增加分數
        if keyword_score > 0 and pattern_result["confidence"] > 0:
            bonus = min(keyword_score * pattern_result["confidence"] * 0.2, 0.1)  # 最多額外增加 0.1
            total_confidence += bonus
            debug_log(3, f"[SmartActivation] 額外獎勵: +{bonus:.2f} (關鍵字和模式同時匹配)")
        
        debug_log(3, f"[SmartActivation] 關鍵字分數: {keyword_score:.2f}, 模式分數: {pattern_result['confidence']:.2f}, 上下文分數: {context_score:.2f}, 總分: {total_confidence:.2f}")
        
        # 收集發現的關鍵字
        keywords_found = self._find_keywords_in_text(text)
        
        # 決策 - 使用調整後的閾值
        if total_confidence >= adjusted_threshold:
            result.update({
                "should_activate": True,
                "confidence": total_confidence,
                "reason": f"匹配模式: {pattern_result['category'] or '關鍵字'}, 智能判斷分數: {total_confidence:.2f}",
                "category": pattern_result["category"],
                "keywords_found": keywords_found
            })
            
            # 更新對話狀態
            self._update_conversation_state(text, activated=True)
            
            info_log(f"[SmartActivation] 啟動觸發: '{text}' (智能判斷分數: {total_confidence:.2f})")
            
        else:
            result.update({
                "confidence": total_confidence,
                "reason": f"智能判斷分數不足: {total_confidence:.2f} < {adjusted_threshold}",
                "keywords_found": keywords_found
            })
            
            debug_log(2, f"[SmartActivation] 未啟動: '{text}' (信心度: {total_confidence:.2f})")
            
        return result
        
    def _check_keywords(self, text: str) -> float:
        """檢查關鍵字匹配，針對短指令進行優化"""
        if not self.context_keywords:
            return 0.0
            
        # 獲取完整的關鍵詞匹配列表
        found_keywords = self._find_keywords_in_text(text)
        
        # 檢測是否為短指令
        is_short_text = len(text.split()) <= 3
        
        # 計算匹配分數
        if is_short_text and found_keywords:
            # 短指令中有關鍵詞匹配，給予更高分數
            # 基礎分數 + 匹配數量獎勵
            base_score = 0.3  # 短指令關鍵詞的基礎分數
            match_bonus = min(0.2 * len(found_keywords), 0.4)  # 最多加 0.4
            score = base_score + match_bonus
            
            # 記錄
            debug_log(3, f"[SmartActivation] 短指令關鍵詞匹配 ({len(found_keywords)}): {found_keywords}, 分數={score:.2f}")
            return min(score, 0.8)  # 最高為 0.8
        else:
            # 標準計算方式
            score = min(len(found_keywords) / len(self.context_keywords), 0.7)  # 最高為 0.7
            return score
        
    def _check_patterns(self, text: str) -> Dict[str, any]:
        """檢查模式匹配 - 基於模式匹配數量的改進版"""
        best_match = {"category": None, "confidence": 0.0}
        match_count = 0  # 記錄匹配的模式數量
        matched_categories = set()  # 記錄匹配的分類
        
        # 對短文本使用更寬容的處理
        is_short_text = len(text.split()) <= 3  # 不超過3個單詞視為短文本
        
        for category, patterns in self.activation_patterns.items():
            category_matched = False
            for pattern in patterns:
                if pattern.search(text):
                    match_count += 1
                    category_matched = True
                    # 記錄最長的匹配文本長度
                    match_length = len(pattern.search(text).group(0))
                    length_factor = min(match_length / 20.0, 1.0)  # 匹配文本長度因子
            
            if category_matched:
                matched_categories.add(category)
        
        # 分數計算基於: 1) 匹配模式數量 2) 匹配分類種類 3) 文本長度
        # 對短文本給予更高的權重
        if is_short_text:
            category_factor = min(len(matched_categories) * 0.4, 1.0)  # 提高短文本的類別匹配權重
            count_factor = min(match_count * 0.3, 1.0)  # 提高短文本的匹配總數權重
            text_length_factor = 0.5  # 固定值，不再懲罰短文本
            debug_log(4, f"[SmartActivation] 短文本模式: 使用寬容評分 '{text}'")
        else:
            category_factor = min(len(matched_categories) * 0.3, 1.0)  # 匹配不同類別的數量
            count_factor = min(match_count * 0.2, 1.0)  # 匹配模式總數
            text_length_factor = min(len(text) / 40.0, 1.0)  # 文本長度因子
        
        # 綜合計算分數
        match_confidence = (category_factor * 0.5 + count_factor * 0.3 + text_length_factor * 0.2)
        
        if match_confidence > 0:
            best_category = max(matched_categories, key=lambda c: sum(1 for p in self.activation_patterns[c] if p.search(text)))
            best_match = {
                "category": best_category,
                "confidence": match_confidence,
                "matched_count": match_count,
                "matched_categories": list(matched_categories)
            }
        
        debug_log(4, f"[SmartActivation] 模式匹配: 分數={match_confidence:.2f}, 匹配數={match_count}, 類別數={len(matched_categories)}")
                        
        return best_match
        
    def _analyze_conversation_context(self, text: str) -> float:
        """分析對話上下文 - 針對短指令進行強化處理"""
        current_time = time.time()
        
        # 檢測是否為短指令
        is_short_text = len(text.split()) <= 3
        
        # 基礎分數：短指令在對話中更容易被接受
        if self.last_interaction_time:
            # 計算距離上次互動的時間間隔
            time_diff = current_time - self.last_interaction_time
            
            if time_diff < 10:  # 10秒內的互動視為活躍對話
                # 短指令在活躍對話中獲得更高的基礎分數
                if is_short_text:
                    context_score = 0.7  # 短指令在活躍對話中得分更高
                else:
                    context_score = 0.6  # 正常指令在對話中
            elif time_diff < 30:  # 30秒內的互動視為對話持續中
                if is_short_text:
                    context_score = 0.5  # 短指令在對話持續中仍有優勢
                else:
                    context_score = 0.4  # 正常指令在對話持續中
            else:  # 超過30秒，視為新對話
                if is_short_text:
                    context_score = 0.3  # 短指令在新對話中也有一定優勢
                else:
                    context_score = 0.2  # 新對話開始
        else:
            # 首次互動
            if is_short_text:
                context_score = 0.3  # 短指令首次互動得分較高
            else:
                context_score = 0.2  # 正常首次互動
        
        # 分析對話歷史中的關鍵詞頻率
        if hasattr(self, 'conversation_context') and self.conversation_context:
            # 獲取最近的對話記錄，短指令時看更多歷史上下文
            recent_history_length = 5 if is_short_text else 3
            recent_texts = [entry.get("text", "") for entry in self.conversation_context[-recent_history_length:] if "text" in entry]
            
            # 檢查關鍵詞在歷史記錄中的出現情況
            keyword_bonus = 0.0
            for keyword in self.context_keywords:
                kw_lower = keyword.lower()
                if any(kw_lower in t.lower() for t in recent_texts):
                    # 短指令時關鍵詞獎勵更高
                    bonus = 0.06 if is_short_text else 0.05
                    keyword_bonus += bonus
                    debug_log(4, f"[SmartActivation] 歷史關鍵詞獎勵: +{bonus:.2f} ({keyword})")
            
            # 限制最大獎勵分數
            max_keyword_bonus = 0.35 if is_short_text else 0.3
            keyword_bonus = min(keyword_bonus, max_keyword_bonus)
            context_score += keyword_bonus
            
            debug_log(4, f"[SmartActivation] 上下文分析: 基礎分數={context_score-keyword_bonus:.2f}, 關鍵詞獎勵={keyword_bonus:.2f}, 短指令={is_short_text}")
            
        # 更新對話歷史
        self._update_conversation_history(text)
        
        return min(context_score, 1.0)  # 確保不超過 1.0
        
    def _find_keywords_in_text(self, text: str) -> List[str]:
        """在文字中查找關鍵字，針對短指令進行優化"""
        found = []
        text_lower = text.lower()
        words = text_lower.split()
        
        # 檢測是否為短指令
        is_short_text = len(words) <= 3
        
        for keyword in self.context_keywords:
            kw_lower = keyword.lower()
            
            # 標準匹配
            if kw_lower in text_lower:
                found.append(keyword)
                continue
                
            # 針對短指令的特殊處理
            if is_short_text:
                # 檢查關鍵詞的部分匹配 (至少3個字符且佔關鍵詞50%以上)
                kw_words = kw_lower.split()
                
                # 對單詞關鍵詞進行部分匹配 (例如: "hey" 可匹配 "hey there")
                if len(kw_words) == 1 and len(kw_lower) >= 3:
                    for word in words:
                        # 關鍵詞是單詞的前綴，且至少匹配3個字符或70%的長度
                        min_match_len = min(3, int(len(kw_lower) * 0.7))
                        if word.startswith(kw_lower[:min_match_len]):
                            found.append(keyword)
                            debug_log(4, f"[SmartActivation] 短指令關鍵詞部分匹配: {word} ~ {keyword}")
                            break
                
                # 對於多單詞關鍵詞，檢查是否有足夠的單詞匹配
                elif len(kw_words) > 1 and len(words) > 0:
                    # 計算有多少單詞匹配
                    matching_words = sum(1 for kw in kw_words if any(w.startswith(kw[:3]) for w in words))
                    # 如果50%以上的單詞匹配，則視為匹配
                    if matching_words >= max(1, len(kw_words) // 2):
                        found.append(keyword)
                        debug_log(4, f"[SmartActivation] 短指令關鍵詞多單詞匹配: {matching_words}/{len(kw_words)} ~ {keyword}")
                
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
