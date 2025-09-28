# modules/llm_module/learning_engine.py
"""
學習引擎

負責分析和記錄使用者的：
- 對話偏好和風格
- 系統使用習慣  
- 互動模式
- 個性化需求

這些資料會回饋給身份管理系統，持續改善使用者體驗。
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from collections import defaultdict, Counter
from utils.debug_helper import debug_log, info_log, error_log


@dataclass
class InteractionPattern:
    """互動模式記錄"""
    timestamp: float
    interaction_type: str  # chat, work, command
    user_input: str
    system_response: str
    response_length: int
    satisfaction_score: Optional[float] = None  # 用戶滿意度（如果有反饋）
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass  
class ConversationStyle:
    """對話風格分析 - 累積評分制"""
    # 累積評分 (-1.0 到 1.0)
    formality_score: float = 0.0        # 正式程度累積評分
    detail_score: float = 0.0           # 詳細程度累積評分  
    technical_score: float = 0.0        # 技術程度累積評分
    interaction_score: float = 0.0      # 互動偏好累積評分
    
    # 評分統計
    total_signals: int = 0              # 總信號數量
    total_interactions: int = 0         # 總互動次數
    signal_confidence: float = 0.0      # 信號可信度 (0.0-1.0)
    last_updated: float = 0.0           # 最後更新時間
    
    # 互動統計
    avg_input_length: float = 0.0       # 平均輸入長度
    avg_response_preference: float = 0.0 # 平均回應長度偏好
    
    # 衰減係數 - 隨時間降低舊評分的權重
    decay_factor: float = 0.95          
    min_signals_for_confidence: int = 10  # 最小信號數量才有可信度
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SystemUsagePattern:
    """系統使用習慣"""
    most_used_functions: List[str] = None
    preferred_interaction_times: List[int] = None  # 小時
    session_duration_preference: str = "medium"    # short, medium, long
    help_seeking_frequency: str = "medium"         # low, medium, high
    
    # 功能使用統計
    function_usage_count: Dict[str, int] = None
    interaction_time_distribution: Dict[int, int] = None
    
    def __post_init__(self):
        if self.most_used_functions is None:
            self.most_used_functions = []
        if self.preferred_interaction_times is None:
            self.preferred_interaction_times = []
        if self.function_usage_count is None:
            self.function_usage_count = {}
        if self.interaction_time_distribution is None:
            self.interaction_time_distribution = {}
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LearningEngine:
    """學習引擎"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.storage_path = Path("memory/learning_data")
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # 學習配置
        self.learning_enabled = config.get("enabled", True)
        self.min_interactions_for_analysis = config.get("min_interactions", 5)
        self.analysis_window_days = config.get("analysis_window_days", 30)
        
        # 數據存儲
        self.interaction_history: List[InteractionPattern] = []
        self.conversation_styles: Dict[str, ConversationStyle] = {}  # identity_id -> style
        self.usage_patterns: Dict[str, SystemUsagePattern] = {}    # identity_id -> pattern
        
        # 分析快取
        self._analysis_cache = {}
        self._last_analysis_time = 0.0
        self._analysis_interval = 3600  # 1小時重新分析一次
        
        # 載入現有數據
        self._load_learning_data()
        
        debug_log(2, f"[LearningEngine] 學習引擎初始化完成，學習功能: {'啟用' if self.learning_enabled else '停用'}")
    
    def record_interaction(self, identity_id: str, interaction_type: str,
                          user_input: str, system_response: str, 
                          metadata: Optional[Dict] = None):
        """記錄一次互動"""
        if not self.learning_enabled:
            return
        
        try:
            # 創建互動記錄
            pattern = InteractionPattern(
                timestamp=time.time(),
                interaction_type=interaction_type,
                user_input=user_input[:500],  # 限制長度
                system_response=system_response[:1000],
                response_length=len(system_response)
            )
            
            # 添加滿意度評分（如果有）
            if metadata and "satisfaction_score" in metadata:
                pattern.satisfaction_score = metadata["satisfaction_score"]
            
            self.interaction_history.append(pattern)
            
            # 觸發增量分析
            self._incremental_analysis(identity_id, pattern)
            
            debug_log(3, f"[LearningEngine] 記錄互動: {identity_id} - {interaction_type}")
            
        except Exception as e:
            error_log(f"[LearningEngine] 記錄互動失敗: {e}")
    
    def process_learning_signals(self, identity_id: str, signals: Dict[str, float]):
        """處理學習信號，累積評分"""
        if not self.learning_enabled:
            return
        
        try:
            # 獲取或創建對話風格記錄
            if identity_id not in self.conversation_styles:
                self.conversation_styles[identity_id] = ConversationStyle()
            
            style = self.conversation_styles[identity_id]
            current_time = time.time()
            
            # 計算時間衰減因子
            if style.last_updated > 0:
                time_diff = current_time - style.last_updated
                # 每天衰減 5%
                decay = style.decay_factor ** (time_diff / 86400)
                
                # 對現有評分應用衰減
                style.formality_score *= decay
                style.detail_score *= decay
                style.technical_score *= decay
                style.interaction_score *= decay
                style.total_signals = int(style.total_signals * decay)
            
            # 累積新信號
            weight = 1.0  # 新信號的權重
            
            if "formality_signal" in signals:
                style.formality_score = self._update_score(style.formality_score, signals["formality_signal"], weight)
            
            if "detail_signal" in signals:
                style.detail_score = self._update_score(style.detail_score, signals["detail_signal"], weight)
                
            if "technical_signal" in signals:
                style.technical_score = self._update_score(style.technical_score, signals["technical_signal"], weight)
                
            if "interaction_signal" in signals:
                style.interaction_score = self._update_score(style.interaction_score, signals["interaction_signal"], weight)
            
            # 更新統計
            style.total_signals += 1
            style.last_updated = current_time
            
            # 計算可信度 - 基於信號數量和一致性
            style.signal_confidence = min(1.0, style.total_signals / style.min_signals_for_confidence)
            
            debug_log(3, f"[LearningEngine] 處理學習信號: {identity_id}, 總信號: {style.total_signals}")
            
        except Exception as e:
            error_log(f"[LearningEngine] 處理學習信號失敗: {e}")
    
    def _update_score(self, current_score: float, new_signal: float, weight: float) -> float:
        """更新累積評分，使用加權平均"""
        # 使用 Exponential Moving Average
        alpha = 0.1  # 學習率
        return current_score * (1 - alpha) + new_signal * alpha
    
    def get_user_preferences(self, identity_id: str) -> Dict[str, Any]:
        """獲取用戶偏好摘要"""
        if identity_id not in self.conversation_styles:
            return {"confidence": 0.0, "preferences": "insufficient_data"}
        
        style = self.conversation_styles[identity_id]
        
        # 只有達到最小可信度才返回偏好
        if style.signal_confidence < 0.3:
            return {"confidence": style.signal_confidence, "preferences": "insufficient_data"}
        
        preferences = {}
        
        # 根據評分判斷偏好類型（只有明顯偏好才標註）
        threshold = 0.3  # 偏好閾值
        
        if abs(style.formality_score) > threshold:
            preferences["formality"] = "formal" if style.formality_score > 0 else "casual"
        
        if abs(style.detail_score) > threshold:
            preferences["detail"] = "detailed" if style.detail_score > 0 else "brief"
            
        if abs(style.technical_score) > threshold:
            preferences["technical"] = "technical" if style.technical_score > 0 else "simple"
            
        if abs(style.interaction_score) > threshold:
            preferences["interaction"] = "interactive" if style.interaction_score > 0 else "independent"
        
        return {
            "confidence": style.signal_confidence,
            "preferences": preferences,
            "scores": {
                "formality": style.formality_score,
                "detail": style.detail_score,
                "technical": style.technical_score,
                "interaction": style.interaction_score
            },
            "total_signals": style.total_signals
        }

    def analyze_conversation_style(self, identity_id: str) -> ConversationStyle:
        """分析對話風格"""
        if identity_id in self.conversation_styles:
            style = self.conversation_styles[identity_id]
        else:
            style = ConversationStyle()
            self.conversation_styles[identity_id] = style
        
        # 如果互動數量不足，返回預設風格
        user_interactions = [p for p in self.interaction_history 
                           if self._get_identity_from_interaction(p) == identity_id]
        
        if len(user_interactions) < self.min_interactions_for_analysis:
            return style
        
        try:
            # 分析輸入長度偏好
            input_lengths = [len(p.user_input) for p in user_interactions]
            style.avg_input_length = sum(input_lengths) / len(input_lengths)
            
            # 分析回應長度偏好
            response_lengths = [p.response_length for p in user_interactions]
            style.avg_response_preference = sum(response_lengths) / len(response_lengths)
            
            # 分析正式程度（基於用詞和句式）
            style.formality_preference = self._analyze_formality(user_interactions)
            
            # 分析詳細程度偏好
            style.verbosity_preference = self._analyze_verbosity_preference(user_interactions)
            
            # 分析問題頻率
            style.question_frequency = self._analyze_question_frequency(user_interactions)
            
            # 分析技術水平
            style.technical_level = self._analyze_technical_level(user_interactions)
            
            style.total_interactions = len(user_interactions)
            
            debug_log(3, f"[LearningEngine] 分析對話風格完成: {identity_id}")
            
        except Exception as e:
            error_log(f"[LearningEngine] 對話風格分析失敗: {e}")
        
        return style
    
    def analyze_usage_patterns(self, identity_id: str) -> SystemUsagePattern:
        """分析系統使用習慣"""
        if identity_id in self.usage_patterns:
            pattern = self.usage_patterns[identity_id]
        else:
            pattern = SystemUsagePattern()
            self.usage_patterns[identity_id] = pattern
        
        # 獲取該用戶的互動記錄
        user_interactions = [p for p in self.interaction_history 
                           if self._get_identity_from_interaction(p) == identity_id]
        
        if len(user_interactions) < self.min_interactions_for_analysis:
            return pattern
        
        try:
            # 分析互動時間分佈
            interaction_hours = [int(time.strftime("%H", time.localtime(p.timestamp))) 
                               for p in user_interactions]
            pattern.interaction_time_distribution = dict(Counter(interaction_hours))
            
            # 找出偏好的互動時間
            if pattern.interaction_time_distribution:
                top_hours = Counter(pattern.interaction_time_distribution).most_common(3)
                pattern.preferred_interaction_times = [hour for hour, count in top_hours]
            
            # 分析功能使用頻率
            work_interactions = [p for p in user_interactions if p.interaction_type == "work"]
            function_mentions = []
            for interaction in work_interactions:
                # 這裡應該解析系統功能提及，暫時使用簡單方法
                if "檔案" in interaction.user_input:
                    function_mentions.append("file_management")
                if "搜尋" in interaction.user_input or "查找" in interaction.user_input:
                    function_mentions.append("search")
                if "設定" in interaction.user_input:
                    function_mentions.append("settings")
            
            pattern.function_usage_count = dict(Counter(function_mentions))
            pattern.most_used_functions = [func for func, count in 
                                         Counter(function_mentions).most_common(5)]
            
            # 分析求助頻率
            help_keywords = ["幫助", "怎麼", "如何", "不知道", "不會"]
            help_interactions = [p for p in user_interactions 
                               if any(keyword in p.user_input for keyword in help_keywords)]
            help_ratio = len(help_interactions) / len(user_interactions)
            
            if help_ratio > 0.3:
                pattern.help_seeking_frequency = "high"
            elif help_ratio > 0.1:
                pattern.help_seeking_frequency = "medium" 
            else:
                pattern.help_seeking_frequency = "low"
            
            debug_log(3, f"[LearningEngine] 分析使用習慣完成: {identity_id}")
            
        except Exception as e:
            error_log(f"[LearningEngine] 使用習慣分析失敗: {e}")
        
        return pattern
    
    def get_personalization_suggestions(self, identity_id: str) -> Dict[str, Any]:
        """獲取個性化建議"""
        style = self.analyze_conversation_style(identity_id)
        pattern = self.analyze_usage_patterns(identity_id)
        
        suggestions = {
            "conversation_adjustments": {
                "formality": style.formality_preference,
                "verbosity": style.verbosity_preference,
                "response_length_target": int(style.avg_response_preference) if style.avg_response_preference > 0 else 200
            },
            "system_optimizations": {
                "suggested_functions": pattern.most_used_functions[:3],
                "optimal_interaction_times": pattern.preferred_interaction_times[:2],
                "help_support_level": pattern.help_seeking_frequency
            },
            "learning_insights": {
                "total_interactions": style.total_interactions,
                "technical_comfort": style.technical_level,
                "interaction_style": "questioning" if style.question_frequency == "high" else "direct"
            }
        }
        
        return suggestions
    
    def _incremental_analysis(self, identity_id: str, new_pattern: InteractionPattern):
        """增量分析新互動"""
        try:
            # 更新對話風格
            if identity_id not in self.conversation_styles:
                self.conversation_styles[identity_id] = ConversationStyle()
            
            style = self.conversation_styles[identity_id]
            style.total_interactions += 1
            
            # 更新平均輸入長度
            old_avg = style.avg_input_length
            style.avg_input_length = (old_avg * (style.total_interactions - 1) + len(new_pattern.user_input)) / style.total_interactions
            
            # 更新平均回應長度偏好
            old_response_avg = style.avg_response_preference
            style.avg_response_preference = (old_response_avg * (style.total_interactions - 1) + new_pattern.response_length) / style.total_interactions
            
        except Exception as e:
            error_log(f"[LearningEngine] 增量分析失敗: {e}")
    
    def _analyze_formality(self, interactions: List[InteractionPattern]) -> str:
        """分析正式程度"""
        formal_indicators = ["請", "謝謝", "不好意思", "麻煩"]
        casual_indicators = ["啊", "哦", "嗯", "吧"]
        
        formal_score = 0
        casual_score = 0
        
        for interaction in interactions:
            text = interaction.user_input
            formal_score += sum(1 for indicator in formal_indicators if indicator in text)
            casual_score += sum(1 for indicator in casual_indicators if indicator in text)
        
        if formal_score > casual_score * 2:
            return "formal"
        elif casual_score > formal_score * 2:
            return "casual"
        else:
            return "neutral"
    
    def _analyze_verbosity_preference(self, interactions: List[InteractionPattern]) -> str:
        """分析詳細程度偏好"""
        avg_response_length = sum(p.response_length for p in interactions) / len(interactions)
        
        if avg_response_length > 500:
            return "detailed"
        elif avg_response_length < 150:
            return "brief"
        else:
            return "moderate"
    
    def _analyze_question_frequency(self, interactions: List[InteractionPattern]) -> str:
        """分析問題頻率"""
        question_count = sum(1 for p in interactions if "?" in p.user_input or "？" in p.user_input)
        question_ratio = question_count / len(interactions)
        
        if question_ratio > 0.5:
            return "high"
        elif question_ratio > 0.2:
            return "medium"
        else:
            return "low"
    
    def _analyze_technical_level(self, interactions: List[InteractionPattern]) -> str:
        """分析技術水平"""
        technical_keywords = ["程式", "代碼", "API", "資料庫", "演算法", "框架"]
        beginner_keywords = ["怎麼", "如何", "不會", "教我"]
        
        technical_score = 0
        beginner_score = 0
        
        for interaction in interactions:
            text = interaction.user_input
            technical_score += sum(1 for keyword in technical_keywords if keyword in text)
            beginner_score += sum(1 for keyword in beginner_keywords if keyword in text)
        
        if technical_score > beginner_score and technical_score > len(interactions) * 0.2:
            return "advanced"
        elif beginner_score > technical_score:
            return "beginner"
        else:
            return "mixed"
    
    def _get_identity_from_interaction(self, pattern: InteractionPattern) -> str:
        """從互動記錄中提取身份ID（簡化實現）"""
        # 在實際實現中，這應該從 metadata 或其他方式獲取
        # 這裡使用簡化的方法
        return "default_user"
    
    def _load_learning_data(self):
        """載入學習數據"""
        try:
            # 載入互動歷史
            history_file = self.storage_path / "interaction_history.json"
            if history_file.exists():
                with open(history_file, 'r', encoding='utf-8') as f:
                    history_data = json.load(f)
                self.interaction_history = [InteractionPattern(**item) for item in history_data]
            
            # 載入對話風格
            styles_file = self.storage_path / "conversation_styles.json"
            if styles_file.exists():
                with open(styles_file, 'r', encoding='utf-8') as f:
                    styles_data = json.load(f)
                self.conversation_styles = {k: ConversationStyle(**v) for k, v in styles_data.items()}
            
            # 載入使用習慣
            patterns_file = self.storage_path / "usage_patterns.json"
            if patterns_file.exists():
                with open(patterns_file, 'r', encoding='utf-8') as f:
                    patterns_data = json.load(f)
                self.usage_patterns = {k: SystemUsagePattern(**v) for k, v in patterns_data.items()}
            
            info_log(f"[LearningEngine] 載入學習數據完成，互動記錄: {len(self.interaction_history)} 條")
            
        except Exception as e:
            error_log(f"[LearningEngine] 載入學習數據失敗: {e}")
    
    def save_learning_data(self):
        """保存學習數據"""
        try:
            # 保存互動歷史（只保留最近的記錄）
            recent_interactions = self.interaction_history[-1000:]  # 最多保留1000條記錄
            history_file = self.storage_path / "interaction_history.json"
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump([pattern.to_dict() for pattern in recent_interactions], 
                         f, ensure_ascii=False, indent=2, default=str)
            
            # 保存對話風格
            styles_file = self.storage_path / "conversation_styles.json"
            with open(styles_file, 'w', encoding='utf-8') as f:
                json.dump({k: v.to_dict() for k, v in self.conversation_styles.items()}, 
                         f, ensure_ascii=False, indent=2)
            
            # 保存使用習慣
            patterns_file = self.storage_path / "usage_patterns.json"
            with open(patterns_file, 'w', encoding='utf-8') as f:
                json.dump({k: v.to_dict() for k, v in self.usage_patterns.items()}, 
                         f, ensure_ascii=False, indent=2)
            
            debug_log(2, "[LearningEngine] 學習數據保存完成")
            
        except Exception as e:
            error_log(f"[LearningEngine] 保存學習數據失敗: {e}")
    
    def get_learning_summary(self, identity_id: str) -> Dict[str, Any]:
        """獲取學習摘要"""
        style = self.conversation_styles.get(identity_id, ConversationStyle())
        pattern = self.usage_patterns.get(identity_id, SystemUsagePattern())
        
        return {
            "identity_id": identity_id,
            "total_interactions": style.total_interactions,
            "conversation_style": style.to_dict(),
            "usage_patterns": pattern.to_dict(),
            "last_analysis": self._last_analysis_time,
            "learning_enabled": self.learning_enabled
        }
    
    def clear_learning_data(self, identity_id: Optional[str] = None):
        """清除學習數據"""
        if identity_id is None:
            # 清除所有數據
            self.interaction_history.clear()
            self.conversation_styles.clear()
            self.usage_patterns.clear()
            info_log("[LearningEngine] 清除所有學習數據")
        else:
            # 清除特定用戶的數據
            if identity_id in self.conversation_styles:
                del self.conversation_styles[identity_id]
            if identity_id in self.usage_patterns:
                del self.usage_patterns[identity_id]
            
            # 過濾互動歷史
            self.interaction_history = [p for p in self.interaction_history 
                                      if self._get_identity_from_interaction(p) != identity_id]
            info_log(f"[LearningEngine] 清除用戶 {identity_id} 的學習數據")
    
    def get_learning_statistics(self) -> Dict[str, Any]:
        """獲取學習引擎統計數據"""
        return {
            "total_interactions": len(self.interaction_history),
            "users_count": len(self.conversation_styles),
            "patterns_learned": len(self.usage_patterns),
            "recent_interactions": len([p for p in self.interaction_history 
                                      if (datetime.now() - p.timestamp).days < 7]),
            "average_response_length": sum(p.response_length for p in self.interaction_history) / 
                                     max(len(self.interaction_history), 1),
            "learning_enabled": self.learning_enabled
        }