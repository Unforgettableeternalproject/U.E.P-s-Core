# modules/llm_module/prompt_manager.py
"""
提示詞管理器 - 整合原 prompt_builder 功能

負責根據不同狀態和模式動態構建提示詞，支援：
- CHAT 狀態：個性化對話提示
- WORK 狀態：工作流程引導提示  
- 系統數值整合
- 身份資訊整合
- 記憶上下文整合
"""

import os
from typing import Dict, Any, Optional, List
from pathlib import Path
from utils.debug_helper import debug_log, info_log, error_log
from core.status_manager import status_manager


class PromptManager:
    """提示詞管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.system_instructions = config.get("system_instruction", {})
        
        # 提示詞模板快取
        self._template_cache = {}
        
        debug_log(2, "[PromptManager] 提示詞管理器初始化完成")
    
    def build_chat_prompt(self, user_input: str, identity_context: Optional[Dict] = None,
                         memory_context: Optional[str] = None, 
                         conversation_history: Optional[List] = None,
                         is_internal: bool = False) -> str:
        """構建對話模式提示詞"""
        
        prompt_parts = []
        
        # 系統指令（非內部調用才添加）
        if not is_internal and "main" in self.system_instructions:
            prompt_parts.append(self.system_instructions["main"])
        
        # 系統狀態資訊
        status_info = self._build_status_context()
        if status_info and not is_internal:
            prompt_parts.append(status_info)
        
        # 身份資訊
        identity_info = self._build_identity_context(identity_context)
        if identity_info:
            prompt_parts.append(identity_info)
        
        # 記憶上下文
        if memory_context:
            memory_section = f"相關記憶：\n{memory_context}"
            prompt_parts.append(memory_section)
        
        # 對話歷史
        history_section = self._build_conversation_history(conversation_history)
        if history_section:
            prompt_parts.append(history_section)
        
        # 用戶輸入
        user_section = f"使用者：{user_input}"
        prompt_parts.append(user_section)
        
        # 回應引導
        if not is_internal:
            prompt_parts.append("請以 U.E.P 的身份回應使用者。")
        
        return "\n\n".join(prompt_parts)
    
    def build_work_prompt(self, user_input: str, available_functions: Optional[str] = None,
                         workflow_context: Optional[Dict] = None,
                         identity_context: Optional[Dict] = None) -> str:
        """構建工作模式提示詞"""
        
        prompt_parts = []
        
        # 系統指令 - 工作模式
        if "work" in self.system_instructions:
            prompt_parts.append(self.system_instructions["work"])
        elif "main" in self.system_instructions:
            # 回退到主要指令
            prompt_parts.append(self.system_instructions["main"])
        
        # 系統狀態（影響工作效率）
        status_info = self._build_status_context(focus_on_work=True)
        if status_info:
            prompt_parts.append(status_info)
        
        # 身份資訊（簡化版）
        identity_info = self._build_identity_context(identity_context, simplified=True)
        if identity_info:
            prompt_parts.append(identity_info)
        
        # 可用系統功能
        if available_functions:
            functions_section = f"可用系統功能：\n{available_functions}"
            prompt_parts.append(functions_section)
        
        # 工作流上下文
        if workflow_context:
            workflow_section = self._build_workflow_context(workflow_context)
            if workflow_section:
                prompt_parts.append(workflow_section)
        
        # 用戶請求
        request_section = f"使用者請求：{user_input}"
        prompt_parts.append(request_section)
        
        # 工作模式指引
        work_guidance = (
            "請分析使用者的請求，並決定：\n"
            "1. 如果需要執行系統功能，請提供 sys_action 建議\n"
            "2. 提供適當的使用者回饋\n"
            "3. 如果需要更多資訊，請詢問具體細節"
        )
        prompt_parts.append(work_guidance)
        
        return "\n\n".join(prompt_parts)
    
    def build_direct_prompt(self, user_input: str) -> str:
        """構建直接模式提示詞（最小化）"""
        return user_input
    
    def _build_status_context(self, focus_on_work: bool = False) -> Optional[str]:
        """構建系統狀態上下文"""
        try:
            modifiers = status_manager.get_personality_modifiers()
            
            if focus_on_work:
                # 工作模式重點關注效率相關狀態
                return (
                    f"當前狀態：助人意願 {modifiers['helpfulness_level']}，"
                    f"自信程度 {modifiers['pride_level']}，"
                    f"情緒狀態 {modifiers['mood_level']}"
                )
            else:
                # 對話模式包含完整狀態資訊
                return (
                    f"當前狀態：情緒 {modifiers['mood_level']}，"
                    f"自尊 {modifiers['pride_level']}，"
                    f"助人意願 {modifiers['helpfulness_level']}，"
                    f"無聊程度 {modifiers['boredom_level']}"
                )
                
        except Exception as e:
            error_log(f"[PromptManager] 構建狀態上下文失敗: {e}")
            return None
    
    def _build_identity_context(self, identity_context: Optional[Dict], 
                               simplified: bool = False) -> Optional[str]:
        """構建身份上下文"""
        if not identity_context:
            return None
        
        try:
            identity = identity_context.get("identity", {})
            preferences = identity_context.get("preferences", {})
            
            if simplified:
                # 簡化版，只包含基本資訊
                name = identity.get("name", "使用者")
                return f"使用者：{name}"
            else:
                # 完整版，包含偏好資訊
                parts = []
                
                name = identity.get("name", "使用者")
                parts.append(f"使用者：{name}")
                
                # 對話偏好
                conversation_prefs = preferences.get("conversation", {})
                if conversation_prefs:
                    formality = conversation_prefs.get("formality", "neutral")
                    verbosity = conversation_prefs.get("verbosity", "moderate")
                    parts.append(f"對話偏好：{formality} 正式度，{verbosity} 詳細度")
                
                return "；".join(parts)
                
        except Exception as e:
            error_log(f"[PromptManager] 構建身份上下文失敗: {e}")
            return None
    
    def _build_conversation_history(self, history: Optional[List]) -> Optional[str]:
        """構建對話歷史上下文"""
        if not history or len(history) == 0:
            return None
        
        try:
            # 只取最近幾條對話
            recent_history = history[-3:] if len(history) > 3 else history
            
            history_parts = []
            for entry in recent_history:
                role = entry.get("role", "unknown")
                content = entry.get("content", "")
                
                if role == "user":
                    history_parts.append(f"使用者：{content}")
                elif role == "assistant":
                    history_parts.append(f"U.E.P：{content}")
            
            if history_parts:
                return "最近對話：\n" + "\n".join(history_parts)
                
        except Exception as e:
            error_log(f"[PromptManager] 構建對話歷史失敗: {e}")
            
        return None
    
    def _build_workflow_context(self, workflow_context: Dict) -> Optional[str]:
        """構建工作流上下文"""
        try:
            parts = []
            
            # 當前步驟
            current_step = workflow_context.get("current_step")
            if current_step:
                parts.append(f"當前步驟：{current_step}")
            
            # 上一步結果
            previous_result = workflow_context.get("previous_result")
            if previous_result:
                parts.append(f"上一步結果：{previous_result}")
            
            # 剩餘步驟
            remaining_steps = workflow_context.get("remaining_steps", [])
            if remaining_steps:
                steps_text = "、".join(remaining_steps[:3])  # 只顯示前3個
                parts.append(f"待執行步驟：{steps_text}")
            
            if parts:
                return "工作流進度：" + "；".join(parts)
                
        except Exception as e:
            error_log(f"[PromptManager] 構建工作流上下文失敗: {e}")
            
        return None
    
    def get_system_instruction(self, mode: str = "main") -> str:
        """獲取系統指令"""
        return self.system_instructions.get(mode, self.system_instructions.get("main", ""))
    
    def update_system_instruction(self, mode: str, instruction: str):
        """更新系統指令"""
        self.system_instructions[mode] = instruction
        debug_log(2, f"[PromptManager] 更新系統指令: {mode}")
    
    def get_template_stats(self) -> Dict[str, Any]:
        """獲取模板使用統計"""
        return {
            "cached_templates": len(self._template_cache),
            "available_instructions": list(self.system_instructions.keys())
        }