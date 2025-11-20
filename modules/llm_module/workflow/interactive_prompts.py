# modules/llm_module/workflow/interactive_prompts.py
"""
互動步驟提示處理器

處理工作流互動步驟的提示生成
"""

from typing import Dict, Any

from utils.debug_helper import debug_log, info_log, error_log


class InteractivePromptsHandler:
    """處理互動步驟的提示生成"""
    
    def __init__(self, llm_module):
        """
        初始化互動提示處理器
        
        Args:
            llm_module: LLM 模組實例
        """
        self.llm_module = llm_module
    
    def process_interactive_step_prompt(self, session_id: str, workflow_type: str,
                                         step_result: dict, review_data: dict, next_step_info: dict):
        """
        處理互動步驟前的提示回應
        
        當當前步驟完成且下一步需要用戶輸入時，生成提示回應
        
        Args:
            session_id: 工作流會話 ID
            workflow_type: 工作流類型
            step_result: 當前步驟的結果
            review_data: 審核數據
            next_step_info: 下一步資訊
        """
        try:
            debug_log(2, f"[LLM.InteractivePrompts] 開始處理互動步驟提示: {workflow_type} ({session_id})")
            
            # 獲取下一步的提示信息
            next_step_prompt = next_step_info.get('prompt', 'Please provide input')
            next_step_id = next_step_info.get('step_id', 'unknown')
            
            # 構建提示 prompt
            current_result = step_result.get('message', 'Current step completed')
            
            prompt = (
                f"You are U.E.P., helping the user with a workflow.\n\n"
                f"Current Situation:\n"
                f"- Workflow: {workflow_type}\n"
                f"- Current Step Result: {current_result}\n"
                f"- Next Step: {next_step_id} (requires user input)\n"
                f"- Prompt for User: {next_step_prompt}\n\n"
            )
            
            # 如果有審核數據，添加上下文
            if review_data:
                action = review_data.get('action', '')
                if action:
                    prompt += f"- Recent Action: {action}\n"
                
                # 如果是 LLM 處理請求，添加處理結果的上下文
                if action == 'llm_processing_request':
                    request_data = review_data.get('request_data', {})
                    input_data = request_data.get('input_data', {})
                    
                    # 檢查是否有格式化的結果列表（例如搜尋結果）
                    if 'formatted_results' in input_data:
                        formatted_results = input_data['formatted_results']
                        prompt += f"\nAvailable Options:\n{formatted_results}\n\n"
                        debug_log(2, f"[LLM.InteractivePrompts] 添加格式化結果到提示中: {len(formatted_results)} 字符")
                    elif input_data:
                        data_str = "\n".join([f"  - {k}: {v}" for k, v in input_data.items() if k != 'formatted_results'])
                        if data_str:
                            prompt += f"\nContext Data:\n{data_str}\n\n"
                
                # 如果有文件相關信息
                if 'file_name' in review_data:
                    prompt += f"- File: {review_data.get('file_name')}\n"
            
            # 檢查是否有可用選項需要顯示
            has_options = review_data and review_data.get('action') == 'llm_processing_request' and \
                         review_data.get('request_data', {}).get('input_data', {}).get('formatted_results')
            
            if has_options:
                prompt += (
                    f"\nGenerate a natural response that:\n"
                    f"1. BRIEFLY acknowledges the search/processing results (1 sentence)\n"
                    f"2. **MUST include the complete list of available options shown above**\n"
                    f"3. Clearly asks the user to choose from the options\n"
                    f"4. Be friendly and conversational\n"
                    f"\nIMPORTANT: \n"
                    f"- Respond in English only\n"
                    f"- MUST show all the numbered options to the user\n"
                    f"- Keep introduction brief, focus on presenting the options clearly"
                )
            else:
                prompt += (
                    f"\nGenerate a natural response that:\n"
                    f"1. BRIEFLY acknowledges the current progress (1 sentence)\n"
                    f"2. Clearly asks the user for the needed input\n"
                    f"3. Translate any non-English prompt to English and use it naturally\n"
                    f"4. Be friendly and conversational (2-3 sentences total)\n"
                    f"\nIMPORTANT: Respond in English only. Keep it concise and natural."
                )
            
            # 調用 LLM 生成回應
            try:
                response_data = self.llm_module.model.query(
                    prompt=prompt,
                    mode="work",
                    tools=None
                )
                
                # 提取文本回應
                response_text = response_data.get('text', '') if isinstance(response_data, dict) else str(response_data)
                
                if not response_text or not response_text.strip():
                    error_log("[LLM.InteractivePrompts] 互動步驟提示生成失敗：空回應")
                    response_text = f"Got it! {next_step_prompt}"
                
                info_log(f"[LLM.InteractivePrompts] 互動步驟提示已生成: {response_text[:100]}...")
                
            except Exception as e:
                error_log(f"[LLM.InteractivePrompts] 生成互動步驟提示時出錯: {e}")
                # 使用英文備用回應
                if next_step_id == 'target_dir_input':
                    response_text = "Got it! Please specify the target directory, or leave it empty to use auto-selection."
                elif next_step_id == 'archive_confirm':
                    response_text = "Understood. Please confirm if you want to proceed with archiving this file. Reply with 'yes' to continue or 'no' to cancel."
                else:
                    response_text = "Got it! Please provide the required input to continue."
            
            # 觸發 TTS 輸出提示
            from core.framework import core_framework
            tts_module = core_framework.get_module('tts')
            if tts_module:
                debug_log(2, f"[LLM.InteractivePrompts] 觸發 TTS 輸出互動步驟提示")
                tts_module.handle({
                    "text": response_text,
                    "session_id": session_id,
                    "emotion": "neutral"
                })
            
            debug_log(1, f"[LLM.InteractivePrompts] ✅ 互動步驟提示處理完畢: {session_id}")
            
        except Exception as e:
            import traceback
            error_log(f"[LLM.InteractivePrompts] 處理互動步驟提示失敗: {e}")
            error_log(f"[LLM.InteractivePrompts] 堆疊追蹤:\n{traceback.format_exc()}")
