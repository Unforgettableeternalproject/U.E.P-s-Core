# core/strategies.py
"""
核心框架的路由策略和決策引擎實現

提供不同的路由策略來支援各種使用場景：
- 智能路由策略：基於模組能力和依賴關係
- 優先級路由策略：基於模組優先級
- 條件路由策略：基於上下文條件
"""

from typing import Dict, Any, List, Optional, Set
from enum import Enum, auto
import time

from core.framework import RouteStrategy, DecisionEngine, ModuleInfo, UEPState
from utils.debug_helper import debug_log, info_log, error_log


class RoutingPriority(Enum):
    """路由優先級"""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4


class SmartRouteStrategy:
    """智能路由策略 - 基於模組能力、依賴關係和上下文進行路由"""
    
    def __init__(self):
        self.name = "smart_route"
        # 預定義的路由規則
        self.route_rules = {
            "chat": {
                "required_capabilities": ["nlp", "memory", "language_model", "speech_synthesis"],
                "optional_capabilities": ["emotion_analysis", "personalization"],
                "execution_order": ["nlp", "mem", "llm", "tts"]
            },
            "command": {
                "required_capabilities": ["nlp", "language_model", "system_control"],
                "optional_capabilities": ["memory", "speech_synthesis"],
                "execution_order": ["nlp", "llm", "sys"]
            },
            "voice_recognition": {
                "required_capabilities": ["speech_recognition", "speaker_identification"],
                "optional_capabilities": ["language_model"],
                "execution_order": ["stt"]
            },
            "system_task": {
                "required_capabilities": ["system_control"],
                "optional_capabilities": ["language_model", "memory"],
                "execution_order": ["sys"]
            }
        }
    
    def calculate_route(self, 
                       intent: str, 
                       context: Dict[str, Any], 
                       available_modules: Dict[str, ModuleInfo]) -> List[str]:
        """
        計算智能路由
        
        Args:
            intent: 處理意圖
            context: 上下文資訊
            available_modules: 可用模組字典
            
        Returns:
            優化的模組執行順序
        """
        debug_log(2, f"[SmartRouteStrategy] 計算路由 - 意圖: {intent}")
        
        # 獲取路由規則
        rule = self.route_rules.get(intent)
        if not rule:
            # 未知意圖，使用預設路由
            return self._default_fallback_route(intent, available_modules)
        
        # 檢查必需的能力
        required_modules = self._find_modules_by_capabilities(
            rule["required_capabilities"], available_modules
        )
        
        if not required_modules:
            error_log(f"[SmartRouteStrategy] 找不到滿足需求的模組: {intent}")
            return []
        
        # 檢查可選能力
        optional_modules = self._find_modules_by_capabilities(
            rule["optional_capabilities"], available_modules
        )
        
        # 考慮上下文優化路由
        optimized_route = self._optimize_route_by_context(
            rule["execution_order"], required_modules, optional_modules, context
        )
        
        debug_log(2, f"[SmartRouteStrategy] 計算結果: {' → '.join(optimized_route)}")
        return optimized_route
    
    def _find_modules_by_capabilities(self, 
                                    capabilities: List[str], 
                                    available_modules: Dict[str, ModuleInfo]) -> Dict[str, ModuleInfo]:
        """根據能力查找模組"""
        matching_modules = {}
        
        for capability in capabilities:
            for module_id, module_info in available_modules.items():
                if capability in module_info.capabilities:
                    matching_modules[module_id] = module_info
                    break
        
        return matching_modules
    
    def _optimize_route_by_context(self, 
                                 base_order: List[str],
                                 required_modules: Dict[str, ModuleInfo],
                                 optional_modules: Dict[str, ModuleInfo],
                                 context: Dict[str, Any]) -> List[str]:
        """根據上下文優化路由"""
        optimized_route = []
        
        # 處理基本執行順序
        for module_id in base_order:
            if module_id in required_modules:
                optimized_route.append(module_id)
        
        # 根據上下文添加可選模組
        current_state = context.get('current_state', UEPState.IDLE)
        
        # 如果是工作狀態，優先考慮系統控制模組
        if current_state == UEPState.WORK:
            for module_id in optional_modules:
                if 'system_control' in optional_modules[module_id].capabilities:
                    if module_id not in optimized_route:
                        optimized_route.append(module_id)
        
        # 如果有活躍的工作上下文，考慮記憶模組
        if context.get('has_working_context', False):
            for module_id in optional_modules:
                if 'memory' in optional_modules[module_id].capabilities:
                    if module_id not in optimized_route:
                        optimized_route.insert(-1, module_id)  # 插入到最後一個之前
        
        return optimized_route
    
    def _default_fallback_route(self, intent: str, available_modules: Dict[str, ModuleInfo]) -> List[str]:
        """預設後備路由"""
        # 簡單的模組選擇邏輯
        route = []
        
        # 總是嘗試包含語言模型
        for module_id, module_info in available_modules.items():
            if 'language_model' in module_info.capabilities:
                route.append(module_id)
                break
        
        return route


