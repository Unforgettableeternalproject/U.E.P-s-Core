# modules/llm_module/workflow/event_handler.py
"""
工作流事件處理器

處理工作流步驟完成、失敗、輸出完成等事件
"""

import time
from typing import Dict, Any, Optional, Set, List

from utils.debug_helper import debug_log, info_log, error_log


class WorkflowEventHandler:
    """處理工作流相關事件"""
    
    def __init__(self, llm_module):
        """
        初始化工作流事件處理器
        
        Args:
            llm_module: LLM 模組實例（用於訪問其他組件）
        """
        self.llm_module = llm_module
        self.event_bus = None
        
        # 工作流事件隊列（初始化為空列表，防止遺留舊事件）
        self._pending_workflow_events = []
        
        # 工作流完成事件去重（追蹤已處理的 (session_id, complete=True) 事件）
        self._processed_workflow_completions: Set[str] = set()
        
        # LLM_PROCESSING 步驟去重
        self._processed_llm_steps: Set[str] = set()
        
        # 待處理的互動步驟提示
        self._pending_interactive_prompts: List[Dict[str, Any]] = []
    
    def subscribe_events(self, event_bus):
        """訂閱工作流相關事件"""
        try:
            self.event_bus = event_bus
            from core.event_bus import SystemEvent
            
            # 訂閱工作流步驟完成事件
            self.event_bus.subscribe(
                SystemEvent.WORKFLOW_STEP_COMPLETED,
                self._handle_workflow_step_completed,
                handler_name="LLM.workflow_step_handler"
            )
            
            # 訂閱工作流失敗事件
            self.event_bus.subscribe(
                SystemEvent.WORKFLOW_FAILED,
                self._handle_workflow_failed,
                handler_name="LLM.workflow_error_handler"
            )
            
            # 訂閱輸出層完成事件（用於處理待處理的互動步驟提示）
            self.event_bus.subscribe(
                SystemEvent.OUTPUT_LAYER_COMPLETE,
                self._handle_output_complete,
                handler_name="LLM.output_complete_handler"
            )
            
            debug_log(2, "[LLM.WorkflowEventHandler] 已訂閱工作流事件")
            
        except Exception as e:
            error_log(f"[LLM.WorkflowEventHandler] 訂閱事件失敗: {e}")
    
    def _handle_workflow_step_completed(self, event):
        """
        處理工作流步驟完成事件
        
        當 SYS 在背景完成一個步驟後，此方法會被調用：
        1. 審核步驟結果
        2. 決定是否批准、修改或取消
        3. 調用相應的 MCP 工具
        
        Args:
            event: Event object containing step completion data
        """
        try:
            debug_log(2, f"[LLM.WorkflowEventHandler] 收到工作流步驟完成事件: {event.event_id}")
            
            data = event.data
            session_id = data.get("session_id")
            workflow_type = data.get("workflow_type")
            step_result = data.get("step_result", {})
            requires_review = data.get("requires_llm_review", False)
            review_data = data.get("llm_review_data")
            
            # 去重檢查：如果是完成事件且已處理過，跳過
            is_complete = step_result.get('complete', False)
            if is_complete and session_id in self._processed_workflow_completions:
                debug_log(2, f"[LLM.WorkflowEventHandler] ⚠️ 跳過重複的工作流完成事件: {session_id}")
                return
            
            # 標記為已處理（在加入隊列前就標記，避免重複加入）
            if is_complete:
                self._processed_workflow_completions.add(session_id)
                debug_log(2, f"[LLM.WorkflowEventHandler] ✅ 已標記工作流完成事件: {session_id}")
            
            debug_log(2, f"[LLM.WorkflowEventHandler] 工作流 {workflow_type} ({session_id}) 步驟完成")
            debug_log(3, f"[LLM.WorkflowEventHandler] 需要審核: {requires_review}, 結果: {step_result.get('success')}")
            debug_log(2, f"[LLM.WorkflowEventHandler] 接收到的 review_data keys: {list(review_data.keys()) if review_data else 'None'}")
            
            # 檢查是否為 LLM_PROCESSING 請求
            requires_llm_processing = data.get('requires_llm_processing', False)
            if requires_llm_processing:
                debug_log(2, f"[LLM.WorkflowEventHandler] 檢測到 LLM_PROCESSING 請求")
                llm_request_data = data.get('llm_request_data', {})
                
                # 去重檢查：避免重複處理同一個 LLM_PROCESSING 步驟
                step_id = llm_request_data.get('step_id', '')
                processing_key = f"{session_id}:{step_id}"
                
                if processing_key in self._processed_llm_steps:
                    debug_log(2, f"[LLM.WorkflowEventHandler] ⚠️ 跳過重複的 LLM_PROCESSING 請求: {processing_key}")
                    return
                
                # 標記為已處理
                self._processed_llm_steps.add(processing_key)
                debug_log(2, f"[LLM.WorkflowEventHandler] ✅ 開始處理 LLM_PROCESSING 請求: {processing_key}")
                
                # 委派給 step_processor
                from .step_processor import WorkflowStepProcessor
                processor = WorkflowStepProcessor(self.llm_module)
                processor.handle_llm_processing_request(session_id, workflow_type, llm_request_data)
                return
            
            # 檢查是否為工作流完成（最後一步）
            is_workflow_complete = step_result.get('complete', False)
            debug_log(2, f"[LLM.WorkflowEventHandler] 步驟結果數據: {step_result}")
            debug_log(2, f"[LLM.WorkflowEventHandler] 工作流完成標記: {is_workflow_complete}")
            should_respond_to_user = review_data and review_data.get('requires_user_response', False) if review_data else False
            should_end_session = review_data and review_data.get('should_end_session', False) if review_data else False
            
            # 獲取當前步驟和下一步資訊
            current_step_info = (review_data.get('current_step_info') if review_data else None) or data.get('current_step_info')
            next_step_info = (review_data.get('next_step_info') if review_data else None) or data.get('next_step_info')
            
            # 檢查當前步驟是否為 Interactive（等待輸入），且不會被跳過
            current_step_is_interactive = (
                current_step_info 
                and current_step_info.get('step_type') == 'interactive' 
                and not current_step_info.get('step_will_be_skipped', False)
            ) if current_step_info else False
            
            next_step_is_interactive = (
                next_step_info 
                and next_step_info.get('step_type') == 'interactive'
                and not next_step_info.get('step_will_be_skipped', False)
            ) if next_step_info else False
            
            debug_log(3, f"[LLM.WorkflowEventHandler] 當前步驟資訊: {current_step_info}")
            debug_log(3, f"[LLM.WorkflowEventHandler] 下一步資訊: {next_step_info}")
            debug_log(3, f"[LLM.WorkflowEventHandler] 當前步驟是互動步驟: {current_step_is_interactive}")
            debug_log(3, f"[LLM.WorkflowEventHandler] 下一步是互動步驟: {next_step_is_interactive}")
            
            # 過濾條件：如果不需要審核且工作流未完成
            if not requires_review and not is_workflow_complete:
                debug_log(2, f"[LLM.WorkflowEventHandler] 步驟不需要審核且工作流未完成")
                return
            
            # 實施 3 時刻回應模式：
            # 1. 工作流觸發 - 由 start_workflow MCP 工具處理（不在這裡）
            # 2. 當前步驟為互動步驟，或下一步為互動步驟 - 需要生成提示給使用者
            # 3. 工作流完成 - 需要生成最終結果
            should_generate_response = is_workflow_complete or current_step_is_interactive or next_step_is_interactive
            
            if not should_generate_response:
                debug_log(2, f"[LLM.WorkflowEventHandler] 步驟完成，下一步非互動步驟，靜默批准並推進")
                # 靜默批准：Processing 步驟後自動推進，不生成回應
                from .workflow_controller import WorkflowController
                controller = WorkflowController(self.llm_module)
                controller.approve_workflow_step(session_id, None)
                return
            
            # 需要生成回應：將工作流事件加入待處理隊列
            debug_log(2, f"[LLM.WorkflowEventHandler] 將工作流事件加入待處理隊列，review_data keys: {list(review_data.keys()) if review_data else 'None'}")
            
            self._pending_workflow_events.append({
                "type": "workflow_step_completed" if not is_workflow_complete else "workflow_completed",
                "session_id": session_id,
                "workflow_type": workflow_type,
                "step_result": step_result,
                "review_data": review_data,
                "is_complete": is_workflow_complete,
                "should_respond": should_respond_to_user,
                "should_end_session": should_end_session,
                "current_step_info": current_step_info,
                "next_step_info": next_step_info,
                "timestamp": time.time()
            })
            
            info_log(f"[LLM.WorkflowEventHandler] 工作流事件已加入隊列: {workflow_type}, is_complete={is_workflow_complete}, current_interactive={current_step_is_interactive}")
            
            # 處理需要生成回應的情況
            if is_workflow_complete:
                debug_log(2, f"[LLM.WorkflowEventHandler] 工作流完成，立即生成最終總結回應")
                # 委派給 step_processor
                from .step_processor import WorkflowStepProcessor
                processor = WorkflowStepProcessor(self.llm_module)
                processor.process_workflow_completion(session_id, workflow_type, step_result, review_data)
                return
            elif current_step_is_interactive or next_step_is_interactive:
                # 訂閱 OUTPUT_LAYER_COMPLETE，在當前 cycle 的輸出完成後再處理
                debug_log(2, f"[LLM.WorkflowEventHandler] 當前或下一步是互動步驟，訂閱 OUTPUT_LAYER_COMPLETE 等待當前輸出完成")
                
                # 保存待處理的互動步驟信息
                self._pending_interactive_prompts.append({
                    'session_id': session_id,
                    'workflow_type': workflow_type,
                    'step_result': step_result,
                    'review_data': review_data,
                    'next_step_info': next_step_info,
                    'current_cycle_session': self.llm_module._get_current_gs_id()
                })
                
                # 立即處理互動步驟提示
                debug_log(2, f"[LLM.WorkflowEventHandler] 立即生成互動步驟提示: {workflow_type}")
                from .interactive_prompts import InteractivePromptsHandler
                prompts_handler = InteractivePromptsHandler(self.llm_module)
                prompts_handler.process_interactive_step_prompt(
                    session_id,
                    workflow_type,
                    step_result,
                    review_data,
                    next_step_info
                )
                
                # 從兩個隊列中移除（已經處理）
                if self._pending_interactive_prompts:
                    self._pending_interactive_prompts.pop()
                
                # 從 _pending_workflow_events 中找到並移除對應的事件
                self._pending_workflow_events = [
                    e for e in self._pending_workflow_events 
                    if e.get('session_id') != session_id
                ]
                debug_log(2, f"[LLM.WorkflowEventHandler] 已從待處理隊列中移除互動步驟事件: {session_id}")
            else:
                # 其他情況：等待下次 handle() 調用
                debug_log(2, f"[LLM.WorkflowEventHandler] 工作流事件已準備好，等待下次 handle() 調用生成回應")
            
        except Exception as e:
            import traceback
            error_log(f"[LLM.WorkflowEventHandler] 處理工作流步驟完成事件失敗: {e}")
            error_log(f"[LLM.WorkflowEventHandler] 堆疊追蹤:\n{traceback.format_exc()}")
    
    def _handle_workflow_failed(self, event):
        """
        處理工作流失敗事件
        
        當工作流執行過程中發生錯誤時：
        1. 生成自然語言的錯誤說明
        2. 調用 cancel_workflow MCP 工具優雅終止工作流
        3. 通知使用者錯誤情況
        
        Args:
            event: Event object containing error data
        """
        try:
            debug_log(2, f"[LLM.WorkflowEventHandler] 收到工作流失敗事件: {event.event_id}")
            
            data = event.data
            session_id = data.get("session_id")
            workflow_type = data.get("workflow_type")
            error_message = data.get("error_message")
            current_step = data.get("current_step")
            
            error_log(f"[LLM.WorkflowEventHandler] 工作流失敗: {workflow_type} ({session_id}) - {error_message}")
            
            # 將錯誤事件加入待處理隊列
            self._pending_workflow_events.append({
                "type": "workflow_failed",
                "session_id": session_id,
                "workflow_type": workflow_type,
                "error_message": error_message,
                "current_step": current_step,
                "timestamp": time.time()
            })
            
            info_log(f"[LLM.WorkflowEventHandler] 工作流錯誤事件已加入隊列: {workflow_type}")
            
        except Exception as e:
            error_log(f"[LLM.WorkflowEventHandler] 處理工作流失敗事件錯誤: {e}")
    
    def _handle_output_complete(self, event):
        """
        處理輸出層完成事件
        
        當 TTS 輸出完成後，檢查是否有待處理的互動步驟提示需要生成
        這確保互動步驟提示在正確的時序生成（在當前 cycle 的輸出之後）
        
        Args:
            event: OUTPUT_LAYER_COMPLETE 事件
        """
        try:
            # 檢查是否有待處理的互動步驟提示
            if not self._pending_interactive_prompts:
                return
            
            current_gs = self.llm_module._get_current_gs_id()
            
            # 處理所有待處理的提示（只處理當前 cycle 的）
            prompts_to_process = []
            remaining_prompts = []
            
            for prompt_info in self._pending_interactive_prompts:
                if prompt_info['current_cycle_session'] == current_gs:
                    prompts_to_process.append(prompt_info)
                else:
                    remaining_prompts.append(prompt_info)
            
            self._pending_interactive_prompts = remaining_prompts
            
            # 處理每個待處理的提示
            from .interactive_prompts import InteractivePromptsHandler
            prompts_handler = InteractivePromptsHandler(self.llm_module)
            
            for prompt_info in prompts_to_process:
                debug_log(2, f"[LLM.WorkflowEventHandler] OUTPUT 完成後處理互動步驟提示: {prompt_info['workflow_type']}")
                prompts_handler.process_interactive_step_prompt(
                    prompt_info['session_id'],
                    prompt_info['workflow_type'],
                    prompt_info['step_result'],
                    prompt_info['review_data'],
                    prompt_info['next_step_info']
                )
        
        except Exception as e:
            import traceback
            error_log(f"[LLM.WorkflowEventHandler] 處理輸出完成事件失敗: {e}")
            error_log(f"[LLM.WorkflowEventHandler] 堆疊追蹤:\n{traceback.format_exc()}")
    
    def get_pending_workflow_context(self) -> Optional[Dict[str, Any]]:
        """
        獲取待處理的工作流上下文數據
        
        從待處理事件隊列中取出工作流事件，構建為 workflow_context
        供 handle() 方法使用
        
        Returns:
            工作流上下文字典，如果沒有待處理事件則返回 None
        """
        try:
            if not self._pending_workflow_events:
                return None
            
            # 取出第一個待處理事件
            event = self._pending_workflow_events.pop(0)
            
            event_type = event.get('type', 'workflow_step_completed')
            
            # 處理工作流錯誤事件
            if event_type == 'workflow_failed':
                workflow_context = {
                    'type': 'workflow_error',
                    'workflow_session_id': event.get('session_id'),
                    'workflow_type': event.get('workflow_type'),
                    'error_message': event.get('error_message'),
                    'current_step': event.get('current_step')
                }
                debug_log(2, f"[LLM.WorkflowEventHandler] 構建工作流錯誤上下文: workflow={workflow_context['workflow_type']}, "
                            f"error={workflow_context['error_message']}")
            else:
                # 構建工作流上下文
                workflow_context = {
                    'type': 'workflow_step_response',
                    'workflow_session_id': event.get('session_id'),
                    'workflow_type': event.get('workflow_type'),
                    'is_complete': event.get('is_complete', False),
                    'should_end_session': event.get('should_end_session', False),
                    'step_result': event.get('step_result', {}),
                    'review_data': event.get('review_data', {}),
                    'next_step_info': event.get('next_step_info')
                }
                
                debug_log(2, f"[LLM.WorkflowEventHandler] 構建工作流上下文: type={workflow_context['type']}, "
                            f"workflow={workflow_context['workflow_type']}, "
                            f"complete={workflow_context['is_complete']}")
            
            return workflow_context
            
        except Exception as e:
            error_log(f"[LLM.WorkflowEventHandler] 獲取工作流上下文失敗: {e}")
            return None
    
    def cleanup_workflow_tracking(self, session_id: str):
        """
        清理工作流追蹤標記
        
        Args:
            session_id: 工作流會話 ID
        """
        try:
            # 清理完成追蹤
            if session_id in self._processed_workflow_completions:
                self._processed_workflow_completions.discard(session_id)
                debug_log(2, f"[LLM.WorkflowEventHandler] 已移除工作流完成追蹤: {session_id}")
            
            # 清理該工作流的所有 LLM_PROCESSING 步驟標記
            steps_to_remove = {key for key in self._processed_llm_steps if key.startswith(f"{session_id}:")}
            for step_key in steps_to_remove:
                self._processed_llm_steps.discard(step_key)
            if steps_to_remove:
                debug_log(2, f"[LLM.WorkflowEventHandler] 已清理 {len(steps_to_remove)} 個 LLM_PROCESSING 步驟標記")
            
            # 清除待處理隊列中該工作流的所有互動提示
            prompts_to_remove = [
                prompt for prompt in self._pending_interactive_prompts
                if prompt.get('session_id') == session_id
            ]
            for prompt in prompts_to_remove:
                self._pending_interactive_prompts.remove(prompt)
                debug_log(2, f"[LLM.WorkflowEventHandler] 已從隊列移除已完成工作流的互動提示: {prompt.get('workflow_type')}")
            
        except Exception as e:
            error_log(f"[LLM.WorkflowEventHandler] 清理工作流追蹤失敗: {e}")
