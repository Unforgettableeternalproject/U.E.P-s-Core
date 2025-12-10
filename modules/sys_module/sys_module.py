import os
import time
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

import yaml
from core.bases.module_base import BaseModule
from core.event_bus import SystemEvent
from configs.config_loader import load_module_config
from utils.debug_helper import info_log, error_log, debug_log
from .schemas import SYSInput, SYSOutput, SessionInfo, SessionDetail

from .actions.window_control   import push_window, fold_window, switch_workspace, screenshot_and_annotate
from .actions.text_processing  import clipboard_tracker, quick_phrases, ocr_extract
from .actions.automation_helper import set_reminder, generate_backup_script, monitor_folder
from .actions.integrations import news_summary, get_weather, get_world_time, code_analysis
from .actions.automation_helper import media_control, local_calendar
from .actions.file_interaction import clean_trash_bin

# Import permission manager
from .permission_manager import get_permission_manager, PermissionType

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

# Import text workflows
from .workflows.text_workflows import (
    create_text_workflow,
    get_available_text_workflows
)

# Import analysis workflows
from .workflows.analysis_workflows import (
    create_analysis_workflow,
    get_available_analysis_workflows
)

# Import info workflows
from .workflows.info_workflows import (
    create_info_workflow,
    get_available_info_workflows
)

# Import utility workflows
from .workflows.utility_workflows import (
    create_utility_workflow,
    get_available_utility_workflows
)

# Import automation workflows
from .workflows.automation_workflows import (
    get_automation_workflow_creator
)

class SYSModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or load_module_config("sys_module")
        self.enabled_modes = set(self.config.get("modes", []))
        self._function_specs = None
        self.session_manager = session_manager
        # Custom session storage for engines
        self.workflow_engines = {}  # session_id -> engine mapping
        
        # ✅ 線程池用於非同步執行工作流步驟
        self.workflow_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="WorkflowExec")
        
        # ✅ Event bus 引用（將在 initialize 時設置）
        self.event_bus = None
        
        # Initialize MCP Server
        self.mcp_server = MCPServer(sys_module=self)
        debug_log(2, "[SYS] MCP Server 已初始化")
        
        # 🔧 獲取權限管理器實例
        self.permission_manager = get_permission_manager()
        
        # 🔧 註冊 user_settings 熱重載回調
        from configs.user_settings_manager import user_settings_manager
        user_settings_manager.register_reload_callback("sys_module", self._reload_from_user_settings)

    def initialize(self):
        # 註冊 WORK_SYS 協作管道的資料提供者
        self._register_collaboration_providers()
        
        # ✅ 獲取 event_bus 引用並訂閱會話事件
        try:
            from core.event_bus import event_bus, SystemEvent
            self.event_bus = event_bus
            
            # 訂閱 SESSION_ENDED 事件以清理 workflow_engine
            event_bus.subscribe(SystemEvent.SESSION_ENDED, self._on_session_ended)
            
            debug_log(2, "[SYS] Event bus 已連接，已訂閱 SESSION_ENDED 事件")
        except Exception as e:
            error_log(f"[SYS] 無法連接 event bus: {e}")
        
        # Register Phase 2 workflows to MCP
        self._register_workflows_to_mcp()
        
        # Register MEM module memory tools to MCP
        self._register_memory_tools_to_mcp()
        
        # 恢復暫停的監控任務
        self._restore_monitoring_tasks()
        
        info_log("[SYS] 初始化完成，啟用模式：" + ", ".join(self.enabled_modes))
        return True
    
    def _on_session_ended(self, event):
        """處理 SESSION_ENDED 事件 - 清理 workflow_engine"""
        try:
            session_id = event.data.get('session_id')
            session_type = event.data.get('session_type')
            
            # 只處理 workflow 類型的會話
            if session_type == 'workflow' and session_id in self.workflow_engines:
                debug_log(2, f"[SYS] 清理 workflow_engine: {session_id}")
                del self.workflow_engines[session_id]
                debug_log(1, f"[SYS] ✅ 已清理 WS {session_id} 的 engine")
        except Exception as e:
            error_log(f"[SYS] 處理 SESSION_ENDED 事件失敗: {e}")
    
    def _restore_monitoring_tasks(self):
        """恢復暫停的背景監控任務"""
        try:
            from modules.sys_module.actions.automation_helper import get_monitoring_pool
            from modules.sys_module.workflows.automation_workflows import get_automation_workflow_creator
            
            info_log("[SYS] 正在檢查暫停的背景監控任務...")
            
            monitoring_pool = get_monitoring_pool()
            
            # 創建監控函數工廠
            def monitor_factory(workflow_type: str, metadata: dict):
                """根據工作流類型重新建立監控函數"""
                try:
                    # 目前主要支持 MediaPlayback 工作流的監控
                    if workflow_type == "MediaPlayback":
                        # 從 metadata 恢復監控邏輯
                        # 注意：這裡只是示例，實際的監控邏輯需要根據工作流類型實現
                        info_log(f"[SYS] 恢復 MediaPlayback 監控: {metadata}")
                        # TODO: 實現具體的監控函數
                        return None  # 暫時返回 None，表示不支持恢復
                    else:
                        debug_log(2, f"[SYS] 不支持恢復的工作流類型: {workflow_type}")
                        return None
                except Exception as e:
                    error_log(f"[SYS] 建立監控函數失敗: {e}")
                    return None
            
            # 調用 restore_monitors
            report = monitoring_pool.restore_monitors(monitor_factory)
            
            if report["restored_count"] > 0:
                info_log(f"[SYS] ✅ 已恢復 {report['restored_count']} 個監控任務")
            if report["failed_count"] > 0:
                info_log(f"[SYS] ⚠️ {report['failed_count']} 個監控任務恢復失敗")
            
            if report["restored_count"] == 0 and report["failed_count"] == 0:
                debug_log(2, "[SYS] 沒有需要恢復的監控任務")
                
        except Exception as e:
            error_log(f"[SYS] 恢復監控任務失敗: {e}")
    
    def shutdown(self):
        """關閉 sys_module，暫停所有監控任務"""
        try:
            from modules.sys_module.actions.automation_helper import get_monitoring_pool
            
            info_log("[SYS] 正在關閉模組，暫停所有監控任務...")
            
            monitoring_pool = get_monitoring_pool()
            
            # 停止所有監控任務（會自動標記為 SUSPENDED）
            active_count = len(monitoring_pool.active_monitors)
            if active_count > 0:
                monitoring_pool.stop_all_monitors(timeout=5)
                info_log(f"[SYS] ✅ 已暫停 {active_count} 個監控任務")
            
            # 關閉線程池
            monitoring_pool.shutdown(wait=False, timeout=5)
            
            info_log("[SYS] 模組已關閉")
            
        except Exception as e:
            error_log(f"[SYS] 關閉模組失敗: {e}")
    
    def _apply_parameter_inference(self, initial_params: Dict[str, Any], 
                                   initial_data: Dict[str, Any], 
                                   session: WorkflowSession):
        """
        根據 YAML 中的 infer_from 規則自動推斷缺失參數
        
        Args:
            initial_params: YAML 中的 initial_params 定義
            initial_data: 用戶提供的初始資料
            session: 工作流會話
        """
        try:
            for param_name, param_def in initial_params.items():
                # 跳過已提供的參數
                if param_name in initial_data:
                    continue
                
                # 檢查是否有推斷規則
                infer_rules = param_def.get("infer_from", [])
                if not infer_rules:
                    continue
                
                # 應用每個推斷規則
                for rule in infer_rules:
                    source_param = rule.get("param")
                    condition = rule.get("condition")
                    inferred_value = rule.get("value")
                    reason = rule.get("reason", "")
                    
                    # 檢查條件
                    if condition == "exists" and source_param in initial_data:
                        # 推斷參數並添加到 session
                        target_step = param_def.get("maps_to_step", param_name)
                        session.add_data(target_step, inferred_value)
                        debug_log(
                            2,
                            f"[SYS] 從 {source_param} 推斷 {param_name}={inferred_value} → {target_step}"
                            + (f" ({reason})" if reason else "")
                        )
                        break  # 找到第一個匹配的規則後停止
                        
        except Exception as e:
            error_log(f"[SYS] 參數推斷失敗: {e}")
    
    def debug(self):
        # Debug level = 1
        debug_log(1, "[SYS] Debug 模式啟用")
        # Debug level = 2
        debug_log(2, f"[SYS] 啟用模式: {self.enabled_modes}")
        # Debug level = 3
        debug_log(3, f"[SYS] 模組設定: {self.config}")


    def _load_function_specs(self):
        """
        ⚠️ 已棄用：functions.yaml 不再使用
        現在所有工作流都透過 workflow_definition 經 MCP 註冊成為工具
        保留此方法以維持向後兼容性，但返回空字典
        """
        if self._function_specs is None:
            debug_log(3, "[SYS] functions.yaml 已棄用，返回空規格")
            self._function_specs = {}
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
            # Don't fail initialization if LLM module is not available
            debug_log(2, f"[SYS] ⚠️  協作管道提供者註冊跳過 (LLM 模組不可用): {e}")
    
    def _register_workflows_to_mcp(self):
        """Register workflows to MCP Server using centralized registry"""
        info_log("[SYS] Registering workflows to MCP Server...")
        
        # 使用集中式工作流註冊器
        from .workflows.workflow_registry import register_all_workflows
        register_all_workflows(self.mcp_server, self)
        
        info_log("[SYS] ✅ Workflows registered to MCP Server.")
    
    def _register_memory_tools_to_mcp(self):
        """Register MEM module memory tools to MCP Server"""
        try:
            from core import registry
            mem_module = registry.get_loaded('mem_module')
            
            if mem_module and hasattr(mem_module, 'register_memory_tools_to_mcp'):
                info_log("[SYS] Registering memory tools to MCP Server...")
                mem_module.register_memory_tools_to_mcp(self.mcp_server)
            else:
                debug_log(2, "[SYS] ⚠️  MEM 模組不可用或不支援 MCP 工具註冊")
        except Exception as e:
            error_log(f"[SYS] 註冊記憶工具失敗: {e}")
            import traceback
            traceback.print_exc()
    
    def query_function_info(self, query_text: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Query available functions and their information (for NLP module)
        
        Args:
            query_text: Text to search for relevant functions
            top_k: Number of top results to return
            
        Returns:
            List of function info dictionaries containing:
            - name: Function name
            - description: Function description
            - work_mode: "direct" or "background"
            - keywords: List of keywords
            - relevance_score: Matching score (0-1)
        """
        try:
            from .workflows.file_workflows import get_file_workflows_info
            
            results = []
            query_lower = query_text.lower()
            query_words = set(query_lower.split())  # Split into words for better matching
            
            # Get all workflow information
            all_workflows = get_file_workflows_info()
            
            # Search and score workflows
            for wf_info in all_workflows:
                workflow_name = wf_info.get('workflow_type', '')
                description = wf_info.get('description', '')
                work_mode = wf_info.get('work_mode', 'direct')
                keywords = wf_info.get('keywords', [])
                
                # Calculate relevance score
                score = 0.0
                matched_keywords = []
                
                # Check name match (highest priority)
                if query_lower in workflow_name.lower():
                    score += 0.5
                
                # Check description match
                if query_lower in description.lower():
                    score += 0.3
                
                # Check keyword matches (word-level matching)
                for keyword in keywords:
                    keyword_lower = keyword.lower()
                    # Match if keyword appears in query or query word matches keyword
                    if keyword_lower in query_lower or any(word in keyword_lower or keyword_lower in word for word in query_words):
                        score += 0.15
                        matched_keywords.append(keyword)
                
                # Cap score at 1.0
                score = min(score, 1.0)
                
                if score > 0:
                    debug_log(3, f"[SYS] Matched workflow: {workflow_name} (score={score:.2f}, keywords={matched_keywords})")
                    results.append({
                        'name': workflow_name,
                        'description': description,
                        'work_mode': work_mode,
                        'keywords': keywords,
                        'relevance_score': score
                    })
            
            # Sort by relevance and return top K
            results.sort(key=lambda x: x['relevance_score'], reverse=True)
            top_results = results[:top_k]
            
            debug_log(2, f"[SYS] Query '{query_text}' found {len(results)} matches, top score: {top_results[0]['relevance_score']:.2f} ({top_results[0]['name']})" if top_results else "[SYS] No matches found")
            
            return top_results
            
        except Exception as e:
            error_log(f"[SYS] Query function info failed: {e}")
            return []
    
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
        """提供可用的系統功能列表給 LLM（從 MCP Server 獲取）"""
        try:
            if not self.mcp_server:
                debug_log(2, "[SYS] MCP Server 未初始化，無法提供功能列表")
                return []
            
            category = kwargs.get('category', 'all')
            
            # 從 MCP Server 獲取已註冊的工具
            functions = []
            
            # 獲取所有已註冊的工具
            registered_tools = self.mcp_server.list_tools()
            
            for tool in registered_tools:
                # MCPTool 是 Pydantic 模型，使用屬性訪問
                tool_name = tool.name if hasattr(tool, 'name') else ''
                tool_description = tool.description if hasattr(tool, 'description') else ''
                
                # 提取參數列表（從 parameters 欄位）
                params = []
                if hasattr(tool, 'parameters') and tool.parameters:
                    params = [param.name for param in tool.parameters]
                
                # 簡單的分類邏輯（基於工具名稱前綴）
                tool_category = 'general'
                if tool_name.startswith('file_'):
                    tool_category = 'file_operations'
                elif tool_name.startswith('workflow_'):
                    tool_category = 'workflow_management'
                elif 'step' in tool_name:
                    tool_category = 'workflow_management'
                
                # 根據分類過濾
                if category == 'all' or tool_category == category:
                    functions.append({
                        "name": tool_name,
                        "category": tool_category,
                        "description": tool_description,
                        "params": params
                    })
            
            debug_log(2, f"[SYS] 提供 {len(functions)} 個功能給 LLM (category={category})")
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
    
    def _get_step_info_for_llm(self, engine, workflow_type: str):
        """
        Extract step information for LLM context
        
        Args:
            engine: Workflow engine instance
            workflow_type: Type of the workflow
            
        Returns:
            dict with current_step, workflow_info, and upcoming_steps overview
        """
        current_step = engine.get_current_step()
        workflow_def = engine.definition
        
        step_info = {}
        
        if current_step:
            step_info["current_step"] = {
                "step_id": current_step.id,
                "step_type": current_step.step_type,
                "prompt": current_step.get_prompt(),
                "description": getattr(current_step, "_description", ""),
                "auto_advance": current_step.should_auto_advance(),  # 使用方法而非屬性
                "priority": current_step.priority,  # 使用 priority 而非 optional
                "optional": current_step.priority == "optional"  # 計算 optional 狀態
            }
        else:
            step_info["current_step"] = None
            
        step_info["workflow_info"] = {
            "workflow_type": workflow_type,
            "name": workflow_def.name,
            "description": workflow_def.description
        }
        
        # 🆕 添加後續可能需要交互的步驟概覽（給 LLM 提供完整流程預期）
        upcoming_interactive_steps = []
        if workflow_def and current_step:
            # 獲取當前步驟之後的所有步驟
            current_step_found = False
            for step in workflow_def.steps.values():
                if current_step_found and step.step_type == step.STEP_TYPE_INTERACTIVE:
                    upcoming_interactive_steps.append({
                        "step_id": step.id,
                        "description": getattr(step, 'description', ''),
                        "prompt_preview": step.get_prompt()[:100] if hasattr(step, 'get_prompt') else ''
                    })
                if step.id == current_step.id:
                    current_step_found = True
        
        step_info["upcoming_interactive_steps"] = upcoming_interactive_steps
        
        return step_info
    
    def _start_workflow(self, workflow_type: str, command: str, initial_data=None):
        """
        Start a new workflow session using the unified workflow engine
        
        Args:
            workflow_type: The type of workflow (test workflows: echo, countdown, data_collector, random_fail, tts_test; file workflows: drop_and_read, intelligent_archive, summarize_tag, translate_document, etc.)
            command: The original command that triggered this workflow
            initial_data: Initial data for the workflow
            
        Returns:
            A dict with session info and first step prompt
        """
        debug_log(1, f"[SYS] 啟動統一工作流程: {workflow_type}")
        
        # ✅ 優先使用已存在的 WS（由 StateManager 在進入 WORK 狀態時創建）
        active_ws_ids = self.session_manager.get_active_workflow_session_ids()
        session = None
        session_id = None
        
        if active_ws_ids:
            # 使用第一個活躍的 WS（架構上同時只會有一個）
            session_id = active_ws_ids[0]
            session = self.session_manager.get_workflow_session(session_id)
            if session:
                debug_log(1, f"[SYS] 使用已存在的工作流會話: {session_id}")
                # 更新會話的工作流類型信息（如果需要）
                if not session.get_data("workflow_type"):
                    session.add_data("workflow_type", workflow_type)
                if not session.get_data("command"):
                    session.add_data("command", command)
        
        # 如果沒有活躍的 WS，才創建新的（向後兼容舊代碼或獨立調用）
        if not session:
            debug_log(2, f"[SYS] 沒有活躍的工作流會話，創建新會話")
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
        
        # 將 initial_data 添加到 session，根據 YAML 的 maps_to_step 映射參數名
        if initial_data:
            # 載入 workflow_definitions.yaml 獲取參數映射
            try:
                from pathlib import Path
                import yaml
                yaml_path = Path(__file__).parent / "workflows" / "workflow_definitions.yaml"
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    workflow_defs = yaml.safe_load(f).get('workflows', {})
                
                # 獲取當前工作流的參數映射
                workflow_def = workflow_defs.get(workflow_type, {})
                initial_params = workflow_def.get('initial_params', {})
                
                # 根據 maps_to_step 映射參數名
                for key, value in initial_data.items():
                    param_def = initial_params.get(key, {})
                    target_step = param_def.get('maps_to_step', key)  # 默認使用原始 key
                    
                    # 🔧 將所有值轉換為字符串，確保 validator 能正確處理
                    # 因為工作流步驟的 validator 假設輸入是字符串
                    value_str = str(value) if value is not None else ""
                    session.add_data(target_step, value_str)
                    debug_log(2, f"[SYS] initial_data: {key} -> {target_step} = {value_str} (原始類型: {type(value).__name__})")
                
                # 🔧 參數推斷：根據 infer_from 規則自動推斷缺失參數
                self._apply_parameter_inference(initial_params, initial_data, session)
                
                debug_log(2, f"[SYS] 已將 initial_data 映射到 session: {list(initial_data.keys())}")
            except Exception as e:
                # 降級處理：直接使用原始 key
                debug_log(1, f"[SYS] 無法載入工作流定義進行參數映射: {e}")
                for key, value in initial_data.items():
                    # 🔧 同樣轉換為字符串
                    value_str = str(value) if value is not None else ""
                    session.add_data(key, value_str)
                debug_log(2, f"[SYS] 已將 initial_data 添加到 session（降級模式）: {list(initial_data.keys())}")
        
        try:
            # Determine workflow engine based on type
            engine = None
            
            # Test workflows
            if workflow_type in ["echo", "countdown", "data_collector", "random_fail", "tts_test"]:
                # Get required modules for test workflows
                llm_module = None
                
                try:
                    from modules.llm_module.llm_module import LLMModule
                    from configs.config_loader import load_module_config
                    config = load_module_config("llm_module")
                    # 禁用隱性快取，避免測試影響系統快取
                    if "use_prompt_caching" in config:
                        config["use_prompt_caching"] = False
                    llm_module = LLMModule(config)
                    debug_log(2, f"[SYS] 已獲取LLM模組實例（測試模式，已禁用快取）")
                except Exception as e:
                    debug_log(2, f"[SYS] 無法獲取LLM模組: {e}")
                
                engine = create_test_workflow(workflow_type, session, llm_module=llm_module)
                
            # File workflows
            elif workflow_type in ["drop_and_read", "intelligent_archive", "summarize_tag", "translate_document", "ocr_extract"]:
                engine = create_file_workflow(workflow_type, session)
            
            # Text workflows
            elif workflow_type in get_available_text_workflows():
                engine = create_text_workflow(workflow_type, session)
            
            # Analysis workflows
            elif workflow_type in get_available_analysis_workflows():
                engine = create_analysis_workflow(workflow_type, session)
            
            # Info workflows
            elif workflow_type in get_available_info_workflows():
                engine = create_info_workflow(workflow_type, session)
            
            # Utility workflows
            elif workflow_type in get_available_utility_workflows():
                engine = create_utility_workflow(workflow_type, session)
            
            # Automation workflows (background services)
            else:
                # 嘗試從 automation workflows 中獲取創建函數
                creator = get_automation_workflow_creator(workflow_type)
                if creator:
                    # 解析 initial_data 中的參數並傳遞給創建函數
                    workflow_params = initial_data.copy() if initial_data else {}
                    
                    # 從 workflow_params 中提取所有可能的參數
                    workflow_def = creator(
                        session=session,
                        **workflow_params  # 使用字典解包傳遞所有參數
                    )
                    
                    # 創建 WorkflowEngine
                    engine = WorkflowEngine(workflow_def, session)
                    engine.auto_advance = True  # ✅ 啟用自動推進
                else:
                    # Unknown workflow type
                    return {
                        "status": "error",
                        "message": f"未知的工作流程類型: {workflow_type}。可用的工作流: {', '.join(get_available_file_workflows() + get_available_text_workflows() + get_available_analysis_workflows() + get_available_info_workflows() + get_available_utility_workflows())}"
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
            
            # 如果是背景模式，提交到 SYS 模組的背景工作流執行器
            if workflow_mode == WorkflowMode.BACKGROUND:
                try:
                    from modules.sys_module.workflow_executor import get_workflow_executor
                    executor = get_workflow_executor()
                    
                    # 提交背景任務
                    task_id = executor.submit_workflow(
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
                    
                    # ✅ 背景工作流已提交，標記 Session 待結束（符合雙條件終止機制）
                    # 條件 1: 外部中斷點（工作已完成） - 設置 pending_end
                    # 條件 2: 循環結束 - 由 Controller 在 CYCLE_COMPLETED 時檢查並執行
                    session = self.session_manager.get_workflow_session(session_id)
                    if session:
                        session.pending_end = True
                        session.pending_end_reason = "背景工作流已提交，MCP session 完成"
                        debug_log(1, f"[SYS] 📋 標記 WS 待結束: {session_id}（等待循環完成）")
                    
                    return {
                        "status": "submitted",
                        "session_id": session_id,
                        "task_id": task_id,
                        "message": (
                            f"Got it, working on it!\n\n"
                            f"Respond naturally like you're talking to a friend - just 1-3 words.\n"
                            f"Examples: 'Sure~', 'Yep!', 'Okay!', 'On it~', 'Got it~'\n"
                            f"No need to say anything professional or explain what you're doing."
                        ),
                        "data": {
                            "workflow_type": workflow_type,
                            "workflow_mode": "background",
                            "task_id": task_id
                        }
                    }
                    
                except Exception as e:
                    error_log(f"[SYS] 提交背景工作流程失敗: {e}")
                    # 清理 workflow engine（WS 交給 LLM 標記後由 Controller 結束）
                    if session_id in self.workflow_engines:
                        del self.workflow_engines[session_id]
                    return {
                        "status": "error",
                        "message": f"提交背景工作流程失敗: {e}"
                    }
            
            # 直接模式：非同步執行，立即返回
            # Store engine separately and register session in SessionManager
            self.workflow_engines[session_id] = engine
            # Session is already registered in SessionManager via create_session
            
            # 🆕 找到「等效第一步」（Effective First Step）
            # 重要：不執行步驟，只是找到第一個需要處理的步驟
            # 但如果工作流可以自動完成（無需用戶輸入），則讓它完成並保存數據
            debug_log(2, "[SYS] 尋找等效第一步...")
            try:
                # 🔧 循環執行 process_input(None) 直到遇到真正需要用戶輸入的步驟
                # 這樣可以跳過所有可以自動執行/跳過的步驟（包括有 initial_data 的 Interactive 步驟）
                max_iterations = 10  # 防止無限循環
                iteration = 0
                step_result = None
                
                # 🔧 設置標記：正在查找等效第一步，禁用事件發布
                engine.finding_effective_first_step = True
                
                while iteration < max_iterations:
                    current_step = engine.get_current_step()
                    if not current_step:
                        debug_log(2, "[SYS] 工作流已完成，無當前步驟")
                        break
                    
                    # 保存舊步驟ID用於後續檢測步驟是否改變
                    old_step_id = current_step.id
                    
                    # 檢查是否是可跳過的 Interactive 步驟
                    is_interactive = current_step.step_type == current_step.STEP_TYPE_INTERACTIVE
                    can_skip = is_interactive and hasattr(current_step, 'should_skip') and current_step.should_skip()
                    
                    debug_log(2, f"[SYS] 檢查步驟 {current_step.id} (類型: {current_step.step_type}, can_skip: {can_skip})")
                    
                    # 🔧 如果不能跳過，這就是等效第一步，停止循環（不執行）
                    if is_interactive and not can_skip:
                        debug_log(2, f"[SYS] 找到需要用戶輸入的步驟: {current_step.id}")
                        break
                    
                    # 🔧 可以跳過或自動執行的步驟，執行它並繼續
                    debug_log(2, f"[SYS] 步驟 {current_step.id} 將被跳過或自動執行")
                    
                    # 🔧 清除 awaiting_llm_review 和 waiting_for_input 標記
                    # 避免阻塞後續步驟的執行和發布不必要的事件
                    # 因為我們只是在尋找等效第一步，不需要真的等待 LLM 審核或用戶輸入
                    engine.awaiting_llm_review = False
                    engine.waiting_for_input = False
                    
                    step_result = engine.process_input(None)
                    iteration += 1
                    
                    # 🔧 檢查 step_result.skip_to（ConditionalStep 可能返回跳轉目標）
                    if step_result and hasattr(step_result, 'skip_to') and step_result.skip_to:
                        debug_log(2, f"[SYS] 檢測到跳轉目標: {step_result.skip_to}")
                        # ConditionalStep 返回了需要跳轉的步驟，繼續循環
                        continue
                    
                    # 如果步驟沒有改變，也停止（避免卡住）
                    new_current_step = engine.get_current_step()
                    if new_current_step and new_current_step.id == old_step_id:
                        debug_log(2, f"[SYS] 步驟未改變，停止循環: {old_step_id}")
                        break
                
                # 🔧 清除標記：查找完成，恢復正常事件發布
                engine.finding_effective_first_step = False
                
                debug_log(2, f"[SYS] 等效第一步查找完成 (迭代次數: {iteration})")
            except Exception as e:
                debug_log(1, f"[SYS] 等效第一步查找失敗: {e}")
                import traceback
                debug_log(1, f"[SYS] 錯誤堆棧: {traceback.format_exc()}")
                step_result = None
            
            # ✅ 獲取當前步驟（這才是真正的「等效第一步」）
            current_step = engine.get_current_step()
            
            # ✅ 立即返回「已啟動」狀態
            info_log(f"[SYS] 已啟動統一工作流程 '{workflow_type}', ID: {session_id}")
            if current_step:
                info_log(f"[SYS] 等效第一步: {current_step.id} (類型: {current_step.step_type})")
                
                # 🔧 如果等效第一步是 Interactive，需要發布 WORKFLOW_REQUIRES_INPUT 事件
                # 因為在查找過程中我們禁用了事件發布
                if current_step.step_type == current_step.STEP_TYPE_INTERACTIVE:
                    try:
                        from core.event_bus import event_bus, SystemEvent
                        event_bus.publish(
                            SystemEvent.WORKFLOW_REQUIRES_INPUT,
                            {
                                "workflow_type": workflow_type,
                                "session_id": session_id,
                                "step_id": current_step.id,
                                "step_type": current_step.step_type,
                                "optional": getattr(current_step, 'optional', False),
                                "prompt": current_step.get_prompt(),
                                "timestamp": time.time()
                            },
                            source="sys"
                        )
                        debug_log(2, f"[SYS] 已為等效第一步發布 WORKFLOW_REQUIRES_INPUT 事件: {current_step.id}")
                    except Exception as e:
                        debug_log(1, f"[SYS] 發布輸入請求事件失敗: {e}")
            
            # 🔧 根據等效第一步的類型，決定後續處理方式
            # 重要：process_input(None) 已經執行過了，所以：
            # - 如果等效第一步是 INTERACTIVE，已經停在那裡等待輸入，不需要背景執行
            # - 如果等效第一步是 PROCESSING，_auto_advance 已經停在那裡，需要背景執行
            if current_step:
                should_execute_in_background = False
                
                if current_step.step_type == current_step.STEP_TYPE_SYSTEM:
                    # SYSTEM 步驟需要背景執行（如檔案對話框）
                    debug_log(2, f"[SYS] 等效第一步是系統操作，提交到背景執行: {current_step.id}")
                    should_execute_in_background = True
                elif current_step.step_type == current_step.STEP_TYPE_PROCESSING:
                    # PROCESSING 步驟提交到背景執行
                    # 注意：這裡不是重新執行 process_input，而是讓背景線程繼續執行
                    debug_log(2, f"[SYS] 等效第一步是處理步驟，提交到背景執行: {current_step.id}")
                    should_execute_in_background = True
                elif current_step.step_type == current_step.STEP_TYPE_INTERACTIVE:
                    # Interactive 步驟：process_input(None) 已經發布了 WORKFLOW_REQUIRES_INPUT 事件
                    # 不需要背景執行，等待用戶輸入
                    debug_log(2, f"[SYS] 等效第一步是互動步驟，已發布輸入請求事件，等待用戶輸入: {current_step.id}")
                else:
                    # 其他類型
                    debug_log(2, f"[SYS] 等效第一步是 {current_step.step_type}，提交到背景執行: {current_step.id}")
                    should_execute_in_background = True
                
                # 如果需要背景執行，提交到背景執行器
                if should_execute_in_background:
                    self.workflow_executor.submit(self._execute_workflow_step_background, session_id, workflow_type)
            else:
                # 沒有當前步驟，工作流已經完成（所有步驟都執行完畢）
                debug_log(2, f"[SYS] 工作流在啟動時已自動完成（所有互動步驟都已跳過）")
                
                # 🔧 發布 WORKFLOW_STEP_COMPLETED 事件，讓 LLM 生成最終回應
                executed_step_ids = []
                final_result_data = {}
                
                if self.event_bus:
                    from core.event_bus import SystemEvent
                    session = self.session_manager.get_session(session_id)
                    step_history = session.get_data("step_history", []) if session else []
                    executed_step_ids = [step["step_id"] for step in step_history] if step_history else []
                    
                    # 🔧 優先從 step_result.data 獲取結果數據（最準確）
                    if step_result and hasattr(step_result, 'data') and step_result.data:
                        final_result_data = step_result.data.copy()
                        debug_log(2, f"[SYS] 從 step_result 獲取最終結果數據，鍵: {list(final_result_data.keys())}")
                    
                    # 🔧 補充從 session 中獲取可能遺漏的數據
                    if session:
                        for key in ["time_info", "full_result", "result_data", "output", "news_list", "source", "count", "weather_info", "weather_data", "location"]:
                            if key not in final_result_data:  # 只補充不存在的鍵
                                value = session.get_data(key)
                                if value:
                                    final_result_data[key] = value
                    
                    event_data = {
                        "session_id": session_id,
                        "workflow_type": workflow_type,
                        "step_result": {
                            "success": True,
                            "complete": True,
                            "message": "Workflow completed automatically",
                            "data": final_result_data
                        },
                        "executed_steps": executed_step_ids,
                        # 🔧 不設置 requires_llm_review，避免雙重回應
                        # 工作流完成事件會觸發 LLM 的 _process_workflow_completion，生成最終總結
                        "llm_review_data": {
                            "requires_user_response": True,
                            "should_end_session": True,
                        },
                        "next_step_info": None  # 工作流已完成
                    }
                    
                    self.event_bus.publish(
                        event_type=SystemEvent.WORKFLOW_STEP_COMPLETED,
                        data=event_data,
                        source="sys"
                    )
                    debug_log(2, f"[SYS] 已發布 workflow_step_completed 事件（工作流自動完成）: {session_id}")
                
                # 返回啟動狀態（auto_continue=True），讓 LLM 跳過初始回應
                # LLM 會等待 workflow_step_completed 事件來生成最終總結
                return {
                    "status": "started",
                    "success": True,
                    "session_id": session_id,
                    "workflow_type": workflow_type,
                    "requires_input": False,
                    "message": f"Workflow '{workflow_type}' started and will complete automatically",
                    "current_step_prompt": None,
                    "data": {
                        "workflow_type": workflow_type,
                        "current_step": None,
                        "step_type": None,
                        "completed": False,  # 尚未完成（從 LLM 的角度）
                        "requires_input": False,
                        "auto_continue": True,  # 🔧 關鍵：告訴 LLM 跳過初始回應
                        "executed_steps": executed_step_ids,
                        "final_result": final_result_data
                    }
                }
            
            # 判斷當前步驟是否會被跳過
            step_will_be_skipped = False
            if current_step and current_step.step_type == current_step.STEP_TYPE_INTERACTIVE:
                # 檢查是否設定了 skip_if_data_exists 且數據已存在
                if hasattr(current_step, 'should_skip') and current_step.should_skip():
                    step_will_be_skipped = True
                    debug_log(2, f"[SYS] 步驟 {current_step.id} 將被跳過（數據已存在）")
            
            # 判斷是否有自動步驟（SYSTEM 或 PROCESSING）和是否需要輸入
            has_auto_step = current_step and current_step.step_type in (
                current_step.STEP_TYPE_SYSTEM, 
                current_step.STEP_TYPE_PROCESSING
            )
            
            # 🔧 修正：當當前步驟會被跳過時，需要預測後續步驟是否需要輸入
            # 這對於 ConditionalStep 和自動推進的工作流很重要
            requires_input = False
            current_step_prompt = None
            
            if current_step:
                if step_will_be_skipped:
                    # 當前步驟會被跳過，檢查工作流是否有後續互動步驟
                    # 由於我們無法精確預測 ConditionalStep 的分支，保守做法是檢查工作流定義
                    # 如果工作流有任何互動步驟，標記為可能需要輸入
                    workflow_steps = engine.definition.steps.values() if engine else []
                    has_interactive_steps = any(
                        step.step_type == current_step.STEP_TYPE_INTERACTIVE 
                        for step in workflow_steps
                    )
                    # 🔧 如果工作流中有互動步驟（除了當前被跳過的），可能後續需要輸入
                    requires_input = has_interactive_steps
                else:
                    # 🆕 當前步驟不會被跳過，檢查是否為互動類型或 Processing 類型
                    if current_step.step_type == current_step.STEP_TYPE_INTERACTIVE:
                        requires_input = True
                        current_step_prompt = current_step.get_prompt()
                    elif current_step.step_type == current_step.STEP_TYPE_PROCESSING:
                        # 🔧 Processing 步驟（如 ConditionalStep）可能會跳轉到 Interactive 步驟
                        # 檢查工作流中是否有未滿足數據的 Interactive 步驟
                        workflow_steps = engine.definition.steps.values() if engine else []
                        for step in workflow_steps:
                            if step.step_type == step.STEP_TYPE_INTERACTIVE:
                                # 檢查步驟數據是否不存在
                                if hasattr(step, 'should_skip') and not step.should_skip():
                                    requires_input = True
                                    current_step_prompt = step.get_prompt()
                                    debug_log(2, f"[SYS] ConditionalStep 可能跳轉到互動步驟: {step.id}")
                                    break
                    debug_log(2, f"[SYS] 當前步驟 {current_step.id} (類型: {current_step.step_type}), requires_input={requires_input}, prompt={current_step_prompt}")
            
            # 🔧 auto_continue 應該只在確定所有步驟都會自動完成時為 True
            # 檢查是否所有 Interactive 步驟的數據都已存在
            auto_continue = False
            if engine and engine.definition and not requires_input:
                # 檢查所有 Interactive 步驟
                all_interactive_data_exists = True
                for step in engine.definition.steps.values():
                    if step.step_type == step.STEP_TYPE_INTERACTIVE:
                        # 檢查步驟是否應該被跳過（數據已存在）
                        if hasattr(step, 'should_skip') and not step.should_skip():
                            all_interactive_data_exists = False
                            break
                
                # 如果所有 Interactive 步驟的數據都存在，工作流會自動完成
                if all_interactive_data_exists:
                    auto_continue = True
                    debug_log(2, f"[SYS] 所有互動步驟數據已存在，工作流將自動完成")
            
            # 🆕 收集工作流的步驟概覽（給 LLM 提供完整流程信息）
            workflow_steps_overview = []
            if engine and engine.definition:
                for step in engine.definition.steps.values():
                    step_overview = {
                        "step_id": step.id,
                        "step_type": step.step_type,
                        "description": getattr(step, 'description', ''),
                    }
                    
                    # 對於 Interactive 步驟，添加提示預覽
                    if step.step_type == step.STEP_TYPE_INTERACTIVE:
                        step_overview["prompt"] = step.get_prompt() if hasattr(step, 'get_prompt') else ''
                        step_overview["optional"] = getattr(step, 'optional', False)
                    
                    workflow_steps_overview.append(step_overview)
            
            return {
                "status": "success",
                "success": True,
                "session_id": session_id,
                "workflow_type": workflow_type,
                "requires_input": requires_input,
                "message": f"Workflow '{workflow_type}' has been started",
                "current_step_prompt": current_step_prompt,
                "data": {
                    "workflow_type": workflow_type,
                    "current_step": current_step.id if current_step else None,
                    "step_type": current_step.step_type if current_step else None,
                    "has_auto_step": has_auto_step,
                    "requires_input": requires_input,
                    "step_will_be_skipped": step_will_be_skipped,
                    "auto_continue": auto_continue,  # 🔧 修正：更準確的判斷
                    "workflow_steps_overview": workflow_steps_overview,  # 🆕 完整步驟概覽
                    "effective_first_step": current_step.id if current_step else None,  # 🆕 明確標記等效第一步
                }
            }
            
        except Exception as e:
            error_log(f"[SYS] 創建統一工作流程引擎失敗: {e}")
            # 清理 workflow engine（WS 交給 LLM 標記後由 Controller 結束）
            if session_id in self.workflow_engines:
                del self.workflow_engines[session_id]
            return {
                "status": "error",
                "message": f"無法為 {workflow_type} 創建工作流程: {e}"
            }
    
    def _execute_workflow_step_background(self, session_id: str, workflow_type: str):
        """
        ✅ 在背景線程執行工作流步驟
        步驟完成後發布事件通知 LLM 審核
        
        Args:
            session_id: Workflow session ID
            workflow_type: Type of workflow
        """
        try:
            debug_log(2, f"[SYS] 背景執行工作流步驟: {session_id}")
            
            # Get engine
            engine = self.workflow_engines.get(session_id)
            if not engine:
                error_log(f"[SYS] 背景執行失敗：找不到引擎 {session_id}")
                return
            
            # 🔧 記錄執行前的步驟 ID（用於事件報告）
            current_step_before = engine.get_current_step()
            executed_step_id = current_step_before.id if current_step_before else None
            
            # 🔧 啟用自動推進，讓引擎根據步驟類型自動決定是否推進
            # 例如：Interactive 步驟跳過後應該自動推進到下一個 Processing 步驟
            original_auto_advance = engine.auto_advance
            engine.auto_advance = True
            
            # Execute step
            result = engine.process_input(None)
            
            # 🔧 恢復原始設置（但通常應該保持 True）
            engine.auto_advance = original_auto_advance
            
            # ✅ 發布事件通知步驟完成
            if self.event_bus:
                from core.event_bus import SystemEvent
                
                # 🔧 檢查是否為工作流完成且需要用戶回應
                llm_review_data = result.llm_review_data if hasattr(result, 'llm_review_data') else None
                is_workflow_complete = result.complete
                
                # 🔧 修正：工作流完成時總是需要 LLM 生成最終回應（向用戶報告結果）
                # 或者當引擎明確標記為等待審核時
                requires_llm_review = engine.is_awaiting_llm_review() or is_workflow_complete
                
                # 🚫 ConditionalStep 等 wrapper 步驟不發布事件（它們只是邏輯分支，不是真正的業務步驟）
                # 只有以下情況才發布事件：
                # 1. 工作流完成（需要 LLM 生成最終回應）
                # 2. 引擎明確要求 LLM 審核（awaiting_llm_review）
                if not requires_llm_review:
                    debug_log(2, f"[SYS] 步驟 {executed_step_id} 完成，但不需要 LLM 審核，跳過事件發布")
                    return
                
                # 🆕 獲取當前步驟和下一步資訊
                # 先獲取 session（後續需要用到）
                session = self.session_manager.get_session(session_id)
                current_step_info = None
                next_step_info = None
                if not is_workflow_complete:
                    # 獲取當前步驟資訊（可能是 Interactive 步驟在等待輸入）
                    current_step = engine.get_current_step()
                    if current_step:
                        # 🔧 檢查當前步驟是否會被跳過
                        step_will_be_skipped = False
                        if current_step.step_type == "interactive" and hasattr(current_step, 'should_skip'):
                            try:
                                step_will_be_skipped = current_step.should_skip()
                            except:
                                pass
                        
                        current_step_info = {
                            "step_id": session.get_data("current_step") if session else None,
                            "step_type": current_step.step_type,
                            "requires_input": current_step.step_type == "interactive",
                            "prompt": current_step.get_prompt() if current_step.step_type == "interactive" else None,
                            "step_will_be_skipped": step_will_be_skipped  # ⚠️ 重要：標記步驟是否會被跳過
                        }
                    # 預覽下一步
                    next_step_info = engine.peek_next_step()
                
                # 🆕 獲取完整的步驟歷史（用於測試驗證）
                step_history = session.get_data("step_history", []) if session else []
                executed_step_ids = [step["step_id"] for step in step_history] if step_history else []
                
                # 🔧 使用最新執行的步驟 ID
                # ⚠️ 重要：不能使用 executed_step_id（執行前記錄），因為在 approve_step 流程中，
                # 實際執行的步驟（如 execute_time_query）是在 handle_llm_review_response 中完成的，
                # 而事件發布在 _background_workflow_execution 中，此時 executed_step_id 仍是舊值
                # 必須從 step_history 獲取真正執行的步驟
                if executed_step_ids:
                    final_executed_step_id = executed_step_ids[-1]
                else:
                    # 沒有歷史記錄時，回退到執行前記錄的 ID（這種情況不應該發生）
                    final_executed_step_id = executed_step_id
                    debug_log(1, f"[SYS] ⚠️ 沒有 step_history，使用執行前記錄的 ID: {executed_step_id}")
                
                event_data = {
                    "session_id": session_id,
                    "workflow_type": workflow_type,
                    "step_result": {
                        "success": result.success,
                        "complete": result.complete,
                        "cancel": result.cancel,
                        "message": result.message,
                        "data": result.data,
                        "step_id": final_executed_step_id  # 🔧 使用最新執行的步驟 ID
                    },
                    "executed_steps": executed_step_ids,  # 🆕 添加所有執行的步驟 ID 列表
                    "requires_llm_review": requires_llm_review,
                    "llm_review_data": llm_review_data,
                    "current_step_info": current_step_info,  # 🆕 當前步驟資訊（可能是 Interactive）
                    "next_step_info": next_step_info  # 🆕 下一步資訊
                }
                
                # ✅ 使用正確的 publish 簽名：event_type, data, source
                self.event_bus.publish(
                    event_type=SystemEvent.WORKFLOW_STEP_COMPLETED,
                    data=event_data,
                    source="sys"
                )
                
                debug_log(2, f"[SYS] 已發布 workflow_step_completed 事件: {session_id}")
            
            # ✅ 處理完成/失敗狀態
            if result.complete:
                # 🔧 工作流完成時不要立即結束會話和清理引擎
                # 保留引擎讓 LLM 能生成最終回應，LLM 會在回應後通過 session_control 結束會話
                info_log(f"[SYS] 工作流步驟已完成: {session_id}, 等待 LLM 生成最終回應")
                
            elif not result.success and not engine.is_awaiting_llm_review():
                # ✅ 工作流失敗：發布事件讓 LLM 處理錯誤並通知用戶
                error_log(f"[SYS] 工作流執行失敗: {session_id} - {result.message}")
                
                if self.event_bus:
                    from core.event_bus import SystemEvent
                    
                    # 獲取步驟資訊
                    session = self.session_manager.get_session(session_id)
                    step_history = session.get_data("step_history", []) if session else []
                    executed_step_ids = [step["step_id"] for step in step_history] if step_history else []
                    final_executed_step_id = executed_step_ids[-1] if executed_step_ids else executed_step_id
                    
                    # 發布失敗事件，讓 LLM 生成錯誤回應並結束會話
                    event_data = {
                        "session_id": session_id,
                        "workflow_type": workflow_type,
                        "step_result": {
                            "success": False,
                            "complete": False,
                            "cancel": False,
                            "message": result.message,
                            "data": result.data,
                            "step_id": final_executed_step_id,
                            "error": True  # 標記為錯誤
                        },
                        "executed_steps": executed_step_ids,
                        "requires_llm_review": True,  # 需要 LLM 處理錯誤
                        "llm_review_data": None,
                        "current_step_info": None,
                        "next_step_info": None
                    }
                    
                    self.event_bus.publish(
                        event_type=SystemEvent.WORKFLOW_STEP_COMPLETED,
                        data=event_data,
                        source="sys"
                    )
                    
                    debug_log(2, f"[SYS] 已發布工作流失敗事件，等待 LLM 處理: {session_id}")
                    
                    # ✅ 清理 workflow engine（但不結束 WS，交給 LLM 標記後由 Controller 結束）
                    if session_id in self.workflow_engines:
                        del self.workflow_engines[session_id]
                        debug_log(2, f"[SYS] 已清理工作流引擎: {session_id}")
                else:
                    # 沒有 event_bus 的緊急情況，記錄錯誤
                    error_log(f"[SYS] ⚠️ 無法發布工作流失敗事件（缺少 event_bus），工作流可能卡住: {session_id}")
                    # 清理 engine
                    if session_id in self.workflow_engines:
                        del self.workflow_engines[session_id]
                
        except Exception as e:
            error_log(f"[SYS] 背景執行工作流步驟異常: {e}")
            
            # ✅ 異常情況：發布事件讓 LLM 處理異常並通知用戶
            if self.event_bus and session_id:
                from core.event_bus import SystemEvent
                
                event_data = {
                    "session_id": session_id,
                    "workflow_type": workflow_type,
                    "step_result": {
                        "success": False,
                        "complete": False,
                        "cancel": False,
                        "message": f"執行異常: {str(e)}",
                        "data": {},
                        "step_id": executed_step_id if 'executed_step_id' in locals() else None,
                        "error": True,
                        "exception": str(e)
                    },
                    "executed_steps": [],
                    "requires_llm_review": True,
                    "llm_review_data": None,
                    "current_step_info": None,
                    "next_step_info": None
                }
                
                self.event_bus.publish(
                    event_type=SystemEvent.WORKFLOW_STEP_COMPLETED,
                    data=event_data,
                    source="sys"
                )
                
                debug_log(2, f"[SYS] 已發布工作流異常事件，等待 LLM 處理: {session_id}")
                
                # ✅ 清理 workflow engine（但不結束 WS，交給 LLM 標記後由 Controller 結束）
                if session_id in self.workflow_engines:
                    del self.workflow_engines[session_id]
                    debug_log(2, f"[SYS] 已清理工作流引擎（異常）: {session_id}")
            else:
                # 沒有 event_bus 的緊急情況
                error_log(f"[SYS] ⚠️ 無法發布工作流異常事件（缺少 event_bus），工作流可能卡住: {session_id}")
                if session_id in self.workflow_engines:
                    del self.workflow_engines[session_id]
    
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
                # Workflow was cancelled（WS 交給 LLM 標記後由 Controller 結束）
                # Clean up engine
                if session_id in self.workflow_engines:
                    del self.workflow_engines[session_id]
                return {
                    "status": "cancelled",
                    "message": result.message,
                    "data": result.data
                }
                
            elif result.complete:
                # 🔧 工作流完成：不立即結束會話，讓 LLM 處理完成事件後調用中斷點
                # LLM 會在生成最終回應後調用 end_workflow_session()
                # 這樣 SESSION_ENDED 和 CYCLE_COMPLETED 會在同一個 Cycle 發布
                debug_log(1, f"[SYS] 工作流完成 {session_id}，等待 LLM 處理")
                
                # ✅ 發布工作流完成事件，讓 LLM 生成最終回應
                if self.event_bus:
                    from core.event_bus import SystemEvent
                    # 🔧 從 engine.definition 獲取正確的 workflow_type
                    workflow_type = engine.definition.workflow_type
                    llm_review_data = result.llm_review_data if hasattr(result, 'llm_review_data') else None
                    
                    # 🆕 獲取完整的步驟歷史（用於測試驗證）
                    session = self.session_manager.get_session(session_id)
                    step_history = session.get_data("step_history", []) if session else []
                    executed_step_ids = [step["step_id"] for step in step_history] if step_history else []
                    
                    event_data = {
                        "session_id": session_id,
                        "workflow_type": workflow_type,
                        "step_result": {
                            "success": result.success,
                            "complete": result.complete,
                            "cancel": result.cancel,
                            "message": result.message,
                            "data": result.data
                        },
                        "executed_steps": executed_step_ids,  # 🆕 添加所有執行的步驟 ID 列表
                        "requires_llm_review": True,  # 完成時總是需要 LLM 審核
                        "llm_review_data": llm_review_data,
                        "next_step_info": None  # 工作流已完成，沒有下一步
                    }
                    
                    self.event_bus.publish(
                        event_type=SystemEvent.WORKFLOW_STEP_COMPLETED,
                        data=event_data,
                        source="sys"
                    )
                    debug_log(2, f"[SYS] 已發布 workflow_step_completed 事件 (complete=True): {session_id}")
                
                return {
                    "status": "completed",
                    "message": result.message,
                    "data": result.data,
                    "session_id": session_id
                }
                
            elif not result.success:
                # 🔧 步驟失敗（failure）：發布事件讓 LLM 處理錯誤並標記 WS 結束
                debug_log(1, f"[SYS] 工作流步驟失敗 {session_id}: {result.message}")
                
                # 清理引擎（WS 交給 LLM 標記後由 Controller 結束）
                if session_id in self.workflow_engines:
                    del self.workflow_engines[session_id]
                
                # 發布失敗事件，讓 LLM 生成錯誤回應
                if self.event_bus:
                    from core.event_bus import SystemEvent
                    workflow_type = engine.definition.workflow_type
                    
                    self.event_bus.publish(
                        event_type=SystemEvent.WORKFLOW_FAILED,
                        data={
                            "session_id": session_id,
                            "workflow_type": workflow_type,
                            "error_message": result.message,
                            "current_step": engine.session.get_data("current_step")
                        },
                        source="sys"
                    )
                    debug_log(2, f"[SYS] 已發布 workflow_failed 事件: {session_id}")
                
                # 返回失敗狀態
                return {
                    "status": "failed",
                    "session_id": session_id,
                    "message": result.message,
                    "data": result.data
                }
                
            else:
                # Step succeeded, check if more input is needed
                current_step = engine.get_current_step()
                if current_step:
                    # 🔧 從 engine.definition 獲取正確的 workflow_type
                    workflow_type = engine.definition.workflow_type
                    
                    # Get step info for LLM context
                    step_info = self._get_step_info_for_llm(engine, workflow_type)
                    
                    # Add previous step result info
                    step_info["previous_step_result"] = {
                        "success": result.success,
                        "message": result.message,
                        "data": result.data or {}
                    }
                    
                    # 🆕 檢查是否需要 LLM 審核（例如 LLM_PROCESSING 完成後下一步是 INTERACTIVE）
                    # 如果 result.llm_review_data 存在，說明步驟需要審核
                    response = {
                        "status": "waiting",
                        "session_id": session_id,
                        "requires_input": True,
                        "prompt": engine.get_prompt(),
                        "message": result.message,
                        "data": result.data,
                        "step_info": step_info
                    }
                    
                    # 🆕 如果需要審核，添加 llm_review_data 到返回值
                    if hasattr(result, 'llm_review_data') and result.llm_review_data is not None:
                        response["llm_review_data"] = result.llm_review_data
                        response["requires_llm_review"] = True
                        debug_log(2, f"[SYS] 步驟需要 LLM 審核，已添加 review_data 到返回值")
                    
                    return response
                else:
                    # Workflow completed（WS 交給 LLM 標記後由 Controller 結束）
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
            # 清理 workflow engine（WS 交給 LLM 標記後由 Controller 結束）
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
        
        # Check if we have an engine for this session
        engine = self.workflow_engines.get(session_id)
        
        if not engine:
            # No engine means workflow is not active or completed
            return {
                "status": "error",
                "message": f"找不到工作流程引擎 ID: {session_id}"
            }
        
        # Check if workflow is waiting for LLM review
        if engine.is_awaiting_llm_review():
            pending_result = engine.pending_review_result
            return {
                "status": "waiting_for_llm_review",
                "session_id": session_id,
                "requires_llm_review": True,
                "message": pending_result.message if pending_result else "等待 LLM 審核",
                "data": pending_result.data if pending_result else {},
                "llm_review_data": pending_result.llm_review_data if pending_result else {}
            }
        
        # Check current step
        current_step = engine.get_current_step()
        
        if not current_step:
            # Workflow completed
            return {
                "status": "completed",
                "session_id": session_id,
                "message": "工作流程已完成"
            }
        
        # Check if step requires input
        if current_step.step_type == current_step.STEP_TYPE_INTERACTIVE:
            prompt = engine.get_prompt()
            return {
                "status": "waiting_for_input",
                "session_id": session_id,
                "requires_input": True,
                "prompt": prompt,
                "message": f"等待使用者輸入: {prompt}"
            }
        
        # Workflow is running
        return {
            "status": "running",
            "session_id": session_id,
            "current_step": current_step.id,
            "message": f"工作流程執行中，當前步驟: {current_step.id}"
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

        # ✨ 支持 operation 參數（從 ModuleCoordinator 傳入）
        operation = inp.operation if hasattr(inp, 'operation') else None
        workflow_decision = data.get('workflow_decision')  # LLM 決策結果
        
        # Check if this is a session continuation with just user_input
        if session_id and user_input and not mode:
            # Auto-set mode to continue_workflow
            mode = "continue_workflow"
            params = {"session_id": session_id, "user_input": user_input}
        
        # ✨ 如果有 operation 參數，覆蓋 mode
        if operation == "start":
            mode = "start_workflow"
            if workflow_decision:
                params = {
                    "workflow_type": workflow_decision.get("workflow_type"),
                    "command": data.get("text", ""),
                    "initial_data": workflow_decision.get("params", {})
                }
        elif operation == "continue":
            mode = "continue_workflow"
            # params 保持不變
        
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
                # Use direct workflow tools: drop_and_read, intelligent_archive, summarize_tag
                # (instead of the deprecated start_workflow with workflow_type parameter)
                # NEW: clean_trash_bin - direct action for trash cleanup
                
                # File Management Actions
                "clean_trash_bin": clean_trash_bin,
                
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
            包含所有類別工作流程的字典
        """
        return {
            "test_workflows": get_available_test_workflows(),
            "file_workflows": get_available_file_workflows(),
            "text_workflows": get_available_text_workflows(),
            "analysis_workflows": get_available_analysis_workflows(),
            "info_workflows": get_available_info_workflows(),
            "utility_workflows": get_available_utility_workflows()
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
                # 工作流被取消 - 標記待結束，等待循環完成
                self.session_manager.mark_workflow_session_for_end(session_id, reason="LLM 取消工作流")
                # 不刪除引擎，讓循環結束時清理
                
                return {
                    "status": "cancelled",
                    "message": result.message,
                    "data": result.to_dict()
                }
            elif result.complete:
                # 工作流完成 - 發布完成事件讓 LLM 生成 follow-up，然後標記待結束
                workflow_type = engine.definition.workflow_type
                
                # ✅ 優先使用步驟自定義的 llm_review_data（包含豐富的上下文數據如文件內容）
                # 如果沒有則使用基本的工作流結果數據
                if hasattr(result, 'llm_review_data') and result.llm_review_data:
                    llm_review_data = result.llm_review_data
                    debug_log(2, f"[SYS] 使用步驟的 llm_review_data，keys: {list(llm_review_data.keys())}")
                else:
                    llm_review_data = {
                        "workflow_result": result.data,
                        "requires_user_response": True,
                        "should_end_session": True
                    }
                    debug_log(2, f"[SYS] 使用默認 llm_review_data")
                
                # 🔧 發布 WORKFLOW_STEP_COMPLETED 事件（complete=True）讓 LLM 知道工作流完成
                # 🆕 獲取完整的步驟歷史（用於測試驗證）
                session = self.session_manager.get_session(session_id)
                step_history = session.get_data("step_history", []) if session else []
                executed_step_ids = [step["step_id"] for step in step_history] if step_history else []
                
                event_data = {
                    "session_id": session_id,
                    "workflow_type": workflow_type,
                    "step_result": {
                        "success": result.success,
                        "complete": result.complete,  # True
                        "cancel": result.cancel,
                        "message": result.message,
                        "data": result.data
                    },
                    "executed_steps": executed_step_ids,  # 🆕 添加所有執行的步驟 ID 列表
                    "requires_llm_review": True,  # 完成時需要 LLM 生成總結
                    "llm_review_data": llm_review_data,  # ✅ 使用豐富的審核數據
                    "next_step_info": None  # 工作流已完成
                }
                
                self.event_bus.publish(
                    event_type=SystemEvent.WORKFLOW_STEP_COMPLETED,
                    data=event_data,
                    source="sys"
                )
                debug_log(2, f"[SYS] ✅ 已發布 workflow_step_completed 事件 (complete=True): {session_id}")
                debug_log(2, f"[SYS] 事件中的 llm_review_data keys: {list(event_data.get('llm_review_data', {}).keys())}")
                
                # ✅ 不在這裡標記會話結束
                # LLM 會在下一個循環收到事件、生成 follow-up、輸出 TTS 後
                # 通過 session_control 標記結束，確保完整的回應週期
                # 不刪除引擎，讓循環結束時清理
                
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
                    # 工作流已完成 - 讓 LLM 在下次循環通過 session_control 標記
                    # 不刪除引擎，讓循環結束時清理
                    
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
    
    def _reload_from_user_settings(self, key_path: str, value: Any) -> bool:
        """
        從 user_settings 熱重載設定
        
        Args:
            key_path: 設定鍵路徑 (例如 "behavior.permissions.allow_file_creation")
            value: 新值
            
        Returns:
            是否成功
        """
        try:
            info_log(f"[SYS] 🔄 重載使用者設定: {key_path} = {value}")
            
            # 所有 behavior.permissions 的設定都是即時生效
            if key_path.startswith("behavior.permissions."):
                permission_name = key_path.split(".")[-1]
                info_log(f"[SYS] 權限設定已更新: {permission_name} = {value}")
                return True
            
            else:
                debug_log(2, f"[SYS] 未處理的設定路徑: {key_path}")
                return False
            
            return True
            
        except Exception as e:
            error_log(f"[SYS] 重載使用者設定失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
            return False