class PriorityRouteStrategy:
    """優先級路由策略 - 基於模組優先級進行路由"""
    
    def __init__(self):
        self.name = "priority_route"
    
    def calculate_route(self, 
                       intent: str, 
                       context: Dict[str, Any], 
                       available_modules: Dict[str, ModuleInfo]) -> List[str]:
        """基於優先級計算路由"""
        # 按優先級排序模組
        sorted_modules = sorted(
            available_modules.items(),
            key=lambda x: x[1].priority,
            reverse=True
        )
        
        # 根據意圖篩選相關模組
        relevant_modules = []
        for module_id, module_info in sorted_modules:
            if self._is_module_relevant(intent, module_info):
                relevant_modules.append(module_id)
        
        return relevant_modules[:3]  # 限制最多3個模組
    
    def _is_module_relevant(self, intent: str, module_info: ModuleInfo) -> bool:
        """判斷模組是否與意圖相關"""
        # 簡單的相關性判斷
        if intent == "chat":
            return any(cap in module_info.capabilities 
                      for cap in ["nlp", "language_model", "speech_synthesis"])
        elif intent == "command":
            return any(cap in module_info.capabilities 
                      for cap in ["nlp", "language_model", "system_control"])
        return True


class ContextAwareDecisionEngine(DecisionEngine):
    """上下文感知決策引擎"""
    
    def __init__(self):
        self.name = "context_aware"
        self.decision_history = []
    
    def make_decision(self, 
                     current_state: UEPState,
                     context: Dict[str, Any],
                     available_options: List[str]) -> Dict[str, Any]:
        """
        基於上下文做出決策
        
        Args:
            current_state: 當前系統狀態
            context: 上下文資訊
            available_options: 可用選項
            
        Returns:
            決策結果
        """
        decision_time = time.time()
        
        # 分析上下文
        context_analysis = self._analyze_context(current_state, context)
        
        # 評估選項
        option_scores = self._evaluate_options(available_options, context_analysis)
        
        # 選擇最佳選項
        best_option = max(option_scores.items(), key=lambda x: x[1])
        
        decision_result = {
            "selected_option": best_option[0],
            "confidence": best_option[1],
            "reasoning": context_analysis.get("primary_factor", "default"),
            "timestamp": decision_time,
            "alternatives": sorted(option_scores.items(), key=lambda x: x[1], reverse=True)[1:3]
        }
        
        # 記錄決策歷史
        self.decision_history.append(decision_result)
        
        debug_log(2, f"[ContextAwareDecisionEngine] 決策: {best_option[0]} (信心度: {best_option[1]:.2f})")
        
        return decision_result
    
    def _analyze_context(self, current_state: UEPState, context: Dict[str, Any]) -> Dict[str, Any]:
        """分析上下文"""
        analysis = {
            "state_factor": 1.0,
            "urgency_factor": 1.0,
            "history_factor": 1.0,
            "primary_factor": "default"
        }
        
        # 狀態因子
        if current_state == UEPState.ERROR:
            analysis["state_factor"] = 0.5
            analysis["primary_factor"] = "error_recovery"
        elif current_state == UEPState.WORK:
            analysis["state_factor"] = 1.2
            analysis["primary_factor"] = "work_priority"
        
        # 緊急度因子
        if context.get("priority") == "high":
            analysis["urgency_factor"] = 1.5
            analysis["primary_factor"] = "high_priority"
        
        # 歷史因子
        recent_decisions = self.decision_history[-5:] if self.decision_history else []
        if len(recent_decisions) > 3:
            # 如果最近決策太頻繁，降低激進性
            analysis["history_factor"] = 0.8
        
        return analysis
    
    def _evaluate_options(self, options: List[str], analysis: Dict[str, Any]) -> Dict[str, float]:
        """評估選項分數"""
        scores = {}
        
        for option in options:
            base_score = 1.0
            
            # 根據選項類型調整分數
            if "llm" in option:
                base_score *= 1.2  # LLM 通常是核心選項
            elif "sys" in option:
                base_score *= analysis["urgency_factor"]  # 系統操作受緊急度影響
            elif "mem" in option:
                base_score *= analysis["history_factor"]  # 記憶操作受歷史因子影響
            
            # 應用狀態因子
            final_score = base_score * analysis["state_factor"]
            
            scores[option] = final_score
        
        return scores


