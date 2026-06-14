"""
Prompt Manager — 提示詞管理

從 U.E.P Core 的 llm_module/prompt_manager.py 遷出並解耦改造：
- 移除所有 U.E.P Core 內部依賴（debug_helper、status_manager、module_interfaces、user_settings_manager）
- 狀態資訊（system values）改為外部傳入 modifiers dict，不再直接存取 status_manager
- 保留完整的 prompt 組裝邏輯：chat、work、direct 三種模式
- 保留工作流上下文的所有子方法（step_response、input_required、error）
- 移除 Event Bus 耦合（register_mem_provider、register_sys_provider）
"""

import logging
from typing import Dict, Any, Optional, List

from session_control.schemas.message import Message
from session_control.schemas.config import PersonaContext

logger = logging.getLogger(__name__)


class PromptManager:
    """提示詞管理器 — 使用靜態配置和動態模組資料構建提示詞"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

        # 從 config 載入靜態系統指令
        self.system_instructions = config.get("system_instructions", {})

        # 模板快取
        self._template_cache: Dict[str, str] = {}

        logger.debug("[PromptManager] 提示詞管理器初始化完成（靜態配置與動態模組資料）")

    # ------------------------------------------------------------------
    # 公開 API：三種 prompt 構建模式
    # ------------------------------------------------------------------

    def build_chat_prompt(
        self,
        user_input: str,
        identity_context: Optional[Dict] = None,
        memory_context: Optional[str] = None,
        conversation_history: Optional[List] = None,
        is_internal: bool = False,
        intent_metadata: Optional[Dict] = None,
        modifiers: Optional[Dict[str, Any]] = None,
    ) -> str:
        """構建對話模式提示詞 — 整合靜態配置與動態模組資料

        Args:
            user_input:           使用者輸入文字
            identity_context:     身份上下文 dict（可選）
            memory_context:       靜態記憶字串（可選，向後相容）
            conversation_history: 對話歷史列表（可選）
            is_internal:          是否為內部調用（省略人格/模式指令）
            intent_metadata:      意圖元資料，可含 degraded_from_work 旗標
            modifiers:            系統狀態修飾詞 dict（由外部 status_manager 提供）
                                  格式例：{"mood_level": "積極", "pride_level": "自信", ...}
                                  若傳入 None，則不產生 Current Status 段落

        Note:
            記憶檢索已改為 MCP 工具方式，不再需要 relevant_memories 參數。
        """
        prompt_parts = []

        # 檢查 {system_values} 佔位符（在替換前確認）
        base_has_placeholder = "{system_values}" in self.system_instructions.get("base_personality", "")
        chat_has_placeholder = "{system_values}" in self.system_instructions.get("chat_mode", "")

        # 基礎人格（非內部調用才加入）
        if not is_internal and "base_personality" in self.system_instructions:
            base_instruction = self.system_instructions["base_personality"]
            base_instruction = self._replace_system_values_placeholder(base_instruction, modifiers)
            prompt_parts.append(base_instruction)

        # 對話模式特定指令（非內部調用才加入）
        if not is_internal and "chat_mode" in self.system_instructions:
            chat_instruction = self.system_instructions["chat_mode"]
            chat_instruction = self._replace_system_values_placeholder(chat_instruction, modifiers)
            prompt_parts.append(chat_instruction)

        # 系統狀態資訊（若指令中已有佔位符則不重複添加）
        if not is_internal and not (base_has_placeholder or chat_has_placeholder):
            if modifiers is not None:
                status_info = self.build_status_context(modifiers)
                if status_info:
                    prompt_parts.append(status_info)

        # 身份資訊
        identity_info = self._build_identity_context(identity_context)
        if identity_info:
            prompt_parts.append(identity_info)

        # 記憶上下文（僅處理靜態 PROFILE 字串，向後相容）
        memory_section = self._build_memory_context(memory_context)
        if memory_section:
            prompt_parts.append(memory_section)

        # 記憶工具提示（精簡版）
        prompt_parts.append(
            "You have memory tools. User profile facts are provided above. "
            "Use tools to retrieve conversation history or store new user information.\n"
            "**Critical Rules:**\n"
            "1. When user says 'remember this **about me**' → MUST call memory_store_observation with memory_type='profile'\n"
            "2. When user shares personal info (interests, background, preferences) → Call memory_store_observation\n"
            "3. When asking about past info → Call memory_retrieve_profile() first, use actual data in response\n"
            "4. Never make assumptions - always retrieve actual stored data using tools\n"
        )

        # 對話歷史
        history_section = self._build_conversation_history(conversation_history)
        if history_section:
            prompt_parts.append(history_section)

        # 降級警告（WORK 請求降級至 CHAT 時插入）
        degraded_work_warning = None
        if intent_metadata and intent_metadata.get("degraded_from_work"):
            degraded_work_warning = (
                "\n**IMPORTANT CONTEXT - Capability Limitation:**\n"
                "The user's request appears to be asking for a task execution or tool usage, "
                "but there is currently NO matching workflow or tool available in the system to handle this request.\n\n"
                "Just take it easy and reply conversationally.\n\n"
                "**DO NOT:**\n"
                "- Pretend you can execute the task\n"
                "- Give the impression the task will be completed\n"
                "- Provide code or detailed instructions as if you're executing them\n"
            )

        # 使用者輸入
        prompt_parts.append(f"User: {user_input}")

        # 降級警告（置於使用者輸入之後）
        if degraded_work_warning:
            prompt_parts.append(degraded_work_warning)

        # 回應引導
        if not is_internal:
            prompt_parts.append("Please respond as U.E.P, considering current mood and status.")

        return "\n\n".join(prompt_parts)

    def build_work_prompt(
        self,
        user_input: str,
        available_functions: Optional[str] = None,
        workflow_context: Optional[Dict] = None,
        identity_context: Optional[Dict] = None,
        use_mcp_tools: bool = False,
        suppress_start_workflow_instruction: bool = False,
        force_tool_use: bool = False,
        modifiers: Optional[Dict[str, Any]] = None,
    ) -> str:
        """構建工作模式提示詞 — 整合系統功能與工作流上下文

        Args:
            user_input:                           使用者請求文字
            available_functions:                  可用功能的文字描述（非 MCP 模式使用）
            workflow_context:                     工作流上下文 dict（可選）
            identity_context:                     身份上下文 dict（可選）
            use_mcp_tools:                        是否使用 MCP tools（True 則省略文字功能描述）
            suppress_start_workflow_instruction:  已有工作流運行時抑制「啟動」強制指示
            force_tool_use:                       tool_choice=ANY 時為 True，強制調用工具
            modifiers:                            系統狀態修飾詞 dict（由外部 status_manager 提供）
        """
        prompt_parts = []

        # 檢查 {system_values} 佔位符
        base_has_placeholder = "{system_values}" in self.system_instructions.get("base_personality", "")
        work_has_placeholder = "{system_values}" in self.system_instructions.get("work_mode", "")

        # 基礎人格
        if "base_personality" in self.system_instructions:
            base_instruction = self.system_instructions["base_personality"]
            base_instruction = self._replace_system_values_placeholder(base_instruction, modifiers)
            prompt_parts.append(base_instruction)

        # 工作模式特定指令
        if "work_mode" in self.system_instructions:
            work_instruction = self.system_instructions["work_mode"]
            work_instruction = self._replace_system_values_placeholder(work_instruction, modifiers)
            prompt_parts.append(work_instruction)

        # 系統狀態（若指令中已有佔位符則不重複添加）
        if not (base_has_placeholder or work_has_placeholder):
            if modifiers is not None:
                status_info = self.build_status_context(modifiers, focus_on_work=True)
                if status_info:
                    prompt_parts.append(status_info)

        # 身份資訊（簡化版）
        identity_info = self._build_identity_context(identity_context, simplified=True)
        if identity_info:
            prompt_parts.append(identity_info)

        # 可用系統功能（非 MCP 模式才加入文字描述）
        if not use_mcp_tools:
            functions_section = self._build_functions_context(available_functions)
            if functions_section:
                prompt_parts.append(functions_section)

        # 工作流上下文
        if workflow_context:
            workflow_section = self._build_workflow_context(workflow_context)
            if workflow_section:
                prompt_parts.append(workflow_section)

        # 使用者請求
        prompt_parts.append(f"User Request: {user_input}")

        # 工作模式指引（根據 tool_choice 與工作流狀態提供不同指引）
        if not suppress_start_workflow_instruction and use_mcp_tools:
            if force_tool_use:
                # tool_choice=ANY：強制調用工具
                mandatory_guidance = (
                    "\n**IMPORTANT - Tool Usage Required:**\n"
                    "You MUST call one of the available MCP tools to handle this request.\n"
                    "- User's request: '{}'\n"
                    "- Carefully read the function declarations and their descriptions\n"
                    "- Match the user's intent with the tool's purpose (e.g., 'play music' → play_media tool, 'get weather' → get_weather tool)\n"
                    "- Select the tool whose description best matches what the user wants to do\n"
                    "- Call the tool with the correct parameters (extract relevant information from user's request)\n"
                    "- DO NOT provide a text-only response\n"
                ).format(user_input[:150])
                prompt_parts.append(mandatory_guidance)
            else:
                # tool_choice=AUTO：LLM 自行決定
                basic_guidance = (
                    "\n**Instructions:**\n"
                    "You have access to MCP tools (see function declarations) to help with tasks.\n"
                    "- If the user's request requires a specific action (file operations, information lookup, etc.), "
                    "use the appropriate tool by calling the corresponding function\n"
                    "- If you just need to respond conversationally, provide a text response without calling any tools\n"
                    "- The tools have detailed descriptions - choose the most appropriate one for the task\n"
                )
                prompt_parts.append(basic_guidance)
        elif suppress_start_workflow_instruction and workflow_context:
            # 已有工作流運行，提供上下文相關指引
            is_step_response = workflow_context.get("type") == "workflow_step_response"

            if is_step_response:
                # 步驟已完成，LLM 應該生成回應
                work_guidance = (
                    "\n**Instructions:**\n"
                    "A workflow step has completed. The step data is provided in the context above.\n"
                    "Your task:\n"
                    "1. Read the workflow data from the context\n"
                    "2. Generate a natural, friendly response in ENGLISH explaining the result to the user\n"
                    "3. IMPORTANT: You MUST provide a text response - the workflow context contains all the data you need\n"
                )
                prompt_parts.append(work_guidance)

        return "\n\n".join(prompt_parts)

    def build_direct_prompt(self, user_input: str) -> str:
        """構建直接模式提示詞（最小化）"""
        return user_input

    # ------------------------------------------------------------------
    # 狀態上下文（公開方法，modifiers 從外部傳入）
    # ------------------------------------------------------------------

    def build_status_context(
        self,
        modifiers: Dict[str, Any],
        focus_on_work: bool = False,
    ) -> Optional[str]:
        """構建系統狀態上下文 — modifiers 由外部 status_manager 提供

        原 _build_status_context_with_guide() 已解耦：不再直接呼叫 status_manager，
        改為接收外部傳入的 modifiers dict。

        Args:
            modifiers:      personality modifiers dict
                            例：{"mood_level": "積極", "pride_level": "自信",
                                 "helpfulness_level": "願意幫助", "boredom_level": "不無聊",
                                 "helpfulness": 0.7}
            focus_on_work:  若為 True，只輸出效率相關狀態（目前兩種模式內容相同，保留擴展空間）

        Returns:
            格式化的狀態字串，若 modifiers 為空則返回 None
        """
        try:
            parts = []

            # 將狀態值轉為英文格式
            status_text = self._format_system_values_english(modifiers)
            parts.append(f"Current Status: {status_text}")

            # 從 config 加入系統值說明
            system_values_guide = self.config.get("system_values_guide", {})
            if system_values_guide:
                explanations = []
                for value_name, explanation_text in system_values_guide.items():
                    if explanation_text and isinstance(explanation_text, str):
                        clean_explanation = explanation_text.strip()
                        if clean_explanation:
                            explanations.append(f"{value_name.upper()}:\n{clean_explanation}")

                if explanations:
                    parts.append("System Values Guide:\n" + "\n\n".join(explanations))

            return "\n\n".join(parts)

        except Exception as e:
            logger.error(f"[PromptManager] 構建狀態上下文失敗: {e}")
            return None

    # ------------------------------------------------------------------
    # 系統指令管理
    # ------------------------------------------------------------------

    def get_system_instruction(self, mode: str = "main") -> str:
        """獲取系統指令"""
        return self.system_instructions.get(mode, self.system_instructions.get("main", ""))

    def update_system_instruction(self, mode: str, instruction: str) -> None:
        """更新系統指令"""
        self.system_instructions[mode] = instruction
        logger.debug(f"[PromptManager] 更新系統指令: {mode}")

    def get_template_stats(self) -> Dict[str, Any]:
        """獲取模板使用統計"""
        return {
            "cached_templates": len(self._template_cache),
            "available_instructions": list(self.system_instructions.keys()),
        }

    # ------------------------------------------------------------------
    # 私有輔助方法
    # ------------------------------------------------------------------

    def _format_system_values_english(self, modifiers: Dict[str, Any]) -> str:
        """格式化系統值為英文格式，並附上數值以便動態調整語氣"""
        status_mapping = {
            "非常積極": "very positive", "積極": "positive", "中性": "neutral",
            "消極": "negative", "非常消極": "very negative",
            "非常自信": "very confident", "自信": "confident", "普通": "normal",
            "沒有自信": "not confident", "非常沒有自信": "very unconfident",
            "非常願意幫助": "very helpful", "願意幫助": "helpful", "中等": "moderate",
            "不太願意幫助": "less helpful", "不願意幫助": "unhelpful",
            "非常無聊": "very bored", "無聊": "bored", "有點無聊": "slightly bored",
            "不無聊": "not bored", "感興趣": "interested",
        }

        mood_en = status_mapping.get(modifiers.get("mood_level", ""), modifiers.get("mood_level", "unknown"))
        pride_en = status_mapping.get(modifiers.get("pride_level", ""), modifiers.get("pride_level", "unknown"))
        helpfulness_en = status_mapping.get(modifiers.get("helpfulness_level", ""), modifiers.get("helpfulness_level", "unknown"))
        boredom_en = status_mapping.get(modifiers.get("boredom_level", ""), modifiers.get("boredom_level", "unknown"))

        # 原始 helpfulness 數值用於動態調整語氣
        helpfulness_value = modifiers.get("helpfulness", 0.5)

        if helpfulness_value > 0.7:
            helpfulness_level = "HIGH"
        elif helpfulness_value >= 0.3:
            helpfulness_level = "MEDIUM"
        else:
            helpfulness_level = "LOW"

        return (
            f"mood={mood_en}, pride={pride_en}, "
            f"helpfulness={helpfulness_en} (level={helpfulness_level}, value={helpfulness_value:.2f}), "
            f"boredom={boredom_en}"
        )

    def _build_memory_context(self, memory_context: Optional[str] = None) -> Optional[str]:
        """構建記憶上下文區段 — 僅用於向後相容的靜態記憶注入

        Note:
            記憶檢索已改為 MCP 工具方式，LLM 透過工具主動檢索所需記憶。
            此方法只處理靜態的 memory_context 字串（向後相容）。
        """
        if memory_context:
            # 提供工具提示，避免將完整 PROFILE 文本塞入 prompt
            return (
                "Profile memory exists but is not inlined to save tokens. "
                "If you need user facts, call memory_retrieve_snapshots with memory_types='profile' "
                "to fetch them before responding."
            )
        return None

    def _build_functions_context(self, available_functions: Optional[str] = None) -> Optional[str]:
        """構建系統功能上下文 — 接受外部傳入的功能描述字串

        原本有從 state_aware_interface 取得的 fallback 邏輯，
        解耦後改為只接受傳入內容，無傳入則返回 None。
        """
        if available_functions:
            return f"Available System Functions:\n{available_functions}"
        return None

    def _build_identity_context(
        self,
        identity_context: Optional[Dict],
        simplified: bool = False,
    ) -> Optional[str]:
        """構建身份上下文

        Note:
            原版在 simplified=False 時會呼叫 get_user_setting 取得 UEP 暱稱，
            解耦後移除此依賴，暱稱邏輯移至外部（由呼叫端注入 identity_context）。
        """
        if not identity_context:
            return None

        try:
            identity = identity_context.get("identity", {})
            preferences = identity_context.get("preferences", {})

            if simplified:
                # 簡化版：只包含基本身份資訊
                name = identity.get("name", "User")
                return f"User: {name}"
            else:
                # 完整版：包含偏好設定
                parts = []

                name = identity.get("name", "User")
                parts.append(f"User: {name}")

                # UEP 暱稱（若呼叫端已注入於 identity_context 中）
                uep_nickname = identity_context.get("uep_nickname")
                if uep_nickname and str(uep_nickname).strip():
                    parts.append(
                        f"Note: The user has given you a nickname: '{uep_nickname}'. "
                        "They may use this nickname when addressing you."
                    )

                # 對話偏好
                conversation_prefs = preferences.get("conversation", {})
                if conversation_prefs:
                    formality = conversation_prefs.get("formality", "neutral")
                    verbosity = conversation_prefs.get("verbosity", "moderate")
                    parts.append(f"Preferences: {formality} formality, {verbosity} verbosity")

                return "; ".join(parts)

        except Exception as e:
            logger.error(f"[PromptManager] 構建身份上下文失敗: {e}")
            return None

    def _build_conversation_history(self, history: Optional[List]) -> Optional[str]:
        """構建對話歷史上下文

        同時支援 ConversationEntry/Message 物件與 dict 格式。
        """
        if not history:
            return None

        try:
            # 只取最近 3 筆
            recent_history = history[-3:] if len(history) > 3 else history

            history_parts = []
            for entry in recent_history:
                if hasattr(entry, "role") and hasattr(entry, "content"):
                    # ConversationEntry 或 Message 物件
                    role = entry.role
                    content = entry.content
                elif isinstance(entry, dict):
                    role = entry.get("role", "unknown")
                    content = entry.get("content", "")
                else:
                    # 未知格式跳過
                    continue

                if role == "user":
                    history_parts.append(f"User: {content}")
                elif role == "assistant":
                    history_parts.append(f"U.E.P: {content}")

            if history_parts:
                return "Recent Conversation:\n" + "\n".join(history_parts)

        except Exception as e:
            logger.error(f"[PromptManager] 構建對話歷史失敗: {e}")

        return None

    def _replace_system_values_placeholder(
        self,
        instruction: str,
        modifiers: Optional[Dict[str, Any]] = None,
    ) -> str:
        """替換系統指令中的 {system_values} 佔位符

        解耦後改為接受外部傳入的 modifiers，而非內部呼叫 status_manager。
        若 modifiers 為 None 則以空字串替換。
        """
        if "{system_values}" not in instruction:
            return instruction

        try:
            if modifiers is not None:
                status_context = self.build_status_context(modifiers)
                system_values_text = status_context if status_context else ""
            else:
                system_values_text = ""

            return instruction.replace("{system_values}", system_values_text)

        except Exception as e:
            logger.error(f"[PromptManager] 替換系統值佔位符失敗: {e}")
            return instruction.replace("{system_values}", "")

    # ------------------------------------------------------------------
    # 工作流上下文構建
    # ------------------------------------------------------------------

    def _build_workflow_context(self, workflow_context: Dict) -> Optional[str]:
        """構建工作流上下文 — 依 type 分派至對應子方法"""
        try:
            context_type = workflow_context.get("type")

            if context_type == "workflow_step_response":
                return self._build_workflow_step_response_context(workflow_context)

            if context_type == "workflow_input_required":
                return self._build_workflow_input_required_context(workflow_context)

            if context_type == "workflow_error":
                return self._build_workflow_error_context(workflow_context)

            # 原有邏輯：工作流進度追蹤
            parts = []

            current_step = workflow_context.get("current_step")
            if current_step:
                parts.append(f"Current Step: {current_step}")

            previous_result = workflow_context.get("previous_result")
            if previous_result:
                parts.append(f"Previous Result: {previous_result}")

            remaining_steps = workflow_context.get("remaining_steps", [])
            if remaining_steps:
                steps_text = ", ".join(remaining_steps[:3])  # 只顯示前 3 步
                parts.append(f"Remaining Steps: {steps_text}")

            if parts:
                return "Workflow Progress: " + "; ".join(parts)

        except Exception as e:
            logger.error(f"[PromptManager] 構建工作流上下文失敗: {e}")

        return None

    def _build_workflow_step_response_context(self, workflow_context: Dict) -> str:
        """構建工作流步驟回應的上下文（通用框架模式）

        生成通用指引，讓 LLM 能夠處理任何類型的工作流步驟數據，
        而不是針對特定工作流硬編碼邏輯。
        """
        is_complete = workflow_context.get("is_complete", False)
        should_end_session = workflow_context.get("should_end_session", False)
        review_data = workflow_context.get("review_data", {})

        context_parts = []

        context_parts.append("=" * 60)
        context_parts.append("WORKFLOW STEP COMPLETED - GENERATE USER RESPONSE")
        context_parts.append("=" * 60)

        # 下一步資訊
        next_step_info = workflow_context.get("next_step_info")
        next_step_is_interactive = (
            next_step_info and next_step_info.get("step_type") == "interactive"
            if next_step_info
            else False
        )

        context_parts.append(f"\nStatus: {'All done' if is_complete else 'Still working'}")

        context_parts.append("\n" + "=" * 60)
        context_parts.append("YOUR TASK:")
        context_parts.append("=" * 60)

        if is_complete:
            context_parts.append("\n✅ Done! Everything wrapped up.")

            # 判斷是否為簡單自動完成工作流（步驟數少且無後續互動）
            executed_steps = review_data.get("executed_steps", [])
            is_simple_auto_workflow = (
                len(executed_steps) <= 2
                and not next_step_is_interactive
            )

            if is_simple_auto_workflow:
                # 簡單自動完成工作流：簡短確認即可
                context_parts.append("\nKeep it super brief - just 1-3 casual words:")
                context_parts.append("'Done!', 'All set!', 'Got it!', 'Yep!', 'Finished!'")
                context_parts.append("\nDon't explain anything or mention technical stuff.")
            else:
                # 複雜工作流：需要詳細說明
                context_parts.append("\nLet the user know what happened in a casual, friendly way:")
                context_parts.append("- Mention the key info from above (keep it simple)")
                context_parts.append("- Stay conversational, like telling a friend")
                context_parts.append("- 2-3 sentences max")
                context_parts.append("- Skip the tech talk (no IDs, no formal terms)")

            context_parts.append("\nThen call approve_step() to wrap things up.")
            if should_end_session:
                context_parts.append("Also set session_control={'action': 'end_session'} in metadata.")

        elif next_step_is_interactive:
            context_parts.append("\n⏭️ The next step requires USER INPUT.")
            if next_step_info:
                context_parts.append(f"\nNext Step Prompt: {next_step_info.get('prompt', '')}")

            context_parts.append("\nGenerate a natural response in ENGLISH that:")
            context_parts.append("1. BRIEFLY acknowledges the current step (1 sentence max)")
            context_parts.append("2. Asks the user for the needed input (use the prompt above as guide)")
            context_parts.append("3. Keep it conversational and friendly (2-3 sentences total)")
            context_parts.append("4. Act as a helpful assistant, NOT a system notification")
            context_parts.append("5. ❌ AVOID: Mentioning 'workflow', 'step', 'session' or other tech terms")

            context_parts.append("\n📋 REQUIRED ACTION:")
            context_parts.append("   Call approve_step() MCP tool AFTER generating your response")

        else:
            # 理論上不應執行到此分支
            context_parts.append("\n⏳ Processing step completed, continuing automatically.")
            context_parts.append("\n📋 REQUIRED ACTION:")
            context_parts.append("   Call approve_step() MCP tool silently (no text response needed)")

        context_parts.append("\n" + "=" * 60)
        context_parts.append("LANGUAGE & PERSONALITY REQUIREMENTS:")
        context_parts.append("=" * 60)
        context_parts.append("⚠️ CRITICAL:")
        context_parts.append("   1. Always respond in ENGLISH")
        context_parts.append("   2. You are U.E.P., a personal assistant with personality")
        context_parts.append("   3. Be natural, warm, and conversational")
        context_parts.append("   4. NEVER mention technical details like IDs, session names, etc.")
        context_parts.append("   5. Talk like a helpful friend, not a machine")
        context_parts.append("=" * 60)

        return "\n".join(context_parts)

    def _build_workflow_input_required_context(self, workflow_context: Dict) -> str:
        """構建工作流需要輸入的上下文

        指示 LLM 使用 provide_workflow_input 工具處理用戶輸入，
        並判斷是否為委託意圖（delegation intent）。
        """
        workflow_type = workflow_context.get("workflow_type", "unknown")
        step_id = workflow_context.get("step_id", "unknown")
        prompt = workflow_context.get("prompt", "請提供輸入")
        is_optional = workflow_context.get("is_optional", False)
        fallback_value = workflow_context.get("fallback_value", "")

        context_parts = []

        context_parts.append("=" * 60)
        context_parts.append("WORKFLOW INPUT REQUIRED - USE provide_workflow_input TOOL")
        context_parts.append("=" * 60)

        context_parts.append(f"\nWorkflow Type: {workflow_type}")
        context_parts.append(f"Step ID: {step_id}")
        context_parts.append(f"Step Type: Interactive Input Step")
        context_parts.append(f"Optional: {is_optional}")
        if is_optional and fallback_value:
            context_parts.append(f"Fallback Value: {fallback_value}")

        context_parts.append(f"\nPrompt for User: {prompt}")

        user_input = workflow_context.get("user_input", "")
        context_parts.append(f"\nUser's Response: {user_input}")

        context_parts.append("\n" + "=" * 60)
        context_parts.append("INSTRUCTIONS")
        context_parts.append("=" * 60)

        context_parts.append(
            "\nYou MUST use the provide_workflow_input tool to handle this input."
            "\nDo NOT generate a text response directly."
        )

        context_parts.append(
            "\n\nYour task:"
            "\n1. Analyze the user's response semantically and understand their intent"
            "\n2. EXTRACT the key information from natural language:"
            "\n   - For paths: extract the essential description (e.g., 'd drive root', 'documents folder')"
            "\n   - The workflow will handle path resolution and validation internally"
            "\n   - Don't try to construct absolute paths yourself"
            "\n3. Determine if it's DELEGATION INTENT or EXPLICIT VALUE"
        )

        if is_optional:
            context_parts.append(
                "\n\n⚠️  This is an OPTIONAL step with fallback."
                "\n\nDELEGATION INTENT examples (use_fallback=True):"
                "\n  - '你決定' / 'you decide'"
                "\n  - '幫我選' / 'help me choose'"
                "\n  - '隨便' / 'whatever' / 'anything'"
                "\n  - '不知道' / 'don't know'"
                "\n  - '預設' / 'default'"
                "\n  - Empty or very vague responses"
                "\n\nEXPLICIT VALUE examples (use_fallback=False):"
                "\n  - Natural language: 'put it in my d drive root' → extract 'd drive root'"
                "\n  - Natural language: 'save to documents folder' → extract 'documents folder'"
                "\n  - Specific path: 'C:\\\\temp' → pass 'C:\\\\temp' as-is"
                "\n  - Any clear indication of what the user wants"
            )
        else:
            context_parts.append(
                "\n\n⚠️  This is a REQUIRED step (not optional)."
                "\n\nYour job is to EXTRACT the key information from user's natural language:"
                "\n  - 'Can you put the file in my d drive root?' → extract 'd drive root'"
                "\n  - 'Save it to the documents folder' → extract 'documents folder'"
                "\n  - 'Use C:\\\\temp' → pass 'C:\\\\temp' as-is"
                "\n\nThe workflow will handle path resolution and validation internally."
                "\n\nSet use_fallback=False for explicit values."
                "\nSet use_fallback=True ONLY if user explicitly delegates the decision to you."
            )

        context_parts.append(
            "\n\nHow to respond:"
            "\n  1. Extract the key information from user's response"
            "\n  2. Call provide_workflow_input("
            "\n       session_id: <auto-injected>,"
            "\n       user_input: <extracted key info>,"
            "\n       use_fallback: <True if delegation, False otherwise>"
            "\n     )"
            "\n\nIMPORTANT:"
            "\n- user_input should be the EXTRACTED key information (e.g., 'd drive root'), NOT the full sentence"
            "\n- Let the workflow handle path resolution, validation, and processing"
            "\n- Do NOT try to resolve paths yourself or call other tools first"
        )

        context_parts.append("\n" + "=" * 60)

        return "\n".join(context_parts)

    def _build_workflow_error_context(self, workflow_context: Dict) -> str:
        """構建工作流錯誤的上下文

        當工作流執行過程中發生錯誤時，指示 LLM：
        1. 生成自然語言的錯誤說明給使用者
        2. 調用 cancel_workflow MCP 工具優雅終止工作流
        """
        session_id = workflow_context.get("workflow_session_id", "unknown")
        error_message = workflow_context.get("error_message", "未知錯誤")
        current_step = workflow_context.get("current_step", "未知步驟")

        context_parts = []

        context_parts.append("=" * 60)
        context_parts.append("WORKFLOW ERROR - INFORM USER AND CANCEL")
        context_parts.append("=" * 60)

        context_parts.append(f"\n⚠️ A workflow has encountered an error and cannot continue.")
        context_parts.append(f"\nError Details:")
        context_parts.append(f"  - Current Step: {current_step}")
        context_parts.append(f"  - Error Message: {error_message}")

        context_parts.append("\n" + "=" * 60)
        context_parts.append("YOUR TASK:")
        context_parts.append("=" * 60)

        context_parts.append("\nYou MUST do the following:")
        context_parts.append("1. Generate a natural, friendly error explanation in ENGLISH")
        context_parts.append("   - Explain what went wrong in simple terms")
        context_parts.append("   - Apologize for the inconvenience")
        context_parts.append("   - Suggest what the user might try instead (if applicable)")
        context_parts.append("   - Keep it conversational (2-3 sentences)")
        context_parts.append("")
        context_parts.append("2. Call the cancel_workflow MCP tool to gracefully terminate:")
        context_parts.append(f"   - session_id: {session_id}")
        context_parts.append(f"   - reason: Brief technical reason (for logs)")

        context_parts.append("\n⚠️ IMPORTANT:")
        context_parts.append("   - Do NOT mention technical terms like 'session_id', 'workflow', 'step'")
        context_parts.append("   - Be empathetic and helpful, like a personal assistant")
        context_parts.append("   - The cancel_workflow call will handle cleanup automatically")

        context_parts.append("\n" + "=" * 60)
        context_parts.append("LANGUAGE REQUIREMENT:")
        context_parts.append("=" * 60)
        context_parts.append("⚠️ CRITICAL: Always respond in ENGLISH")
        context_parts.append("=" * 60)

        return "\n".join(context_parts)
