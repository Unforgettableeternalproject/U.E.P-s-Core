# modules/llm_module/llm_module.py
"""
LLM 模組重構版本

新功能：
1. 支援 CHAT 和 WORK 狀態分離處理
2. 整合 StatusManager 系統數值管理
3. Context Caching 上下文快取
4. 學習功能：記錄使用者偏好和對話風格
5. 與 Working Context 和身份管理系統整合
6. 內建 Prompt 管理，不再依賴外部 prompt_builder
"""

import re
import time
import json
from typing import Dict, Any, Optional, List
from pathlib import Path

from core.bases.module_base import BaseModule
from core.working_context import working_context_manager, ContextType
from core.status_manager import status_manager
from core.states.state_manager import state_manager, UEPState

from .schemas import (
    LLMInput, LLMOutput, SystemAction, LLMMode, SystemState,
    ConversationEntry, LearningData, StatusUpdate
)
from .gemini_client import GeminiWrapper
from .prompt_manager import PromptManager
from .learning_engine import LearningEngine
from .cache_manager import cache_manager, CacheType
from .mcp_client import MCPClient
from .module_interfaces import (
    state_aware_interface, CollaborationChannel, set_collaboration_state
)

from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, info_log, error_log


class LLMModule(BaseModule):
    def __init__(self, config=None):
        super().__init__()
        self.config = config or load_module_config("llm_module")
        
        # 核心組件
        self.model = GeminiWrapper(self.config)
        self.prompt_manager = PromptManager(self.config)
        self.learning_engine = LearningEngine(self.config.get("learning", {}))
        
        # 統一快取管理器 (整合Gemini顯性快取 + 本地快取)
        self.cache_manager = cache_manager
        
        # 狀態和會話管理
        self.state_manager = state_manager
        self.status_manager = status_manager
        self.session_info = {}
        
        # 狀態感知模組接口
        self.module_interface = state_aware_interface
        
        # MCP 客戶端 (用於與 SYS 模組的 MCP Server 通訊)
        # ✅ 傳遞 self 以便 MCP Client 可以獲取當前會話信息
        self.mcp_client = MCPClient(llm_module=self)
        
        # 🔧 工作流事件隊列（初始化為空列表，防止遺留舊事件）
        self._pending_workflow_events = []
        
        # 🔧 工作流完成事件去重（追蹤已處理的 (session_id, complete=True) 事件）
        self._processed_workflow_completions = set()
        
        # 監聽系統狀態變化以自動切換協作管道
        self._setup_state_listener()
        
        # 統計數據
        self.processing_stats = {
            "total_requests": 0,
            "chat_requests": 0, 
            "work_requests": 0,
            "total_processing_time": 0.0,
            "cache_hits": 0
        }

    def debug(self):
        # Debug level = 1
        debug_log(1, "[LLM] Debug 模式啟用 - 重構版本")
        # Debug level = 2  
        debug_log(2, f"[LLM] 模型名稱: {self.model.model_name}")
        debug_log(2, f"[LLM] 溫度: {self.model.temperature}")
        debug_log(2, f"[LLM] Top P: {self.model.top_p}")
        debug_log(2, f"[LLM] 最大輸出字元數: {self.model.max_tokens}")
        debug_log(2, f"[LLM] 統一快取管理器: 啟用 (Gemini + 本地快取)")
        debug_log(2, f"[LLM] Learning Engine: {'啟用' if self.learning_engine.learning_enabled else '停用'}")
        debug_log(2, f"[LLM] MCP Client: {'已連接' if self.mcp_client.mcp_server else '未連接'}")
        # Debug level = 4
        debug_log(4, f"[LLM] 完整模組設定: {self.config}")
    
    def _setup_state_listener(self):
        """設定系統狀態監聽器，自動切換協作管道"""
        try:
            # 獲取當前系統狀態並設定初始協作管道
            current_state = self.state_manager.get_current_state()
            set_collaboration_state(current_state)
            
            debug_log(2, f"[LLM] 狀態感知模組接口設定完成，初始狀態: {current_state}")
            debug_log(3, f"[LLM] 管道狀態: {self.module_interface.get_channel_status()}")
            
        except Exception as e:
            error_log(f"[LLM] 狀態監聽器設定失敗: {e}")
    
    def _update_collaboration_channels(self, new_state: UEPState):
        """根據系統狀態更新協作管道"""
        try:
            old_status = self.module_interface.get_channel_status()
            set_collaboration_state(new_state)
            new_status = self.module_interface.get_channel_status()
            
            if old_status != new_status:
                debug_log(2, f"[LLM] 協作管道更新: {old_status} → {new_status}")
                
        except Exception as e:
            error_log(f"[LLM] 協作管道更新失敗: {e}")
        
    def initialize(self):
        """初始化 LLM 模組"""
        debug_log(1, "[LLM] 初始化中...")
        self.debug()
        
        try:
            # Gemini 客戶端在 __init__ 中已經初始化，檢查是否正常
            if not hasattr(self.model, 'client') or self.model.client is None:
                error_log("[LLM] Gemini 模型初始化失敗")
                return False
            
            # 註冊 StatusManager 回調
            self.status_manager.register_update_callback("llm_module", self._on_status_update)
            
            # 獲取當前系統狀態
            current_state = self.state_manager.get_current_state()
            debug_log(2, f"[LLM] 當前系統狀態: {current_state}")
            
            # ✅ 連接 event_bus 並訂閱工作流事件
            try:
                from core.event_bus import event_bus, SystemEvent
                self.event_bus = event_bus
                # 訂閱工作流步驟完成事件
                self.event_bus.subscribe(
                    SystemEvent.WORKFLOW_STEP_COMPLETED,
                    self._handle_workflow_step_completed,
                    handler_name="LLM.workflow_step_handler"
                )
                # 🆕 訂閱工作流失敗事件
                self.event_bus.subscribe(
                    SystemEvent.WORKFLOW_FAILED,
                    self._handle_workflow_failed,
                    handler_name="LLM.workflow_error_handler"
                )
                # 🆕 訂閱輸出層完成事件（用於處理待處理的互動步驟提示）
                self.event_bus.subscribe(
                    SystemEvent.OUTPUT_LAYER_COMPLETE,
                    self._handle_output_complete,
                    handler_name="LLM.output_complete_handler"
                )
                debug_log(2, "[LLM] Event bus 已連接，已訂閱 WORKFLOW_STEP_COMPLETED, WORKFLOW_FAILED 和 OUTPUT_LAYER_COMPLETE 事件")
            except Exception as e:
                error_log(f"[LLM] 無法連接 event bus: {e}")
                self.event_bus = None
            
            self.is_initialized = True
            info_log("[LLM] LLM 模組重構版初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[LLM] 初始化失敗: {e}")
            return False
    
    def _handle_workflow_step_completed(self, event):
        """
        ✅ 處理工作流步驟完成事件
        
        當 SYS 在背景完成一個步驟後，此方法會被調用：
        1. 審核步驟結果
        2. 決定是否批准、修改或取消
        3. 調用相應的 MCP 工具
        
        Args:
            event: Event object containing step completion data
        """
        try:
            debug_log(2, f"[LLM] 收到工作流步驟完成事件: {event.event_id}")
            
            data = event.data
            session_id = data.get("session_id")
            workflow_type = data.get("workflow_type")
            step_result = data.get("step_result", {})
            requires_review = data.get("requires_llm_review", False)
            review_data = data.get("llm_review_data")
            
            # 🔧 去重檢查：如果是完成事件且已處理過，跳過
            is_complete = step_result.get('complete', False)
            if is_complete and session_id in self._processed_workflow_completions:
                debug_log(2, f"[LLM] ⚠️ 跳過重複的工作流完成事件: {session_id}")
                return
            
            # 🔧 標記為已處理（在加入隊列前就標記，避免重複加入）
            if is_complete:
                self._processed_workflow_completions.add(session_id)
                debug_log(2, f"[LLM] ✅ 已標記工作流完成事件: {session_id}")
            
            debug_log(2, f"[LLM] 工作流 {workflow_type} ({session_id}) 步驟完成")
            debug_log(3, f"[LLM] 需要審核: {requires_review}, 結果: {step_result.get('success')}")
            debug_log(2, f"[LLM] 接收到的 review_data keys: {list(review_data.keys()) if review_data else 'None'}")
            
            # 🔧 檢查是否為 LLM_PROCESSING 請求
            requires_llm_processing = data.get('requires_llm_processing', False)
            if requires_llm_processing:
                debug_log(2, f"[LLM] 檢測到 LLM_PROCESSING 請求")
                llm_request_data = data.get('llm_request_data', {})
                
                # 🔧 去重檢查：避免重複處理同一個 LLM_PROCESSING 步驟
                step_id = llm_request_data.get('step_id', '')
                processing_key = f"{session_id}:{step_id}"
                
                if not hasattr(self, '_processed_llm_steps'):
                    self._processed_llm_steps = set()
                
                if processing_key in self._processed_llm_steps:
                    debug_log(2, f"[LLM] ⚠️ 跳過重複的 LLM_PROCESSING 請求: {processing_key}")
                    return
                
                # 標記為已處理
                self._processed_llm_steps.add(processing_key)
                debug_log(2, f"[LLM] ✅ 開始處理 LLM_PROCESSING 請求: {processing_key}")
                
                self._handle_llm_processing_request(session_id, workflow_type, llm_request_data)
                return
            
            # 🆕 檢查是否為工作流完成（最後一步）
            is_workflow_complete = step_result.get('complete', False)
            debug_log(2, f"[LLM] 步驟結果數據: {step_result}")
            debug_log(2, f"[LLM] 工作流完成標記: {is_workflow_complete}")
            should_respond_to_user = review_data and review_data.get('requires_user_response', False) if review_data else False
            should_end_session = review_data and review_data.get('should_end_session', False) if review_data else False
            
            # 🆕 獲取當前步驟和下一步資訊
            # 🔧 優先從 review_data 中讀取，否則從 data 中讀取
            current_step_info = (review_data.get('current_step_info') if review_data else None) or data.get('current_step_info')
            next_step_info = (review_data.get('next_step_info') if review_data else None) or data.get('next_step_info')
            
            # 🔧 修正：檢查當前步驟是否為 Interactive（等待輸入），且不會被跳過
            # ⚠️ 重要：如果步驟會被跳過（step_will_be_skipped=True），不應視為需要互動
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
            
            debug_log(3, f"[LLM] 當前步驟資訊: {current_step_info}")
            debug_log(3, f"[LLM] 下一步資訊: {next_step_info}")
            debug_log(3, f"[LLM] 當前步驟是互動步驟: {current_step_is_interactive}")
            debug_log(3, f"[LLM] 下一步是互動步驟: {next_step_is_interactive}")
            
            # 🔧 過濾條件：如果不需要審核且工作流未完成
            # ⚠️ 重要：工作流完成時即使不需要審核也要生成最終總結
            if not requires_review and not is_workflow_complete:
                debug_log(2, f"[LLM] 步驟不需要審核且工作流未完成")
                return
            
            # 🔧 實施 3 時刻回應模式：
            # 1. 工作流觸發 - 由 start_workflow MCP 工具處理（不在這裡）
            # 2. 當前步驟為互動步驟，或下一步為互動步驟 - 需要生成提示給使用者
            # 3. 工作流完成 - 需要生成最終結果
            should_generate_response = is_workflow_complete or current_step_is_interactive or next_step_is_interactive
            
            if not should_generate_response:
                debug_log(2, f"[LLM] 步驟完成，下一步非互動步驟，靜默批准並推進")
                # ✅ 靜默批准：Processing 步驟後自動推進，不生成回應
                self._approve_workflow_step(session_id, None)
                return
            
            # ✅ 需要生成回應：將工作流事件加入待處理隊列
            if not hasattr(self, '_pending_workflow_events'):
                self._pending_workflow_events = []
            
            debug_log(2, f"[LLM] 將工作流事件加入待處理隊列，review_data keys: {list(review_data.keys()) if review_data else 'None'}")
            
            self._pending_workflow_events.append({
                "type": "workflow_step_completed" if not is_workflow_complete else "workflow_completed",
                "session_id": session_id,
                "workflow_type": workflow_type,
                "step_result": step_result,
                "review_data": review_data,
                "is_complete": is_workflow_complete,
                "should_respond": should_respond_to_user,
                "should_end_session": should_end_session,
                "current_step_info": current_step_info,  # 🆕 傳遞當前步驟資訊
                "next_step_info": next_step_info,  # 🆕 傳遞下一步資訊
                "timestamp": time.time()
            })
            
            info_log(f"[LLM] 工作流事件已加入隊列: {workflow_type}, is_complete={is_workflow_complete}, current_interactive={current_step_is_interactive}")
            
            # 🆕 處理需要生成回應的情況
            # 🔧 修正：工作流完成時不檢查互動步驟，直接處理完成邏輯
            if is_workflow_complete:
                debug_log(2, f"[LLM] 工作流完成，立即生成最終總結回應")
                self._process_workflow_completion(session_id, workflow_type, step_result, review_data)
                return  # ⚠️ 重要：工作流完成後直接返回，不再處理後續邏輯
            elif current_step_is_interactive or next_step_is_interactive:
                # ⚠️ 關鍵修正：訂閱 OUTPUT_LAYER_COMPLETE，在當前 cycle 的輸出完成後再處理
                # 這樣可以確保互動步驟提示在正確的順序生成
                debug_log(2, f"[LLM] 當前或下一步是互動步驟，訂閱 OUTPUT_LAYER_COMPLETE 等待當前輸出完成")
                
                # 保存待處理的互動步驟信息
                if not hasattr(self, '_pending_interactive_prompts'):
                    self._pending_interactive_prompts = []
                
                self._pending_interactive_prompts.append({
                    'session_id': session_id,
                    'workflow_type': workflow_type,
                    'step_result': step_result,
                    'review_data': review_data,
                    'next_step_info': next_step_info,
                    'current_cycle_session': self._get_current_gs_id()  # 記錄當前 cycle 的 session
                })
                
                # 🔧 修正：不要立即批准步驟！
                # 當下一步是互動步驟時，LLM 應該生成提示給用戶，但不批准當前步驟
                # 工作流會自動進入等待輸入狀態（因為 _auto_advance 檢測到 InteractiveStep）
                # 只有當 LLM 需要批准當前步驟的結果時才調用 approve_step
                # 在這種情況下（LLM_PROCESSING → INTERACTIVE），LLM 只是提供提示，不批准
                
                # ⚠️ 重要：立即處理互動步驟提示，不等待 OUTPUT 完成
                # 因為在某些情況下（如步驟完成事件晚於輸出完成），OUTPUT 可能已經完成
                # 此時不會再觸發 OUTPUT_LAYER_COMPLETE 事件處理，導致提示永遠不會生成
                debug_log(2, f"[LLM] 立即生成互動步驟提示: {workflow_type}")
                self._process_interactive_step_prompt(
                    session_id,
                    workflow_type,
                    step_result,
                    review_data,
                    next_step_info
                )
                # 🔧 修正：從兩個隊列中移除（已經處理）
                # ⚠️ 重要：必須從 _pending_workflow_events 中移除，否則會在 handle() 中被再次處理
                # 導致 LLM 生成決策並錯誤地調用 approve_step
                self._pending_interactive_prompts.pop()
                # 從 _pending_workflow_events 中找到並移除對應的事件
                if hasattr(self, '_pending_workflow_events') and self._pending_workflow_events:
                    # 找到匹配的事件（session_id 相同）
                    self._pending_workflow_events = [
                        e for e in self._pending_workflow_events 
                        if e.get('session_id') != session_id
                    ]
                    debug_log(2, f"[LLM] 已從待處理隊列中移除互動步驟事件: {session_id}")
            else:
                # 🔧 其他情況：等待下次 handle() 調用
                debug_log(2, f"[LLM] 工作流事件已準備好，等待下次 handle() 調用生成回應")
            
        except Exception as e:
            import traceback
            error_log(f"[LLM] 處理工作流步驟完成事件失敗: {e}")
            error_log(f"[LLM] 堆疊追蹤:\n{traceback.format_exc()}")
    
    def _handle_llm_processing_request(self, session_id: str, workflow_type: str, llm_request_data: dict):
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
            debug_log(2, f"[LLM] 開始處理 LLM_PROCESSING 請求: {workflow_type}")
            
            # 提取請求數據
            prompt = llm_request_data.get('prompt')
            output_key = llm_request_data.get('output_data_key')
            task_description = llm_request_data.get('task_description', '')
            
            if not prompt:
                error_log(f"[LLM] LLM_PROCESSING 請求缺少 prompt")
                return
            
            if not output_key:
                error_log(f"[LLM] LLM_PROCESSING 請求缺少 output_data_key")
                return
            
            debug_log(3, f"[LLM] 任務描述: {task_description}")
            debug_log(3, f"[LLM] 輸出鍵: {output_key}")
            debug_log(3, f"[LLM] Prompt 長度: {len(prompt)} 字符")
            
            # 🔧 使用 internal 模式生成 LLM 回應（節省 token）
            # internal 模式：不使用 UEP 系統提示詞、不使用快取、不使用 MCP 工具
            debug_log(2, f"[LLM] 正在調用 Gemini API（internal 模式）...")
            
            # 構建簡潔的系統提示詞（僅針對工作流任務）
            workflow_system_prompt = (
                "You are a helpful assistant processing workflow tasks. "
                "Provide clear, concise responses based on the given instructions. "
                "Follow the format requirements strictly. And ALWAYS respond in English"
            )
            
            response_data = self.model.query(
                prompt, 
                mode="internal",  # 🔧 使用 internal 模式
                cached_content=None,  # 不使用快取
                tools=None,  # 不使用 MCP 工具
                system_instruction=workflow_system_prompt  # 使用簡潔的系統提示詞
            )
            
            if not response_data or 'text' not in response_data:
                error_log(f"[LLM] Gemini API 回應無效: {response_data}")
                return
            
            llm_result = response_data['text']
            debug_log(2, f"[LLM] LLM 回應已生成 (長度: {len(llm_result)})")
            debug_log(3, f"[LLM] 回應內容預覽: {llm_result[:200]}...")
            
            # 寫入工作流會話數據
            from core.sessions.session_manager import session_manager
            workflow_session = session_manager.get_workflow_session(session_id)
            
            if not workflow_session:
                error_log(f"[LLM] 找不到工作流會話: {session_id}")
                return
            
            workflow_session.add_data(output_key, llm_result)
            debug_log(2, f"[LLM] 已將 LLM 結果寫入會話數據鍵: {output_key}")
            
            # 🔧 觸發工作流繼續執行
            # 使用 provide_workflow_input MCP 工具來推進工作流
            debug_log(2, f"[LLM] 調用 provide_workflow_input 推進工作流...")
            
            import asyncio
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # 調用 MCP 工具推進工作流（使用空字符串作為輸入，因為LLM結果已寫入會話）
            continue_result = loop.run_until_complete(
                self.mcp_client.call_tool(
                    "provide_workflow_input",
                    {
                        "session_id": session_id,
                        "user_input": "",  # 空輸入，因為數據已在會話中
                        "use_fallback": True  # 使用 fallback 模式直接推進
                    }
                )
            )
            
            debug_log(2, f"[LLM] 工作流推進結果: {continue_result.get('status')}")
            debug_log(2, f"[LLM] LLM_PROCESSING 請求處理完成，工作流已繼續")
            
        except Exception as e:
            import traceback
            error_log(f"[LLM] 處理 LLM_PROCESSING 請求失敗: {e}")
            error_log(f"[LLM] 堆疊追蹤:\n{traceback.format_exc()}")
    
    def _handle_workflow_failed(self, event):
        """
        ✅ 處理工作流失敗事件
        
        當工作流執行過程中發生錯誤時：
        1. 生成自然語言的錯誤說明
        2. 調用 cancel_workflow MCP 工具優雅終止工作流
        3. 通知使用者錯誤情況
        
        Args:
            event: Event object containing error data
        """
        try:
            debug_log(2, f"[LLM] 收到工作流失敗事件: {event.event_id}")
            
            data = event.data
            session_id = data.get("session_id")
            workflow_type = data.get("workflow_type")
            error_message = data.get("error_message")
            current_step = data.get("current_step")
            
            error_log(f"[LLM] 工作流失敗: {workflow_type} ({session_id}) - {error_message}")
            
            # ✅ 將錯誤事件加入待處理隊列
            if not hasattr(self, '_pending_workflow_events'):
                self._pending_workflow_events = []
            
            self._pending_workflow_events.append({
                "type": "workflow_failed",
                "session_id": session_id,
                "workflow_type": workflow_type,
                "error_message": error_message,
                "current_step": current_step,
                "timestamp": time.time()
            })
            
            info_log(f"[LLM] 工作流錯誤事件已加入隊列: {workflow_type}")
            
        except Exception as e:
            error_log(f"[LLM] 處理工作流失敗事件錯誤: {e}")
    
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
            if not hasattr(self, '_pending_interactive_prompts') or not self._pending_interactive_prompts:
                return
            
            current_gs = self._get_current_gs_id()
            
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
            for prompt_info in prompts_to_process:
                debug_log(2, f"[LLM] OUTPUT 完成後處理互動步驟提示: {prompt_info['workflow_type']}")
                self._process_interactive_step_prompt(
                    prompt_info['session_id'],
                    prompt_info['workflow_type'],
                    prompt_info['step_result'],
                    prompt_info['review_data'],
                    prompt_info['next_step_info']
                )
        
        except Exception as e:
            import traceback
            error_log(f"[LLM] 處理輸出完成事件失敗: {e}")
            error_log(f"[LLM] 堆疊追蹤:\n{traceback.format_exc()}")
    
    def _submit_workflow_review_request(self, session_id: str, workflow_type: str, is_complete: bool):
        """
        提交工作流審核請求到 ModuleCoordinator
        
        通過 ModuleCoordinator 提交一個內部處理請求，觸發新的 PROCESSING → OUTPUT 循環，
        讓 LLM 生成工作流進度/完成回應並通過 TTS 播放
        
        Args:
            session_id: 工作流會話 ID
            workflow_type: 工作流類型
            is_complete: 是否為工作流完成事件
        """
        try:
            from core.module_coordinator import module_coordinator
            from core.sessions.session_manager import unified_session_manager
            
            # 獲取當前活躍的 GS
            all_sessions = unified_session_manager.get_all_active_session_ids()
            gs_id = all_sessions.get('general_session_id')
            
            debug_log(3, f"[LLM] 查找 GS: all_sessions={all_sessions}, gs_id={gs_id}")
            
            if not gs_id:
                error_log(f"[LLM] 無法找到活躍 GS，無法觸發審核循環")
                # 如果沒有 GS，直接批准步驟
                if not is_complete:
                    self._approve_workflow_step(session_id, None)
                return
            
            # 構建內部處理請求
            # 這個請求會被路由到 LLM，LLM 會看到 _pending_workflow_events 並處理
            internal_request = {
                "session_id": gs_id,
                "cycle_index": getattr(module_coordinator, 'current_cycle_index', 0) + 1,
                "layer": "PROCESSING",
                "input_text": f"[WORKFLOW_EVENT] {workflow_type} - {'completed' if is_complete else 'step_completed'}",
                "metadata": {
                    "workflow_review": True,
                    "workflow_session_id": session_id,
                    "workflow_type": workflow_type,
                    "is_complete": is_complete
                }
            }
            
            debug_log(2, f"[LLM] 生成工作流審核回應: {gs_id}")
            
            # 🔧 生成審核回應文本
            response_text = self._generate_workflow_review_text(is_complete)
            
            if response_text:
                # 通過 ModuleCoordinator 提交處理層請求
                # 這會觸發: Router → TTS → OUTPUT_LAYER_COMPLETE
                completion_data = {
                    "session_id": gs_id,
                    "cycle_index": internal_request.get("cycle_index", 0),
                    "layer": "PROCESSING",
                    "response": response_text,
                    "source_module": "llm",
                    "llm_output": {
                        "text": response_text,
                        "success": True,
                        "metadata": {
                            "workflow_review": True,
                            "workflow_session_id": session_id,
                            "session_control": {'action': 'end_session'} if is_complete else None
                        }
                    },
                    "timestamp": time.time(),
                    "completion_type": "processing_layer_finished",
                    "success": True
                }
                
                # 提交到 ModuleCoordinator
                from core.event_bus import event_bus, SystemEvent
                event_bus.publish(
                    event_type=SystemEvent.PROCESSING_LAYER_COMPLETE,
                    data=completion_data,
                    source="llm"
                )
                
                debug_log(2, f"[LLM] 已發布工作流審核回應事件")
            
            # 處理完成後，批准工作流步驟（如果不是完成事件）
            if not is_complete:
                self._approve_workflow_step(session_id, None)
            
        except Exception as e:
            error_log(f"[LLM] 提交工作流審核請求失敗: {e}")
            import traceback
            debug_log(1, f"[LLM] 錯誤詳情: {traceback.format_exc()}")
            # 失敗時直接批准步驟
            if not is_complete:
                self._approve_workflow_step(session_id, None)
    
    def _generate_workflow_review_text(self, is_complete: bool) -> Optional[str]:
        """
        生成工作流審核回應文本
        
        從待處理事件隊列中取出事件，生成適當的審核回應文本
        
        Args:
            is_complete: 是否為工作流完成事件
            
        Returns:
            審核回應文本
        """
        try:
            if not hasattr(self, '_pending_workflow_events') or not self._pending_workflow_events:
                return None
            
            # 取出第一個待處理事件
            event = self._pending_workflow_events.pop(0)
            
            workflow_type = event.get('workflow_type', 'unknown')
            step_result = event.get('step_result', {})
            review_data = event.get('review_data', {})
            
            # 根據事件類型生成回應
            if is_complete:
                # 工作流完成：生成完成回應
                if workflow_type == 'drop_and_read' and review_data:
                    file_name = review_data.get('file_name', '檔案')
                    content = review_data.get('full_content', '')
                    content_length = review_data.get('content_length', 0)
                    
                    # 🔧 使用 LLM 智能處理檔案內容
                    if content_length > 500:
                        # 內容過長：建議使用摘要功能，只提供前100字符預覽
                        preview = content[:100] if content else ""
                        
                        prompt = (
                            f"You are U.E.P., an interdimensional being. You've just read a file named '{file_name}' "
                            f"which contains {content_length} characters.\n\n"
                            f"Here's a brief preview of the beginning:\n{preview}...\n\n"
                            f"The content is quite long. Please respond to the user in English:\n"
                            f"1. Acknowledge that you've read the file\n"
                            f"2. Mention the file is long ({content_length} characters)\n"
                            f"3. Provide a very brief description of what you see in the preview (in English, even if the content is in another language)\n"
                            f"4. Suggest using the summary feature for detailed analysis\n\n"
                            f"Keep your response natural, friendly, and concise (2-3 sentences max)."
                        )
                    else:
                        # 內容適中：用英文描述/摘要內容
                        prompt = (
                            f"You are U.E.P., an interdimensional being. You've just read a file named '{file_name}'.\n\n"
                            f"File content:\n{content}\n\n"
                            f"Please respond to the user in English:\n"
                            f"1. Acknowledge that you've read the file\n"
                            f"2. Provide a brief, natural description or summary of the content IN ENGLISH\n"
                            f"   - If the content is in another language (e.g., Chinese, Japanese), translate or explain it in English\n"
                            f"   - Focus on the main topic and key points\n"
                            f"3. Keep it conversational and concise (3-4 sentences max)\n\n"
                            f"IMPORTANT: Always respond in English, regardless of the original language of the content."
                        )
                    
                    # 調用 LLM 生成智能回應
                    try:
                        response = self.model.query(prompt, mode="internal")
                        return response.get("text", f"I've read the file {file_name}.")
                    except Exception as e:
                        error_log(f"[LLM] 生成檔案內容回應失敗: {e}")
                        # 降級方案
                        if content_length > 500:
                            return f"I've read the file {file_name} ({content_length} characters). The content is quite long. I recommend using the summary feature."
                        else:
                            return f"I've read the file {file_name}. The file contains approximately {content_length} characters of content."
                
                return f"Workflow {workflow_type} has been completed successfully."
            else:
                # 中間步驟：生成進度回應
                if workflow_type == 'drop_and_read':
                    if review_data and 'file_path' in review_data:
                        return "好的，我已經收到檔案了，正在讀取內容..."
                
                return f"工作流 {workflow_type} 正在進行中，請稍候..."
                
        except Exception as e:
            error_log(f"[LLM] 生成工作流審核文本失敗: {e}")
            return None
    
    def _get_pending_workflow_context(self) -> Optional[Dict[str, Any]]:
        """
        獲取待處理的工作流上下文數據
        
        從待處理事件隊列中取出工作流事件，構建為 workflow_context
        供 handle() 方法使用
        
        Returns:
            工作流上下文字典，如果沒有待處理事件則返回 None
        """
        try:
            if not hasattr(self, '_pending_workflow_events') or not self._pending_workflow_events:
                return None
            
            # 取出第一個待處理事件
            event = self._pending_workflow_events.pop(0)
            
            event_type = event.get('type', 'workflow_step_completed')
            
            # 🆕 處理工作流錯誤事件
            if event_type == 'workflow_failed':
                workflow_context = {
                    'type': 'workflow_error',
                    'workflow_session_id': event.get('session_id'),
                    'workflow_type': event.get('workflow_type'),
                    'error_message': event.get('error_message'),
                    'current_step': event.get('current_step')
                }
                debug_log(2, f"[LLM] 構建工作流錯誤上下文: workflow={workflow_context['workflow_type']}, "
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
                    'next_step_info': event.get('next_step_info')  # 🆕 包含下一步資訊
                }
                
                debug_log(2, f"[LLM] 構建工作流上下文: type={workflow_context['type']}, "
                            f"workflow={workflow_context['workflow_type']}, "
                            f"complete={workflow_context['is_complete']}")
            
            return workflow_context
            
        except Exception as e:
            error_log(f"[LLM] 獲取工作流上下文失敗: {e}")
            return None
    
    def _approve_workflow_step(self, session_id: str, modifications: Optional[Dict] = None):
        """批准工作流步驟並繼續"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        loop.run_until_complete(
            self.mcp_client.call_tool("approve_step", {
                "session_id": session_id,
                "modifications": modifications or {}
            })
        )
        debug_log(2, f"[LLM] 已批准工作流步驟: {session_id}")
        # 步驟批准後會自動執行，不需要額外的事件通知
    
    def _modify_workflow_step(self, session_id: str, modifications: Dict[str, Any]):
        """修改工作流步驟並重試"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        loop.run_until_complete(
            self.mcp_client.call_tool("modify_step", {
                "session_id": session_id,
                "modifications": modifications
            })
        )
        debug_log(2, f"[LLM] 已修改工作流步驟: {session_id}")
    
    def _cancel_workflow(self, session_id: str, reason: str):
        """取消工作流"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        loop.run_until_complete(
            self.mcp_client.call_tool("cancel_workflow", {
                "session_id": session_id,
                "reason": reason
            })
        )
        debug_log(2, f"[LLM] 已取消工作流: {session_id}")
    
    def _handle_workflow_completion(self, session_id: str, workflow_type: str, 
                                    step_result: Dict[str, Any], review_data: Dict[str, Any],
                                    should_end_session: bool):
        """
        🆕 處理工作流完成事件
        
        當工作流的最後一步完成時：
        1. 提取工作流結果數據
        2. 生成用戶回應（告訴用戶結果）
        3. 結束會話（如果需要）
        
        Args:
            session_id: 工作流會話 ID
            workflow_type: 工作流類型
            step_result: 最後一步的結果
            review_data: LLM 審核數據（包含檔案內容等）
            should_end_session: 是否應該結束會話
        """
        try:
            info_log(f"[LLM] 處理工作流完成: {workflow_type} ({session_id})")
            
            # 提取檔案信息
            file_name = review_data.get('file_name', 'unknown file')
            content = review_data.get('full_content', '')
            content_length = review_data.get('content_length', 0)
            
            # 構建 prompt 讓 LLM 生成用戶回應
            prompt = (
                f"A workflow has been completed successfully.\n\n"
                f"Workflow: {workflow_type}\n"
                f"File: {file_name}\n"
                f"Content Length: {content_length} characters\n\n"
                f"File Content:\n{content[:1000]}{'...' if len(content) > 1000 else ''}\n\n"
                f"Please generate a friendly response to the user in Traditional Chinese, "
                f"summarizing what was done and providing key insights from the file content. "
                f"Keep it concise and helpful."
            )
            
            # 調用 LLM 生成回應
            # ⚠️ 關鍵：工作流完成回應時不提供 MCP 工具且不使用快取（避免 LLM 從快取中調用 approve_step 等工具）
            debug_log(2, f"[LLM] 生成工作流完成回應（不提供 MCP 工具，不使用快取）")
            response = self.model.query(prompt, mode="internal", tools=None, cached_content=None)
            
            if "text" in response:
                user_response = response["text"]
            else:
                user_response = f"已成功讀取檔案 {file_name}，內容長度: {content_length} 字符。"
            
            info_log(f"[LLM] 工作流完成回應: {user_response[:100]}...")
            
            # 🆕 將回應發送到處理層完成事件，觸發 TTS 輸出
            from core.event_bus import event_bus, SystemEvent
            import time
            
            # 準備 LLM 輸出數據
            llm_output = {
                "text": user_response,
                "sys_action": None,
                "status_updates": None,
                "learning_data": None,
                "conversation_entry": None,
                "session_state": None,
                "memory_observation": None,
                "memory_summary": None,
                "emotion": "neutral",
                "confidence": 0.9,
                "processing_time": 0.0,
                "success": True,
                "error": None,
                "tokens_used": 0,
                "metadata": {
                    "mode": "WORK",
                    "workflow_type": workflow_type,
                    "workflow_session_id": session_id,
                    # 🆕 Task 5: 結束會話控制
                    "session_control": {"action": "end_session"} if should_end_session else None
                },
                "mood": "neutral",
                "status": "ok"
            }
            
            # 發布處理層完成事件，觸發 TTS 輸出
            event_bus.publish(
                SystemEvent.PROCESSING_LAYER_COMPLETE,
                {
                    "session_id": "workflow_completion",  # 臨時會話 ID
                    "cycle_index": 0,
                    "layer": "PROCESSING",
                    "response": user_response,
                    "source_module": "llm",
                    "llm_output": llm_output,
                    "timestamp": time.time(),
                    "completion_type": "processing_layer_finished",
                    "mode": "WORK",
                    "success": True
                },
                source="llm"
            )
            
            info_log(f"[LLM] 已發布工作流完成回應到處理層" + 
                    (f"，將結束會話" if should_end_session else ""))
            
            # ✅ 清除 workflow_processing 標誌，允許下一次輸入層運行
            from core.working_context import working_context_manager
            working_context_manager.set_skip_input_layer(False, reason="workflow_completion_processed")
            debug_log(2, "[LLM] 已清除 workflow_processing 標誌")
            
            # 🔧 清理追蹤標記，防止內存洩漏
            if session_id in self._processed_workflow_completions:
                self._processed_workflow_completions.discard(session_id)
                debug_log(2, f"[LLM] 已移除工作流完成追蹤: {session_id}")
            
            # 🔧 清理該工作流的所有 LLM_PROCESSING 步驟標記
            if hasattr(self, '_processed_llm_steps'):
                steps_to_remove = {key for key in self._processed_llm_steps if key.startswith(f"{session_id}:")}
                for step_key in steps_to_remove:
                    self._processed_llm_steps.discard(step_key)
                if steps_to_remove:
                    debug_log(2, f"[LLM] 已清理 {len(steps_to_remove)} 個 LLM_PROCESSING 步驟標記")
            
        except Exception as e:
            error_log(f"[LLM] 處理工作流完成失敗: {e}")
    
    def set_mcp_server(self, mcp_server):
        """
        設置 MCP Server 實例
        
        Args:
            mcp_server: SYS 模組的 MCP Server 實例
        """
        self.mcp_client.set_mcp_server(mcp_server)
        info_log("[LLM] MCP Server 已設置，MCP 工具功能已啟用")
        debug_log(2, f"[LLM] 可用的 MCP 工具: {len(self.mcp_client.get_tools_for_llm())} 個")
    
    def get_mcp_tools_for_llm(self) -> List[Dict[str, Any]]:
        """
        獲取 MCP 工具規範供 LLM function calling 使用
        
        Returns:
            工具規範列表
        """
        return self.mcp_client.get_tools_for_llm()
    
    async def handle_mcp_tool_call(self, tool_name: str, tool_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        處理 LLM 的 MCP 工具呼叫
        
        Args:
            tool_name: 工具名稱
            tool_params: 工具參數
            
        Returns:
            工具執行結果
        """
        debug_log(2, f"[LLM] 處理 MCP 工具呼叫: {tool_name}")
        return await self.mcp_client.call_tool(tool_name, tool_params)
        
    def handle(self, data: dict) -> dict:
        """主要處理方法 - 重構版本，支援新的 CHAT/WORK 模式和新 Router 整合"""
        start_time = time.time()
        
        try:
            # 解析輸入為新架構
            llm_input = LLMInput(**data)
            info_log(f"[LLM] 開始處理請求 - 模式: {llm_input.mode}, 用戶輸入: {llm_input.text[:50]}...")
            debug_log(1, f"[LLM] 處理輸入 - 模式: {llm_input.mode}, 用戶輸入: {llm_input.text[:100]}...")
            
            # 🔧 在處理開始時獲取並保存 session_id 和 cycle_index
            # 避免在事件發布時動態讀取導致cycle已遞增的問題
            self._current_processing_session_id = self._get_current_gs_id()
            # ✅ 優先從輸入數據中讀取 cycle_index（由 MC 從 STATE_ADVANCED 或 INPUT_LAYER_COMPLETE 傳遞）
            # 如果輸入數據中沒有，才從 working_context 讀取
            if hasattr(llm_input, 'cycle_index') and llm_input.cycle_index is not None:
                self._current_processing_cycle_index = llm_input.cycle_index
                debug_log(3, f"[LLM] 從輸入數據讀取 cycle_index: {self._current_processing_cycle_index}")
            else:
                self._current_processing_cycle_index = self._get_current_cycle_index()
                debug_log(3, f"[LLM] 從 working_context 讀取 cycle_index: {self._current_processing_cycle_index}")
            debug_log(3, f"[LLM] 記錄處理上下文: session={self._current_processing_session_id}, cycle={self._current_processing_cycle_index}")
            
            # 檢查是否來自新 Router
            if llm_input.source_layer:
                info_log(f"[LLM] 來自新Router - 來源層級: {llm_input.source_layer}")
                debug_log(2, f"[LLM] 來自新Router - 來源層級: {llm_input.source_layer}")
                if llm_input.processing_context:
                    debug_log(3, f"[LLM] 處理層上下文: {llm_input.processing_context}")
            
            # 🔧 檢查是否為內部呼叫（繞過會話檢查和系統提示詞）
            is_internal = getattr(llm_input, 'is_internal', False)
            
            if is_internal:
                debug_log(1, "[LLM] 內部呼叫模式 - 使用簡潔系統提示詞")
                # 內部呼叫：使用簡潔提示詞，不使用快取或會話檢查
                try:
                    # 允許自定義系統提示詞（用於工作流），否則使用默認簡潔版本
                    internal_system_prompt = getattr(llm_input, 'system_instruction', None)
                    if not internal_system_prompt:
                        internal_system_prompt = (
                            "You are a helpful assistant. "
                            "Provide clear and concise responses."
                        )
                    
                    response_data = self.model.query(
                        llm_input.text,
                        mode="internal",
                        cached_content=None,  # 內部呼叫不使用快取
                        system_instruction=internal_system_prompt
                    )
                    
                    response_text = response_data.get("content", response_data.get("text", ""))
                    
                    processing_time = time.time() - start_time
                    self.processing_stats["total_requests"] += 1
                    self.processing_stats["total_processing_time"] += processing_time
                    
                    return {
                        "status": "ok",
                        "text": response_text,
                        "mode": "internal",
                        "processing_time": processing_time,
                        "timestamp": time.time()
                    }
                except Exception as e:
                    error_log(f"[LLM] 內部呼叫失敗: {e}")
                    return {
                        "status": "error",
                        "message": f"內部呼叫失敗: {str(e)}",
                        "timestamp": time.time()
                    }
            
            # 1. 獲取當前系統狀態和會話信息
            current_state = self.state_manager.get_current_state()
            info_log(f"[LLM] 當前系統狀態: {current_state}")
            
            # 1.1 更新協作管道（確保與系統狀態同步）
            self._update_collaboration_channels(current_state)
            
            status = self._get_current_system_status()
            # 🔧 如果有工作流會話ID，傳遞給 _get_current_session_info
            workflow_session_id = getattr(llm_input, 'workflow_session_id', None)
            self.session_info = self._get_current_session_info(workflow_session_id)
            
            # 1.2 會話架構檢查 - LLM 不應該在沒有適當會話的情況下運作
            if not self._validate_session_architecture(current_state):
                error_log("[LLM] 會話架構違規 - 拒絕處理請求")
                return {
                    "status": "error",
                    "message": "會話架構違規：LLM 需要適當的會話上下文",
                    "error_type": "session_architecture_violation",
                    "timestamp": time.time()
                }
            
            # 2. 處理身份上下文 (優先使用來自Router的)
            if llm_input.identity_context:
                identity_context = llm_input.identity_context
                debug_log(2, f"[LLM] 使用Router提供的Identity上下文: {identity_context}")
            else:
                identity_context = self._get_identity_context()
                debug_log(2, f"[LLM] 使用本地Identity上下文: {identity_context}")
            
            debug_log(2, f"[LLM] 系統狀態: {current_state}")
            debug_log(2, f"[LLM] StatusManager: {status}")
            debug_log(2, f"[LLM] 會話信息: {self.session_info}")
            
            # 3. 補充系統上下文到llm_input (整合Router數據)
            llm_input = self._enrich_with_system_context(
                llm_input, current_state, status, self.session_info, identity_context
            )
            
            # 🔧 檢查是否有待處理的工作流事件
            # 如果有，將工作流數據注入到 llm_input.workflow_context
            pending_workflow = self._get_pending_workflow_context()
            if pending_workflow:
                info_log(f"[LLM] 檢測到待處理工作流事件: {pending_workflow['workflow_type']}")
                # 將工作流數據合併到 workflow_context
                if llm_input.workflow_context:
                    llm_input.workflow_context.update(pending_workflow)
                else:
                    llm_input.workflow_context = pending_workflow
                # 確保進入 WORK 模式
                llm_input.mode = LLMMode.WORK
            
            # ✅ 檢查是否為工作流輸入場景（Interactive Input Step）
            # ✅ 優先級：如果工作流正在等待輸入，清除舊的 pending_workflow 並構建新的 workflow_input_required context
            from core.working_context import working_context_manager
            workflow_waiting_input = working_context_manager.is_workflow_waiting_input()
            
            if workflow_waiting_input and self.session_info and self.session_info.get('session_type') == 'workflow':
                # ✅ 如果有舊的 pending_workflow 數據，清除它（工作流現在需要用戶輸入，不再是審核場景）
                if pending_workflow:
                    debug_log(2, "[LLM] 工作流等待輸入，清除舊的 pending_workflow 數據")
                    self._pending_workflow_events.clear()  # 清除待處理事件
                    pending_workflow = None
                info_log("[LLM] 檢測到工作流輸入場景 - 構建 workflow_input_required context")
                
                # ✅ 從 working_context_manager 獲取實際的工作流輸入上下文
                saved_context = working_context_manager.get_context_data('workflow_input_context', {})
                workflow_session_id = saved_context.get('workflow_session_id') or self.session_info.get('session_id')
                
                # 構建 workflow_input_required context（使用實際值）
                workflow_input_context = {
                    'type': 'workflow_input_required',
                    'workflow_session_id': workflow_session_id,
                    'workflow_type': saved_context.get('workflow_type', 'unknown'),
                    'step_id': saved_context.get('step_id', 'input_step'),
                    'step_type': saved_context.get('step_type', 'interactive'),
                    'prompt': saved_context.get('prompt', '請提供輸入'),
                    'user_input': llm_input.text,  # 用戶的輸入文本
                    'is_optional': saved_context.get('optional', False),
                    'fallback_value': ''  # 空字串作為 fallback
                }
                
                # 合併到 workflow_context
                if llm_input.workflow_context:
                    llm_input.workflow_context.update(workflow_input_context)
                else:
                    llm_input.workflow_context = workflow_input_context
                
                # 確保進入 WORK 模式
                llm_input.mode = LLMMode.WORK
                
                debug_log(2, f"[LLM] workflow_input_context 已構建: {workflow_input_context}")
            
            # 根據模式切換處理邏輯
            if llm_input.mode == LLMMode.CHAT:
                output = self._handle_chat_mode(llm_input, status)
            elif llm_input.mode == LLMMode.WORK:
                output = self._handle_work_mode(llm_input, status)
            else:
                # 向後兼容舊的 intent 系統
                output = self._handle_legacy_mode(llm_input, status)
            
            # 轉換為字典格式返回（保持與舊系統的兼容）
            result = output.dict()
            result["status"] = "ok" if output.success else "error"
            
            # ✨ 如果 metadata 中有 workflow_decision，提取到頂層
            if output.metadata and "workflow_decision" in output.metadata:
                result["workflow_decision"] = output.metadata["workflow_decision"]
            
            # ✅ 事件驅動：發布處理層完成事件
            if output.success and result.get("text"):
                self._notify_processing_layer_completion(result)
            
            return result
                
        except Exception as e:
            error_log(f"[LLM] 處理時發生錯誤: {e}")
            return {
                "text": "處理時發生錯誤，請稍後再試。",
                "processing_time": time.time() - start_time,
                "tokens_used": 0,
                "success": False,
                "error": str(e),
                "confidence": 0.0,
                "metadata": {},
                "status": "error"
            }
    
    def _handle_chat_mode(self, llm_input: "LLMInput", status: Dict[str, Any]) -> "LLMOutput":
        """處理 CHAT 模式 - 與 MEM 協作的日常對話"""
        start_time = time.time()
        debug_log(2, "[LLM] 處理 CHAT 模式")
        
        try:
            # 1. MEM 協作：檢索相關記憶 (CHAT狀態專用)
            relevant_memories = []
            if not llm_input.memory_context:  # 只有在沒有提供記憶上下文時才檢索
                relevant_memories = self._retrieve_relevant_memory(llm_input.text, max_results=5)
                if relevant_memories:
                    debug_log(2, f"[LLM] 整合 {len(relevant_memories)} 條相關記憶到對話上下文")
                    # 將檢索到的記憶轉換為記憶上下文
                    llm_input.memory_context = self._format_memories_for_context(relevant_memories)
            
            # 2. 檢查 Context Cache (包含動態記憶)
            import hashlib
            base = f"{llm_input.mode}|{self.session_info.get('session_id','')}"
            text_sig = hashlib.sha256(llm_input.text.encode("utf-8")).hexdigest()[:16]
            mem_sig  = hashlib.sha256((llm_input.memory_context or "").encode("utf-8")).hexdigest()[:16]
            cache_key = f"chat:{base}:{text_sig}:{mem_sig}:{len(relevant_memories)}"
            cached_response = self.cache_manager.get_cached_response(cache_key)
            
            if cached_response and not llm_input.ignore_cache:
                debug_log(2, "[LLM] 使用快取回應（包含記憶上下文）")
                return cached_response
            
            # 提取 intent metadata（用於檢測降級的 WORK 請求）
            intent_metadata = None
            if llm_input.entities and isinstance(llm_input.entities, dict):
                intent_metadata = llm_input.entities.get('intent_metadata')
            elif llm_input.processing_context and isinstance(llm_input.processing_context, dict):
                intent_metadata = llm_input.processing_context.get('intent_metadata')
            
            # 3. 構建 CHAT 提示（整合記憶和降級警告）
            prompt = self.prompt_manager.build_chat_prompt(
                user_input=llm_input.text,
                identity_context=llm_input.identity_context,
                memory_context=llm_input.memory_context,
                conversation_history=getattr(llm_input, 'conversation_history', None),
                is_internal=False,
                relevant_memories=relevant_memories,  # 新增：傳入檢索到的記憶
                intent_metadata=intent_metadata  # 新增：傳入 intent metadata
            )
            
            # 3. 獲取或創建系統快取
            cached_content_ids = self._get_system_caches("chat")
            
            # 4. 呼叫 Gemini API (使用快取)
            response_data = self.model.query(
                prompt, 
                mode="chat",
                cached_content=cached_content_ids.get("persona")
            )
            response_text = response_data.get("text", "")
            
            # === 詳細回應日誌 ===
            info_log(f"[LLM] 🤖 Gemini回應: {response_text}")
            debug_log(1, f"[LLM] 📊 回應信心度: {response_data.get('confidence', 'N/A')}")
            
            # 記憶觀察日誌
            if response_data.get("memory_observation"):
                debug_log(1, f"[LLM] 💭 記憶觀察: {response_data['memory_observation']}")
            
            # 狀態更新日誌
            status_updates = response_data.get("status_updates")
            if status_updates:
                debug_log(1, f"[LLM] 📈 建議狀態更新:")
                for key, value in status_updates.items():
                    if value is not None:
                        debug_log(1, f"[LLM]   {key}: {value:+.2f}" if isinstance(value, (int, float)) else f"[LLM]   {key}: {value}")
            
            # 學習信號日誌
            learning_signals = response_data.get("learning_signals")
            if learning_signals:
                debug_log(1, f"[LLM] 🧠 學習信號:")
                for signal_type, value in learning_signals.items():
                    if value is not None:
                        debug_log(1, f"[LLM]   {signal_type}: {value:+.2f}")
            
            # 會話控制日誌
            session_control = response_data.get("session_control")
            if session_control:
                debug_log(1, f"[LLM] 🎮 會話控制建議:")
                debug_log(1, f"[LLM]   應結束會話: {session_control.get('should_end_session', False)}")
                if session_control.get('end_reason'):
                    debug_log(1, f"[LLM]   結束原因: {session_control['end_reason']}")
                if session_control.get('confidence'):
                    debug_log(1, f"[LLM]   信心度: {session_control['confidence']:.2f}")
            
            # 快取資訊日誌
            meta = response_data.get("_meta", {})
            if meta.get("cached_input_tokens", 0) > 0:
                debug_log(2, f"[LLM] 📚 快取命中: {meta['cached_input_tokens']} tokens")
            debug_log(2, f"[LLM] 📝 總輸入tokens: {meta.get('total_input_tokens', 0)}")
            
            # 處理 StatusManager 更新
            if "status_updates" in response_data and response_data["status_updates"]:
                self._process_status_updates(response_data["status_updates"])
            
            # 4. 處理MEM模組整合 (CHAT模式)
            memory_operations = self._process_chat_memory_operations(
                llm_input, response_data, response_text
            )
            
            # === 記憶操作日誌 ===
            if memory_operations:
                info_log(f"[LLM] 🧠 記憶操作處理:")
                for i, op in enumerate(memory_operations):
                    op_type = op.get('operation', 'unknown')
                    content = op.get('content', {})
                    if op_type == 'store':
                        user_text = content.get('user_input', '')[:50] + "..." if len(content.get('user_input', '')) > 50 else content.get('user_input', '')
                        assistant_text = content.get('assistant_response', '')[:50] + "..." if len(content.get('assistant_response', '')) > 50 else content.get('assistant_response', '')
                        info_log(f"[LLM]   #{i+1} 儲存對話: 用戶='{user_text}', 助手='{assistant_text}'")
                    else:
                        info_log(f"[LLM]   #{i+1} {op_type}: {str(content)[:100]}")
            else:
                debug_log(2, f"[LLM] 📝 無記憶操作需要處理")
            
            # 5. 處理學習信號
            if self.learning_engine.learning_enabled:
                # 處理新的累積評分學習信號
                ctx = llm_input.identity_context or {}
                if "learning_signals" in response_data and response_data["learning_signals"]:
                    
                    identity_id = (ctx.get("identity") or {}).get("id") or ctx.get("identity_id") or "default"
                    self.learning_engine.process_learning_signals(identity_id, response_data["learning_signals"])
                    
                # 保留舊的互動記錄（用於統計和分析）
                identity_id = (ctx.get("identity") or {}).get("id") or ctx.get("identity_id") or "default"
                self.learning_engine.record_interaction(
                    identity_id=identity_id,
                    interaction_type="CHAT",
                    user_input=llm_input.text,
                    system_response=response_text,
                    metadata={
                        "memory_used": bool(llm_input.memory_context),
                        "identity_used": bool(llm_input.identity_context)
                    }
                )
            
            # 5. 處理會話控制建議
            session_control_result = self._process_session_control(
                response_data, "CHAT", llm_input
            )
            
            # 6. 快取回應
            output = LLMOutput(
                text=response_text,
                processing_time=time.time() - start_time,
                tokens_used=len(response_text.split()),
                success=True,
                error=None,
                confidence=response_data.get("confidence", 0.85),
                sys_action=None,
                status_updates=StatusUpdate(**response_data["status_updates"]) if response_data.get("status_updates") else None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=response_data.get("memory_observation"),
                memory_summary=None,
                emotion="neutral",
                mood="neutral",
                metadata={
                    "mode": "CHAT",
                    "cached": False,
                    "memory_context_size": len(llm_input.memory_context) if llm_input.memory_context else 0,
                    "identity_context_size": len(llm_input.identity_context) if llm_input.identity_context else 0,
                    "memory_operations_count": len(memory_operations),
                    "memory_operations": memory_operations,
                    "session_control": session_control_result
                }
            )
            
            self.cache_manager.cache_response(cache_key, output)
            
            # 發布 LLM 回應生成事件
            self._publish_llm_response_event(output, "CHAT", {
                "memory_context_used": bool(llm_input.memory_context),
                "relevant_memories_count": len(relevant_memories) if relevant_memories else 0
            })
            
            return output
            
        except Exception as e:
            error_log(f"[LLM] CHAT 模式處理錯誤: {e}")
            return LLMOutput(
                text="聊天處理時發生錯誤，請稍後再試。",
                processing_time=time.time() - start_time,
                tokens_used=0,
                success=False,
                error=str(e),
                confidence=0.0,
                sys_action=None,
                status_updates=None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=None,
                memory_summary=None,
                emotion="neutral",
                mood="neutral",
                metadata={"mode": "CHAT", "error_type": "processing_error"}
            )
    
    def _handle_work_mode(self, llm_input: "LLMInput", status: Dict[str, Any]) -> "LLMOutput":
        """處理 WORK 模式 - 通過 MCP 與 SYS 協作的工作任務
        
        MCP 架構流程：
        
        Cycle 0（啟動工作流）：
        - LLM 通過 MCP function calling 調用 start_workflow
        - 返回：「工作流已啟動，第一步是...」
        
        Cycle 1+（工作流步驟互動）：
        - SYS 通過 review_step 返回當前步驟信息
        - LLM 將步驟轉換為用戶友好的描述
        - 用戶回應後，LLM 通過 MCP 調用 approve_step/modify_step/cancel_workflow
        - 重複直到工作流完成
        
        phase 參數（向後兼容）:
        - decision: 決策工作流類型（已廢棄，使用 MCP function calling）
        - response: 生成工作流回應（默認，包含 MCP 調用）
        """
        start_time = time.time()
        phase = getattr(llm_input, 'phase', 'response')  # 默認為 response 模式
        cycle_index = getattr(llm_input, 'cycle_index', 0)
        
        debug_log(2, f"[LLM] 處理 WORK 模式 (phase={phase}, cycle={cycle_index})")
        
        try:
            # ✨ Cycle 0 Decision Phase: 決策工作流類型
            if cycle_index == 0 and phase == 'decision':
                return self._decide_workflow(llm_input, start_time)
            
            # ✨ Response Phase: 生成工作流回應
            else:
                return self._generate_workflow_response(llm_input, status, start_time)
                
        except Exception as e:
            error_log(f"[LLM] WORK 模式處理錯誤: {e}")
            return LLMOutput(
                text="工作任務處理時發生錯誤，請稍後再試。",
                processing_time=time.time() - start_time,
                tokens_used=0,
                success=False,
                error=str(e),
                confidence=0.0,
                sys_action=None,
                status_updates=None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=None,
                memory_summary=None,
                emotion="neutral",
                mood="neutral",
                metadata={"mode": "WORK", "error_type": "processing_error", "phase": phase}
            )
    
    def _generate_system_report_response(self, llm_input: "LLMInput", status: Dict[str, Any], start_time: float) -> "LLMOutput":
        """生成系統報告回應（系統主動通知）
        
        系統報告是系統主動發出的通知（如待辦事項提醒、日曆事件等）
        不需要工作流引擎或 MCP 工具，直接生成簡潔友善的訊息
        
        Args:
            llm_input: LLM 輸入（包含通知內容）
            status: 系統狀態
            start_time: 開始時間
            
        Returns:
            LLMOutput: 生成的通知訊息
        """
        try:
            info_log("[LLM] 🔔 生成系統通知訊息")
            
            # Build system report prompt (request concise, friendly notification)
            notification_content = llm_input.text
            debug_log(2, f"[LLM] 📋 通知內容（輸入）: {notification_content}")
            
            system_report_prompt = f"""You are U.E.P., an interdimensional being. Your system has detected an event that you need to inform the user about.

System detected event:
{notification_content}

Your task: Convert this system-detected event into a friendly, natural spoken message to inform the user.
Think of it as: "I (U.E.P.) noticed this and want to let you know."

Requirements:
1. Use first person ("I") to show you're informing them (e.g., "I noticed...", "Just wanted to let you know...")
2. Keep it brief (1-2 sentences)
3. Friendly, conversational tone
4. Include all the important details from the notification
5. Don't ask questions - you're informing, not requesting

Examples:

System event: "Reminder: Todo item 'Complete Report' is due in one hour"
Your message: "Hey, just wanted to remind you - 'Complete Report' is due in an hour."

System event: "Reminder: 'Team Meeting' is about to start, Location: Meeting Room A"
Your message: "Heads up, 'Team Meeting' is starting soon in Meeting Room A."

System event: "Alert: Todo item 'Submit Proposal' is overdue"
Your message: "Just letting you know, 'Submit Proposal' is overdue."

Now convert the system event above into your spoken message:"""

            # 調試：記錄完整 Prompt
            debug_log(3, f"[LLM] 📝 系統通知 Prompt（前200字符）:\n{system_report_prompt[:200]}...")
            debug_log(3, f"[LLM] 📝 系統通知 Prompt（後200字符）:\n...{system_report_prompt[-200:]}")

            # 調用 Gemini API（不使用 MCP 工具和 function calling）
            response_data = self.model.query(
                system_report_prompt,
                mode="chat"  # 使用 chat 模式避免 function calling
            )
            
            # 調試：記錄 LLM 原始回應
            debug_log(3, f"[LLM] 🤖 Gemini 原始回應: {response_data.get('text', '')[:200]}")
            
            response_text = response_data.get("text", "").strip()
            
            if not response_text:
                # 如果 LLM 沒有生成回應，使用原始通知內容
                response_text = notification_content
                info_log("[LLM] ⚠️ LLM 未生成回應，使用原始通知內容")
            
            info_log(f"[LLM] ✅ 系統通知訊息已生成：{response_text[:50]}...")
            
            # 🔑 設置 session_control：系統通知完成後應該結束會話
            # 通知是一次性的，不需要持續對話
            session_control = {
                "should_end_session": True,
                "end_reason": "system_notification_complete",
                "confidence": 1.0
            }
            debug_log(2, f"[LLM] 🔚 系統通知完成，設置會話結束標記")
            
            return LLMOutput(
                text=response_text,
                processing_time=time.time() - start_time,
                tokens_used=response_data.get("_meta", {}).get("total_input_tokens", 0),
                success=True,
                error=None,
                confidence=1.0,
                sys_action=None,
                status_updates=None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=None,
                memory_summary=None,
                emotion="neutral",
                mood="cheerful",
                metadata={
                    "mode": "WORK",
                    "phase": "response",
                    "system_report": True,
                    "notification_type": getattr(llm_input, 'metadata', {}).get('notification_type', 'unknown'),
                    "session_control": session_control  # ✅ 添加 session_control
                }
            )
            
        except Exception as e:
            error_log(f"[LLM] 生成系統通知訊息失敗: {e}")
            # 失敗時返回原始通知內容
            return LLMOutput(
                text=llm_input.text,
                processing_time=time.time() - start_time,
                tokens_used=0,
                success=False,
                error=str(e),
                confidence=0.0,
                sys_action=None,
                status_updates=None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=None,
                memory_summary=None,
                emotion="neutral",
                mood="neutral",
                metadata={"mode": "WORK", "error_type": "system_report_error", "system_report": True}
            )
    
    def _decide_workflow(self, llm_input: "LLMInput", start_time: float) -> "LLMOutput":
        """決策工作流類型（Cycle 0, phase=decision）
        
        使用 LLM + MCP 工具來理解用戶意圖並決定適當的工作流
        用戶輸入為英文，系統內部溝通也使用英文
        """
        debug_log(2, "[LLM] 🎯 Using LLM with MCP tools to decide workflow")
        
        try:
            text = llm_input.text
            
            # 構建 decision 提示（英文）
            # LLM 使用自然語言理解來決定工作流，不依賴關鍵詞匹配
            decision_prompt = f"""
You are analyzing user intent to determine the appropriate workflow.

User input: "{text}"

Available workflows:
1. drop_and_read - Read file content via drag-and-drop interface
2. intelligent_archive - Archive and organize files intelligently  
3. summarize_tag - Generate summary and tags for files
4. file_selection - Let user choose specific file operations

Based on the user's input, determine which workflow is most appropriate.
Provide your analysis in JSON format:
{{
    "workflow_type": "<workflow_name>",
    "params": {{}},
    "reasoning": "<brief explanation in English>"
}}

Note: You have access to system functions via MCP tools. The SYS module will execute the chosen workflow.
"""
            
            # 調用 Gemini API 進行決策
            # 注意：MCP 工具在 workflow 執行時使用，decision 階段只需要 LLM 理解意圖
            response_data = self.model.query(
                decision_prompt,
                mode="work"
            )
            
            response_text = response_data.get("text", "")
            
            # 解析 LLM 的決策結果
            workflow_decision = self._parse_workflow_decision(response_text)
            
            if not workflow_decision:
                # 如果解析失敗，使用默認決策
                workflow_decision = {
                    "workflow_type": "file_selection",
                    "params": {},
                    "reasoning": "Unable to determine specific operation, let user choose"
                }
            
            info_log(f"[LLM] Decision result: {workflow_decision['workflow_type']} - {workflow_decision['reasoning']}")
            
            return LLMOutput(
                text="",  # decision phase doesn't return user-facing text
                processing_time=time.time() - start_time,
                tokens_used=response_data.get("_meta", {}).get("total_input_tokens", 0),
                success=True,
                error=None,
                confidence=0.85,
                sys_action=None,
                status_updates=None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=None,
                memory_summary=None,
                emotion="neutral",
                mood="neutral",
                metadata={
                    "mode": "WORK",
                    "phase": "decision",
                    "workflow_decision": workflow_decision
                }
            )
            
        except Exception as e:
            error_log(f"[LLM] Workflow decision error: {e}")
            raise
    
    def _parse_workflow_decision(self, response_text: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 返回的工作流決策
        
        Args:
            response_text: LLM 的原始響應文本
            
        Returns:
            解析後的 workflow_decision，失敗時返回 None
        """
        try:
            import json
            import re
            
            # 嘗試直接解析 JSON
            try:
                decision = json.loads(response_text)
                if "workflow_type" in decision:
                    return decision
            except json.JSONDecodeError:
                pass
            
            # 嘗試從文本中提取 JSON
            json_match = re.search(r'\{[^{}]*"workflow_type"[^{}]*\}', response_text, re.DOTALL)
            if json_match:
                try:
                    decision = json.loads(json_match.group())
                    return decision
                except json.JSONDecodeError:
                    pass
            
            # 如果無法解析，記錄錯誤
            debug_log(2, f"[LLM] Unable to parse workflow decision from: {response_text[:200]}")
            return None
            
        except Exception as e:
            error_log(f"[LLM] Error parsing workflow decision: {e}")
            return None
    
    def _handle_workflow_input_fast_path(self, llm_input: "LLMInput", workflow_context: Dict[str, Any], start_time: float) -> "LLMOutput":
        """
        快速路徑處理工作流輸入場景
        當檢測到 workflow_input_required 時，直接調用 provide_workflow_input 工具
        避免通過 Gemini API 理解用戶意圖，加快響應速度並避免超時
        
        ⚠️ 注意：如果輸入需要 LLM 解析（如自然語言轉結構化數據），返回 None 讓正常流程處理
        
        Args:
            llm_input: LLM 輸入
            workflow_context: 工作流上下文（包含 workflow_session_id、user_input 等）
            start_time: 開始時間
            
        Returns:
            LLMOutput: 處理結果，或 None 表示需要正常流程處理
        """
        try:
            import asyncio
            
            # 提取工作流資訊
            workflow_session_id = workflow_context.get('workflow_session_id', 'unknown')
            user_input = workflow_context.get('user_input', llm_input.text)
            is_optional = workflow_context.get('is_optional', False)
            step_id = workflow_context.get('step_id', 'unknown')
            prompt = workflow_context.get('prompt', '')
            
            # 🔧 檢測是否需要 LLM 解析
            # 如果提示要求結構化數據（包含 JSON、task_name、priority 等關鍵字），
            # 且用戶輸入是自然語言（不是 JSON 或 key=value 格式），
            # 則不使用快速路徑，讓 LLM 解析
            requires_structured_data = any(keyword in prompt.lower() for keyword in [
                'json', 'task_name', 'task_description', 'priority', 'deadline'
            ])
            
            is_natural_language = not (
                user_input.strip().startswith('{') or  # JSON 格式
                '=' in user_input  # key=value 格式
            )
            
            if requires_structured_data and is_natural_language:
                debug_log(2, f"[LLM] 快速路徑跳過：輸入需要 LLM 解析（step={step_id}）")
                return None  # 返回 None 讓正常流程處理
            
            info_log(f"[LLM] 快速路徑：直接提交工作流輸入 '{user_input}' 到步驟 {step_id}")
            
            # 直接調用 provide_workflow_input 工具
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # 調用 MCP 工具
            function_call_result = loop.run_until_complete(
                self.mcp_client.call_tool("provide_workflow_input", {
                    "session_id": workflow_session_id,
                    "user_input": user_input,
                    "use_fallback": False  # 用戶提供了明確輸入
                })
            )
            
            debug_log(2, f"[LLM] 快速路徑執行結果: {function_call_result.get('status')}")
            
            # 🔧 快速路徑的職責：
            # 1. 快速提交用戶輸入到工作流（避免 Gemini API 超時）
            # 2. 不生成任何回應文本（返回空字符串）
            # 3. 讓工作流步驟完成事件（WORKFLOW_STEP_COMPLETED）觸發正常的 LLM 審核流程
            #    - 工作流推進到下一步（如 archive_confirm）後會發出事件
            #    - LLM 訂閱該事件，將其加入 _pending_workflow_events 隊列
            #    - 下次循環時，LLM 會調用 Gemini 生成自然語言回應
            
            # ✅ 檢查工具調用是否成功
            result_status = function_call_result.get("status", "unknown")
            
            if result_status == "success":
                info_log(f"[LLM] 快速路徑：工作流輸入已成功提交，等待工作流事件觸發後續回應生成")
                # 返回空字符串，讓工作流事件驅動後續流程
                response_text = ""
            else:
                # 錯誤情況：提供簡單錯誤訊息
                error_msg = function_call_result.get("error", "Unknown error")
                response_text = f"處理時發生問題：{error_msg}。請再試一次。"
                error_log(f"[LLM] 快速路徑失敗: {error_msg}")
            
            # 構建 LLMOutput
            return LLMOutput(
                text=response_text,
                processing_time=time.time() - start_time,
                tokens_used=0,  # 快速路徑不使用 LLM tokens
                success=result_status == "success",
                error=None if result_status == "success" else function_call_result.get("error"),
                confidence=0.9,
                sys_action=None,
                status_updates=None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=None,
                memory_summary=None,
                emotion="neutral",
                mood="neutral",
                metadata={
                    "mode": "WORK",
                    "workflow_context_size": len(str(workflow_context)),
                    "sys_actions_count": 0,
                    "sys_actions": [],
                    "system_context_size": 0,
                    "session_control": None,
                    "function_call_made": True,
                    "function_call_result": function_call_result,
                    "fast_path": True  # 標記使用了快速路徑
                }
            )
            
        except Exception as e:
            error_log(f"[LLM] 快速路徑處理工作流輸入失敗: {e}")
            # 返回錯誤結果
            return LLMOutput(
                text="抱歉，處理您的輸入時發生錯誤，請稍後再試。",
                processing_time=time.time() - start_time,
                tokens_used=0,
                success=False,
                error=str(e),
                confidence=0.0,
                sys_action=None,
                status_updates=None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=None,
                memory_summary=None,
                emotion="neutral",
                mood="neutral",
                metadata={
                    "mode": "WORK",
                    "error_type": "fast_path_error",
                    "fast_path": True
                }
            )
    
    def _generate_workflow_response(self, llm_input: "LLMInput", status: Dict[str, Any], start_time: float) -> "LLMOutput":
        """生成工作流回應（所有 Cycle, phase=response）"""
        debug_log(2, "[LLM] 💬 生成工作流回應")
        
        try:
            # ✅ 檢查是否為系統報告模式（系統主動通知）
            is_system_report = getattr(llm_input, 'system_report', False)
            if is_system_report:
                debug_log(2, "[LLM] 🔔 系統報告模式：生成簡潔通知訊息")
                return self._generate_system_report_response(llm_input, status, start_time)
            
            # ✅ 檢查是否有運行中的工作流引擎
            # 這個檢查很重要：
            # 1. WS 是容器（可能存在但沒有工作流）
            # 2. WorkflowEngine 是實際的工作流執行器
            # 3. 只有當 WorkflowEngine 存在時，才認為有活躍的工作流
            has_active_workflow = False
            if self.session_info and self.session_info.get('session_type') == 'workflow':
                session_id = self.session_info.get('session_id')
                
                # 檢查 SYS 模組的 workflow_engines 字典中是否有對應的引擎
                try:
                    from core.framework import core_framework
                    sys_module = core_framework.get_module('sys')
                    
                    if sys_module and hasattr(sys_module, 'workflow_engines'):
                        has_active_workflow = session_id in sys_module.workflow_engines
                        if has_active_workflow:
                            debug_log(2, f"[LLM] 檢測到活躍的工作流引擎: {session_id}")
                        else:
                            debug_log(2, f"[LLM] WS 存在但無工作流引擎: {session_id}")
                    else:
                        debug_log(2, f"[LLM] 無法訪問 SYS 模組的 workflow_engines")
                except Exception as e:
                    debug_log(2, f"[LLM] 檢查工作流引擎時出錯: {e}")
                    # 保守策略：如果無法檢查，假設有工作流（避免重複啟動）
                    has_active_workflow = True
            
            # ✅ 檢查是否有待處理的工作流事件（正在審核步驟）
            pending_workflow = getattr(llm_input, 'workflow_context', None)
            is_reviewing_step = pending_workflow and pending_workflow.get('type') == 'workflow_step_response'
            
            # 🔧 快速路徑：如果是工作流輸入場景，直接調用 provide_workflow_input
            # 避免花費時間通過 Gemini API 理解用戶意圖，加快響應速度
            is_workflow_input = pending_workflow and pending_workflow.get('type') == 'workflow_input_required'
            debug_log(2, f"[LLM] 檢查快速路徑條件: pending_workflow={pending_workflow is not None}, type={pending_workflow.get('type') if pending_workflow else None}, is_workflow_input={is_workflow_input}")
            
            # ✅ 檢查：如果正在審核步驟結果，但用戶只是提供了簡單回應（如 "yes", "no"），
            # 這可能是誤判，實際上用戶是在回應互動步驟而不是審核步驟
            if is_reviewing_step and llm_input.text:
                user_text_lower = llm_input.text.strip().lower()
                simple_responses = ['yes', 'y', 'no', 'n', 'ok', 'confirm', 'cancel', 
                                   '確認', '取消', 'skip', '跳過', '']
                if user_text_lower in simple_responses:
                    # 用戶提供了簡單回應，這可能是互動步驟的輸入而非步驟審核
                    # 檢查是否有活躍的互動步驟在等待輸入
                    from core.working_context import working_context_manager
                    if working_context_manager.is_workflow_waiting_input():
                        debug_log(2, "[LLM] 檢測到簡單回應且工作流在等待輸入，切換為工作流輸入場景")
                        # 構建 workflow_input_required context
                        saved_context = working_context_manager.get_context_data('workflow_input_context', {})
                        workflow_session_id = saved_context.get('workflow_session_id') or self.session_info.get('session_id')
                        
                        pending_workflow = {
                            'type': 'workflow_input_required',
                            'workflow_session_id': workflow_session_id,
                            'workflow_type': saved_context.get('workflow_type', 'unknown'),
                            'step_id': saved_context.get('step_id', 'input_step'),
                            'user_input': llm_input.text,
                            'is_optional': saved_context.get('optional', False)
                        }
                        is_workflow_input = True
                        is_reviewing_step = False
            
            if is_workflow_input and pending_workflow:
                info_log("[LLM] 🚀 檢測到工作流輸入場景，嘗試使用快速路徑直接提交輸入")
                fast_path_result = self._handle_workflow_input_fast_path(llm_input, pending_workflow, start_time)
                if fast_path_result is not None:
                    return fast_path_result
                # 快速路徑返回 None 表示需要 LLM 解析，繼續正常流程
                debug_log(2, "[LLM] 快速路徑返回 None，繼續正常流程讓 LLM 解析輸入")
            
            from core.working_context import working_context_manager
            
            # ✅ 檢查是否有 MCP Server 可用
            # 即使在 workflow_step_response 時也提供工具，讓 LLM 自己決定是否使用（tool_choice=AUTO）
            is_step_response = pending_workflow and pending_workflow.get('type') == 'workflow_step_response'
            mcp_tools = None
            if self.mcp_client and hasattr(self.mcp_client, 'get_tools_as_gemini_format'):
                mcp_tools = self.mcp_client.get_tools_as_gemini_format()
                debug_log(2, f"[LLM] MCP 工具已準備: {len(mcp_tools) if mcp_tools else 0} 個")
                if is_step_response:
                    debug_log(2, "[LLM] 步驟回應模式：提供工具但不強制使用（tool_choice=AUTO）")
            
            # 🔧 決定 tool_choice 模式（在構建 prompt 之前）
            if not has_active_workflow and not is_reviewing_step and mcp_tools:
                tool_choice = "ANY"  # 強制調用工具（新請求應該啟動工作流）
                force_tool_use = True
            else:
                tool_choice = "AUTO"  # 自動決定（可能需要繼續工作流或只是回應）
                force_tool_use = False
            
            # 構建 WORK 提示
            prompt = self.prompt_manager.build_work_prompt(
                user_input=llm_input.text,
                available_functions=None,  # 不再需要文字描述，使用 MCP tools
                workflow_context=pending_workflow,
                identity_context=llm_input.identity_context,
                use_mcp_tools=True if mcp_tools else False,
                suppress_start_workflow_instruction=bool(has_active_workflow or is_reviewing_step),  # ✅ 已有工作流時抑制啟動指示
                force_tool_use=force_tool_use  # 🔧 傳遞是否強制調用工具
            )
            
            # 獲取或創建任務快取
            cached_content_ids = self._get_system_caches("work")
            
            # 🔍 DEBUG: 記錄發送給 Gemini 的 prompt
            if mcp_tools:
                debug_log(3, f"[LLM] Prompt 總長度: {len(prompt)} 字符")
                debug_log(3, f"[LLM] Prompt 前 500 字符:\n{prompt[:500]}...")
            
            # ✅ 呼叫 Gemini API (使用 MCP tools 進行 function calling)
            # tool_choice 已在構建 prompt 時決定
            debug_log(2, f"[LLM] Function calling 模式: {tool_choice} (has_active_workflow={has_active_workflow}, is_reviewing_step={is_reviewing_step}, has_tools={mcp_tools is not None})")
            
            response_data = self.model.query(
                prompt, 
                mode="work",
                cached_content=cached_content_ids.get("functions"),
                tools=mcp_tools,  # 傳入 MCP tools
                tool_choice=tool_choice  # 🔧 修復：使用動態決定的模式
            )
            
            # � 處理 MALFORMED_FUNCTION_CALL 錯誤：降級為 AUTO 模式重試
            if response_data.get("error") == "malformed_function_call" and tool_choice == "ANY":
                error_log(f"[LLM] 檢測到 MALFORMED_FUNCTION_CALL，降級為 AUTO 模式重試")
                debug_log(2, "[LLM] 使用 tool_choice=AUTO 重新調用 Gemini")
                
                response_data = self.model.query(
                    prompt, 
                    mode="work",
                    cached_content=cached_content_ids.get("functions"),
                    tools=mcp_tools,
                    tool_choice="AUTO"  # 降級為 AUTO 模式
                )
                
                # 如果還是失敗，最後嘗試不使用工具（純文本回應）
                if response_data.get("error") == "malformed_function_call":
                    error_log(f"[LLM] AUTO 模式仍然失敗，使用純文本模式")
                    response_data = self.model.query(
                        prompt, 
                        mode="work",
                        cached_content=cached_content_ids.get("functions"),
                        tools=None,  # 不使用工具
                        tool_choice="NONE"
                    )
            
            # �🔍 DEBUG: 記錄 Gemini 的原始響應
            debug_log(3, f"[LLM] Gemini 響應類型: {list(response_data.keys())}")
            if 'function_call' in response_data:
                debug_log(3, f"[LLM] Function call: {response_data['function_call']}")
            if 'text' in response_data:
                debug_log(3, f"[LLM] Text 響應: {response_data.get('text', '')[:200]}")
            
            # ✅ 處理 function call 回應
            function_call_result = None
            response_text = ""  # 初始化 response_text
            skip_default_followup = False  # 初始化跳過標誌
            follow_up_prompt = ""  # 初始化 follow_up_prompt
            
            if "function_call" in response_data and response_data["function_call"]:
                debug_log(2, f"[LLM] 檢測到 function call: {response_data['function_call']['name']}")
                
                # 同步調用 async function
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                function_call_result = loop.run_until_complete(
                    self.mcp_client.handle_llm_function_call(response_data["function_call"])
                )
                
                debug_log(2, f"[LLM] MCP 工具執行結果: {function_call_result.get('status')}")
                
                # ✅ 讓 Gemini 根據 MCP 結果生成回應
                # 構建包含工具執行結果的 follow-up prompt
                result_status = function_call_result.get("status", "unknown")
                # ✅ 從 content.data 獲取工作流資料（MCP ToolResult 結構）
                content = function_call_result.get("content", {})
                result_data = content.get("data", {}) if isinstance(content, dict) else {}
                result_message = function_call_result.get("formatted_message", "") or content.get("message", "")
                tool_name = function_call_result.get("tool_name", "unknown")
                
                # ✅ 判斷工作流狀態：從 message 推斷
                # - "started" (但不是 "completed" 或 "finished") → started
                # - "completed" 或 "finished" → completed
                # - 其他 → unknown
                workflow_status = "unknown"
                if isinstance(result_message, str):
                    msg_lower = result_message.lower()
                    # 先檢查 completed/finished（優先級較高）
                    if " completed" in msg_lower or " finished" in msg_lower or "完成" in result_message:
                        workflow_status = "completed"
                    # 再檢查 started（但確保不是 "will complete" 這類未來式）
                    elif "started" in msg_lower or "已啟動" in result_message:
                        workflow_status = "started"
                
                debug_log(2, f"[LLM] 推斷工作流狀態: {workflow_status} (from message: {result_message[:50]}...)")
                
                # ✅ 構建包含語言指示的 follow-up prompt
                language_instruction = (
                    "You are U.E.P., an interdimensional being who prefers to use English for communication.\n"
                    "Your current task: Provide a brief, friendly response to the user in English.\n\n"
                )
                
                if result_status == "success":
                    # ✅ resolve_path 成功：要求 LLM 繼續調用 provide_workflow_input
                    if tool_name == "resolve_path":
                        resolved_path = result_data.get("data", {}).get("resolved_path", "") if isinstance(result_data, dict) else ""
                        path_exists = result_data.get("data", {}).get("exists", False) if isinstance(result_data, dict) else False
                        
                        follow_up_prompt = (
                            f"The path has been successfully resolved:\n"
                            f"  Original: {result_data.get('data', {}).get('original_description', 'unknown')}\n"
                            f"  Resolved: {resolved_path}\n"
                            f"  Exists: {path_exists}\n\n"
                            f"Now you MUST call the provide_workflow_input tool to submit this resolved path:\n"
                            f"  provide_workflow_input(\n"
                            f"    session_id: <auto-injected>,\n"
                            f"    user_input: '{resolved_path}',\n"
                            f"    use_fallback: False\n"
                            f"  )\n\n"
                            f"DO NOT generate a text response. ONLY call the tool."
                        )
                        
                        # ✅ 保留工具列表，讓 LLM 能夠調用 provide_workflow_input
                        follow_up_response = self.model.query(
                            follow_up_prompt,
                            mode="work",
                            tools=mcp_tools  # ✅ 保留工具列表
                        )
                        
                        # 如果有 function call，處理它
                        if "function_call" in follow_up_response and follow_up_response["function_call"]:
                            debug_log(2, f"[LLM] resolve_path 後續調用: {follow_up_response['function_call']['name']}")
                            
                            # 執行第二個 function call
                            second_result = loop.run_until_complete(
                                self.mcp_client.handle_llm_function_call(follow_up_response["function_call"])
                            )
                            
                            debug_log(2, f"[LLM] 第二個工具執行結果: {second_result.get('status')}")
                            
                            # 這次需要文字回應
                            final_prompt = (
                                f"{language_instruction}"
                                f"The input has been successfully submitted to the workflow.\n"
                                f"Result: {second_result.get('formatted_message', '')}\n\n"
                                f"Please inform the user in a brief, friendly tone that you're processing their request.\n"
                                f"IMPORTANT: Respond in English only."
                            )
                            
                            final_response = self.model.query(final_prompt, mode="work", tools=None)
                            response_text = final_response.get("text", "Processing your request...")
                            
                            # 儲存完整的 function call 結果
                            function_call_result = second_result
                        else:
                            # LLM 沒有調用工具，使用預設回應
                            response_text = "I'm processing your request..."
                            debug_log(1, "[LLM] resolve_path 後 LLM 沒有調用 provide_workflow_input")
                        
                        # 跳過後續的 follow-up 處理
                        skip_default_followup = True
                    # ✅ provide_workflow_input 成功：提示下一步需求或確認完成
                    elif tool_name == "provide_workflow_input":
                        # 🔧 修復：從根級別提取工作流狀態信息（不是從 data 字段）
                        # MCP 工具返回的結構是 {status, requires_input, step_info, ...}
                        workflow_result_status = result_data.get("status", "unknown") if isinstance(result_data, dict) else "unknown"
                        requires_input = result_data.get("requires_input", False) if isinstance(result_data, dict) else False
                        step_info = result_data.get("step_info", {}) if isinstance(result_data, dict) else {}
                        current_step = step_info.get("current_step", {}) if step_info else {}
                        workflow_info = step_info.get("workflow_info", {}) if step_info else {}
                        previous_result = step_info.get("previous_step_result", {}) if step_info else {}
                        
                        # 如果工作流還在等待輸入（進入下一個 Interactive 步驟）
                        # LLM 應該提示用戶下一步需要什麼輸入
                        if workflow_result_status == "waiting" and requires_input:
                            debug_log(2, "[LLM] provide_workflow_input: 工作流需要下一步輸入，生成提示")
                            
                            # 提取下一步的信息
                            next_step_id = current_step.get("step_id", "unknown")
                            next_step_prompt = current_step.get("prompt", "")
                            next_step_description = current_step.get("description", "")
                            previous_message = previous_result.get("message", "")
                            
                            follow_up_prompt = (
                                f"{language_instruction}"
                                f"The user's input has been processed successfully.\n"
                                f"Previous step result: {previous_message}\n\n"
                                f"Now the workflow needs the next input:\n"
                                f"Step: {next_step_id}\n"
                                f"Description: {next_step_description}\n"
                                f"Prompt: {next_step_prompt}\n\n"
                                f"Your task: Inform the user in a natural, friendly way:\n"
                                f"1. Briefly acknowledge their previous input\n"
                                f"2. Clearly explain what input is needed next\n"
                                f"3. Use the step's prompt as guidance but rephrase it naturally\n\n"
                                f"IMPORTANT:\n"
                                f"- Keep it concise (2-3 sentences)\n"
                                f"- Be conversational and helpful\n"
                                f"- Respond in English only\n"
                            )
                            
                            final_response = self.model.query(follow_up_prompt, mode="work", tools=None)
                            response_text = final_response.get("text", next_step_prompt)
                            skip_default_followup = True
                        # 如果工作流完成或取消，讓 LLM 生成確認訊息
                        elif workflow_result_status in ["completed", "cancelled"]:
                            final_status = "completed" if workflow_result_status == "completed" else "cancelled"
                            follow_up_prompt = (
                                f"{language_instruction}"
                                f"The workflow has been {final_status}.\n"
                                f"Result: {result_message}\n\n"
                                f"Please inform the user in a friendly, conversational way.\n"
                                f"IMPORTANT: Keep it natural and concise, respond in English only."
                            )
                            
                            final_response = self.model.query(follow_up_prompt, mode="work", tools=None)
                            response_text = final_response.get("text", f"Workflow {final_status}.")
                            skip_default_followup = True
                        # 其他情況：工作流正在處理（Processing 步驟），不需要回應
                        # 等待 Processing 步驟完成後的 LLM 審核
                        else:
                            debug_log(2, f"[LLM] provide_workflow_input: 工作流處理中 (status={workflow_result_status})，跳過回應")
                            response_text = ""
                            skip_default_followup = True
                    # ✅ approve_step 成功：基於工作流上下文生成適當的回應
                    elif tool_name == "approve_step":
                        # 檢查是否為 workflow_step_response 場景
                        pending_workflow = getattr(llm_input, 'workflow_context', None)
                        if pending_workflow and pending_workflow.get('type') == 'workflow_step_response':
                            # 提取工作流上下文
                            is_complete = pending_workflow.get('is_complete', False)
                            next_step_info = pending_workflow.get('next_step_info')
                            next_step_is_interactive = next_step_info and next_step_info.get('step_type') == 'interactive' if next_step_info else False
                            step_result = pending_workflow.get('step_result', {})
                            review_data = pending_workflow.get('review_data', {})
                            debug_log(2, f"[LLM] 從 pending_workflow 提取的 review_data keys: {list(review_data.keys()) if review_data else 'None'}")
                            
                            if is_complete:
                                # 🔧 工作流完成：生成總結回應並結束會話
                                follow_up_prompt = (
                                    f"{language_instruction}"
                                    f"The workflow has completed successfully.\n"
                                    f"Step Result: {step_result.get('message', 'Success')}\n"
                                )
                                
                                # ✅ 提供豐富的審核數據給 LLM（包括文件內容等）
                                if review_data:
                                    debug_log(2, f"[LLM] 檢查 review_data 是否包含 full_content: {'full_content' in review_data}")
                                    # 特殊處理：如果有 full_content（文件讀取），提供完整內容
                                    if 'full_content' in review_data:
                                        debug_log(2, f"[LLM] 發現 full_content，添加到 prompt")
                                        file_name = review_data.get('file_name', 'unknown')
                                        content = review_data.get('full_content', '')
                                        content_length = review_data.get('content_length', len(content))
                                        follow_up_prompt += (
                                            f"\nFile Read Results:\n"
                                            f"- File: {file_name}\n"
                                            f"- Content Length: {content_length} characters\n"
                                            f"- Full Content:\n{content}\n"
                                        )
                                    else:
                                        debug_log(2, f"[LLM] 未發現 full_content，使用通用數據")
                                        # 通用數據：顯示前 500 字符
                                        follow_up_prompt += f"Workflow Data: {str(review_data)[:500]}\n"
                                
                                follow_up_prompt += (
                                    f"\nGenerate a natural, friendly response that:\n"
                                    f"1. Confirms the task is complete\n"
                                    f"2. Summarizes the key results/data (for file read, briefly mention the content)\n"
                                    f"3. Keep it conversational (2-3 sentences)\n"
                                    f"IMPORTANT: Respond in English only."
                                )
                                
                                # ✅ 工作流完成：設置 session_control 以觸發會話結束
                                # 在生成 follow-up 回應後，將通過 _process_session_control 檢測並標記待結束
                                # ModuleCoordinator 會在 processing→output 時檢測 session_control 並標記 WS
                                # Controller 會在 CYCLE_COMPLETED 時結束 WS（確保 LLM 回應和 TTS 輸出完成）
                                try:
                                    session_id = pending_workflow.get('session_id')
                                    wf_type = pending_workflow.get('workflow_type', 'workflow')
                                    if session_id:
                                        # 將 session_control 添加到 response_data，以便後續處理
                                        response_data["session_control"] = {
                                            "should_end_session": True,
                                            "end_reason": f"workflow_completed:{wf_type}",
                                            "confidence": 0.9
                                        }
                                        debug_log(1, f"[LLM] 🔚 工作流完成，已設置 session_control: {session_id}")
                                except Exception as e:
                                    error_log(f"[LLM] 設置 session_control 時出錯: {e}")
                            elif next_step_is_interactive:
                                # 下一步需要輸入：生成提示
                                next_prompt = next_step_info.get('prompt', 'Please provide input') if next_step_info else 'Please provide input'
                                follow_up_prompt = (
                                    f"{language_instruction}"
                                    f"The current step has been processed.\n"
                                    f"Next Step: User input required\n"
                                    f"Prompt: {next_prompt}\n\n"
                                    f"Generate a natural response that:\n"
                                    f"1. BRIEFLY acknowledges progress (1 sentence)\n"
                                    f"2. Asks the user for the needed input\n"
                                    f"3. Be friendly and conversational (2-3 sentences total)\n"
                                    f"IMPORTANT: Respond in English only."
                                )
                            else:
                                # 預設：確認步驟已批准
                                follow_up_prompt = (
                                    f"{language_instruction}"
                                    f"The step has been approved and the workflow is continuing.\n"
                                    f"Result: {step_result.get('message', 'Success')}\n\n"
                                    f"Generate a brief, friendly acknowledgment that you're processing the request.\n"
                                    f"IMPORTANT: Respond in English only."
                                )
                        else:
                            # 非 workflow_step_response 場景：使用預設回應
                            follow_up_prompt = (
                                f"{language_instruction}"
                                f"The step has been approved successfully.\n"
                                f"Result: {result_message}\n\n"
                                f"Please inform the user in a friendly tone that the process is continuing.\n"
                                f"IMPORTANT: Respond in English only."
                            )
                        # 不跳過，使用構建的 follow_up_prompt
                    # ✅ 工作流已啟動（新的非同步模式）
                    elif workflow_status == "started":
                        # 工作流已啟動，檢查是否需要用戶輸入
                        workflow_type = result_data.get("workflow_type", "task")
                        requires_input = result_data.get("requires_input", False)
                        current_step_prompt = result_data.get("current_step_prompt")
                        # 🔧 auto_continue 在嵌套的 data 字典中
                        workflow_data = result_data.get("data", {})
                        auto_continue = workflow_data.get("auto_continue", False)
                        
                        # 🔧 修正：requires_input 優先於 auto_continue
                        # 即使 auto_continue=True，如果當前步驟需要用戶輸入，也必須生成提示
                        if requires_input and current_step_prompt:
                            # 當前步驟需要輸入：生成提示詢問用戶
                            follow_up_prompt = (
                                f"{language_instruction}"
                                f"The workflow '{workflow_type}' has been started.\n"
                                f"The current step requires user input.\n"
                                f"Prompt: {current_step_prompt}\n\n"
                                f"Generate a natural response that:\n"
                                f"1. BRIEFLY confirms the workflow has started (1 sentence)\n"
                                f"2. Asks the user for the needed input based on the prompt\n"
                                f"3. Be friendly and conversational (2-3 sentences total)\n"
                                f"IMPORTANT: Respond in English only."
                            )
                        elif auto_continue:
                            # 🔧 工作流會自動完成（所有步驟都會自動執行，無需輸入）
                            # 跳過初始回應，等待工作流完成後再生成總結
                            debug_log(2, f"[LLM] 工作流會自動完成 ({workflow_type})，跳過初始回應，等待完成事件")
                            skip_default_followup = True
                            response_text = ""  # 不輸出初始回應
                        else:
                            # 工作流自動執行（參數已提供或無需輸入）
                            follow_up_prompt = (
                                f"{language_instruction}"
                                f"The workflow '{workflow_type}' has been started successfully.\n"
                                f"Result: {result_message}\n\n"
                                f"Please inform the user in a natural, friendly tone that you're processing their request and explain what will happen next (e.g., 'I'm checking the weather now').\n"
                                f"IMPORTANT: Respond in English only."
                            )
                    elif workflow_status == "completed":
                        # 工作流已完成（一步到位，舊模式）
                        follow_up_prompt = (
                            f"{language_instruction}"
                            f"The task has been completed successfully.\n"
                            f"Result: {result_message}\n\n"
                            f"Please inform the user in a friendly tone that the task is complete and briefly explain the result.\n"
                            f"IMPORTANT: Respond in English only."
                        )
                    else:
                        # 其他成功狀態
                        follow_up_prompt = (
                            f"{language_instruction}"
                            f"The workflow is currently running.\n"
                            f"Status: {result_message}\n\n"
                            f"Please inform the user in a natural, friendly tone that you're processing their request and explain what will happen next.\n"
                            f"IMPORTANT: Respond in English only."
                        )
                else:
                    # 失敗：讓 LLM 解釋錯誤並提供建議
                    error_msg = function_call_result.get("error", "Unknown error")
                    follow_up_prompt = (
                        f"{language_instruction}"
                        f"An error occurred while processing the request.\n"
                        f"Error: {error_msg}\n\n"
                        f"Please explain the problem to the user in a friendly way and suggest how they can resolve it.\n"
                        f"IMPORTANT: Respond in English only."
                    )
                
                # 檢查是否跳過預設 follow-up（已在特殊處理中完成）
                if not skip_default_followup:
                    debug_log(3, f"[LLM] 發送 follow-up prompt 給 Gemini 處理結果")
                    
                    # 第二次調用 Gemini（不使用 tools，只要文本回應）
                    follow_up_response = self.model.query(
                        follow_up_prompt,
                        mode="work",
                        tools=None  # 不需要 tools，只要文本回應
                    )
                    
                    response_text = follow_up_response.get("text", result_message)
                else:
                    debug_log(3, f"[LLM] 跳過預設 follow-up（已在特殊處理中完成）")
            else:
                response_text = response_data.get("text", "")
            
            # 處理 StatusManager 更新
            if "status_updates" in response_data and response_data["status_updates"]:
                self._process_status_updates(response_data["status_updates"])
            
            # 4. 處理SYS模組整合 (WORK模式) - 只在沒有使用 MCP function call 時才處理
            sys_actions = []
            if not function_call_result:
                sys_actions = self._process_work_system_actions(
                    llm_input, response_data, response_text
                )
            
            # 5. 處理學習信號
            if self.learning_engine.learning_enabled:
                # 處理新的累積評分學習信號
                ctx = llm_input.identity_context or {}
                if "learning_signals" in response_data and response_data["learning_signals"]:
                    identity_id = (ctx.get("identity") or {}).get("id") or ctx.get("identity_id") or "default"
                    self.learning_engine.process_learning_signals(identity_id, response_data["learning_signals"])
                
                # 保留舊的互動記錄（用於統計和分析）
                identity_id = (ctx.get("identity") or {}).get("id") or ctx.get("identity_id") or "default"
                self.learning_engine.record_interaction(
                    identity_id=identity_id,
                    interaction_type="WORK",
                    user_input=llm_input.text,
                    system_response=response_text,
                    metadata={
                        "workflow_context": llm_input.workflow_context,
                        "system_context_used": bool(llm_input.system_context)
                    }
                )
            
            # 6. 處理會話控制建議
            session_control_result = self._process_session_control(
                response_data, "WORK", llm_input
            )
            
            # 提取 sys_action
            sys_action_obj = None
            if sys_actions and len(sys_actions) > 0:
                sys_action_obj = SystemAction(**sys_actions[0])
            
            output = LLMOutput(
                text=response_text,
                processing_time=time.time() - start_time,
                tokens_used=len(response_text.split()),
                success=True,
                error=None,
                confidence=response_data.get("confidence", 0.90),
                sys_action=sys_action_obj,
                status_updates=StatusUpdate(**response_data["status_updates"]) if response_data.get("status_updates") else None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=None,
                memory_summary=None,
                emotion="neutral",
                mood="neutral",
                metadata={
                    "mode": "WORK",
                    "workflow_context_size": len(llm_input.workflow_context) if llm_input.workflow_context else 0,
                    "sys_actions_count": len(sys_actions),
                    "sys_actions": sys_actions,
                    "system_context_size": len(llm_input.system_context) if llm_input.system_context else 0,
                    "session_control": session_control_result,
                    "function_call_made": function_call_result is not None,  # ✅ 標記是否調用了 MCP function
                    "function_call_result": function_call_result if function_call_result else None
                }
            )
            
            # 發布 LLM 回應生成事件
            self._publish_llm_response_event(output, "WORK", {
                "workflow_context": bool(llm_input.workflow_context),
                "function_call_made": function_call_result is not None,
                "tool_name": function_call_result.get("tool_name") if function_call_result else None
            })
            
            return output
            
        except Exception as e:
            import traceback
            error_log(f"[LLM] WORK 模式處理錯誤: {e}")
            error_log(f"[LLM] 堆疊追蹤:\n{traceback.format_exc()}")
            return LLMOutput(
                text="工作任務處理時發生錯誤，請稍後再試。",
                processing_time=time.time() - start_time,
                tokens_used=0,
                success=False,
                error=str(e),
                confidence=0.0,
                sys_action=None,
                status_updates=None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=None,
                memory_summary=None,
                emotion="neutral",
                mood="neutral",
                metadata={"mode": "WORK", "error_type": "processing_error"}
            )
    
    def _handle_legacy_mode(self, llm_input: "LLMInput", status: Dict[str, Any]) -> "LLMOutput":
        """處理舊的 intent 系統以保持向後兼容"""
        start_time = time.time()
        debug_log(2, f"[LLM] 處理舊版 intent: {getattr(llm_input, 'intent', 'unknown')}")
        
        # 將舊的 intent 轉換為新的模式
        legacy_intent = getattr(llm_input, 'intent', 'chat')
        
        if legacy_intent == "chat":
            # 轉為 CHAT 模式
            llm_input.mode = LLMMode.CHAT
            return self._handle_chat_mode(llm_input, status)
        elif legacy_intent == "command":
            # 轉為 WORK 模式
            llm_input.mode = LLMMode.WORK
            return self._handle_work_mode(llm_input, status)
        else:
            return LLMOutput(
                text=f"抱歉，目前暫不支援 '{legacy_intent}' 類型的處理。",
                processing_time=time.time() - start_time,
                tokens_used=0,
                success=False,
                error=f"不支援的 intent: {legacy_intent}",
                confidence=0.0,
                sys_action=None,
                status_updates=None,
                learning_data=None,
                conversation_entry=None,
                session_state=None,
                memory_observation=None,
                memory_summary=None,
                emotion="neutral",
                mood="neutral",
                metadata={"legacy_intent": legacy_intent}
            )
    
    def _analyze_system_action(self, response_text: str, workflow_context: Optional[Dict[str, Any]]) -> Optional["SystemAction"]:
        """
        [DEPRECATED] 分析回應文本是否需要系統動作
        
        注意：此方法已廢棄。根據 U.E.P 架構設計：
        1. 意圖分析應該在 NLP 模組階段完成
        2. LLM 在 WORK 模式下應該從 Gemini 結構化回應中獲取系統動作
        3. 不應該重複分析文本來判斷系統功能需求
        
        此方法保留僅用於向後兼容，建議移除對此方法的調用。
        """
        debug_log(3, "[LLM] 警告：使用了已廢棄的 _analyze_system_action 方法")
        return None
    
    def _on_status_update(self, status_type: str, old_value: float, new_value: float, reason: str = ""):
        """StatusManager 狀態更新回調"""
        debug_log(2, f"[LLM] 系統狀態更新 - {status_type}: {old_value} -> {new_value} ({reason})")
        
        # 根據狀態變化調整 LLM 行為
        if status_type == "mood" and new_value < 0.3:
            debug_log(1, "[LLM] 偵測到系統心情低落，調整回應風格")
        elif status_type == "boredom" and new_value > 0.8:
            debug_log(1, "[LLM] 偵測到系統無聊，建議主動互動")
    
    def shutdown(self):
        """關閉 LLM 模組並保存狀態"""
        try:
            info_log("[LLM] LLM 模組關閉中...")
            
            # 保存學習資料
            if self.learning_engine:
                self.learning_engine.save_learning_data()
                debug_log(2, "[LLM] 學習資料已保存")
            
            # 清理 Context Cache
            if self.cache_manager:
                cache_stats = self.cache_manager.get_cache_statistics()
                debug_log(2, f"[LLM] Cache 統計: {cache_stats}")
                
            # 取消 StatusManager 回調
            self.status_manager.unregister_update_callback("llm_module")
            
            info_log("[LLM] LLM 模組重構版關閉完成")
            
        except Exception as e:
            error_log(f"[LLM] 關閉時發生錯誤: {e}")
    
    def get_module_status(self) -> Dict[str, Any]:
        """獲取模組狀態資訊"""
        try:
            status = {
                "initialized": self.is_initialized,
                "model_status": "active" if self.model else "inactive",
                "learning_enabled": self.learning_engine.learning_enabled if self.learning_engine else False,
                "cache_enabled": self.cache_manager is not None,
            }
            
            if self.cache_manager:
                status["cache_stats"] = self.cache_manager.get_cache_statistics()
                
            if self.learning_engine and self.learning_engine.learning_enabled:
                status["learning_stats"] = {
                    "total_interactions": len(self.learning_engine.interaction_history),
                    "conversation_styles": len(self.learning_engine.conversation_styles),
                    "usage_patterns": len(self.learning_engine.usage_patterns)
                }
                
            return status
            
        except Exception as e:
            error_log(f"[LLM] 獲取模組狀態失敗: {e}")
            return {"error": str(e)}
    
    def _process_status_updates(self, status_updates) -> None:
        """
        處理來自LLM回應的StatusManager更新
        支援物件格式（來自 schema）和陣列格式（舊版相容）
        """
        try:
            if not status_updates:
                return
            
            # 處理物件格式（來自 Gemini schema）
            if isinstance(status_updates, dict):
                # 使用 StatusManager 的專用 delta 更新方法
                if "mood_delta" in status_updates and status_updates["mood_delta"] is not None:
                    self.status_manager.update_mood(status_updates["mood_delta"], "LLM情緒分析")
                    debug_log(2, f"[LLM] Mood 更新: += {status_updates['mood_delta']}")
                
                if "pride_delta" in status_updates and status_updates["pride_delta"] is not None:
                    self.status_manager.update_pride(status_updates["pride_delta"], "LLM情緒分析")  
                    debug_log(2, f"[LLM] Pride 更新: += {status_updates['pride_delta']}")
                
                if "helpfulness_delta" in status_updates and status_updates["helpfulness_delta"] is not None:
                    self.status_manager.update_helpfulness(status_updates["helpfulness_delta"], "LLM情緒分析")
                    debug_log(2, f"[LLM] Helpfulness 更新: += {status_updates['helpfulness_delta']}")
                
                if "boredom_delta" in status_updates and status_updates["boredom_delta"] is not None:
                    self.status_manager.update_boredom(status_updates["boredom_delta"], "LLM情緒分析")
                    debug_log(2, f"[LLM] Boredom 更新: += {status_updates['boredom_delta']}")
                
                # 統計更新次數
                updates_count = sum(1 for key in ["mood_delta", "pride_delta", "helpfulness_delta", "boredom_delta"] 
                                  if key in status_updates and status_updates[key] is not None)
                if updates_count > 0:
                    debug_log(1, f"[LLM] StatusManager 已應用 {updates_count} 個狀態更新")
            
            # 處理陣列格式（舊版相容）
            elif isinstance(status_updates, list):
                for update in status_updates:
                    status_type = update.get("status_type")
                    value = update.get("value") 
                    reason = update.get("reason", "LLM回應觸發")
                    
                    if status_type and value is not None:
                        # 使用對應的專用更新方法
                        try:
                            if status_type == "mood":
                                self.status_manager.update_mood(value, reason)
                            elif status_type == "pride":
                                self.status_manager.update_pride(value, reason)
                            elif status_type == "helpfulness":
                                self.status_manager.update_helpfulness(value, reason)
                            elif status_type == "boredom":
                                self.status_manager.update_boredom(value, reason)
                            else:
                                debug_log(1, f"[LLM] 未知的狀態類型: {status_type}")
                                continue
                            
                            debug_log(2, f"[LLM] StatusManager更新成功: {status_type}+={value}, 原因: {reason}")
                        except Exception as e:
                            debug_log(1, f"[LLM] StatusManager更新失敗: {status_type}={value}, 錯誤: {e}")
                        
        except Exception as e:
            error_log(f"[LLM] 處理StatusManager更新時出錯: {e}")
    
    def _get_current_system_status(self) -> Dict[str, Any]:
        """獲取當前系統狀態"""
        try:
            # 🔧 添加 None 檢查，防止 'NoneType' object is not subscriptable 錯誤
            status_dict = self.status_manager.get_status_dict()
            if status_dict is None:
                debug_log(1, "[LLM] status_manager.get_status_dict() 返回 None，使用預設值")
                status_dict = {}
            
            personality_modifiers = self.status_manager.get_personality_modifiers()
            if personality_modifiers is None:
                debug_log(1, "[LLM] status_manager.get_personality_modifiers() 返回 None，使用預設值")
                personality_modifiers = {}
            
            return {
                "status_values": status_dict,
                "personality_modifiers": personality_modifiers,
                "system_mode": self.state_manager.get_current_state().value
            }
        except Exception as e:
            error_log(f"[LLM] 獲取系統狀態失敗: {e}")
            return {"error": str(e)}
    
    def _get_current_gs_id(self) -> str:
        """
        獲取當前 General Session ID
        從 working_context 的全局數據中讀取 (由 SystemLoop 設置)
        
        Returns:
            str: 當前 GS ID,如果無法獲取則返回 'unknown'
        """
        try:
            from core.working_context import working_context_manager
            gs_id = working_context_manager.global_context_data.get('current_gs_id', 'unknown')
            return gs_id
        except Exception as e:
            error_log(f"[LLM] 獲取 GS ID 失敗: {e}")
            return 'unknown'
    
    def _get_current_cycle_index(self) -> int:
        """
        獲取當前循環計數
        從 working_context 的全局數據中讀取 (由 Controller 在 GS 創建時設置)
        
        Returns:
            int: 當前 cycle_index,如果無法獲取則返回 0（假設為第一個 cycle）
        """
        try:
            from core.working_context import working_context_manager
            cycle_index = working_context_manager.global_context_data.get('current_cycle_index', 0)
            return cycle_index
        except Exception as e:
            error_log(f"[LLM] 獲取 cycle_index 失敗: {e}")
            return 0
    
    def _get_current_session_info(self, workflow_session_id: Optional[str] = None) -> Dict[str, Any]:
        """獲取當前會話信息 - 優先獲取 CS 或 WS（LLM 作為邏輯中樞的執行會話）
        
        Args:
            workflow_session_id: 可選的指定工作流會話ID，如果提供則優先返回該會話的信息
        """
        try:
            # 從統一會話管理器獲取會話信息
            from core.sessions.session_manager import session_manager
            
            # 如果指定了 workflow_session_id，優先獲取該特定會話
            if workflow_session_id:
                current_ws = session_manager.get_workflow_session(workflow_session_id)
                if current_ws:
                    debug_log(2, f"[LLM] 使用指定的工作流會話: {workflow_session_id}")
                    return {
                        "session_id": workflow_session_id,
                        "session_type": "workflow",
                        "start_time": getattr(current_ws, 'start_time', None),
                        "interaction_count": getattr(current_ws, 'step_count', 0),
                        "last_activity": getattr(current_ws, 'last_activity', None),
                        "active_session_type": "WS"
                    }
            
            # LLM 在 CHAT 狀態時應該獲取當前 CS
            active_cs_ids = session_manager.get_active_chatting_session_ids()
            if active_cs_ids:
                # 在架構下，同一時間只會有一個 CS 執行中
                current_cs_id = active_cs_ids[0]
                current_cs = session_manager.get_chatting_session(current_cs_id)
                
                if current_cs:
                    return {
                        "session_id": current_cs_id,
                        "session_type": "chatting",
                        "start_time": getattr(current_cs, 'start_time', None),
                        "interaction_count": getattr(current_cs, 'turn_count', 0),
                        "last_activity": getattr(current_cs, 'last_activity', None),
                        "active_session_type": "CS"
                    }
            
            # LLM 在 WORK 狀態時應該獲取當前 WS
            active_ws_ids = session_manager.get_active_workflow_session_ids()
            if active_ws_ids:
                # 在架構下，同一時間只會有一個 WS 執行中
                current_ws_id = active_ws_ids[0]
                current_ws = session_manager.get_workflow_session(current_ws_id)
                
                if current_ws:
                    return {
                        "session_id": current_ws_id,
                        "session_type": "workflow",
                        "start_time": getattr(current_ws, 'start_time', None),
                        "interaction_count": getattr(current_ws, 'step_count', 0),
                        "last_activity": getattr(current_ws, 'last_activity', None),
                        "active_session_type": "WS"
                    }
            
            # 如果沒有 CS 或 WS，可能系統處於 IDLE 狀態或其他狀態
            return {
                "session_id": "no_active_session", 
                "session_type": "idle",
                "start_time": None,
                "interaction_count": 0,
                "last_activity": None,
                "active_session_type": "NONE"
            }
            
        except Exception as e:
            error_log(f"[LLM] 獲取會話信息失敗: {e}")
            return {
                "session_id": "error", 
                "session_type": "error",
                "active_session_type": "ERROR"
            }
    
    def _get_identity_context(self) -> Dict[str, Any]:
        """從Working Context獲取Identity信息，對通用身份採用預設處理"""
        try:
            # 使用正確的方法獲取當前身份
            identity_data = working_context_manager.get_current_identity()
            
            if not identity_data:
                debug_log(2, "[LLM] 沒有設置身份信息，使用預設值")
                return {
                    "identity": {
                        "name": "default_user",
                        "traits": {}
                    },
                    "preferences": {}
                }
            
            # 檢查是否為通用身份
            identity_status = identity_data.get("status", "unknown")
            if identity_status == "temporary":
                debug_log(2, "[LLM] 檢測到通用身份，使用基本設置")
                return {
                    "identity": {
                        "name": "用戶",
                        "traits": {},
                        "status": "temporary"
                    },
                    "preferences": {}  # 通用身份不使用特殊偏好
                }
            
            # 正式身份使用完整資料
            return {
                "identity": {
                    "name": identity_data.get("user_identity", identity_data.get("identity_id", "default_user")),
                    "traits": identity_data.get("traits", {}),
                    "status": identity_status
                },
                "preferences": identity_data.get("conversation_preferences", {})
            }
        except Exception as e:
            error_log(f"[LLM] 獲取Identity上下文失敗: {e}")
            return {}
    
    def _enrich_with_system_context(self, 
                                  llm_input: LLMInput,
                                  current_state: Any,
                                  status: Dict[str, Any],
                                  session_info: Dict[str, Any],
                                  identity_context: Dict[str, Any]) -> LLMInput:
        """補充系統上下文到LLM輸入 - 支援新 Router 整合"""
        try:
            # 創建新的enriched input
            enriched_data = llm_input.dict()
            
            # 補充系統上下文
            if not enriched_data.get("system_context"):
                enriched_data["system_context"] = {}
            
            enriched_data["system_context"].update({
                "current_state": current_state.value if hasattr(current_state, 'value') else str(current_state),
                "status_manager": status,
                "session_info": session_info
            })
            
            # 補充身份上下文 (不覆蓋Router提供的)
            if not enriched_data.get("identity_context"):
                enriched_data["identity_context"] = {}
            # 只在沒有Router數據時補充本地身份上下文
            if not llm_input.source_layer:
                enriched_data["identity_context"].update(identity_context)
            
            # 處理新Router提供的協作上下文
            if llm_input.collaboration_context:
                debug_log(2, f"[LLM] 處理協作上下文: {list(llm_input.collaboration_context.keys())}")
                
                # 設置記憶檢索標誌
                if "mem" in llm_input.collaboration_context:
                    enriched_data["enable_memory_retrieval"] = True
                    mem_config = llm_input.collaboration_context["mem"]
                    if mem_config.get("retrieve_relevant"):
                        enriched_data["memory_context"] = "協作模式：需要檢索相關記憶"
                
                # 設置系統動作標誌
                if "sys" in llm_input.collaboration_context:
                    enriched_data["enable_system_actions"] = True
                    sys_config = llm_input.collaboration_context["sys"]
                    if sys_config.get("allow_execution"):
                        enriched_data["workflow_context"] = {"execution_allowed": True}
            
            # 處理Router的會話上下文
            if llm_input.session_context:
                enriched_data["session_id"] = llm_input.session_context.get("session_id")
                enriched_data["system_context"]["router_session"] = llm_input.session_context
            
            # 處理NLP實體信息
            if llm_input.entities:
                enriched_data["system_context"]["nlp_entities"] = llm_input.entities
            
            return LLMInput(**enriched_data)
            
        except Exception as e:
            error_log(f"[LLM] 補充系統上下文失敗: {e}")
            return llm_input
    
    def _process_chat_memory_operations(self, 
                                      llm_input: LLMInput,
                                      response_data: Dict[str, Any], 
                                      response_text: str) -> List[Dict[str, Any]]:
        """處理CHAT模式的MEM模組操作"""
        memory_operations = []
        
        try:
            # 1. 從Gemini回應中提取記憶操作
            if "memory_operations" in response_data:
                memory_operations.extend(response_data["memory_operations"])
                debug_log(2, f"[LLM] 從回應提取記憶操作: {len(memory_operations)}個")
            
            # 2. 自動記憶儲存邏輯
            if self._should_store_conversation(llm_input, response_text):
                store_operation = {
                    "operation": "store",
                    "content": {
                        "user_input": llm_input.text,
                        "assistant_response": response_text,
                        "timestamp": time.time(),
                        "conversation_context": llm_input.memory_context,
                        "identity_context": llm_input.identity_context
                    },
                    "metadata": {
                        "interaction_type": "chat",
                        "memory_type": "conversation",
                        "auto_generated": True,
                        "ttl_seconds": 60 * 60 * 24 * 7,     # 一週
                        "erasable": True
                    }
                }
                memory_operations.append(store_operation)
                debug_log(2, "[LLM] 自動添加對話記憶儲存")
            
            # 3. 發送記憶操作到MEM模組 (通過Router)
            if memory_operations:
                self._send_to_mem_module(memory_operations)
            
            return memory_operations
            
        except Exception as e:
            error_log(f"[LLM] 處理CHAT記憶操作失敗: {e}")
            return []
    
    def _should_store_conversation(self, llm_input: LLMInput, response_text: str) -> bool:
        """判斷是否應該儲存對話"""
        try:
            # 檢查對話長度
            if len(llm_input.text) < 10 or len(response_text) < 10:
                return False
            
            sensitive_patterns = [r"\b\d{10}\b", r"@.+\.", r"\b[A-Z]\d{9}\b"]  # 可擴充
            if any(re.search(p, llm_input.text) for p in sensitive_patterns):
                return False  # 含敏感資訊不自動存
            
            # 檢查是否為重要對話
            important_keywords = ["remember", "record", "important", "remind", "save"]
            if any(keyword in llm_input.text for keyword in important_keywords):
                return True
            
            # 檢查是否包含個人信息或偏好
            personal_keywords = ["like", "hate", "prefer", "would have", "name", "birthday"]
            if any(keyword in llm_input.text for keyword in personal_keywords):
                return True
            
            # 預設儲存較長的有意義對話
            return len(llm_input.text) > 50
            
        except Exception as e:
            error_log(f"[LLM] 判斷對話儲存失敗: {e}")
            return False
    
    def _send_to_mem_module(self, memory_operations: List[Dict[str, Any]]) -> None:
        """向MEM模組發送記憶操作 - 通過狀態感知接口"""
        try:
            debug_log(1, f"[LLM] 準備發送 {len(memory_operations)} 個記憶操作到MEM模組")
            
            # 檢查 CHAT-MEM 協作管道是否啟用
            if not self.module_interface.is_channel_active(CollaborationChannel.CHAT_MEM):
                debug_log(2, "[LLM] 記憶操作跳過: MEM模組只在CHAT狀態下運行")
                return
            
            # 逐個處理記憶操作
            for i, operation in enumerate(memory_operations):
                operation_type = operation.get('operation', 'unknown')
                debug_log(3, f"[LLM] 記憶操作 #{i+1}: {operation_type}")
                
                try:
                    # 通過狀態感知接口發送對話儲存請求
                    conversation_data = {
                        "operation_type": operation_type,
                        "content": operation.get('content', {}),
                        "metadata": operation.get('metadata', {}),
                        "source_module": "llm_module"
                    }
                    
                    result = self.module_interface.get_chat_mem_data(
                        "conversation_storage",
                        conversation_data=conversation_data
                    )
                    
                    if result:
                        debug_log(2, f"[LLM] 記憶操作 #{i+1} 成功: {operation_type}")
                    else:
                        debug_log(2, f"[LLM] 記憶操作 #{i+1} 未執行: {operation_type}")
                        
                except Exception as op_error:
                    error_log(f"[LLM] 處理記憶操作 #{i+1} 時出錯: {op_error}")
            
        except Exception as e:
            error_log(f"[LLM] 發送記憶操作失敗: {e}")
    
    def _retrieve_relevant_memory(self, user_input: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """從MEM模組檢索相關記憶 - 通過狀態感知接口"""
        try:
            debug_log(2, f"[LLM] 檢索相關記憶: {user_input[:50]}...")
            
            # 檢查 CHAT-MEM 協作管道是否啟用
            if not self.module_interface.is_channel_active(CollaborationChannel.CHAT_MEM):
                debug_log(2, "[LLM] 記憶檢索失敗: MEM模組只在CHAT狀態下運行")
                return []
            
            # 通過狀態感知接口檢索記憶
            memories = self.module_interface.get_chat_mem_data(
                "memory_retrieval",
                query=user_input,
                max_results=max_results,
                memory_types=["conversation", "user_info", "context"]
            )
            
            if memories:
                debug_log(1, f"[LLM] 檢索到 {len(memories)} 條相關記憶")
                return memories
            else:
                debug_log(2, "[LLM] 未檢索到相關記憶")
                return []
            
        except Exception as e:
            error_log(f"[LLM] 記憶檢索失敗: {e}")
            return []
    
    def _format_memories_for_context(self, memories: List[Any]) -> str:
        """將檢索到的記憶格式化為對話上下文
        
        Args:
            memories: MemorySearchResult 對象列表
        """
        try:
            if not memories:
                return ""
            
            context_parts = ["Relevant Memory Context:"]
            
            for i, memory_result in enumerate(memories[:5], 1):  # 限制最多5條記憶
                # 從 MemorySearchResult 中取得 memory_entry
                memory_entry = memory_result.memory_entry
                
                memory_type = memory_entry.memory_type.value  # MemoryType enum
                content = memory_entry.content
                timestamp = memory_entry.created_at.strftime("%Y-%m-%d %H:%M") if memory_entry.created_at else ""
                
                # 格式化記憶內容
                if memory_type == "interaction_history":
                    # 對話記憶格式
                    context_parts.append(f"{i}. [Conversation] {content}")
                elif memory_type == "profile":
                    # 用戶信息記憶格式
                    context_parts.append(f"{i}. [User Info] {content}")
                elif memory_type == "snapshot":
                    # 快照記憶格式
                    context_parts.append(f"{i}. [Recent Context] {content}")
                else:
                    # 一般記憶格式
                    context_parts.append(f"{i}. [{memory_type.replace('_', ' ').title()}] {content}")
            
            formatted_context = "\n".join(context_parts)
            debug_log(3, f"[LLM] 格式化記憶上下文: {len(formatted_context)} 字符")
            
            return formatted_context
            
        except Exception as e:
            error_log(f"[LLM] 格式化記憶上下文失敗: {e}")
            import traceback
            debug_log(1, traceback.format_exc())
            return ""
    
    def _process_work_system_actions(self, 
                                   llm_input: LLMInput,
                                   response_data: Dict[str, Any], 
                                   response_text: str) -> List[Dict[str, Any]]:
        """處理WORK模式的SYS模組操作 - LLM作為決策機"""
        sys_actions = []
        
        try:
            # 從Gemini回應中提取系統動作決策
            if "sys_action" in response_data:
                sys_action = response_data["sys_action"]
                if isinstance(sys_action, dict):
                    sys_actions.append(sys_action)
                    action = sys_action.get('action', 'unknown')
                    target = sys_action.get('target', 'unknown')
                    debug_log(1, f"[LLM] 決策: {action} -> {target}")
                    
                    # 🔧 處理 MCP 工具調用（工作流控制）
                    if action == 'execute_function' and target in ['approve_step', 'cancel_workflow', 'modify_step']:
                        debug_log(2, f"[LLM] 檢測到 MCP 工具調用: {target}")
                        
                        # 🔧 從工作流上下文獲取 workflow_session_id（注意欄位名）
                        session_id = None
                        if llm_input.workflow_context:
                            session_id = llm_input.workflow_context.get('workflow_session_id')  # 正確的欄位名
                        
                        if not session_id:
                            error_log(f"[LLM] 無法執行 {target}: 缺少 workflow_session_id")
                            debug_log(1, f"[LLM] workflow_context keys: {list(llm_input.workflow_context.keys()) if llm_input.workflow_context else 'None'}")
                        else:
                            # 執行 MCP 工具
                            if target == 'approve_step':
                                debug_log(2, f"[LLM] 執行 approve_step: {session_id}")
                                self._approve_workflow_step(session_id, None)
                            elif target == 'cancel_workflow':
                                reason = sys_action.get('parameters', {}).get('reason', 'User cancelled')
                                self._cancel_workflow(session_id, reason)
                            elif target == 'modify_step':
                                modifications = sys_action.get('parameters', {})
                                self._modify_workflow_step(session_id, modifications)
            
            # 發送其他系統動作到 SYS 模組
            non_mcp_actions = [a for a in sys_actions if not (a.get('action') == 'execute_function' and a.get('target') in ['approve_step', 'cancel_workflow', 'modify_step'])]
            if non_mcp_actions:
                self._send_to_sys_module(non_mcp_actions, llm_input.workflow_context)
            
            return sys_actions
            
        except Exception as e:
            error_log(f"[LLM] 處理WORK系統動作失敗: {e}")
            return []
    
    def _get_available_sys_functions(self) -> Optional[List[Dict[str, Any]]]:
        """從 SYS 模組獲取可用功能清單"""
        try:
            debug_log(2, "[LLM] 嘗試從 SYS 模組獲取功能清單")
            
            # 檢查 WORK-SYS 協作管道是否啟用
            if not self.module_interface.is_channel_active(CollaborationChannel.WORK_SYS):
                debug_log(2, "[LLM] SYS 協作管道未啟用，返回空功能清單")
                return None
            
            # 通過狀態感知接口獲取功能註冊表
            function_registry = self.module_interface.get_work_sys_data(
                "function_registry",
                request_type="get_all_functions"
            )
            
            if function_registry and isinstance(function_registry, list):
                debug_log(1, f"[LLM] 成功獲取 {len(function_registry)} 個可用功能")
                return function_registry
            elif function_registry and isinstance(function_registry, dict):
                # 處理字典格式的功能註冊表
                functions = []
                for category, funcs in function_registry.items():
                    if isinstance(funcs, list):
                        functions.extend(funcs)
                debug_log(1, f"[LLM] 成功獲取 {len(functions)} 個可用功能（來自字典格式）")
                return functions
            else:
                debug_log(2, "[LLM] SYS 模組功能註冊表為空或格式錯誤")
                return None
                
        except Exception as e:
            error_log(f"[LLM] 獲取 SYS 功能清單失敗: {e}")
            return None
    
    def _format_functions_for_prompt(self, functions_list: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        """將功能列表格式化為提示詞字符串"""
        try:
            if not functions_list:
                debug_log(2, "[LLM] 沒有可用的系統功能")
                return "目前沒有可用的系統功能。我只能提供一般的回應和建議，無法執行具體的系統操作。"
            
            formatted_functions = []
            for i, func in enumerate(functions_list, 1):
                func_name = func.get("name", "unknown")
                func_desc = func.get("description", "無描述")
                func_category = func.get("category", "general")
                
                # 格式化單個功能
                func_str = f"{i}. {func_name} ({func_category}): {func_desc}"
                
                # 添加參數信息（如果有）
                parameters = func.get("parameters", {})
                if parameters:
                    param_strs = []
                    for param_name, param_info in parameters.items():
                        param_type = param_info.get("type", "unknown")
                        param_desc = param_info.get("description", "")
                        param_strs.append(f"   - {param_name} ({param_type}): {param_desc}")
                    if param_strs:
                        func_str += "\n" + "\n".join(param_strs)
                
                formatted_functions.append(func_str)
            
            result = "\n".join(formatted_functions)
            debug_log(2, f"[LLM] 格式化了 {len(functions_list)} 個系統功能")
            return result
            
        except Exception as e:
            error_log(f"[LLM] 格式化功能列表失敗: {e}")
            return "功能列表格式化失敗，無法提供系統功能信息。"
    
    def _process_session_control(self, response_data: Dict[str, Any], mode: str, llm_input: "LLMInput") -> Optional[Dict[str, Any]]:
        """處理會話控制建議 - LLM 決定會話是否應該結束"""
        try:
            session_control = response_data.get("session_control")
            if not session_control:
                return None
            
            should_end = session_control.get("should_end_session", False)
            end_reason = session_control.get("end_reason", "unknown")
            confidence = session_control.get("confidence", 0.5)
            
            if should_end and confidence >= 0.7:  # 只在高信心度時結束會話
                debug_log(1, f"[LLM] 會話結束建議: {mode} 模式 - 原因: {end_reason} (信心度: {confidence:.2f})")
                
                # 通知 session manager 結束當前會話
                self._request_session_end(mode, end_reason, confidence, llm_input)
                
                return {
                    "session_ended": True,
                    "reason": end_reason,
                    "confidence": confidence
                }
            elif should_end:
                debug_log(2, f"[LLM] 會話結束建議信心度不足: {confidence:.2f} < 0.7")
                
            return None
            
        except Exception as e:
            error_log(f"[LLM] 處理會話控制失敗: {e}")
            return None
    
    def _request_session_end(self, mode: str, reason: str, confidence: float, llm_input: "LLMInput") -> None:
        """標記會話待結束 - 由 ModuleCoordinator 的雙條件機制處理實際結束"""
        try:
            # ✅ 架構正確性：LLM 通過 session_control 建議結束
            # ModuleCoordinator 檢測到後標記 pending_end
            # Controller 會在 CYCLE_COMPLETED 時檢查並執行結束
            # 這確保：
            # 1. LLM 回應能完整生成並輸出
            # 2. TTS 能完成語音合成
            # 3. 所有去重鍵能正確清理
            # 4. 會話在循環邊界乾淨地結束
            
            # session_control 已在回應中設置，ModuleCoordinator 會檢測並標記
            debug_log(1, f"[LLM] 📋 會話結束請求已通過 session_control 發送: {reason} (mode={mode}, confidence={confidence:.2f})")
            
            if mode == "CHAT":
                debug_log(1, f"[LLM] 🔚 標記 CS 待結束 (原因: {reason}, 信心度: {confidence:.2f})")
                debug_log(2, f"[LLM] session_control 已設置，等待循環完成後由 ModuleCoordinator 處理")
                        
            elif mode == "WORK":
                debug_log(1, f"[LLM] 🔚 標記 WS 待結束 (原因: {reason}, 信心度: {confidence:.2f})")
                debug_log(2, f"[LLM] session_control 已設置，等待循環完成後由 ModuleCoordinator 處理")
            
        except Exception as e:
            error_log(f"[LLM] 標記會話結束失敗: {e}")
    
    def _send_to_sys_module(self, sys_actions: List[Dict[str, Any]], workflow_context: Optional[Dict[str, Any]]) -> None:
        """向SYS模組發送系統動作 - 通過狀態感知接口"""
        try:
            debug_log(1, f"[LLM] 準備發送 {len(sys_actions)} 個系統動作到SYS模組")
            
            # 檢查 WORK-SYS 協作管道是否啟用
            if not self.module_interface.is_channel_active(CollaborationChannel.WORK_SYS):
                debug_log(2, "[LLM] 系統動作跳過: SYS模組只在WORK狀態下運行")
                return
            
            for i, action_dict in enumerate(sys_actions):
                action = action_dict.get('action', 'unknown')
                target = action_dict.get('target', 'unknown')
                debug_log(3, f"[LLM] 系統動作 #{i+1}: {action} -> {target}")
                
                try:
                    # 通過狀態感知接口獲取工作流狀態並執行功能
                    workflow_status = self.module_interface.get_work_sys_data(
                        "workflow_status",
                        workflow_id=workflow_context.get('workflow_id') if workflow_context else 'default'
                    )
                    
                    if workflow_status:
                        debug_log(3, f"[LLM] 工作流狀態: {workflow_status.get('current_step', 'unknown')}")
                    
                    # 獲取可用功能並嘗試執行
                    available_functions = self.module_interface.get_work_sys_data(
                        "function_registry",
                        category=action
                    )
                    
                    if available_functions and action in available_functions:
                        debug_log(2, f"[LLM] 系統動作 #{i+1} 已處理: {action}")
                    else:
                        debug_log(2, f"[LLM] 系統動作 #{i+1} 功能不可用: {action}")
                        
                except Exception as action_error:
                    error_log(f"[LLM] 處理系統動作 #{i+1} 時出錯: {action_error}")
            
        except Exception as e:
            error_log(f"[LLM] 發送系統動作失敗: {e}")
    
    def _get_system_caches(self, mode: str) -> Dict[str, str]:
        """獲取系統快取ID"""
        cached_content_ids = {}
        
        try:
            if mode == "chat":
                # CHAT模式：persona + style_policy + session_anchor
                persona_cache = self.cache_manager.get_or_create_cache(
                    name="uep:persona:v1",
                    cache_type=CacheType.PERSONA,
                    content_builder=lambda: self._build_persona_cache_content()
                )
                if persona_cache:
                    cached_content_ids["persona"] = persona_cache
                
                style_cache = self.cache_manager.get_or_create_cache(
                    name="uep:style_policy:v1", 
                    cache_type=CacheType.STYLE_POLICY,
                    content_builder=lambda: self._build_style_policy_cache_content()
                )
                if style_cache:
                    cached_content_ids["style_policy"] = style_cache
                
            elif mode == "work":
                # WORK模式：functions + task_spec 
                functions_cache = self.cache_manager.get_or_create_cache(
                    name="uep:functions:v1",
                    cache_type=CacheType.FUNCTIONS,
                    content_builder=lambda: self._build_functions_cache_content()
                )
                if functions_cache:
                    cached_content_ids["functions"] = functions_cache
            
            debug_log(2, f"[LLM] 系統快取準備完成 ({mode}): {len(cached_content_ids)}個")
            return cached_content_ids
            
        except Exception as e:
            error_log(f"[LLM] 系統快取獲取失敗: {e}")
            return {}
    
    def _build_persona_cache_content(self) -> str:
        """構建persona快取內容"""
        return f"""
你是U.E.P (Unified Experience Partner)，一個智能的統一體驗夥伴。

核心特質：
- 友善、專業、樂於學習和幫助
- 具有記憶和學習能力，能夠記住用戶偏好
- 會根據系統狀態調整回應風格和行為

當前系統狀態：System operational with mood tracking enabled

回應語言：Traditional Chinese (zh-TW)
回應格式：根據模式要求的JSON結構
"""
    
    def _build_style_policy_cache_content(self) -> str:
        """構建風格策略快取內容"""
        return """
回應風格調整規則：
1. Mood值影響語氣：
   - 高(>0.7): 活潑、熱情、積極
   - 中(0.3-0.7): 平穩、友善、專業
   - 低(<0.3): 沉穩、謹慎、溫和

2. Pride值影響自信度：
   - 高(>0.7): 積極主動、自信表達
   - 中(0.3-0.7): 平衡謙遜、適度自信
   - 低(<0.3): 謙遜低調、保守表達

3. Boredom值影響主動性：
   - 高(>0.7): 主動提出建議、探索新話題
   - 中(0.3-0.7): 回應導向、適度延伸
   - 低(<0.3): 被動回應、簡潔回答

JSON回應安全規範：
- 所有字符串值必須正確轉義
- 避免使用可能破壞JSON結構的特殊字符
- 確保數值在有效範圍內
"""
    
    def _build_functions_cache_content(self) -> str:
        """構建functions快取內容"""
        return """
U.E.P 系統可用功能規格：

檔案操作功能：
- file_open: 開啟檔案 (參數: file_path)
- file_create: 建立檔案 (參數: file_path, content)
- file_delete: 刪除檔案 (參數: file_path)
- file_copy: 複製檔案 (參數: source_path, dest_path)

系統操作功能：
- program_launch: 啟動程式 (參數: program_name, arguments)
- command_execute: 執行指令 (參數: command, working_directory)
- file_search: 搜尋檔案 (參數: search_pattern, search_path)
- info_query: 查詢系統資訊 (參數: query_type, parameters)

記憶管理功能：
- memory_store: 儲存記憶 (參數: content, memory_type, metadata)
- memory_retrieve: 檢索記憶 (參數: query, max_results, similarity_threshold)
"""

    def _notify_processing_layer_completion(self, result: Dict[str, Any]):
        """
        ✅ 事件驅動版本：發布處理層完成事件
        LLM 作為主要邏輯模組，處理完成後觸發輸出層
        
        事件數據包含 session_id 和 cycle_index 用於 flow-based 去重
        這些資訊應該從上游 INPUT_LAYER_COMPLETE 事件傳遞過來
        """
        try:
            response_text = result.get("text", "")
            if not response_text:
                debug_log(2, "[LLM] 無回應文字，跳過處理層完成通知")
                return
            
            info_log(f"[LLM] 處理層完成，發布事件: 回應='{response_text[:50]}...'")
            
            # 從 working_context 獲取 session_id 和 cycle_index
            # 🔧 使用處理開始時保存的 session_id 和 cycle_index
            # 而不是動態讀取，避免 SystemLoop 已遞增 cycle_index 導致的不一致
            session_id = getattr(self, '_current_processing_session_id', self._get_current_gs_id())
            cycle_index = getattr(self, '_current_processing_cycle_index', self._get_current_cycle_index())
            
            debug_log(3, f"[LLM] 發布事件使用: session={session_id}, cycle={cycle_index}")
            
            # 準備處理層完成數據
            processing_layer_completion_data = {
                # Flow-based 去重所需欄位
                "session_id": session_id,
                "cycle_index": cycle_index,
                "layer": "PROCESSING",
                
                # 原有數據
                "response": response_text,
                "source_module": "llm",
                "llm_output": result,
                "timestamp": time.time(),
                "completion_type": "processing_layer_finished",
                "mode": result.get("mode", "unknown"),
                "success": result.get("success", False)
            }
            
            # ✅ 使用事件總線發布事件
            from core.event_bus import event_bus, SystemEvent
            event_bus.publish(
                event_type=SystemEvent.PROCESSING_LAYER_COMPLETE,
                data=processing_layer_completion_data,
                source="llm"
            )
            
            debug_log(2, f"[LLM] 處理層完成事件已發布 (session={session_id}, cycle={cycle_index})")
            
        except Exception as e:
            error_log(f"[LLM] 發布處理層完成事件失敗: {e}")
    
    def _process_workflow_completion(self, session_id: str, workflow_type: str, 
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
            debug_log(2, f"[LLM] 開始處理工作流完成: {workflow_type} ({session_id})")
            debug_log(2, f"[LLM] review_data keys: {list(review_data.keys()) if review_data else 'None'}")
            
            # 構建總結 prompt
            result_message = step_result.get('message', 'Task completed successfully')
            
            prompt = (
                f"The '{workflow_type}' workflow has completed successfully.\n\n"
                f"Result: {result_message}\n"
            )
            
            # ✅ 優先處理 full_content（文件讀取結果）
            if review_data:
                if 'full_content' in review_data:
                    debug_log(2, f"[LLM] 發現 full_content，添加到 prompt")
                    file_name = review_data.get('file_name', 'unknown')
                    content = review_data.get('full_content', '')
                    content_length = review_data.get('content_length', len(content))
                    
                    # 判斷內容是否應該完整唸出（英文且不超過 500 字符）
                    should_read_full = content_length <= 500 and content.strip()
                    
                    if should_read_full:
                        prompt += (
                            f"\nFile Read Results:\n"
                            f"- File: {file_name}\n"
                            f"- Content ({content_length} characters):\n{content}\n\n"
                            f"Generate a natural response that:\n"
                            f"1. Briefly confirms you've read the file '{file_name}'\n"
                            f"2. READ OUT THE ACTUAL FILE CONTENT directly (don't just summarize - say what's written in the file)\n"
                            f"3. Keep your introduction brief, then read the content naturally\n"
                            f"IMPORTANT: Actually read the file content aloud, not just describe it. Respond in English only."
                        )
                    else:
                        # 內容太長，明確告知用戶
                        prompt += (
                            f"\nFile Read Results:\n"
                            f"- File: {file_name}\n"
                            f"- Content Length: {content_length} characters\n"
                            f"- Content Preview:\n{content[:200]}...\n\n"
                            f"Generate a natural response that:\n"
                            f"1. Confirms the file has been read successfully\n"
                            f"2. EXPLICITLY state that the file is too long ({content_length} characters) to read out completely\n"
                            f"3. Offer to help in other ways (e.g., summarize, search for specific content, answer questions about it)\n"
                            f"4. Keep it conversational (2-3 sentences)\n"
                            f"IMPORTANT: Respond in English only."
                        )
                else:
                    # 通用數據：優先從 step_result 獲取實際結果數據
                    result_data = step_result.get('data', {})
                    if not result_data:
                        result_data = review_data.get('result_data', review_data)
                    
                    if result_data:
                        debug_log(2, f"[LLM] 添加結果數據到 prompt，鍵: {list(result_data.keys())}")
                        # 對於新聞摘要，特別處理 news_list
                        if 'news_list' in result_data:
                            news_list = result_data.get('news_list', [])
                            source = result_data.get('source', 'unknown')
                            count = result_data.get('count', len(news_list))
                            prompt += (
                                f"\nNews Summary Results:\n"
                                f"- Source: {source}\n"
                                f"- Count: {count}\n"
                                f"- Headlines:\n"
                            )
                            for i, title in enumerate(news_list[:10], 1):  # 最多顯示 10 條
                                prompt += f"  {i}. {title}\n"
                            prompt += (
                                f"\nGenerate a natural response that:\n"
                                f"1. Confirms the news summary is ready\n"
                                f"2. Mention how many news items were found\n"
                                f"3. Briefly mention 1-2 interesting headlines\n"
                                f"4. Keep it conversational (2-3 sentences)\n"
                                f"IMPORTANT: Respond in English only."
                            )
                        # 對於待辦事項查詢，特別處理 tasks
                        elif 'tasks' in result_data:
                            tasks = result_data.get('tasks', [])
                            task_count = len(tasks)
                            
                            # 如果任務超過 3 件，只顯示前 3 件並提供摘要統計
                            if task_count > 3:
                                prompt += (
                                    f"\nTodo Tasks List ({task_count} tasks total - showing first 3):\n\n"
                                )
                                
                                # 只顯示前 3 件
                                for i, task in enumerate(tasks[:3], 1):
                                    task_name = task.get('task_name', 'Unnamed task')
                                    priority = task.get('priority', 'medium')
                                    status = task.get('status', 'pending')
                                    deadline = task.get('deadline', '')
                                    
                                    prompt += f"{i}. {task_name} (Priority: {priority}, Status: {status})\n"
                                    if deadline:
                                        prompt += f"   Deadline: {deadline}\n"
                                    prompt += "\n"
                                
                                # 提供摘要統計
                                priority_counts = {}
                                status_counts = {}
                                for task in tasks:
                                    priority = task.get('priority', 'medium')
                                    status = task.get('status', 'pending')
                                    priority_counts[priority] = priority_counts.get(priority, 0) + 1
                                    status_counts[status] = status_counts.get(status, 0) + 1
                                
                                prompt += f"Summary Statistics:\n"
                                prompt += f"- Total: {task_count} tasks\n"
                                prompt += f"- By Priority: {', '.join(f'{k}: {v}' for k, v in priority_counts.items())}\n"
                                prompt += f"- By Status: {', '.join(f'{k}: {v}' for k, v in status_counts.items())}\n\n"
                                
                                prompt += (
                                    f"Generate a natural response that:\n"
                                    f"1. Confirms you found {task_count} todo tasks\n"
                                    f"2. LIST the first 3 tasks briefly (name and priority)\n"
                                    f"3. Provide a SUMMARY of all tasks (e.g., 'In total, you have 5 high priority tasks, 3 medium priority tasks')\n"
                                    f"4. Mention any urgent or overdue items if present\n"
                                    f"5. Keep it concise (2-3 sentences max)\n"
                                    f"6. DO NOT use emojis or special characters (for TTS compatibility)\n"
                                    f"IMPORTANT: Don't read all tasks - summarize! Respond in English only."
                                )
                            else:
                                # 3 件或更少，全部列出
                                prompt += (
                                    f"\nTodo Tasks List ({task_count} tasks):\n\n"
                                )
                                for i, task in enumerate(tasks, 1):
                                    task_name = task.get('task_name', 'Unnamed task')
                                    priority = task.get('priority', 'medium')
                                    status = task.get('status', 'pending')
                                    description = task.get('task_description', '')
                                    deadline = task.get('deadline', '')
                                    
                                    prompt += f"{i}. {task_name} (Priority: {priority}, Status: {status})\n"
                                    if description:
                                        prompt += f"   Description: {description}\n"
                                    if deadline:
                                        prompt += f"   Deadline: {deadline}\n"
                                    prompt += "\n"
                                
                                prompt += (
                                    f"Generate a natural response that:\n"
                                    f"1. Confirms you found {task_count} todo task(s)\n"
                                    f"2. LIST OUT ALL the tasks clearly with their names, priorities, and status\n"
                                    f"3. Mention any high-priority or overdue tasks if present\n"
                                    f"4. Keep it organized and easy to understand\n"
                                    f"5. DO NOT use emojis or special characters (for TTS compatibility)\n"
                                    f"IMPORTANT: Actually list all the tasks, don't just say they exist. Respond in English only."
                                )
                        # 對於行事曆查詢，特別處理 events
                        elif 'events' in result_data:
                            events = result_data.get('events', [])
                            event_count = len(events)
                            
                            # 如果事件超過 3 件，只顯示前 3 件並提供摘要
                            if event_count > 3:
                                prompt += (
                                    f"\nCalendar Events ({event_count} events total - showing first 3):\n\n"
                                )
                                
                                # 只顯示前 3 件
                                for i, event in enumerate(events[:3], 1):
                                    summary = event.get('summary', 'Untitled event')
                                    start_time = event.get('start_time', '')
                                    location = event.get('location', '')
                                    
                                    prompt += f"{i}. {summary}\n"
                                    if start_time:
                                        prompt += f"   Start: {start_time}\n"
                                    if location:
                                        prompt += f"   Location: {location}\n"
                                    prompt += "\n"
                                
                                # 計算今天/本週的事件數量
                                from datetime import datetime, timedelta
                                now = datetime.now()
                                today_end = now.replace(hour=23, minute=59, second=59)
                                week_end = now + timedelta(days=7)
                                
                                today_count = 0
                                week_count = 0
                                
                                for event in events:
                                    start_str = event.get('start_time', '')
                                    if start_str:
                                        try:
                                            start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                                            if start_dt <= today_end:
                                                today_count += 1
                                            elif start_dt <= week_end:
                                                week_count += 1
                                        except:
                                            pass
                                
                                prompt += f"Summary:\n"
                                prompt += f"- Total: {event_count} events\n"
                                if today_count > 0:
                                    prompt += f"- Today: {today_count} events\n"
                                if week_count > 0:
                                    prompt += f"- This week: {week_count} events\n"
                                prompt += "\n"
                                
                                prompt += (
                                    f"Generate a natural response that:\n"
                                    f"1. Confirms you found {event_count} calendar events\n"
                                    f"2. LIST the first 3 events briefly (title and time)\n"
                                    f"3. Provide a SUMMARY (e.g., 'You have 2 events today and 3 more this week')\n"
                                    f"4. Mention any upcoming or important events\n"
                                    f"5. Keep it concise (2-3 sentences max)\n"
                                    f"6. DO NOT use emojis or special characters (for TTS compatibility)\n"
                                    f"IMPORTANT: Don't read all events - summarize! Respond in English only."
                                )
                            else:
                                # 3 件或更少，全部列出
                                prompt += (
                                    f"\nCalendar Events ({event_count} events):\n\n"
                                )
                                for i, event in enumerate(events, 1):
                                    summary = event.get('summary', 'Untitled event')
                                    start_time = event.get('start_time', '')
                                    end_time = event.get('end_time', '')
                                    location = event.get('location', '')
                                    description = event.get('description', '')
                                    
                                    prompt += f"{i}. {summary}\n"
                                    if start_time:
                                        prompt += f"   Start: {start_time}\n"
                                    if end_time:
                                        prompt += f"   End: {end_time}\n"
                                    if location:
                                        prompt += f"   Location: {location}\n"
                                    if description:
                                        prompt += f"   Description: {description}\n"
                                    prompt += "\n"
                                
                                prompt += (
                                    f"Generate a natural response that:\n"
                                    f"1. Confirms you found {event_count} calendar event(s)\n"
                                    f"2. LIST OUT ALL the events with their times and locations\n"
                                    f"3. Mention any upcoming or important events\n"
                                    f"4. Keep it organized and easy to understand\n"
                                    f"5. DO NOT use emojis or special characters (for TTS compatibility)\n"
                                    f"IMPORTANT: Actually list all the events, don't just say they exist. Respond in English only."
                                )
                        else:
                            prompt += f"Data: {str(result_data)[:500]}\n\n"
                            prompt += (
                                f"Generate a natural, friendly response that:\n"
                                f"1. Confirms the task is complete\n"
                                f"2. Summarizes the key results\n"
                                f"3. Keep it conversational (2-3 sentences)\n"
                                f"IMPORTANT: Respond in English only."
                            )
                    else:
                        prompt += (
                            f"Generate a natural, friendly response that:\n"
                            f"1. Confirms the task is complete\n"
                            f"2. Summarizes the key results\n"
                            f"3. Keep it conversational (2-3 sentences)\n"
                            f"IMPORTANT: Respond in English only."
                        )
            else:
                prompt += (
                    f"Generate a natural, friendly response that:\n"
                    f"1. Confirms the task is complete\n"
                    f"2. Summarizes the key results\n"
                    f"3. Keep it conversational (2-3 sentences)\n"
                    f"IMPORTANT: Respond in English only."
                )
            
            # 生成回應
            info_log(f"[LLM] 生成工作流完成總結回應...")
            response = self.model.query(prompt, mode="work", tools=None)
            response_text = response.get("text", "The task has been completed successfully.")
            
            info_log(f"[LLM] 工作流完成回應: {response_text[:100]}...")
            
            # 觸發 TTS 輸出
            from core.framework import core_framework
            tts_module = core_framework.get_module('tts')
            if tts_module:
                debug_log(2, f"[LLM] 觸發 TTS 輸出最終總結")
                tts_module.handle({
                    "text": response_text,
                    "session_id": session_id,
                    "emotion": "neutral"
                })
            
            # ✅ 標記工作流會話待結束
            # Controller 會在下一個 CYCLE_COMPLETED 時執行實際結束
            from core.sessions.session_manager import unified_session_manager
            unified_session_manager.mark_workflow_session_for_end(
                session_id, 
                reason=f"workflow_completed:{workflow_type}"
            )
            debug_log(1, f"[LLM] 🔚 已標記 WS 待結束: {session_id} (workflow_completed:{workflow_type})")
            
            # 🔧 修正：工作流完成時不需要批准步驟
            # 工作流已經完成，不需要繼續執行下一步
            # 只需標記會話待結束即可，Controller 會在循環邊界處理
            debug_log(2, f"[LLM] 工作流已完成，跳過批准步驟")
            
            debug_log(1, f"[LLM] ✅ 工作流完成處理完畢: {session_id}")
            
            # 🔧 清除待處理隊列中該工作流的所有互動提示
            # 工作流已完成，不應該再生成互動步驟的提示
            if hasattr(self, '_pending_interactive_prompts'):
                prompts_to_remove = [
                    prompt for prompt in self._pending_interactive_prompts
                    if prompt.get('session_id') == session_id
                ]
                for prompt in prompts_to_remove:
                    self._pending_interactive_prompts.remove(prompt)
                    debug_log(2, f"[LLM] 已從隊列移除已完成工作流的互動提示: {prompt.get('workflow_type')}/{prompt.get('next_step_info', {}).get('step_id')}")
            
            # 🔧 清除 workflow_processing 標誌，允許下一次輸入層運行
            from core.working_context import working_context_manager
            working_context_manager.set_skip_input_layer(False, reason="workflow_completion_processed")
            debug_log(2, "[LLM] 已清除 workflow_processing 標誌")
            
            # 🔧 清理追蹤標記，防止內存洩漏
            if session_id in self._processed_workflow_completions:
                self._processed_workflow_completions.discard(session_id)
                debug_log(2, f"[LLM] 已移除工作流完成追蹤: {session_id}")
            
            # 🔧 清理該工作流的所有 LLM_PROCESSING 步驟標記
            if hasattr(self, '_processed_llm_steps'):
                steps_to_remove = {key for key in self._processed_llm_steps if key.startswith(f"{session_id}:")}
                for step_key in steps_to_remove:
                    self._processed_llm_steps.discard(step_key)
                if steps_to_remove:
                    debug_log(2, f"[LLM] 已清理 {len(steps_to_remove)} 個 LLM_PROCESSING 步驟標記")
            
        except Exception as e:
            import traceback
            error_log(f"[LLM] 處理工作流完成失敗: {e}")
            error_log(f"[LLM] 堆疊追蹤:\n{traceback.format_exc()}")
            # 即使失敗也要批准步驟，避免工作流卡住
            try:
                self._approve_workflow_step(session_id, None)
            except:
                pass
    
    def _process_interactive_step_prompt(self, session_id: str, workflow_type: str,
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
            debug_log(2, f"[LLM] 開始處理互動步驟提示: {workflow_type} ({session_id})")
            
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
                
                # 🔧 如果是 LLM 處理請求，添加處理結果的上下文
                if action == 'llm_processing_request':
                    request_data = review_data.get('request_data', {})
                    input_data = request_data.get('input_data', {})
                    
                    # 檢查是否有格式化的結果列表（例如搜尋結果）
                    if 'formatted_results' in input_data:
                        formatted_results = input_data['formatted_results']
                        prompt += f"\nAvailable Options:\n{formatted_results}\n\n"
                        debug_log(2, f"[LLM] 添加格式化結果到提示中: {len(formatted_results)} 字符")
                    
                    # 或者如果有其他輸入數據
                    elif input_data:
                        # 將輸入數據轉換為可讀格式
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
            
            # 安全地記錄 prompt 的前 200 字符
            prompt_preview = str(prompt)[:200] if len(str(prompt)) > 200 else str(prompt)
            debug_log(3, f"[LLM] 互動步驟提示 prompt:\n{prompt_preview}...")
            
            # 調用 LLM 生成回應
            try:
                response_data = self.model.query(
                    prompt=prompt,
                    mode="work",
                    tools=None  # 不需要工具
                )
                
                # 提取文本回應
                response_text = response_data.get('text', '') if isinstance(response_data, dict) else str(response_data)
                
                if not response_text or not response_text.strip():
                    error_log("[LLM] 互動步驟提示生成失敗：空回應")
                    response_text = f"Got it! {next_step_prompt}"
                
                info_log(f"[LLM] 互動步驟提示已生成: {response_text[:100]}...")
                
            except Exception as e:
                error_log(f"[LLM] 生成互動步驟提示時出錯: {e}")
                # 使用英文備用回應，避免中文出現在 TTS 中
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
                debug_log(2, f"[LLM] 觸發 TTS 輸出互動步驟提示")
                tts_module.handle({
                    "text": response_text,
                    "session_id": session_id,
                    "emotion": "neutral"
                })
            
            # 🔧 修正：不要批准步驟！
            # 當下一步是互動步驟時，LLM 只是提供提示，不批准當前步驟
            # 工作流應該停在 INTERACTIVE 步驟，等待用戶輸入
            # WorkflowEngine 的 _auto_advance 會檢測到 InteractiveStep 並自動發布 workflow_requires_input 事件
            # 用戶提供輸入後，才會調用 provide_workflow_input 繼續工作流
            
            debug_log(1, f"[LLM] ✅ 互動步驟提示處理完畢: {session_id}")
            
        except Exception as e:
            import traceback
            error_log(f"[LLM] 處理互動步驟提示失敗: {e}")
            error_log(f"[LLM] 堆疊追蹤:\n{traceback.format_exc()}")
            # 即使失敗也要批准步驟，避免工作流卡住
            try:
                self._approve_workflow_step(session_id, None)
            except:
                pass
    
    def _validate_session_architecture(self, current_state) -> bool:
        """驗證會話架構 - 確保 LLM 在適當的會話上下文中運作"""
        try:
            from core.sessions.session_manager import session_manager
            from core.states.state_manager import UEPState
            
            # 檢查是否有活躍的 GS
            current_gs = session_manager.get_current_general_session()
            if not current_gs:
                debug_log(1, "[LLM] 會話架構檢查：沒有活躍的 GS")
                return False
            
            # 根據系統狀態檢查相應的會話類型
            if current_state == UEPState.CHAT:
                # CHAT 狀態需要 CS
                active_cs_ids = session_manager.get_active_chatting_session_ids()
                if not active_cs_ids:
                    debug_log(1, "[LLM] 會話架構檢查：CHAT 狀態但沒有活躍的 CS")
                    return False
                    
            elif current_state == UEPState.WORK:
                # WORK 狀態需要 WS
                active_ws_ids = session_manager.get_active_workflow_session_ids()
                if not active_ws_ids:
                    debug_log(1, "[LLM] 會話架構檢查：WORK 狀態但沒有活躍的 WS")
                    return False
            
            debug_log(2, f"[LLM] 會話架構檢查通過：狀態={current_state}, GS={current_gs is not None}")
            return True
            
        except Exception as e:
            error_log(f"[LLM] 會話架構檢查失敗: {e}")
            return False
    
    def _publish_llm_response_event(self, output: LLMOutput, mode: str, extra_data: Dict[str, Any]):
        """發布 LLM 回應生成事件"""
        try:
            from core.event_bus import event_bus, SystemEvent
            
            event_data = {
                "mode": mode,
                "response": output.text,  # 使用 "response" 而非 "response_text" 以符合測試
                "confidence": output.confidence,
                "processing_time": output.processing_time,
                "tokens_used": output.tokens_used,
                "session_id": getattr(self, '_current_processing_session_id', None),
                "cycle_index": getattr(self, '_current_processing_cycle_index', None),
                "success": output.success,
                **extra_data
            }
            
            event_bus.publish(
                SystemEvent.LLM_RESPONSE_GENERATED,
                event_data,
                source="llm"
            )
            
            debug_log(2, f"[LLM] 已發布 LLM_RESPONSE_GENERATED 事件 (mode={mode})")
            
        except Exception as e:
            error_log(f"[LLM] 發布回應事件失敗: {e}")
    
    def _publish_learning_data_event(self, identity_id: str, interaction_type: str, 
                                     learning_signals: Dict, user_input: str, system_response: str):
        """發布學習資料返回事件"""
        try:
            from core.event_bus import event_bus, SystemEvent
            
            event_data = {
                "identity_id": identity_id,
                "interaction_type": interaction_type,
                "learning_signals": learning_signals,
                "user_input_length": len(user_input),
                "response_length": len(system_response),
                "session_id": getattr(self, '_current_processing_session_id', None),
                "timestamp": time.time()
            }
            
            event_bus.publish(
                SystemEvent.LLM_LEARNING_DATA_RETURNED,
                event_data,
                source="llm"
            )
            
            debug_log(2, f"[LLM] 已發布 LLM_LEARNING_DATA_RETURNED 事件")
            
        except Exception as e:
            error_log(f"[LLM] 發布學習資料事件失敗: {e}")