class ConditionalRouteStrategy:
    """條件路由策略 - 基於條件分支進行路由"""
    
    def __init__(self):
        self.name = "conditional_route"
        self.conditions = {}
    
    def add_condition(self, condition_name: str, condition_func: callable, route: List[str]):
        """添加條件路由規則"""
        self.conditions[condition_name] = {
            "condition": condition_func,
            "route": route
        }
    
    def calculate_route(self, 
                       intent: str, 
                       context: Dict[str, Any], 
                       available_modules: Dict[str, ModuleInfo]) -> List[str]:
        """基於條件計算路由"""
        # 檢查所有條件
        for condition_name, rule in self.conditions.items():
            if rule["condition"](intent, context, available_modules):
                debug_log(2, f"[ConditionalRouteStrategy] 匹配條件: {condition_name}")
                return rule["route"]
        
        # 沒有匹配的條件，使用預設路由
        return self._default_route(intent, available_modules)
    
    def _default_route(self, intent: str, available_modules: Dict[str, ModuleInfo]) -> List[str]:
        """預設路由"""
        if intent == "chat":
            return ["llm"]
        elif intent == "command":
            return ["sys"]
        else:
            return list(available_modules.keys())[:1]  # 取第一個可用模組


# 預設策略實例
smart_strategy = SmartRouteStrategy()
priority_strategy = PriorityRouteStrategy()
conditional_strategy = ConditionalRouteStrategy()
context_decision_engine = ContextAwareDecisionEngine()


# 添加一些預設的條件路由規則
def is_emergency_context(intent: str, context: Dict[str, Any], available_modules: Dict[str, ModuleInfo]) -> bool:
    """檢查是否為緊急上下文"""
    return context.get("priority") == "emergency" or context.get("current_state") == UEPState.ERROR

def has_active_workflow(intent: str, context: Dict[str, Any], available_modules: Dict[str, ModuleInfo]) -> bool:
    """檢查是否有活躍的工作流"""
    return context.get("has_active_session", False) or context.get("current_state") == UEPState.WORK

# 註冊條件路由規則
conditional_strategy.add_condition(
    "emergency_bypass", 
    is_emergency_context, 
    ["sys"]  # 緊急情況直接路由到系統模組
)

conditional_strategy.add_condition(
    "workflow_priority", 
    has_active_workflow, 
    ["sys", "llm"]  # 有活躍工作流時優先系統模組
)
