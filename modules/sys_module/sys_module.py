import os
import time
import asyncio

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
    WorkflowType, WorkflowMode, StepResult, WorkflowEngine, WorkflowDefinition
)

# Import MCP Server
from .mcp_server import MCPServer

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
        
        # Initialize MCP Server
        self.mcp_server = MCPServer(sys_module=self)
        debug_log(2, "[SYS] MCP Server 已初始化")

    def initialize(self):
        # 註冊 WORK_SYS 協作管道的資料提供者
        self._register_collaboration_providers()
        
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

    def _register_collaboration_providers(self):
        """註冊 WORK_SYS 協作管道的資料提供者"""
        try:
            from modules.llm_module.module_interfaces import state_aware_interface
            
            # 1. 註冊工作流狀態提供者
            state_aware_interface.register_work_sys_provider(
                data_type="workflow_status",
                provider_func=self._provide_workflow_status
            )
            
            # 2. 註冊功能列表提供者
            state_aware_interface.register_work_sys_provider(
                data_type="function_registry",
                provider_func=self._provide_function_registry
            )
            
            info_log("[SYS] ✅ 已註冊 WORK_SYS 協作管道資料提供者")
            debug_log(2, "[SYS] 註冊提供者: workflow_status, function_registry")
            
        except Exception as e:
            error_log(f"[SYS] ❌ 註冊協作管道提供者失敗: {e}")
    
    def _provide_workflow_status(self, **kwargs):
        """提供當前工作流狀態給 LLM"""
        try:
            workflow_id = kwargs.get('workflow_id')
            
            if not workflow_id:
                # 如果沒有指定 workflow_id，返回所有活躍工作流的摘要
                active_workflows = []
                for wf_id, engine in self.workflow_engines.items():
                    session = self.session_manager.get_workflow_session(wf_id)
                    if session:
                        # 檢查會話狀態（兼容不同的狀態類型）
                        status_value = session.status.value if hasattr(session.status, 'value') else str(session.status)
                        
                        # 只包含活躍的工作流
                        if 'active' in status_value.lower() or 'executing' in status_value.lower() or 'ready' in status_value.lower():
                            # 計算進度
                            progress = 0.0
                            if hasattr(session, 'stats') and session.stats:
                                total_steps = session.stats.get('total_steps', 0)
                                completed_steps = session.stats.get('completed_steps', 0)
                                if total_steps > 0:
                                    progress = completed_steps / total_steps
                            
                            active_workflows.append({
                                "workflow_id": wf_id,
                                "workflow_type": engine.definition.workflow_type,
                                "status": status_value,
                                "progress": progress
                            })
                
                return {
                    "active_workflows": active_workflows,
                    "total_count": len(active_workflows)
                }
            
            # 查詢特定工作流的詳細狀態
            session = self.session_manager.get_workflow_session(workflow_id)
            if not session:
                return {
                    "status": "not_found",
                    "workflow_id": workflow_id,
                    "message": "找不到指定的工作流會話"
                }
            
            engine = self.workflow_engines.get(workflow_id)
            if not engine:
                return {
                    "status": "no_engine",
                    "workflow_id": workflow_id,
                    "message": "工作流引擎未初始化"
                }
            
            # 獲取當前步驟
            current_step = engine.get_current_step()
            
            # 獲取可用功能（根據當前工作流類型）
            available_functions = self._get_available_functions_for_workflow(engine)
            
            # 計算進度（基於步驟完成情況）
            progress = 0.0
            if hasattr(session, 'stats') and session.stats:
                total_steps = session.stats.get('total_steps', 0)
                completed_steps = session.stats.get('completed_steps', 0)
                if total_steps > 0:
                    progress = completed_steps / total_steps
            
            return {
                "workflow_id": workflow_id,
                "workflow_type": engine.definition.workflow_type,
                "workflow_name": engine.definition.name,
                "workflow_mode": engine.definition.workflow_mode.value,
                "current_step": current_step.id if current_step else None,
                "current_step_type": current_step.step_type if current_step else None,
                "progress": progress,
                "status": session.status.value if hasattr(session.status, 'value') else str(session.status),
                "requires_llm_review": engine.definition.requires_llm_review,
                "available_functions": available_functions,
                "metadata": session.session_metadata if hasattr(session, 'session_metadata') else {}
            }
            
        except Exception as e:
            error_log(f"[SYS] 提供工作流狀態失敗: {e}")
            return {
                "status": "error",
                "message": str(e)
            }
    
    def _provide_function_registry(self, **kwargs):
        """提供可用的系統功能列表給 LLM"""
        try:
            category = kwargs.get('category', 'all')
            
            # 從 functions.yaml 讀取可用功能
            specs = self._load_function_specs()
            
            if category == 'all':
                # 返回所有功能
                functions = []
                for name, spec in specs.items():
                    if name in self.enabled_modes:
                        functions.append({
                            "name": name,
                            "category": spec.get('category', 'general'),
                            "description": spec.get('description', ''),
                            "params": list(spec.get('params', {}).keys())
                        })
                return functions
            else:
                # 根據分類過濾
                functions = []
                for name, spec in specs.items():
                    if name in self.enabled_modes and spec.get('category') == category:
                        functions.append({
                            "name": name,
                            "category": category,
                            "description": spec.get('description', ''),
                            "params": list(spec.get('params', {}).keys())
                        })
                return functions
                
        except Exception as e:
            error_log(f"[SYS] 提供功能列表失敗: {e}")
            return []
    
    def _get_available_functions_for_workflow(self, engine):
        """根據工作流類型獲取可用功能"""
        try:
            # 基本功能始終可用
            base_functions = ["cancel_workflow", "get_workflow_status"]
            
            # 根據工作流類型添加特定功能
            workflow_type = engine.definition.workflow_type
            
            if "file" in workflow_type.lower():
                base_functions.extend(["file_read", "file_write", "file_list"])
            
            if engine.definition.requires_llm_review:
                base_functions.extend(["review_step", "approve_step", "modify_step"])
            
            return base_functions
            
        except Exception as e:
            debug_log(2, f"[SYS] 獲取可用功能失敗: {e}")
            return []

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
        session_result = self.session_manager.create_session(
            workflow_type=workflow_type,
            command=command,
            initial_data=initial_data or {}
        )
        
        # Handle return value - could be session object or session_id string
        if isinstance(session_result, str):
            session_id = session_result
            # Get the actual session object from session manager
            session = self.session_manager.get_workflow_session(session_id)
            if not session:
                raise ValueError(f"無法獲取會話對象: {session_id}")
        elif hasattr(session_result, 'session_id'):
            session = session_result
            session_id = session.session_id
        else:
            raise ValueError(f"無效的會話創建結果: {type(session_result)}")
        
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
            
            # 階段五：檢查工作流程執行模式
            workflow_def = engine.definition
            workflow_mode = workflow_def.workflow_mode
            
            debug_log(2, f"[SYS] 工作流程執行模式: {workflow_mode}")
            
            # 如果是背景模式，提交到 BackgroundWorker
            if workflow_mode == WorkflowMode.BACKGROUND:
                try:
                    from modules.ui_module.debug.background_worker import get_worker_manager
                    worker_manager = get_worker_manager()
                    
                    # 提交背景任務
                    task_id = worker_manager.submit_workflow(
                        workflow_engine=engine,
                        workflow_type=workflow_type,
                        session_id=session_id,
                        metadata={
                            "command": command,
                            "initial_data": initial_data
                        }
                    )
                    
                    info_log(f"[SYS] 已提交背景工作流程 '{workflow_type}', task_id: {task_id}, session_id: {session_id}")
                    
                    # 將 task_id 儲存到 session
                    session.add_data("background_task_id", task_id)
                    
                    # 不儲存 engine 到 workflow_engines，因為在背景執行
                    # BackgroundWorker 會管理 engine 的生命週期
                    
                    return {
                        "status": "submitted",
                        "session_id": session_id,
                        "task_id": task_id,
                        "message": f"已提交 {workflow_type} 工作流程到背景執行",
                        "data": {
                            "workflow_type": workflow_type,
                            "workflow_mode": "background",
                            "task_id": task_id
                        }
                    }
                    
                except Exception as e:
                    error_log(f"[SYS] 提交背景工作流程失敗: {e}")
                    # 清理會話
                    self.session_manager.end_session(
                        session_id,
                        reason=f"提交背景任務失敗: {e}"
                    )
                    return {
                        "status": "error",
                        "message": f"提交背景工作流程失敗: {e}"
                    }
            
            # 直接模式：同步執行，保留原有邏輯
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
                reason=f"無法為 {workflow_type} 創建工作流程: {e}"
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
        
        # Check if session is in an active state (ready, executing, or waiting)
        active_statuses = [SessionStatus.READY, SessionStatus.EXECUTING, SessionStatus.WAITING]
        if session.status not in active_statuses:
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
                    reason=f"cancelled: {result.message}"
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
                    reason=f"completed: {result.message}"
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
                        reason=f"completed: {result.message}"
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
                reason=f"error: {str(e)}"
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
        
        # Check if session is in an active state
        active_statuses = [SessionStatus.READY, SessionStatus.EXECUTING, SessionStatus.WAITING]
        if session.status not in active_statuses:
            return {
                "status": "error",
                "message": f"工作流程不在活動狀態: {session.status.value}"
            }
        
        session.cancel(reason)
        info_log(f"[SYS] 已取消工作流程 ID: {session_id}, 原因: {reason}")
        
        # Clean up engine
        if session_id in self.workflow_engines:
            del self.workflow_engines[session_id]
        
        return {
            "status": "cancelled",
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
        
        # Get current step info
        current_step_info = session.get_current_step()
        current_step_name = current_step_info.get("step_name", "N/A") if current_step_info else "無"
        
        return {
            "status": "success",
            "data": session_data,
            "message": f"工作流程狀態: {session.status.value}, 當前步驟: {current_step_name}"
        }
    
    def _list_active_workflows(self):
        """List all active workflow sessions"""
        # Get active sessions from SessionManager
        active_sessions = self.session_manager.get_active_sessions()
        
        sessions_info = []
        for session in active_sessions:
            # Only include sessions that have corresponding engines
            if session.session_id in self.workflow_engines:
                # Get current step info
                current_step_info = session.get_current_step()
                current_step_name = current_step_info.get("step_name") if current_step_info else None
                
                sessions_info.append({
                    "session_id": session.session_id,
                    "workflow_type": session.task_definition.get("workflow_type", "unknown"),
                    "command": session.task_definition.get("command", ""),
                    "current_step": current_step_name,
                    "created_at": session.created_at.isoformat() if hasattr(session.created_at, 'isoformat') else str(session.created_at),
                    "last_active": session.last_activity.isoformat() if hasattr(session.last_activity, 'isoformat') else str(session.last_activity)
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
    # ========== Async Methods for MCP Server ==========
    
    async def start_workflow_async(self, workflow_type: str, command: str, initial_data: dict = None) -> dict:
        """
        Async wrapper for starting a workflow (for MCP Server)
        
        Args:
            workflow_type: Type of workflow to start
            command: Original command that triggered this workflow
            initial_data: Initial data for the workflow
            
        Returns:
            Dict with status, session_id, and workflow info
        """
        # Run synchronous _start_workflow in executor to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            self._start_workflow,
            workflow_type,
            command,
            initial_data or {}
        )
        return result
    
    async def continue_workflow_async(self, session_id: str, user_input: str = None, additional_data: dict = None) -> dict:
        """
        Async wrapper for continuing a workflow (for MCP Server)
        
        Args:
            session_id: Workflow session ID
            user_input: User's input for the current step
            additional_data: Additional data to pass to the workflow
            
        Returns:
            Dict with status, message, and workflow state
        """
        # Merge user_input into additional_data if needed
        if user_input is None and additional_data:
            user_input = additional_data.get("user_input", "")
        
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            self._continue_workflow,
            session_id,
            user_input or ""
        )
        return result
    
    async def modify_and_reexecute_step_async(self, session_id: str, modifications: dict) -> dict:
        """
        Modify current step parameters and re-execute (for MCP Server)
        
        Args:
            session_id: Workflow session ID
            modifications: Parameters to modify
            
        Returns:
            Dict with status and new step result
        """
        # Get the workflow engine
        engine = self.workflow_engines.get(session_id)
        if not engine:
            return {
                "status": "error",
                "message": f"找不到工作流程引擎 ID: {session_id}"
            }
        
        try:
            # Get current step
            current_step = engine.get_current_step()
            if not current_step:
                return {
                    "status": "error",
                    "message": "沒有當前步驟可以修改"
                }
            
            # Apply modifications to session data
            session = self.session_manager.get_session(session_id)
            if session:
                for key, value in modifications.items():
                    session.set_data(key, value)
                
                debug_log(2, f"[SYS] 已應用修改: {modifications}")
            
            # Re-execute the current step
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                engine.process_input,
                ""  # Empty input to trigger re-execution
            )
            
            return {
                "status": "success",
                "message": "步驟已修改並重新執行",
                "data": result.to_dict()
            }
            
        except Exception as e:
            error_log(f"[SYS] 修改步驟失敗: {e}")
            return {
                "status": "error",
                "message": f"修改步驟失敗: {str(e)}"
            }
    
    async def cancel_workflow_async(self, session_id: str, reason: str = "使用者取消") -> dict:
        """
        Async wrapper for cancelling a workflow (for MCP Server)
        
        Args:
            session_id: Workflow session ID
            reason: Reason for cancellation
            
        Returns:
            Dict with status and cancellation message
        """
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            self._cancel_workflow,
            session_id,
            reason
        )
        return result
    
    async def handle_llm_review_response_async(self, session_id: str, action: str, modified_params: dict = None) -> dict:
        """
        處理 LLM 審核響應（異步方法供 MCP Server 調用）
        
        Args:
            session_id: 工作流會話 ID
            action: LLM 決策 ('approve', 'modify', 'cancel')
            modified_params: 修改的參數（當 action='modify' 時）
            
        Returns:
            包含狀態和結果的字典
        """
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            self._handle_llm_review_response,
            session_id,
            action,
            modified_params
        )
        return result
    
    def _handle_llm_review_response(self, session_id: str, action: str, modified_params: dict = None) -> dict:
        """
        處理 LLM 審核響應（同步方法）
        
        Args:
            session_id: 工作流會話 ID
            action: LLM 決策 ('approve', 'modify', 'cancel')
            modified_params: 修改的參數（當 action='modify' 時）
            
        Returns:
            包含狀態和結果的字典
        """
        # 檢查會話是否存在
        engine = self.workflow_engines.get(session_id)
        if not engine:
            return {
                "status": "error",
                "message": f"找不到工作流程引擎 ID: {session_id}"
            }
        
        # 檢查引擎是否正在等待 LLM 審核
        if not engine.is_awaiting_llm_review():
            return {
                "status": "error",
                "message": "當前工作流沒有待審核的步驟"
            }
        
        try:
            # 調用引擎的 LLM 審核響應處理方法
            result = engine.handle_llm_review_response(action, modified_params)
            
            if result.cancel:
                # 工作流被取消
                self.session_manager.end_session(session_id, reason="LLM 取消工作流")
                del self.workflow_engines[session_id]
                
                return {
                    "status": "cancelled",
                    "message": result.message,
                    "data": result.to_dict()
                }
            elif result.complete:
                # 工作流完成
                self.session_manager.end_session(session_id, reason="工作流正常完成")
                del self.workflow_engines[session_id]
                
                return {
                    "status": "completed",
                    "message": result.message,
                    "data": result.to_dict()
                }
            elif result.success:
                # 步驟成功，繼續工作流
                current_step = engine.get_current_step()
                if current_step:
                    return {
                        "status": "success",
                        "requires_input": current_step.step_type == current_step.STEP_TYPE_INTERACTIVE,
                        "prompt": engine.get_prompt() if current_step else "工作流程已完成",
                        "message": result.message,
                        "data": {
                            "workflow_type": engine.definition.workflow_type,
                            "current_step": current_step.id,
                            **result.data
                        }
                    }
                else:
                    # 工作流已完成
                    self.session_manager.end_session(session_id, reason="工作流正常完成")
                    del self.workflow_engines[session_id]
                    
                    return {
                        "status": "completed",
                        "message": "工作流程已完成",
                        "data": result.to_dict()
                    }
            else:
                # 處理失敗
                return {
                    "status": "error",
                    "message": result.message,
                    "data": result.to_dict()
                }
            
        except Exception as e:
            error_log(f"[SYS] 處理 LLM 審核響應失敗: {e}")
            return {
                "status": "error",
                "message": f"處理 LLM 審核響應失敗: {str(e)}"
            }
    
    def get_mcp_server(self):
        """Get the MCP Server instance"""
        return self.mcp_server
