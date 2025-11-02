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
from .module_interfaces import state_aware_interface


class PromptManager:
    """提示詞管理器 - 使用靜態配置和動態模組資料構建提示詞"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # Load static system instructions from config
        self.system_instructions = config.get("system_instructions", {})
        
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
                         identity_context: Optional[Dict] = None,
                         workflow_hint: Optional[str] = None,
                         use_mcp_tools: bool = False,
                         suppress_start_workflow_instruction: bool = False) -> str:
        """構建工作模式提示詞 - 整合系統功能與工作流上下文
        
        Args:
            suppress_start_workflow_instruction: 當已有工作流運行時，抑制「立即啟動工作流」的強制指示
        """
        
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
        
        # 可用系統功能 - 只在不使用 MCP tools 時才添加文字描述
        if not use_mcp_tools:
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
        
        # ✅ 工作模式指引（根據是否使用 MCP tools 而不同）
        if use_mcp_tools:
            work_guidance = (
                "Available Workflows:\n"
                "- drop_and_read: Read file content (for requests like 'read file', 'show content', 'open file')\n"
                "- intelligent_archive: Smart file organization (for 'organize', 'archive', 'sort files')\n"
                "- summarize_tag: Summarize and tag files (for 'summarize', 'tag', 'analyze files')\n"
                "- file_processing: General file operations (for other file tasks)\n\n"
            )
            
            # ✅ NLP 工作流提示（如果有）- 放在可用工作流之後
            if workflow_hint:
                if isinstance(workflow_hint, dict):
                    workflow_name = workflow_hint.get('workflow_name', 'unknown')
                    confidence = workflow_hint.get('confidence', 0)
                    work_guidance += (
                        f"**NLP Analysis Result:**\n"
                        f"The system has analyzed the user's request and identified a matching workflow:\n"
                        f"- Recommended workflow: '{workflow_name}'\n"
                        f"- Match confidence: {confidence:.2f}\n"
                        f"- YOU MUST use this workflow name as the 'workflow_type' parameter\n\n"
                    )
                elif isinstance(workflow_hint, str):
                    work_guidance += (
                        f"**NLP Analysis Result:**\n"
                        f"- Recommended workflow: '{workflow_hint}'\n"
                        f"- YOU MUST use this workflow name as the 'workflow_type' parameter\n\n"
                    )
                else:
                    work_guidance += f"**NLP suggests:** {workflow_hint}\n\n"
            
            # ✅ 只在沒有活躍工作流時才添加「立即啟動」指示
            if not suppress_start_workflow_instruction:
                work_guidance += (
                    "**CRITICAL INSTRUCTIONS - DO NOT IGNORE:**\n"
                    "1. YOU MUST IMMEDIATELY call the 'start_workflow' function. DO NOT ask for clarification.\n"
                    "2. Required parameters:\n"
                    "   - workflow_type: Use the NLP recommended workflow above (if provided) or select from the available workflows list\n"
                    "   - command: Copy the user's original request exactly as provided\n"
                    "   - initial_data: Use empty dict {} (the workflow will collect missing information)\n"
                    "3. DO NOT respond with plain text asking for more information\n"
                    "4. DO NOT say you need the file name - the workflow will handle file selection\n"
                    "5. The workflow system is designed to collect missing information interactively AFTER it starts\n\n"
                    "**REPEAT: You MUST call start_workflow immediately. Do not ask questions first.**\n\n"
                    "Example:\n"
                    "User: 'read the file'\n"
                    "YOU MUST: call start_workflow(workflow_type='drop_and_read', command='read the file', initial_data={})"
                )
            else:
                # 已有工作流運行，檢查是否為步驟回應上下文
                # ✅ 修復：確保 workflow_context 不為 None
                is_step_response = (workflow_context is not None and 
                                   workflow_context.get('type') == 'workflow_step_response')
                
                if is_step_response:
                    # ✅ 步驟已完成，LLM 應該生成回應，不要呼叫工具
                    work_guidance += (
                        "\n**Instructions:**\n"
                        "A workflow step has completed. The step data is provided in the context above.\n"
                        "Your task:\n"
                        "1. Read the workflow data from the context\n"
                        "2. Generate a natural, friendly response in ENGLISH explaining the result to the user\n"
                        "3. DO NOT call any MCP tools (review_step, approve_step, etc.) - just provide a text response\n"
                        "4. The workflow context already contains all the data you need\n"
                    )
                else:
                    # 工作流正在進行中，等待用戶輸入或系統操作
                    work_guidance += (
                        "\n**Instructions:**\n"
                        "The workflow is currently running. Based on the situation:\n"
                        "- If you need to check workflow status: use get_workflow_status\n"
                        "- If the workflow is waiting for user input: provide guidance on what's needed\n"
                        "- DO NOT call start_workflow again - a workflow is already active\n"
                    )
        else:
            work_guidance = (
                "Instructions:\n"
                "1. Analyze the user's request against the Available System Functions above\n"
                "2. If the request matches a system function:\n"
                "   - Set sys_action.action to the appropriate action type (start_workflow, execute_function, or provide_options)\n"
                "   - Set sys_action.target to the exact function name from the list\n"
                "   - Provide sys_action.reason explaining why this function matches\n"
                "   - Include any required parameters in sys_action.parameters\n"
                "3. If the request is unclear or needs more information:\n"
                "   - Ask for specific clarification in your text response\n"
                "   - Set sys_action.action to 'provide_options' and sys_action.target to 'clarification'\n"
                "4. Always provide a helpful text response to the user"
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
                mem_data = state_aware_interface.get_chat_mem_data("context")
                if mem_data and isinstance(mem_data, dict):
                    mem_memories = mem_data.get("relevant_memories", "")
                    if mem_memories:
                        context_parts.append(f"Relevant Memory:\n{mem_memories}")
            except Exception as e:
                debug_log(1, f"[PromptManager] 無法從 MEM 模組獲取記憶資料: {e}")
        
        return "\n\n".join(context_parts) if context_parts else None
    
    def _build_functions_context(self, available_functions: Optional[str] = None) -> Optional[str]:
        """構建系統功能上下文 - 優先使用傳入內容，否則從 SYS 模組獲取"""
        if available_functions:
            return f"Available System Functions:\n{available_functions}"
        
        # Try to get functions data from SYS module
        try:
            sys_data = state_aware_interface.get_work_sys_data("function_registry")
            if sys_data:
                if isinstance(sys_data, (list, tuple, set)):
                    return "Available System Functions:\n" + "\n".join(map(str, sys_data))
                if isinstance(sys_data, dict) and sys_data.get("available_functions"):
                    return "Available System Functions:\n" + "\n".join(sys_data["available_functions"])
            return None
        except Exception as e:
            debug_log(1, f"[PromptManager] 無法從 SYS 模組獲取功能資料: {e}")
        
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
            # 🆕 檢查是否為工作流步驟回應類型
            context_type = workflow_context.get("type")
            
            if context_type == "workflow_step_response":
                # 這是工作流步驟完成，需要 LLM 生成用戶回應
                return self._build_workflow_step_response_context(workflow_context)
            
            # 原有邏輯：工作流進度追蹤
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
    
    def _build_workflow_step_response_context(self, workflow_context: Dict) -> str:
        """
        構建工作流步驟回應的上下文（通用框架模式）
        
        這個方法生成通用的指引，讓 LLM 能夠處理任何類型的工作流步驟數據
        而不是針對特定工作流硬編碼邏輯
        
        Args:
            workflow_context: 工作流上下文數據
            
        Returns:
            格式化的上下文字符串
        """
        workflow_type = workflow_context.get('workflow_type', 'unknown')
        is_complete = workflow_context.get('is_complete', False)
        should_end_session = workflow_context.get('should_end_session', False)
        review_data = workflow_context.get('review_data', {})
        step_result = workflow_context.get('step_result', {})
        
        context_parts = []
        
        # 基礎資訊
        context_parts.append("=" * 60)
        context_parts.append("WORKFLOW STEP COMPLETED - GENERATE USER RESPONSE")
        context_parts.append("=" * 60)
        
        context_parts.append(f"\nWorkflow Type: {workflow_type}")
        context_parts.append(f"Step Status: {'COMPLETED (Final)' if is_complete else 'IN PROGRESS'}")
        
        # 步驟結果資訊
        if step_result:
            context_parts.append(f"\nStep Result:")
            if 'success' in step_result:
                context_parts.append(f"  - Success: {step_result['success']}")
            if 'message' in step_result:
                context_parts.append(f"  - Message: {step_result['message']}")
        
        # 工作流數據（通用處理）
        if review_data:
            context_parts.append(f"\nWorkflow Data:")
            for key, value in review_data.items():
                # 格式化不同類型的數據
                if isinstance(value, str):
                    # 長文本截斷預覽
                    if len(value) > 200:
                        preview = value[:200] + f"... ({len(value)} chars total)"
                        context_parts.append(f"  - {key}: {preview}")
                    else:
                        context_parts.append(f"  - {key}: {value}")
                elif isinstance(value, (int, float, bool)):
                    context_parts.append(f"  - {key}: {value}")
                elif isinstance(value, (list, dict)):
                    context_parts.append(f"  - {key}: {type(value).__name__} with {len(value)} items")
                else:
                    context_parts.append(f"  - {key}: {str(value)[:100]}")
        
        # 🔧 通用指引（框架模式，不針對特定工作流）
        context_parts.append("\n" + "=" * 60)
        context_parts.append("YOUR TASK:")
        context_parts.append("=" * 60)
        
        if is_complete:
            context_parts.append("\n✅ The workflow has completed successfully.")
            context_parts.append("\nGenerate a natural, friendly response in ENGLISH that:")
            context_parts.append("1. Acknowledges the workflow completion")
            context_parts.append("2. Summarizes the key results/data provided above")
            context_parts.append("3. If the data contains content in another language (e.g., Chinese):")
            context_parts.append("   - Translate or explain it naturally in English")
            context_parts.append("   - Focus on the main points and key information")
            context_parts.append("4. Keep your response conversational and concise (2-4 sentences)")
            context_parts.append("5. Maintain your U.E.P. personality")
            
            if should_end_session:
                context_parts.append("\n⚠️ IMPORTANT: This is the final response.")
                context_parts.append("   Set session_control={'action': 'end_session'} in metadata")
        else:
            context_parts.append("\n⏳ The workflow is in progress.")
            context_parts.append("\nGenerate a brief progress update in ENGLISH that:")
            context_parts.append("1. Acknowledges the current step completion")
            context_parts.append("2. Briefly mentions what's happening or what will happen next")
            context_parts.append("3. Keep it short and reassuring (1-2 sentences)")
        
        context_parts.append("\n" + "=" * 60)
        context_parts.append("LANGUAGE REQUIREMENT:")
        context_parts.append("=" * 60)
        context_parts.append("⚠️ CRITICAL: Always respond in ENGLISH, regardless of the")
        context_parts.append("   original language in the workflow data.")
        context_parts.append("   Your role is to translate/interpret the data for the user.")
        context_parts.append("=" * 60)
        
        return "\n".join(context_parts)
    
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
        state_aware_interface.register_chat_mem_provider(data_type, provider_func)
        debug_log(2, f"[PromptManager] MEM 模組資料提供者已註冊: {data_type}")
    
    def register_sys_provider(self, data_type: str, provider_func: Callable):
        """註冊 SYS 模組資料提供者"""
        state_aware_interface.register_work_sys_provider(data_type, provider_func)
        debug_log(2, f"[PromptManager] SYS 模組資料提供者已註冊: {data_type}")
    
    def get_template_stats(self) -> Dict[str, Any]:
        """獲取模板使用統計"""
        data_info = state_aware_interface.get_available_data_types()
        return {
            "cached_templates": len(self._template_cache),
            "available_instructions": list(self.system_instructions.keys()),
            "mem_providers_count": len(data_info.get("chat_mem_providers", [])),
            "sys_providers_count": len(data_info.get("work_sys_providers", [])),
            "total_providers": data_info.get("total_providers", 0)
        }