"""
簡化路由器 - 純文字處理版本
根據新架構設計：NLP 負責狀態管理，Router 只處理純文字路由

設計原則：
1. Router 只接收純文字輸入/輸出
2. NLP 直接管理狀態設置和上下文
3. Router 根據當前狀態進行簡單的模組路由
4. 專注於文字傳遞，不處理複雜意圖分析
"""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum
import time
import re

from core.states.state_manager import UEPState, state_manager
from core.working_context import working_context_manager
from utils.debug_helper import debug_log, info_log, error_log


class TextSource(Enum):
    """文字來源類型"""
    USER_INPUT = "user_input"      # 來自使用者（NLP處理後）
    SYSTEM_OUTPUT = "system_output"  # 來自處理層


@dataclass
class Input:
    """簡化的文字輸入結構"""
    text: str                    # 純文字內容
    source: TextSource          # 來源類型
    source_module: str          # 來源模組名稱
    metadata: Dict[str, Any] = None  # type: ignore # 簡單元數據


@dataclass
class TextRoutingDecision:
    """文字路由決策"""
    target_module: str          # 目標模組
    text_content: str          # 要傳遞的文字
    routing_metadata: Dict[str, Any] = None  # type: ignore # 路由元數據
    reasoning: str = ""        # 決策原因


class Router:
    """簡化文字路由器"""
    
    def __init__(self):
        """初始化簡化路由器"""
        self.state_manager = state_manager
        self.context_manager = working_context_manager
        
        # 狀態-模組映射（簡化版）
        self.state_routing_map = {
            UEPState.CHAT: {
                "primary": "llm",
                "secondary": ["mem"],
            },
            UEPState.WORK: {
                "primary": "sys", 
                "secondary": ["llm"],
            },
            UEPState.IDLE: {
                "primary": None,
                "secondary": [],
            }
        }
        
        # 表情符號過濾正則表達式
        # 匹配所有 emoji 和特殊 Unicode 字符
        self.emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F680-\U0001F6FF"  # transport & map symbols
            "\U0001F1E0-\U0001F1FF"  # flags (iOS)
            "\U00002702-\U000027B0"  # dingbats
            "\U000024C2-\U0001F251"
            "\U0001F900-\U0001F9FF"  # supplemental symbols
            "\U0001FA00-\U0001FAFF"  # extended symbols
            "\x01-\x1F"              # 控制字符
            "]+", 
            flags=re.UNICODE
        )
        
        info_log("[SimpleRouter] 簡化文字路由器初始化完成")
    
    def route_text(self, text_input: Input) -> TextRoutingDecision:
        """
        路由純文字到適當的處理模組
        
        Args:
            text_input: 簡化的文字輸入
            
        Returns:
            TextRoutingDecision: 路由決策
        """
        debug_log(2, f"[SimpleRouter] 處理文字路由 - 來源: {text_input.source.value}")
        debug_log(3, f"[SimpleRouter] 文字內容: {text_input.text[:50]}...")
        
        # 0. 對系統輸出過濾表情符號 (防止 TTS 處理錯誤)
        filtered_text = text_input.text
        if text_input.source == TextSource.SYSTEM_OUTPUT:
            filtered_text = self._filter_emojis(text_input.text)
        
        # 1. 獲取當前系統狀態
        current_state = self.state_manager.get_current_state()
        debug_log(2, f"[SimpleRouter] 當前狀態: {current_state}")
        
        # 2. 根據狀態決定目標模組
        target_module = self._decide_target_module(current_state, text_input)
        
        # 3. 創建路由決策
        decision = TextRoutingDecision(
            target_module=target_module,
            text_content=filtered_text,  # 使用過濾後的文字
            routing_metadata={
                "source": text_input.source.value,
                "source_module": text_input.source_module,
                "current_state": current_state.value,
                "timestamp": time.time(),
                "emoji_filtered": filtered_text != text_input.text
            },
            reasoning=f"狀態:{current_state.value} -> 模組:{target_module}"
        )
        
        debug_log(1, f"[SimpleRouter] 路由決策: {decision.reasoning}")
        return decision
    
    def _filter_emojis(self, text: str) -> str:
        """
        過濾文字中的表情符號和特殊字符
        
        Args:
            text: 原始文字
            
        Returns:
            str: 過濾後的文字
        """
        if not text:
            return text
            
        filtered = self.emoji_pattern.sub('', text)
        
        # 如果過濾掉了內容，記錄日誌
        if filtered != text:
            debug_log(2, f"[SimpleRouter] 過濾表情符號: {len(text)} -> {len(filtered)} 字符")
        
        return filtered.strip()
    
    def _decide_target_module(self, current_state: UEPState, text_input: Input) -> str:
        """
        根據當前狀態決定目標模組
        
        Args:
            current_state: 當前系統狀態
            text_input: 文字輸入
            
        Returns:
            str: 目標模組名稱
        """
        # 系統輸出優先路由到輸出層
        if text_input.source == TextSource.SYSTEM_OUTPUT:
            debug_log(3, "[SimpleRouter] 系統輸出，路由到 TTS")
            return "tts"
        
        # 階段三：WORK 狀態下檢查工作流輸入需求
        if current_state == UEPState.WORK and text_input.source == TextSource.USER_INPUT:
            # 檢查是否有工作流正在等待輸入
            is_workflow_waiting = self.context_manager.is_workflow_waiting_input()
            
            if is_workflow_waiting:
                debug_log(2, "[SimpleRouter] 工作流等待輸入，路由到 SYS 模組")
                return "sys"
            else:
                # 工作流不需要輸入，正常路由到 SYS 處理
                debug_log(3, "[SimpleRouter] WORK 狀態，路由到 SYS 模組")
                return "sys"
        
        # 使用者輸入根據狀態決定處理模組
        if current_state in self.state_routing_map:
            primary_module = self.state_routing_map[current_state]["primary"]
            
            if primary_module:
                debug_log(3, f"[SimpleRouter] 狀態 {current_state.value} -> 主要模組: {primary_module}")
                return primary_module
        
        # 預設情況：使用者輸入給 LLM
        debug_log(3, "[SimpleRouter] 使用者輸入，預設路由到 LLM")
        return "llm"
    
    def route_user_input(self, text: str, source_module: str = "nlp") -> TextRoutingDecision:
        """
        路由使用者輸入文字的便利方法
        
        Args:
            text: 使用者輸入文字
            source_module: 來源模組名稱
            
        Returns:
            TextRoutingDecision: 路由決策
        """
        text_input = Input(
            text=text,
            source=TextSource.USER_INPUT,
            source_module=source_module
        )
        return self.route_text(text_input)
    
    def route_system_output(self, text: str, source_module: str) -> TextRoutingDecision:
        """
        路由系統輸出文字的便利方法
        
        Args:
            text: 系統輸出文字
            source_module: 來源模組名稱
            
        Returns:
            TextRoutingDecision: 路由決策
        """
        text_input = Input(
            text=text,
            source=TextSource.SYSTEM_OUTPUT,
            source_module=source_module
        )
        return self.route_text(text_input)
    
    def get_routing_info(self) -> Dict[str, Any]:
        """獲取路由器狀態信息"""
        current_state = self.state_manager.get_current_state()
        
        return {
            "router_type": "Router",
            "current_state": current_state.value,
            "state_routing_map": {
                state.value: config for state, config in self.state_routing_map.items()
            },
            "supported_sources": [source.value for source in TextSource],
        }


# 創建全局實例（保持向後兼容的名稱）
router = Router()
simple_router = router  # 別名，支援兩種名稱