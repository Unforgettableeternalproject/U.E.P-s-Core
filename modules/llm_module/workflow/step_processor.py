# modules/llm_module/workflow/step_processor.py
"""
工作流步驟處理器

處理 LLM_PROCESSING 請求和工作流完成邏輯
"""

import asyncio
from typing import Dict, Any

from utils.debug_helper import debug_log, info_log, error_log


class WorkflowStepProcessor:
    """處理工作流步驟的執行和完成"""
    
    def __init__(self, llm_module):
        """
        初始化步驟處理器
        
        Args:
            llm_module: LLM 模組實例
        """
        self.llm_module = llm_module
    
    def handle_llm_processing_request(self, session_id: str, workflow_type: str, llm_request_data: dict):
        """
        處理工作流中的 LLM_PROCESSING 請求
        
        當工作流步驟類型為 STEP_TYPE_LLM_PROCESSING 時，會調用此方法來：
        1. 提取 LLM 請求數據（prompt, output_key）
        2. 生成 LLM 回應
        3. 將結果寫入工作流會話數據
        4. 觸發工作流繼續執行
        
        Args:
            session_id: 工作流會話ID
            workflow_type: 工作流類型
            llm_request_data: 包含 prompt, output_data_key 等的請求數據
        """
        try:
            debug_log(2, f"[LLM.StepProcessor] 開始處理 LLM_PROCESSING 請求: {workflow_type}")
            
            # 提取請求數據
            prompt = llm_request_data.get('prompt')
            output_key = llm_request_data.get('output_data_key')
            task_description = llm_request_data.get('task_description', '')
            
            if not prompt:
                error_log(f"[LLM.StepProcessor] LLM_PROCESSING 請求缺少 prompt")
                return
            
            if not output_key:
                error_log(f"[LLM.StepProcessor] LLM_PROCESSING 請求缺少 output_data_key")
                return
            
            debug_log(3, f"[LLM.StepProcessor] 任務描述: {task_description}")
            debug_log(3, f"[LLM.StepProcessor] 輸出鍵: {output_key}")
            debug_log(3, f"[LLM.StepProcessor] Prompt 長度: {len(prompt)} 字符")
            
            # 使用 internal 模式生成 LLM 回應（節省 token）
            debug_log(2, f"[LLM.StepProcessor] 正在調用 Gemini API（internal 模式）...")
            
            # 構建簡潔的系統提示詞（僅針對工作流任務）
            workflow_system_prompt = (
                "You are a helpful assistant processing workflow tasks. "
                "Provide clear, concise responses based on the given instructions. "
                "Follow the format requirements strictly. And ALWAYS respond in English"
            )
            
            response_data = self.llm_module.model.query(
                prompt, 
                mode="internal",
                cached_content=None,
                tools=None,
                system_instruction=workflow_system_prompt
            )
            
            if not response_data or 'text' not in response_data:
                error_log(f"[LLM.StepProcessor] Gemini API 回應無效: {response_data}")
                return
            
            llm_result = response_data['text']
            debug_log(2, f"[LLM.StepProcessor] LLM 回應已生成 (長度: {len(llm_result)})")
            debug_log(3, f"[LLM.StepProcessor] 回應內容預覽: {llm_result[:200]}...")
            
            # 寫入工作流會話數據
            from core.sessions.session_manager import session_manager
            workflow_session = session_manager.get_workflow_session(session_id)
            
            if not workflow_session:
                error_log(f"[LLM.StepProcessor] 找不到工作流會話: {session_id}")
                return
            
            workflow_session.add_data(output_key, llm_result)
            debug_log(2, f"[LLM.StepProcessor] 已將 LLM 結果寫入會話數據鍵: {output_key}")
            
            # 觸發工作流繼續執行
            debug_log(2, f"[LLM.StepProcessor] 調用 provide_workflow_input 推進工作流...")
            
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # 調用 MCP 工具推進工作流
            continue_result = loop.run_until_complete(
                self.llm_module.mcp_client.call_tool(
                    "provide_workflow_input",
                    {
                        "session_id": session_id,
                        "user_input": "",
                        "use_fallback": True
                    }
                )
            )
            
            debug_log(2, f"[LLM.StepProcessor] 工作流推進結果: {continue_result.get('status')}")
            debug_log(2, f"[LLM.StepProcessor] LLM_PROCESSING 請求處理完成")
            
        except Exception as e:
            import traceback
            error_log(f"[LLM.StepProcessor] 處理 LLM_PROCESSING 請求失敗: {e}")
            error_log(f"[LLM.StepProcessor] 堆疊追蹤:\n{traceback.format_exc()}")
    
    def process_workflow_completion(self, session_id: str, workflow_type: str, 
                                     step_result: dict, review_data: dict):
        """
        處理工作流完成，生成最終總結回應並觸發 TTS
        
        Args:
            session_id: 工作流會話 ID
            workflow_type: 工作流類型
            step_result: 最後步驟的結果
            review_data: 審核數據（包含完整的工作流結果）
        """
        try:
            debug_log(2, f"[LLM.StepProcessor] 開始處理工作流完成: {workflow_type} ({session_id})")
            debug_log(2, f"[LLM.StepProcessor] review_data keys: {list(review_data.keys()) if review_data else 'None'}")
            
            # 構建總結 prompt
            result_message = step_result.get('message', 'Task completed successfully')
            
            prompt = (
                f"The '{workflow_type}' workflow has completed successfully.\n\n"
                f"Result: {result_message}\n"
            )
            
            # 處理 full_content（文件讀取結果）
            if review_data and 'full_content' in review_data:
                debug_log(2, f"[LLM.StepProcessor] 發現 full_content，添加到 prompt")
                file_name = review_data.get('file_name', 'unknown')
                content = review_data.get('full_content', '')
                content_length = review_data.get('content_length', len(content))
                
                # 判斷內容是否應該完整唸出
                should_read_full = content_length <= 500 and content.strip()
                
                if should_read_full:
                    prompt += (
                        f"\nFile Read Results:\n"
                        f"- File: {file_name}\n"
                        f"- Content ({content_length} characters):\n{content}\n\n"
                        f"Generate a natural response that:\n"
                        f"1. Briefly confirms you've read the file '{file_name}'\n"
                        f"2. READ OUT THE ACTUAL FILE CONTENT directly\n"
                        f"3. Keep your introduction brief, then read the content naturally\n"
                        f"IMPORTANT: Actually read the file content aloud. Respond in English only."
                    )
                else:
                    prompt += (
                        f"\nFile Read Results:\n"
                        f"- File: {file_name}\n"
                        f"- Content Length: {content_length} characters\n"
                        f"- Content Preview:\n{content[:200]}...\n\n"
                        f"Generate a natural response that:\n"
                        f"1. Confirms the file has been read successfully\n"
                        f"2. State that the file is too long to read out completely\n"
                        f"3. Offer to help in other ways\n"
                        f"IMPORTANT: Respond in English only."
                    )
            elif review_data:
                # 通用數據處理
                result_data = step_result.get('data', {}) or review_data.get('result_data', review_data)
                
                if result_data:
                    self._add_result_data_to_prompt(prompt, result_data, workflow_type)
                else:
                    prompt += (
                        f"Generate a natural, friendly response that:\n"
                        f"1. Confirms the task is complete\n"
                        f"2. Summarizes the key results\n"
                        f"IMPORTANT: Respond in English only."
                    )
            
            # 生成回應
            info_log(f"[LLM.StepProcessor] 生成工作流完成總結回應...")
            response = self.llm_module.model.query(prompt, mode="work", tools=None)
            response_text = response.get("text", "The task has been completed successfully.")
            
            info_log(f"[LLM.StepProcessor] 工作流完成回應: {response_text[:100]}...")
            
            # 觸發 TTS 輸出
            from core.framework import core_framework
            tts_module = core_framework.get_module('tts')
            if tts_module:
                debug_log(2, f"[LLM.StepProcessor] 觸發 TTS 輸出最終總結")
                tts_module.handle({
                    "text": response_text,
                    "session_id": session_id,
                    "emotion": "neutral"
                })
            
            # 標記工作流會話待結束
            from core.sessions.session_manager import unified_session_manager
            unified_session_manager.mark_workflow_session_for_end(
                session_id, 
                reason=f"workflow_completed:{workflow_type}"
            )
            debug_log(1, f"[LLM.StepProcessor] 🔚 已標記 WS 待結束: {session_id}")
            
            # 清除 workflow_processing 標誌
            from core.working_context import working_context_manager
            working_context_manager.set_skip_input_layer(False, reason="workflow_completion_processed")
            debug_log(2, "[LLM.StepProcessor] 已清除 workflow_processing 標誌")
            
            debug_log(1, f"[LLM.StepProcessor] ✅ 工作流完成處理完畢: {session_id}")
            
        except Exception as e:
            import traceback
            error_log(f"[LLM.StepProcessor] 處理工作流完成失敗: {e}")
            error_log(f"[LLM.StepProcessor] 堆疊追蹤:\n{traceback.format_exc()}")
    
    def _add_result_data_to_prompt(self, prompt: str, result_data: dict, workflow_type: str) -> str:
        """
        根據結果數據類型添加到 prompt
        
        Args:
            prompt: 原始 prompt
            result_data: 結果數據
            workflow_type: 工作流類型
            
        Returns:
            更新後的 prompt
        """
        # 新聞摘要
        if 'news_list' in result_data:
            news_list = result_data.get('news_list', [])
            source = result_data.get('source', 'unknown')
            count = result_data.get('count', len(news_list))
            prompt += f"\nNews Summary Results:\n- Source: {source}\n- Count: {count}\n"
            for i, title in enumerate(news_list[:10], 1):
                prompt += f"  {i}. {title}\n"
            prompt += (
                f"\nGenerate a natural response mentioning the news count and highlighting 1-2 interesting headlines.\n"
                f"IMPORTANT: Respond in English only."
            )
        
        # 待辦事項查詢
        elif 'tasks' in result_data:
            tasks = result_data.get('tasks', [])
            task_count = len(tasks)
            
            if task_count > 3:
                prompt += f"\nTodo Tasks ({task_count} total - showing first 3):\n"
                for i, task in enumerate(tasks[:3], 1):
                    prompt += f"{i}. {task.get('task_name', 'Unnamed')} (Priority: {task.get('priority', 'medium')})\n"
                prompt += (
                    f"\nSummarize all tasks and provide statistics.\n"
                    f"IMPORTANT: Don't read all tasks - summarize! Respond in English only."
                )
            else:
                prompt += f"\nTodo Tasks ({task_count} tasks):\n"
                for i, task in enumerate(tasks, 1):
                    prompt += f"{i}. {task.get('task_name', 'Unnamed')} (Priority: {task.get('priority', 'medium')})\n"
                prompt += f"\nList all tasks clearly. Respond in English only."
        
        # 行事曆查詢
        elif 'events' in result_data:
            events = result_data.get('events', [])
            event_count = len(events)
            
            if event_count > 3:
                prompt += f"\nCalendar Events ({event_count} total - showing first 3):\n"
                for i, event in enumerate(events[:3], 1):
                    prompt += f"{i}. {event.get('summary', 'Untitled')} - {event.get('start_time', '')}\n"
                prompt += f"\nSummarize events. Respond in English only."
            else:
                prompt += f"\nCalendar Events ({event_count} events):\n"
                for i, event in enumerate(events, 1):
                    prompt += f"{i}. {event.get('summary', 'Untitled')} - {event.get('start_time', '')}\n"
                prompt += f"\nList all events. Respond in English only."
        
        else:
            prompt += f"Data: {str(result_data)[:500]}\n\nSummarize the results naturally. Respond in English only."
        
        return prompt
