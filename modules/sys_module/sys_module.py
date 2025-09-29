import os
import time

import yaml
from core.bases.module_base import BaseModule
from configs.config_loader import load_module_config
from utils.debug_helper import info_log, error_log, debug_log
from .schemas import SYSInput, SYSOutput, SessionInfo, SessionDetail

from .actions.window_control   import push_window, fold_window, switch_workspace, screenshot_and_annotate
from .actions.text_processing  import clipboard_tracker, quick_phrases, ocr_extract
from .actions.automation_helper import set_reminder, generate_backup_script, monitor_folder
from .actions.integrations import news_summary, get_weather, get_world_time, code_analysis, media_control

# Import session management
from core.sessions.session_manager import session_manager, WorkflowSession, SessionStatus
from .workflows import (
    WorkflowType, StepResult, WorkflowEngine, WorkflowDefinition
)

# Import test workflows
from .workflows.test_workflows import (
    create_test_workflow,
    get_available_test_workflows
)

# Import file workflows
from .workflows.file_workflows import (
    create_file_workflow,
    get_available_file_workflows
)

class SYSModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or load_module_config("sys_module")
        self.enabled_modes = set(self.config.get("modes", []))
        self._function_specs = None
        self.session_manager = session_manager
        # Custom session storage for engines
        self.workflow_engines = {}  # session_id -> engine mapping

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
        Start a new workflow session using the unified workflow engine
        
        Args:
            workflow_type: The type of workflow (test workflows: echo, countdown, data_collector, random_fail, tts_test; file workflows: file_processing, file_interaction, etc.)
            command: The original command that triggered this workflow
            initial_data: Initial data for the workflow
            
        Returns:
            A dict with session info and first step prompt
        """
        debug_log(1, f"[SYS] 啟動統一工作流程: {workflow_type}")
        
        # Create session
        session = self.session_manager.create_session(
            workflow_type=workflow_type,
            command=command,
            initial_data=initial_data or {}
        )
        
        session_id = session.session_id
        
        try:
            # Determine workflow engine based on type
            engine = None
            
            # Test workflows
            if workflow_type in ["echo", "countdown", "data_collector", "random_fail", "tts_test"]:
                # Get required modules for test workflows
                llm_module = None
                tts_module = None
                
                try:
                    from modules.llm_module.llm_module import LLMModule
                    from configs.config_loader import load_module_config
                    config = load_module_config("llm_module")
                    llm_module = LLMModule(config)
                    debug_log(2, f"[SYS] 已獲取LLM模組實例")
                except Exception as e:
                    debug_log(2, f"[SYS] 無法獲取LLM模組: {e}")
                    
                if workflow_type == "tts_test":
                    try:
                        from modules.tts_module.tts_module import TTSModule
                        from configs.config_loader import load_module_config
                        config = load_module_config("tts_module")
                        tts_module = TTSModule(config)
                        debug_log(2, f"[SYS] 已獲取TTS模組實例")
                    except Exception as e:
                        debug_log(2, f"[SYS] 無法獲取TTS模組: {e}")
                
                engine = create_test_workflow(workflow_type, session, llm_module=llm_module, tts_module=tts_module)
                
            # File workflows
            elif workflow_type in ["file_processing", "file_interaction"]:
                engine = create_file_workflow(workflow_type, session)
            elif workflow_type in ["drop_and_read", "intelligent_archive", "summarize_tag"]:
                engine = create_file_workflow(workflow_type, session)
                
            # Other workflow types - validate against WorkflowType enum
            else:
                try:
                    wf_type = WorkflowType(workflow_type)
                    if wf_type == WorkflowType.FILE_PROCESSING:
                        engine = create_file_workflow("file_processing", session)
                    else:
                        # Try as test workflow
                        engine = create_test_workflow(workflow_type, session)
                except ValueError:
                    return {
                        "status": "error",
                        "message": f"未知的工作流程類型: {workflow_type}"
                    }
            
            if not engine:
                return {
                    "status": "error",
                    "message": f"無法為 {workflow_type} 創建工作流程引擎"
                }
                
            # Store engine separately and register session in SessionManager
            self.workflow_engines[session_id] = engine
            # Session is already registered in SessionManager via create_session
            
            # Get initial prompt
            prompt = engine.get_prompt()
            
            info_log(f"[SYS] 已啟動統一工作流程 '{workflow_type}', ID: {session_id}")
            return {
                "status": "success",
                "session_id": session_id,
                "requires_input": True,
                "prompt": prompt,
                "message": f"已啟動 {workflow_type} 工作流程，請回應下一步指示",
                "data": {
                    "workflow_type": workflow_type,
                    "current_step": engine.get_current_step().id if engine.get_current_step() else None
                }
            }
            
        except Exception as e:
            error_log(f"[SYS] 創建統一工作流程引擎失敗: {e}")
            self.session_manager.end_session(
                session_id, 
                success=False, 
                message=f"無法為 {workflow_type} 創建工作流程"
            )
            # Clean up engine if it was created
            if session_id in self.workflow_engines:
                del self.workflow_engines[session_id]
            return {
                "status": "error",
                "message": f"無法為 {workflow_type} 創建工作流程: {e}"
            }
    
    def _continue_workflow(self, session_id: str, user_input: str):
        """
        Continue a workflow session using the new workflow engine
        
        Args:
            session_id: The workflow session ID
            user_input: User's input for the current step
            
        Returns:
            A dict with step results and next prompt
        """
        # Check if session exists
        session = self.session_manager.get_session(session_id)
        if not session:
            return {
                "status": "error",
                "message": f"找不到工作流程會話 ID: {session_id}"
            }
            
        # Check if engine exists
        engine = self.workflow_engines.get(session_id)
        if not engine:
            return {
                "status": "error", 
                "message": f"找不到工作流程引擎 ID: {session_id}"
            }
        
        if session.status != SessionStatus.ACTIVE:
            return {
                "status": "error",
                "message": f"工作流程已不再活動狀態: {session.status.value}"
            }
        
        try:
            # Process user input with the workflow engine
            result = engine.process_input(user_input)
            
            # Handle the result
            if result.cancel:
                # Workflow was cancelled
                self.session_manager.end_session(
                    session_id,
                    success=False,
                    message=result.message
                )
                # Clean up engine
                if session_id in self.workflow_engines:
                    del self.workflow_engines[session_id]
                return {
                    "status": "cancelled",
                    "message": result.message,
                    "data": result.data
                }
                
            elif result.complete:
                # Workflow completed successfully
                self.session_manager.end_session(
                    session_id,
                    success=True,
                    message=result.message
                )
                # Clean up engine
                if session_id in self.workflow_engines:
                    del self.workflow_engines[session_id]
                return {
                    "status": "completed",
                    "message": result.message,
                    "data": result.data
                }
                
            elif not result.success:
                # Step failed, ask for input again
                return {
                    "status": "waiting",
                    "session_id": session_id,
                    "requires_input": True,
                    "prompt": engine.get_prompt(),
                    "message": result.message
                }
                
            else:
                # Step succeeded, check if more input is needed
                current_step = engine.get_current_step()
                if current_step:
                    return {
                        "status": "waiting",
                        "session_id": session_id,
                        "requires_input": True,
                        "prompt": engine.get_prompt(),
                        "message": result.message,
                        "data": result.data
                    }
                else:
                    # Workflow completed
                    self.session_manager.end_session(
                        session_id,
                        success=True,
                        message=result.message
                    )
                    # Clean up engine
                    if session_id in self.workflow_engines:
                        del self.workflow_engines[session_id]
                    return {
                        "status": "completed",
                        "message": result.message,
                        "data": result.data
                    }
                    
        except Exception as e:
            error_log(f"[SYS] 工作流程執行錯誤: {e}")
            self.session_manager.end_session(
                session_id,
                success=False,
                message=f"工作流程執行錯誤: {e}"
            )
            # Clean up engine
            if session_id in self.workflow_engines:
                del self.workflow_engines[session_id]
            return {
                "status": "error",
                "message": f"工作流程執行錯誤: {e}"
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
        # Get active sessions from SessionManager
        active_sessions = self.session_manager.get_active_sessions()
        
        sessions_info = []
        for session in active_sessions:
            # Only include sessions that have corresponding engines
            if session.session_id in self.workflow_engines:
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
                "sessions": sessions_info
            },
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

        try:
            # Unified workflow handlers
            workflow_handlers = {
                "start_workflow": self._start_workflow,
                "continue_workflow": self._continue_workflow,
                "cancel_workflow": self._cancel_workflow,
                "get_workflow_status": self._get_workflow_status,
                "list_active_workflows": self._list_active_workflows,
            }
            
            # Standard action handlers (excluding file interaction - use workflows instead)
            action_handlers = {
                # File interaction actions are now workflow-only
                # "drop_and_read": use start_workflow with workflow_type="drop_and_read"
                # "intelligent_archive": use start_workflow with workflow_type="intelligent_archive" 
                # "summarize_tag": use start_workflow with workflow_type="summarize_tag"
                
                # Window Control Actions
                "push_window": push_window,
                "fold_window": fold_window,
                "switch_workspace": switch_workspace,
                "screenshot_and_annotate": screenshot_and_annotate,
                
                # Text Processing Actions  
                "clipboard_tracker": clipboard_tracker,
                "quick_phrases": quick_phrases,
                "ocr_extract": ocr_extract,
                
                # Automation Helper Actions
                "set_reminder": set_reminder,
                "generate_backup_script": generate_backup_script,
                "monitor_folder": monitor_folder,
                
                # Integration Actions
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
                    prompt=result.get("prompt"),
                    session_data=result.get("session_data")  # 傳遞會話數據
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

    def get_available_workflows(self) -> dict:
        """
        獲取所有可用的工作流程類型
        
        Returns:
            包含測試和文件工作流程的字典
        """
        return {
            "test_workflows": get_available_test_workflows(),
            "file_workflows": get_available_file_workflows()
        }
    
    # 舊的專門處理函數已被移除，統一使用 _start_workflow 和 _continue_workflow