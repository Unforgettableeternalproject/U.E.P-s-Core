import os
import time

import yaml
from core.module_base import BaseModule
from configs.config_loader import load_module_config
from utils.debug_helper import info_log, error_log, debug_log
from .schemas import SYSInput, SYSOutput, SessionInfo, SessionDetail

from .actions.file_interaction import drop_and_read, intelligent_archive, summarize_tag
from .actions.window_control   import push_window, fold_window, switch_workspace, screenshot_and_annotate
from .actions.text_processing  import clipboard_tracker, quick_phrases, ocr_extract
from .actions.automation_helper import set_reminder, generate_backup_script, monitor_folder
from .actions.integrations import news_summary, get_weather, get_world_time, code_analysis, media_control

# Import session management
from core.session_manager import SessionManager, WorkflowSession, SessionStatus
from .workflows import WorkflowType, get_next_step, create_file_processing_workflow

# Import test workflows
from .actions.test_workflows import (
    simple_echo_workflow,
    countdown_workflow,
    data_collector_workflow,
    random_fail_workflow,
    get_test_workflow
)

class SYSModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or load_module_config("sys_module")
        self.enabled_modes = set(self.config.get("modes", []))
        self._function_specs = None
        self.session_manager = SessionManager()

    def initialize(self):
        info_log("[SYS] 初始化完成，啟用模式：" + ", ".join(self.enabled_modes))
        return True
    
    def debug(self):
        # Debug level = 1
        debug_log(1, "[SYS] Debug 模式啟用")
        # Debug level = 2
        debug_log(2, f"[SYS] 啟用模式: {self.enabled_modes}")
        # Debug level = 3
        debug_log(3, f"[SYS] 模組設定: {self.config}")


    def _load_function_specs(self):
        if self._function_specs is None:
            path = os.path.join(os.path.dirname(__file__), "functions.yaml")
            with open(path, "r", encoding="utf-8") as f:
                self._function_specs = yaml.safe_load(f)
        return self._function_specs

    def _validate_params(self, mode, params):
        specs = self._load_function_specs()
        if mode not in specs:
            return False, f"找不到 mode: {mode} 的規範"
        param_specs = specs[mode].get("params", {})
        # 檢查必填欄位
        for key, rule in param_specs.items():
            if rule.get("required", False) and key not in params:
                return False, f"缺少必要參數: {key}"
            if key in params:
                expected_type = rule.get("type")
                value = params[key]
                # 型別檢查
                if expected_type == "str" and not isinstance(value, str):
                    return False, f"參數 {key} 應為字串"
                if expected_type == "int" and not isinstance(value, int):
                    return False, f"參數 {key} 應為整數"
                if expected_type == "dict" and not isinstance(value, dict):
                    return False, f"參數 {key} 應為字典"
        return True, ""
        
    # Session Workflow Methods
    
    def _start_workflow(self, workflow_type: str, command: str, initial_data=None):
        """
        Start a new workflow session
        
        Args:
            workflow_type: The type of workflow
            command: The original command that triggered this workflow
            initial_data: Initial data for the workflow
            
        Returns:
            A dict with session info and first step prompt
        """
        # Validate workflow type
        try:
            wf_type = WorkflowType(workflow_type)
        except ValueError:
            return {
                "status": "error",
                "message": f"未知的工作流程類型: {workflow_type}"
            }
        
        # Create session
        session = self.session_manager.create_session(
            workflow_type=workflow_type,
            command=command,
            initial_data=initial_data or {}
        )
        
        # Get first step based on workflow type
        first_step = None
        if workflow_type == WorkflowType.FILE_PROCESSING.value:
            workflow_steps = create_file_processing_workflow(session)
            first_step = workflow_steps.get(1)
        
        if not first_step:
            self.session_manager.end_session(
                session.session_id, 
                success=False, 
                message=f"無法為 {workflow_type} 創建工作流程"
            )
            return {
                "status": "error",
                "message": f"無法為 {workflow_type} 創建工作流程"
            }
        
        # Return session info with prompt for first step
        info_log(f"[SYS] 已啟動工作流程 '{workflow_type}', ID: {session.session_id}")
        return {
            "status": "success",
            "session_id": session.session_id,
            "requires_input": True,
            "prompt": first_step.get_prompt(),
            "message": f"已啟動 {workflow_type} 工作流程，請回應下一步指示",
            "data": {
                "workflow_type": workflow_type,
                "step": 1
            }
        }
    
    def _continue_workflow(self, session_id: str, user_input: str):
        """
        Continue a workflow session with user input
        
        Args:
            session_id: The session ID
            user_input: User's input for the current step
            
        Returns:
            The result of processing the current step and the next step
        """
        session = self.session_manager.get_session(session_id)
        
        if not session:
            return {
                "status": "error",
                "message": f"找不到工作流程會話 ID: {session_id}"
            }
        
        if session.status != SessionStatus.ACTIVE:
            return {
                "status": "error",
                "message": f"工作流程已不再活動狀態: {session.status.value}"
            }
        
        # Get current step handler
        current_step_num = session.current_step
        workflow_type = session.workflow_type
        
        if workflow_type == WorkflowType.FILE_PROCESSING.value:
            workflow = create_file_processing_workflow(session)
            current_step = workflow.get(current_step_num)
            
            if not current_step:
                current_step = get_next_step(session, current_step_num - 1)
                
            if not current_step:
                self.session_manager.end_session(
                    session.session_id,
                    success=False,
                    message=f"找不到工作流程的步驟 {current_step_num}"
                )
                return {
                    "status": "error",
                    "message": f"找不到工作流程的步驟 {current_step_num}"
                }
        else:
            return {
                "status": "error",
                "message": f"不支援的工作流程類型: {workflow_type}"
            }
        
        # Execute current step with user input
        try:
            success, message, step_data = current_step.execute(user_input)
            
            # If step failed, ask for input again
            if not success:
                return {
                    "status": "waiting",
                    "session_id": session_id,
                    "requires_input": True,
                    "prompt": message,
                    "message": message,
                    "data": {
                        "workflow_type": workflow_type,
                        "step": current_step_num
                    }
                }
            
            # If cancelled in the step execution
            if step_data and step_data.get("cancelled", False):
                return {
                    "status": "cancelled",
                    "session_id": session_id,
                    "requires_input": False,
                    "message": "工作流程已取消",
                    "data": {
                        "workflow_type": workflow_type,
                        "step": current_step_num,
                        "cancelled": True
                    }
                }
            
            # Step succeeded, add data to session and advance
            if step_data:
                for key, value in step_data.items():
                    session.add_data(key, value)
            
            session.advance_step()
            next_step_num = session.current_step
            next_step = get_next_step(session, current_step_num)
            
            # If workflow completed
            if not next_step:
                if session.status == SessionStatus.COMPLETED:
                    return {
                        "status": "completed",
                        "session_id": session_id,
                        "requires_input": False,
                        "message": message,
                        "data": session.data
                    }
                else:
                    # Something went wrong
                    return {
                        "status": "error",
                        "session_id": session_id,
                        "requires_input": False,
                        "message": f"工作流程異常終止: {session.status.value}",
                        "data": session.data
                    }
            
            # Continue to next step
            return {
                "status": "continue",
                "session_id": session_id,
                "requires_input": True,
                "prompt": next_step.get_prompt(),
                "message": message,
                "data": {
                    "workflow_type": workflow_type,
                    "previous_step": current_step_num,
                    "step": next_step_num
                }
            }
        
        except Exception as e:
            error_log(f"[SYS] 工作流程步驟執行錯誤: {e}")
            session.fail_session(f"步驟執行發生錯誤: {e}")
            return {
                "status": "error",
                "session_id": session_id,
                "requires_input": False,
                "message": f"工作流程執行錯誤: {e}",
                "data": {
                    "error": str(e)
                }
            }
    
    def _cancel_workflow(self, session_id: str, reason: str = "使用者取消"):
        """Cancel an active workflow session"""
        session = self.session_manager.get_session(session_id)
        
        if not session:
            return {
                "status": "error",
                "message": f"找不到工作流程會話 ID: {session_id}"
            }
        
        if session.status != SessionStatus.ACTIVE:
            return {
                "status": "warning",
                "message": f"工作流程已經處於非活動狀態: {session.status.value}"
            }
        
        session.cancel_session(reason)
        info_log(f"[SYS] 已取消工作流程 ID: {session_id}, 原因: {reason}")
        
        return {
            "status": "success",
            "message": f"已取消工作流程: {reason}"
        }
    
    def _get_workflow_status(self, session_id: str):
        """Get the current status of a workflow session"""
        session = self.session_manager.get_session(session_id)
        
        if not session:
            return {
                "status": "error",
                "message": f"找不到工作流程會話 ID: {session_id}"
            }
        
        # Convert to dict for output
        session_data = session.to_dict()
        
        return {
            "status": "success",
            "data": session_data,
            "message": f"工作流程狀態: {session.status.value}, 步驟: {session.current_step}"
        }
    
    def _list_active_workflows(self):
        """List all active workflow sessions"""
        active_sessions = self.session_manager.get_active_sessions()
        
        # Clean up old sessions while we're at it
        self.session_manager.cleanup_old_sessions()
        
        # Convert to simple info dicts
        sessions_info = []
        for session in active_sessions:
            sessions_info.append({
                "session_id": session.session_id,
                "workflow_type": session.workflow_type,
                "command": session.command,
                "current_step": session.current_step,
                "created_at": session.created_at,
                "last_active": session.last_active
            })
        
        return {
            "status": "success",
            "data": {
                "active_count": len(sessions_info),
                "sessions": sessions_info            },
            "message": f"找到 {len(sessions_info)} 個活動中的工作流程"
        }
        
    def handle(self, data: dict) -> dict:
        try:
            inp = SYSInput(**data)
        except Exception as e:
            return SYSOutput(status="error", message=f"輸入錯誤：{e}").dict()

        mode = inp.mode
        params = inp.params or {}
        session_id = inp.session_id
        user_input = inp.user_input

        # list_functions 為特殊 mode，不受 enabled 篩選
        if mode == "list_functions":
            return SYSOutput(status="success", data=self._list_functions()).dict()

        # Check if this is a session continuation with just user_input
        if session_id and user_input and not mode:
            # Auto-set mode to continue_workflow
            mode = "continue_workflow"
            params = {"session_id": session_id, "user_input": user_input}
        
        # Workflow modes are always enabled
        workflow_modes = {"start_workflow", "continue_workflow", "cancel_workflow", 
                         "get_workflow_status", "list_active_workflows"}
        
        if mode not in workflow_modes and mode not in self.enabled_modes:
            return SYSOutput(status="error", message=f"未知或未啟用模式：{mode}").dict()

        vaild, msg = self._validate_params(mode, params)
        if not vaild:
            return SYSOutput(status="error", message=f"參數驗證失敗：{msg}").dict()

        try:# Workflow handlers
            workflow_handlers = {
                "start_workflow": self._start_workflow,
                "continue_workflow": self._continue_workflow,
                "cancel_workflow": self._cancel_workflow,
                "get_workflow_status": self._get_workflow_status,
                "list_active_workflows": self._list_active_workflows,
                # Test workflow handlers
                "test_workflow_echo": self._start_test_workflow,
                "test_workflow_countdown": self._start_test_workflow,
                "test_workflow_data_collector": self._start_test_workflow,
                "test_workflow_random_fail": self._start_test_workflow,
                "test_workflow_continue": self._continue_test_workflow  # Generic continuation handler for test workflows
            }
            
            # Standard action handlers
            action_handlers = {
                "drop_and_read": drop_and_read,
                "intelligent_archive": intelligent_archive,
                "summarize_tag": summarize_tag,
                "push_window": push_window,
                "fold_window": fold_window,
                "switch_workspace": switch_workspace,
                "screenshot_and_annotate": screenshot_and_annotate,
                "clipboard_tracker": clipboard_tracker,
                "quick_phrases": quick_phrases,
                "ocr_extract": ocr_extract,
                "set_reminder": set_reminder,
                "generate_backup_script": generate_backup_script,
                "monitor_folder": monitor_folder,
                "news_summary": news_summary,
                "get_weather": get_weather,
                "get_world_time": get_world_time,
                "code_analysis": code_analysis,
                "media_control": media_control,
            }
            
            # Check if this is a workflow operation first
            if mode in workflow_handlers:
                workflow_handler = workflow_handlers[mode]
                
                if mode == "start_workflow":
                    workflow_type = params.get("workflow_type")
                    command = params.get("command")
                    initial_data = params.get("initial_data", {})
                    result = workflow_handler(workflow_type, command, initial_data)
                elif mode == "continue_workflow":
                    session_id = params.get("session_id")
                    user_input = params.get("user_input", "")
                    result = workflow_handler(session_id, user_input)
                elif mode == "cancel_workflow":
                    session_id = params.get("session_id")
                    reason = params.get("reason", "使用者取消")
                    result = workflow_handler(session_id, reason)
                elif mode == "get_workflow_status":
                    session_id = params.get("session_id")
                    result = workflow_handler(session_id)
                else:  # list_active_workflows
                    result = workflow_handler()
                
                # Convert result to SYSOutput format
                out = SYSOutput(
                    status=result.get("status", "error"),
                    data=result.get("data"),
                    message=result.get("message", ""),
                    session_id=result.get("session_id"),
                    requires_input=result.get("requires_input", False),
                    prompt=result.get("prompt")
                )
                return out.dict()
            
            # Standard action handling
            func = action_handlers.get(mode)
            if not func:
                error_log(f"[SYS] [{mode}] 未知的操作模式")
                return SYSOutput(status="error", message=f"未知的操作模式: {mode}").dict()
                
            result = func(**params)
            info_log(f"[SYS] [{mode}] 執行完成")
            return SYSOutput(status="success", data=result).dict()
        except Exception as e:
            error_log(f"[SYS] [{mode}] 執行失敗：{e}")
            return SYSOutput(status="error", message=str(e)).dict()
    
    def _list_functions(self) -> dict:
        """
        讀取 functions.yaml 並回傳所有 mode 定義
        """
        try:
            path = os.path.join(os.path.dirname(__file__), "functions.yaml")
            with open(path, "r", encoding="utf-8") as f:
                funcs = yaml.safe_load(f)
            return funcs
        except Exception as e:
            error_log(f"[SYS] 列出功能失敗：{e}")
            return {}

    def _start_test_workflow(self, workflow_type: str, params: dict = None):
        """
        啟動測試工作流程
        
        Args:
            workflow_type: 工作流程類型 (echo, countdown, data_collector, random_fail)
            params: 初始參數
            
        Returns:
            包含工作流程會話資訊的字典
        """
        debug_log(1, f"[SYS] 啟動測試工作流程: {workflow_type}")
        
        # 驗證工作流程類型
        workflow_func = get_test_workflow(workflow_type)
        if not workflow_func:
            error_log(f"[SYS] 未知的測試工作流程類型: {workflow_type}")
            return {
                "status": "error",
                "message": f"未知的測試工作流程類型: {workflow_type}"
            }
        
        # 創建會話
        session_id = f"test-{workflow_type}-{int(time.time())}"
        
        # 初始化會話數據
        session_data = {
            "step": 1,
            **(params or {})
        }
        
        # 初次執行工作流程
        try:
            # 獲取 LLM 模組實例
            llm_module = None
            try:
                from modules.llm_module.llm_module import LLMModule
                from configs.config_loader import load_module_config
                
                config = load_module_config("llm_module")
                llm_module = LLMModule(config)
                debug_log(2, f"[SYS] 已獲取LLM模組實例用於測試工作流程")
            except Exception as e:
                debug_log(2, f"[SYS] 無法獲取LLM模組，測試工作流程將在無LLM模式下運行: {e}")
            
            # 執行工作流程
            result = workflow_func(session_data, llm_module)
            
            # 返回結果
            return {
                "status": result.get("status", "processing"),
                "message": result.get("message", f"已啟動 {workflow_type} 測試工作流程"),
                "session_id": session_id,
                "requires_input": result.get("requires_input", False),
                "prompt": result.get("prompt", ""),
                "data": {
                    "workflow_type": workflow_type,
                    "step": session_data.get("step", 1)
                },
                "session_data": result.get("session_data", session_data)
            }
        except Exception as e:
            error_log(f"[SYS] 啟動測試工作流程時發生錯誤: {e}")
            return {
                "status": "error",
                "message": f"啟動測試工作流程時發生錯誤: {e}"
            }

    def _continue_test_workflow(self, session_id: str, user_input: str, session_data: dict):
        """
        繼續執行測試工作流程
        
        Args:
            session_id: 工作流程會話 ID
            user_input: 用戶輸入
            session_data: 會話數據
            
        Returns:
            更新後的工作流程狀態
        """
        debug_log(1, f"[SYS] 繼續執行測試工作流程: {session_id}, 步驟: {session_data.get('step', '?')}")
        
        # 從會話ID解析工作流程類型
        workflow_type = "unknown"
        try:
            # 從 session_id 中提取 workflow_type (格式: test-{workflow_type}-{timestamp})
            parts = session_id.split("-")
            if len(parts) >= 2 and parts[0] == "test":
                workflow_type = parts[1]
        except Exception:
            pass
        
        # 獲取工作流程函數
        workflow_func = get_test_workflow(workflow_type)
        if not workflow_func:
            error_log(f"[SYS] 未知的測試工作流程類型: {workflow_type}")
            return {
                "status": "error",
                "message": f"未知的測試工作流程類型: {workflow_type}",
                "session_id": session_id
            }
        
        # 處理用戶輸入
        if user_input is not None:
            # 根據當前步驟處理輸入
            step = session_data.get("step", 1)
            
            # 特殊值處理
            if user_input.lower() in ("取消", "退出", "exit", "quit", "cancel"):
                debug_log(1, f"[SYS] 用戶請求取消測試工作流程: {session_id}")
                return {
                    "status": "cancelled",
                    "message": "已取消測試工作流程",
                    "session_id": session_id,
                    "requires_input": False
                }
            
            # 根據工作流程類型處理輸入
            if workflow_type == "echo":
                session_data["message"] = user_input
            elif workflow_type == "countdown":
                if "count" not in session_data:
                    try:
                        session_data["count"] = int(user_input)
                    except ValueError:
                        return {
                            "status": "error",
                            "message": f"無效的數字: {user_input}，請提供有效數字",
                            "session_id": session_id,
                            "requires_input": True,
                            "prompt": "請輸入一個正整數:"
                        }
                elif user_input.lower() == "跳過":
                    session_data["count"] = 0  # 直接結束倒數
            elif workflow_type == "data_collector":
                if step == 1 and "name" not in session_data:
                    session_data["name"] = user_input
                elif step == 2 and "age" not in session_data:
                    session_data["age"] = user_input
                elif step == 3 and "interests" not in session_data:
                    session_data["interests"] = user_input
                elif step == 4 and "feedback" not in session_data:
                    session_data["feedback"] = user_input
            elif workflow_type == "random_fail":
                if step == 1 and "fail_chance" not in session_data:
                    try:
                        fail_chance = int(user_input)
                        if fail_chance < 0 or fail_chance > 100:
                            raise ValueError("機率必須在0-100之間")
                        session_data["fail_chance"] = fail_chance
                    except ValueError:
                        return {
                            "status": "error",
                            "message": f"無效的機率值: {user_input}，請提供0-100之間的數字",
                            "session_id": session_id,
                            "requires_input": True,
                            "prompt": "請設定失敗機率 (0-100):"
                        }
                elif step == 2:
                    if user_input.lower() in ("是", "y", "yes", "繼續"):
                        # 重置步驟，重新擲骰
                        session_data["retry_count"] = session_data.get("retry_count", 0)  # 保留重試次數
                    else:
                        # 用戶不想重試，終止工作流程
                        return {
                            "status": "cancelled",
                            "message": "用戶取消了重試",
                            "session_id": session_id,
                            "requires_input": False
                        }
        
        # 獲取 LLM 模組實例
        llm_module = None
        try:
            from modules.llm_module.llm_module import LLMModule
            from configs.config_loader import load_module_config
            
            config = load_module_config("llm_module")
            llm_module = LLMModule(config)
        except Exception:
            pass
        
        # 執行工作流程
        try:
            result = workflow_func(session_data, llm_module)
            
            # 更新會話數據
            if "session_data" in result:
                session_data = result["session_data"]
            
            # 如果工作流程完成或出錯，獲取結果
            status = result.get("status", "processing")
            result_data = {}
            if status == "completed" and "result" in result:
                result_data = result["result"]
            
            # 返回結果
            return {
                "status": status,
                "message": result.get("message", ""),
                "session_id": session_id,
                "requires_input": result.get("requires_input", False),
                "prompt": result.get("prompt", ""),
                "data": {
                    "workflow_type": workflow_type,
                    "step": session_data.get("step", 1),
                    **result_data
                },
                "session_data": session_data
            }
        except Exception as e:
            error_log(f"[SYS] 執行測試工作流程時發生錯誤: {e}")
            return {
                "status": "error",
                "message": f"執行測試工作流程時發生錯誤: {e}",
                "session_id": session_id,
                "requires_input": False
            }