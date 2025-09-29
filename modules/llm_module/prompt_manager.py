# modules/llm_module/prompt_manager.py
"""
Prompt Manager - Integrated prompt building functionality

Responsible for dynamically building prompts based on different states and modes:
- CHAT mode: Personalized conversation prompts
- WORK mode: Workflow guidance prompts  
- System values integration
- Identity information integration
- Memory context integration
- Module interface integration (MEM/SYS)
"""

import os
import time
from typing import Dict, Any, Optional, List, Callable
from pathlib import Path
from utils.debug_helper import debug_log, info_log, error_log
from core.status_manager import status_manager
from .module_interfaces import LegacyModuleDataProvider


class PromptManager:
    """提示詞管理器 - 使用靜態配置和動態模組資料構建提示詞"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # Load static system instructions from config
        self.system_instructions = config.get("system_instructions", {})
        
        # Module data provider for dynamic content
        self.data_provider = LegacyModuleDataProvider()
        
        # Cache for built prompts
        self._template_cache = {}
        
        debug_log(2, "[PromptManager] 提示詞管理器初始化完成（支援靜態配置與動態模組資料）")
    
    def build_chat_prompt(self, user_input: str, identity_context: Optional[Dict] = None,
                         memory_context: Optional[str] = None, 
                         conversation_history: Optional[List] = None,
                         is_internal: bool = False,
                         relevant_memories: Optional[List[Dict]] = None) -> str:
        """構建對話模式提示詞 - 整合靜態配置與動態模組資料"""
        
        prompt_parts = []
        
        # 檢查是否有 {system_values} 佔位符（在替換前檢查）
        base_has_placeholder = "{system_values}" in self.system_instructions.get("base_personality", "")
        chat_has_placeholder = "{system_values}" in self.system_instructions.get("chat_mode", "")
        
        # 基礎人格（總是包含，除非是內部調用）
        if not is_internal and "base_personality" in self.system_instructions:
            base_instruction = self.system_instructions["base_personality"]
            base_instruction = self._replace_system_values_placeholder(base_instruction)
            prompt_parts.append(base_instruction)
        
        # 對話模式特定指令（非內部調用才添加）
        if not is_internal and "chat_mode" in self.system_instructions:
            chat_instruction = self.system_instructions["chat_mode"]
            chat_instruction = self._replace_system_values_placeholder(chat_instruction)
            prompt_parts.append(chat_instruction)
        
        # 系統狀態資訊（如果指令中沒有 {system_values} 佔位符才添加）
        if not is_internal and not (base_has_placeholder or chat_has_placeholder):
            status_info = self._build_status_context_with_guide()
            if status_info:
                prompt_parts.append(status_info)
        
        # 身份資訊
        identity_info = self._build_identity_context(identity_context)
        if identity_info:
            prompt_parts.append(identity_info)
        
        # 記憶上下文 - 從 MEM 模組獲取或使用傳入的內容，並整合檢索到的記憶
        memory_section = self._build_memory_context(memory_context, relevant_memories)
        if memory_section:
            prompt_parts.append(memory_section)
        
        # 對話歷史
        history_section = self._build_conversation_history(conversation_history)
        if history_section:
            prompt_parts.append(history_section)
        
        # 用戶輸入
        user_section = f"User: {user_input}"
        prompt_parts.append(user_section)
        
        # 回應引導
        if not is_internal:
            prompt_parts.append("Please respond as U.E.P, considering current mood and status.")
        
        return "\n\n".join(prompt_parts)
    
    def build_work_prompt(self, user_input: str, available_functions: Optional[str] = None,
                         workflow_context: Optional[Dict] = None,
                         identity_context: Optional[Dict] = None) -> str:
        """構建工作模式提示詞 - 整合系統功能與工作流上下文"""
        
        prompt_parts = []
        
        # 檢查是否有 {system_values} 佔位符（在替換前檢查）
        base_has_placeholder = "{system_values}" in self.system_instructions.get("base_personality", "")
        work_has_placeholder = "{system_values}" in self.system_instructions.get("work_mode", "")
        
        # 基礎人格（總是包含）
        if "base_personality" in self.system_instructions:
            base_instruction = self.system_instructions["base_personality"]
            base_instruction = self._replace_system_values_placeholder(base_instruction)
            prompt_parts.append(base_instruction)
        
        # 工作模式特定指令
        if "work_mode" in self.system_instructions:
            work_instruction = self.system_instructions["work_mode"]
            work_instruction = self._replace_system_values_placeholder(work_instruction)
            prompt_parts.append(work_instruction)
        
        # 系統狀態（如果指令中沒有 {system_values} 佔位符才添加）
        if not (base_has_placeholder or work_has_placeholder):
            status_info = self._build_status_context_with_guide(focus_on_work=True)
            if status_info:
                prompt_parts.append(status_info)
        
        # 身份資訊（簡化版）
        identity_info = self._build_identity_context(identity_context, simplified=True)
        if identity_info:
            prompt_parts.append(identity_info)
        
        # 可用系統功能 - 從 SYS 模組獲取或使用傳入的內容
        functions_section = self._build_functions_context(available_functions)
        if functions_section:
            prompt_parts.append(functions_section)
        
        # 工作流上下文
        if workflow_context:
            workflow_section = self._build_workflow_context(workflow_context)
            if workflow_section:
                prompt_parts.append(workflow_section)
        
        # 用戶請求
        request_section = f"User Request: {user_input}"
        prompt_parts.append(request_section)
        
        # 工作模式指引
        work_guidance = (
            "Please analyze the user's request and decide:\n"
            "1. If system function execution is needed, provide sys_action recommendation\n"
            "2. Provide appropriate user feedback\n"
            "3. If more information is needed, ask for specific details"
        )
        prompt_parts.append(work_guidance)
        
        return "\n\n".join(prompt_parts)
    
    def build_direct_prompt(self, user_input: str) -> str:
        """構建直接模式提示詞（最小化）"""
        return user_input
    
    def _build_status_context_with_guide(self, focus_on_work: bool = False) -> Optional[str]:
        """構建系統狀態上下文 - 包含系統值說明（英文版）"""
        try:
            modifiers = status_manager.get_personality_modifiers()
            
            # Convert status to English
            status_text = self._format_system_values_english(modifiers)
            
            parts = []
            
            if focus_on_work:
                # Work mode focuses on efficiency-related states
                parts.append(f"Current Status: {status_text}")
            else:
                # Chat mode includes full status information
                parts.append(f"Current Status: {status_text}")
            
            # Add system values explanations from config
            system_values_guide = self.config.get("system_values_guide", {})
            if system_values_guide:
                explanations = []
                for value_name, explanation_text in system_values_guide.items():
                    if explanation_text and isinstance(explanation_text, str):
                        # Clean up the explanation text
                        clean_explanation = explanation_text.strip()
                        if clean_explanation:
                            explanations.append(f"{value_name.upper()}:\n{clean_explanation}")
                
                if explanations:
                    parts.append("System Values Guide:\n" + "\n\n".join(explanations))
            
            return "\n\n".join(parts)
                
        except Exception as e:
            error_log(f"[PromptManager] 構建狀態上下文失敗: {e}")
            return None
    
    def _format_system_values_english(self, modifiers: Dict[str, Any]) -> str:
        """格式化系統值為英文格式"""
        status_mapping = {
            "非常積極": "very positive", "積極": "positive", "中性": "neutral",
            "消極": "negative", "非常消極": "very negative",
            "非常自信": "very confident", "自信": "confident", "普通": "normal", 
            "沒有自信": "not confident", "非常沒有自信": "very unconfident",
            "非常願意幫助": "very helpful", "願意幫助": "helpful", "中等": "moderate",
            "不太願意幫助": "less helpful", "不願意幫助": "unhelpful",
            "非常無聊": "very bored", "無聊": "bored", "有點無聊": "slightly bored",
            "不無聊": "not bored", "感興趣": "interested"
        }
        
        mood_en = status_mapping.get(modifiers['mood_level'], modifiers['mood_level'])
        pride_en = status_mapping.get(modifiers['pride_level'], modifiers['pride_level'])
        helpfulness_en = status_mapping.get(modifiers['helpfulness_level'], modifiers['helpfulness_level'])
        boredom_en = status_mapping.get(modifiers['boredom_level'], modifiers['boredom_level'])
        
        return f"mood={mood_en}, pride={pride_en}, helpfulness={helpfulness_en}, boredom={boredom_en}"
    
    def _build_memory_context(self, memory_context: Optional[str] = None, 
                             relevant_memories: Optional[List[Dict]] = None) -> Optional[str]:
        """構建記憶上下文區段 - 整合檢索到的記憶"""
        context_parts = []
        
        # 原有的記憶上下文
        if memory_context:
            context_parts.append(f"Memory Context:\n{memory_context}")
        
        # 新的檢索記憶
        if relevant_memories:
            memory_text_parts = ["Retrieved Relevant Memories:"]
            for i, memory in enumerate(relevant_memories, 1):
                memory_type = memory.get("type", "general")
                content = memory.get("content", "")
                
                if memory_type == "conversation":
                    user_input = memory.get("user_input", "")
                    assistant_response = memory.get("assistant_response", "")
                    memory_text_parts.append(f"{i}. [Conversation] User: {user_input} | Assistant: {assistant_response}")
                elif memory_type == "user_info":
                    memory_text_parts.append(f"{i}. [User Info] {content}")
                else:
                    memory_text_parts.append(f"{i}. [{memory_type}] {content}")
                    
            context_parts.append("\n".join(memory_text_parts))
        
        # 如果都沒有，嘗試從 MEM 模組獲取 (向後兼容)
        if not context_parts:
            try:
                mem_data = self.data_provider.get_mem_data("context")
                if mem_data and isinstance(mem_data, dict):
                    mem_memories = mem_data.get("relevant_memories", "")
                    if mem_memories:
                        context_parts.append(f"Relevant Memory:\n{mem_memories}")
            except Exception as e:
                debug_log(3, f"[PromptManager] 無法從 MEM 模組獲取記憶資料: {e}")
        
        return "\n\n".join(context_parts) if context_parts else None
    
    def _build_functions_context(self, available_functions: Optional[str] = None) -> Optional[str]:
        """構建系統功能上下文 - 優先使用傳入內容，否則從 SYS 模組獲取"""
        if available_functions:
            return f"Available System Functions:\n{available_functions}"
        
        # Try to get functions data from SYS module
        try:
            sys_data = self.data_provider.get_sys_data("function_registry")
            if sys_data:
                if isinstance(sys_data, (list, tuple, set)):
                    return "Available System Functions:\n" + "\n".join(map(str, sys_data))
                if isinstance(sys_data, dict) and sys_data.get("available_functions"):
                    return "Available System Functions:\n" + "\n".join(sys_data["available_functions"])
            return None
        except Exception as e:
            debug_log(3, f"[PromptManager] 無法從 SYS 模組獲取功能資料: {e}")
        
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
                # Simplified version with basic info only
                name = identity.get("name", "User")
                return f"User: {name}"
            else:
                # Full version with preferences
                parts = []
                
                name = identity.get("name", "User")
                parts.append(f"User: {name}")
                
                # Conversation preferences
                conversation_prefs = preferences.get("conversation", {})
                if conversation_prefs:
                    formality = conversation_prefs.get("formality", "neutral")
                    verbosity = conversation_prefs.get("verbosity", "moderate")
                    parts.append(f"Preferences: {formality} formality, {verbosity} verbosity")
                
                return "; ".join(parts)
                
        except Exception as e:
            error_log(f"[PromptManager] 構建身份上下文失敗: {e}")
            return None
    
    def _build_conversation_history(self, history: Optional[List]) -> Optional[str]:
        """構建對話歷史上下文"""
        if not history or len(history) == 0:
            return None
        
        try:
            # Only take recent conversations
            recent_history = history[-3:] if len(history) > 3 else history
            
            history_parts = []
            for entry in recent_history:
                # 處理 ConversationEntry 對象或字典
                if hasattr(entry, 'role') and hasattr(entry, 'content'):
                    # 這是 ConversationEntry 對象
                    role = entry.role
                    content = entry.content
                elif isinstance(entry, dict):
                    # 這是字典格式
                    role = entry.get("role", "unknown")
                    content = entry.get("content", "")
                else:
                    # 未知格式，跳過
                    continue
                
                if role == "user":
                    history_parts.append(f"User: {content}")
                elif role == "assistant":
                    history_parts.append(f"U.E.P: {content}")
            
            if history_parts:
                return "Recent Conversation:\n" + "\n".join(history_parts)
                
        except Exception as e:
            error_log(f"[PromptManager] 構建對話歷史失敗: {e}")
            
        return None
    
    def _build_workflow_context(self, workflow_context: Dict) -> Optional[str]:
        """構建工作流上下文"""
        try:
            parts = []
            
            # Current step
            current_step = workflow_context.get("current_step")
            if current_step:
                parts.append(f"Current Step: {current_step}")
            
            # Previous result
            previous_result = workflow_context.get("previous_result")
            if previous_result:
                parts.append(f"Previous Result: {previous_result}")
            
            # Remaining steps
            remaining_steps = workflow_context.get("remaining_steps", [])
            if remaining_steps:
                steps_text = ", ".join(remaining_steps[:3])  # Show only first 3
                parts.append(f"Remaining Steps: {steps_text}")
            
            if parts:
                return "Workflow Progress: " + "; ".join(parts)
                
        except Exception as e:
            error_log(f"[PromptManager] 構建工作流上下文失敗: {e}")
            
        return None
    
    def _replace_system_values_placeholder(self, instruction: str) -> str:
        """替換系統指令中的 {system_values} 佔位符"""
        if "{system_values}" not in instruction:
            return instruction
        
        try:
            # Use the same status context building logic
            status_context = self._build_status_context_with_guide()
            system_values_text = status_context if status_context else ""
            
            return instruction.replace("{system_values}", system_values_text)
            
        except Exception as e:
            error_log(f"[PromptManager] 替換系統值佔位符失敗: {e}")
            return instruction.replace("{system_values}", "")
    
    def get_system_instruction(self, mode: str = "main") -> str:
        """獲取系統指令"""
        return self.system_instructions.get(mode, self.system_instructions.get("main", ""))
    
    def update_system_instruction(self, mode: str, instruction: str):
        """更新系統指令"""
        self.system_instructions[mode] = instruction
        debug_log(2, f"[PromptManager] 更新系統指令: {mode}")
    
    def register_mem_provider(self, data_type: str, provider_func: Callable):
        """註冊 MEM 模組資料提供者"""
        self.data_provider.register_mem_provider(data_type, provider_func)
        debug_log(2, f"[PromptManager] MEM 模組資料提供者已註冊: {data_type}")
    
    def register_sys_provider(self, data_type: str, provider_func: Callable):
        """註冊 SYS 模組資料提供者"""
        self.data_provider.register_sys_provider(data_type, provider_func)
        debug_log(2, f"[PromptManager] SYS 模組資料提供者已註冊: {data_type}")
    
    def get_template_stats(self) -> Dict[str, Any]:
        """獲取模板使用統計"""
        data_info = self.data_provider.get_available_data_types()
        return {
            "cached_templates": len(self._template_cache),
            "available_instructions": list(self.system_instructions.keys()),
            "mem_providers_count": len(data_info.get("mem_data_types", [])),
            "sys_providers_count": len(data_info.get("sys_data_types", [])),
            "total_providers": data_info.get("total_providers", 0)
        }