"""
tests/test_stage2_workflow_enhancements.py
階段 2 工作流引擎增強功能測試

測試 WorkflowMode、LLM 審核迴圈、以及相關的工作流定義更新。
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock

# 導入要測試的模組
from modules.sys_module.workflows import (
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowStep,
    StepResult,
    WorkflowMode,
    StepTemplate
)
from modules.sys_module.sys_module import SYSModule
from modules.sys_module.mcp_server.mcp_server import MCPServer
from modules.sys_module.mcp_server.tool_definitions import ToolResult
from core.sessions.session_manager import session_manager, SessionStatus


class TestWorkflowMode:
    """測試 WorkflowMode 枚舉"""
    
    def test_workflow_mode_values(self):
        """測試 WorkflowMode 枚舉值"""
        assert WorkflowMode.DIRECT == "direct"
        assert WorkflowMode.BACKGROUND == "background"
    
    def test_workflow_mode_is_string_enum(self):
        """測試 WorkflowMode 是字符串枚舉"""
        assert isinstance(WorkflowMode.DIRECT.value, str)
        assert isinstance(WorkflowMode.BACKGROUND.value, str)


class TestWorkflowDefinitionEnhancements:
    """測試 WorkflowDefinition 的增強功能"""
    
    def test_workflow_definition_default_values(self):
        """測試 WorkflowDefinition 的預設值"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試描述"
        )
        
        assert workflow_def.workflow_mode == WorkflowMode.DIRECT
        assert workflow_def.requires_llm_review == False
        assert workflow_def.auto_advance_on_approval == True
    
    def test_workflow_definition_with_custom_values(self):
        """測試 WorkflowDefinition 的自訂值"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試描述",
            workflow_mode=WorkflowMode.BACKGROUND,
            requires_llm_review=True,
            auto_advance_on_approval=False
        )
        
        assert workflow_def.workflow_mode == WorkflowMode.BACKGROUND
        assert workflow_def.requires_llm_review == True
        assert workflow_def.auto_advance_on_approval == False
    
    def test_workflow_definition_get_info_includes_new_fields(self):
        """測試 get_info() 包含新欄位"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試描述",
            workflow_mode=WorkflowMode.BACKGROUND,
            requires_llm_review=True,
            auto_advance_on_approval=False
        )
        
        info = workflow_def.get_info()
        
        assert "workflow_mode" in info
        assert info["workflow_mode"] == "background"
        assert "requires_llm_review" in info
        assert info["requires_llm_review"] == True
        assert "auto_advance_on_approval" in info
        assert info["auto_advance_on_approval"] == False


class TestStepResultEnhancements:
    """測試 StepResult 的增強功能"""
    
    def test_step_result_default_values(self):
        """測試 StepResult 的預設值"""
        result = StepResult(
            success=True,
            message="測試訊息"
        )
        
        assert result.llm_review_data is None
        assert result.requires_user_confirmation == False
    
    def test_step_result_with_llm_review_data(self):
        """測試 StepResult 包含 LLM 審核資料"""
        review_data = {
            "step_id": "test_step",
            "action": "test_action",
            "confidence": 0.95
        }
        
        result = StepResult(
            success=True,
            message="測試訊息",
            llm_review_data=review_data
        )
        
        assert result.llm_review_data == review_data
    
    def test_step_result_with_user_confirmation(self):
        """測試 StepResult 需要使用者確認"""
        result = StepResult(
            success=True,
            message="測試訊息",
            requires_user_confirmation=True
        )
        
        assert result.requires_user_confirmation == True
    
    def test_step_result_to_dict_includes_new_fields(self):
        """測試 to_dict() 包含新欄位"""
        review_data = {"test": "data"}
        result = StepResult(
            success=True,
            message="測試訊息",
            llm_review_data=review_data,
            requires_user_confirmation=True
        )
        
        result_dict = result.to_dict()
        
        assert "llm_review_data" in result_dict
        assert result_dict["llm_review_data"] == review_data
        assert "requires_user_confirmation" in result_dict
        assert result_dict["requires_user_confirmation"] == True


class TestWorkflowEngineEnhancements:
    """測試 WorkflowEngine 的增強功能"""
    
    def setup_method(self):
        """每個測試前的設置"""
        # 創建測試會話
        self.session = session_manager.create_session(
            workflow_type="test",
            command="test command",
            initial_data={}
        )
        if isinstance(self.session, str):
            session_id = self.session
            self.session = session_manager.get_workflow_session(session_id)
    
    def teardown_method(self):
        """每個測試後的清理"""
        if self.session:
            session_manager.end_session(self.session.session_id, reason="測試結束")
    
    def test_workflow_engine_initialization_with_llm_review(self):
        """測試 WorkflowEngine 初始化時的 LLM 審核屬性"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試",
            requires_llm_review=True
        )
        
        # 創建簡單步驟
        step = StepTemplate.create_input_step(self.session, "test_step", "測試輸入")
        workflow_def.add_step(step)
        workflow_def.set_entry_point("test_step")
        
        engine = WorkflowEngine(workflow_def, self.session)
        
        assert hasattr(engine, 'llm_review_timeout')
        assert hasattr(engine, 'awaiting_llm_review')
        assert hasattr(engine, 'pending_review_result')
        assert engine.awaiting_llm_review == False
        assert engine.pending_review_result is None
    
    def test_workflow_engine_is_awaiting_llm_review(self):
        """測試 is_awaiting_llm_review() 方法"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試"
        )
        
        step = StepTemplate.create_input_step(self.session, "test_step", "測試輸入")
        workflow_def.add_step(step)
        workflow_def.set_entry_point("test_step")
        
        engine = WorkflowEngine(workflow_def, self.session)
        
        assert engine.is_awaiting_llm_review() == False
        
        # 手動設置為等待狀態
        engine.awaiting_llm_review = True
        assert engine.is_awaiting_llm_review() == True
    
    def test_workflow_engine_request_llm_review(self):
        """測試 _request_llm_review() 方法"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試",
            requires_llm_review=True
        )
        
        step = StepTemplate.create_input_step(self.session, "test_step", "測試輸入")
        workflow_def.add_step(step)
        workflow_def.set_entry_point("test_step")
        
        engine = WorkflowEngine(workflow_def, self.session)
        
        # 創建一個成功的步驟結果
        original_result = StepResult.success("步驟成功", {"key": "value"})
        
        # 請求 LLM 審核
        review_result = engine._request_llm_review(original_result, step)
        
        assert engine.awaiting_llm_review == True
        assert engine.pending_review_result == original_result
        assert review_result.success == True
        assert "等待 LLM 審核" in review_result.message
        assert review_result.llm_review_data is not None
        assert "step_id" in review_result.llm_review_data
    
    def test_workflow_engine_handle_llm_review_approve(self):
        """測試 handle_llm_review_response() - 批准操作"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試",
            auto_advance_on_approval=False
        )
        
        step1 = StepTemplate.create_input_step(self.session, "step1", "步驟1")
        step2 = StepTemplate.create_input_step(self.session, "step2", "步驟2")
        workflow_def.add_step(step1)
        workflow_def.add_step(step2)
        workflow_def.set_entry_point("step1")
        workflow_def.add_transition("step1", "step2")
        
        engine = WorkflowEngine(workflow_def, self.session)
        
        # 設置等待審核狀態
        pending_result = StepResult.success("步驟完成", {"data": "test"})
        engine.awaiting_llm_review = True
        engine.pending_review_result = pending_result
        
        # 批准操作
        result = engine.handle_llm_review_response("approve")
        
        assert engine.awaiting_llm_review == False
        assert engine.pending_review_result is None
        assert result.success == True
    
    def test_workflow_engine_handle_llm_review_cancel(self):
        """測試 handle_llm_review_response() - 取消操作"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試"
        )
        
        step = StepTemplate.create_input_step(self.session, "test_step", "測試")
        workflow_def.add_step(step)
        workflow_def.set_entry_point("test_step")
        
        engine = WorkflowEngine(workflow_def, self.session)
        
        # 設置等待審核狀態
        pending_result = StepResult.success("步驟完成")
        engine.awaiting_llm_review = True
        engine.pending_review_result = pending_result
        
        # 取消操作
        result = engine.handle_llm_review_response("cancel")
        
        assert engine.awaiting_llm_review == False
        assert result.cancel == True
        assert "取消" in result.message
    
    def test_workflow_engine_handle_llm_review_modify(self):
        """測試 handle_llm_review_response() - 修改操作"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試"
        )
        
        # 創建一個處理步驟
        def test_processor(session):
            value = session.get_data("test_value", 0)
            return StepResult.success(f"處理值: {value}", {"result": value * 2})
        
        step = StepTemplate.create_processing_step(
            self.session, "test_step", test_processor
        )
        workflow_def.add_step(step)
        workflow_def.set_entry_point("test_step")
        
        engine = WorkflowEngine(workflow_def, self.session)
        
        # 設置初始數據
        self.session.add_data("test_value", 5)
        
        # 執行步驟以產生結果
        initial_result = step.execute()
        
        # 設置等待審核狀態
        engine.awaiting_llm_review = True
        engine.pending_review_result = initial_result
        
        # 修改參數並重新執行
        modified_params = {"test_value": 10}
        result = engine.handle_llm_review_response("modify", modified_params)
        
        # 驗證修改已應用
        assert self.session.get_data("test_value") == 10
    
    def test_workflow_engine_process_input_triggers_llm_review(self):
        """測試 process_input() 在需要時觸發 LLM 審核"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試",
            requires_llm_review=True
        )
        
        step = StepTemplate.create_input_step(self.session, "test_step", "測試輸入")
        workflow_def.add_step(step)
        workflow_def.set_entry_point("test_step")
        
        engine = WorkflowEngine(workflow_def, self.session)
        
        # 提供輸入
        result = engine.process_input("測試輸入值")
        
        # 應該觸發 LLM 審核
        assert engine.awaiting_llm_review == True
        assert "等待 LLM 審核" in result.message
        assert result.llm_review_data is not None
    
    def test_workflow_engine_get_status_includes_llm_review_info(self):
        """測試 get_status() 包含 LLM 審核資訊"""
        workflow_def = WorkflowDefinition(
            workflow_type="test",
            name="測試工作流",
            description="測試",
            workflow_mode=WorkflowMode.BACKGROUND,
            requires_llm_review=True
        )
        
        step = StepTemplate.create_input_step(self.session, "test_step", "測試")
        workflow_def.add_step(step)
        workflow_def.set_entry_point("test_step")
        
        engine = WorkflowEngine(workflow_def, self.session)
        
        status = engine.get_status()
        
        assert "workflow_mode" in status
        assert status["workflow_mode"] == "background"
        assert "requires_llm_review" in status
        assert status["requires_llm_review"] == True
        assert "awaiting_llm_review" in status
        assert status["awaiting_llm_review"] == False


class TestSYSModuleLLMReviewIntegration:
    """測試 SYS 模組的 LLM 審核整合"""
    
    def setup_method(self):
        """每個測試前的設置"""
        from configs.config_loader import load_module_config
        config = load_module_config("sys_module")
        self.sys_module = SYSModule(config)
        self.sys_module.initialize()
    
    def test_sys_module_has_handle_llm_review_response_method(self):
        """測試 SYS 模組有 LLM 審核響應處理方法"""
        assert hasattr(self.sys_module, 'handle_llm_review_response_async')
        assert hasattr(self.sys_module, '_handle_llm_review_response')
    
    def test_handle_llm_review_response_no_engine(self):
        """測試處理 LLM 審核響應時沒有引擎"""
        result = self.sys_module._handle_llm_review_response(
            session_id="non_existent_session",
            action="approve"
        )
        
        assert result["status"] == "error"
        assert "找不到" in result["message"]
    
    @pytest.mark.asyncio
    async def test_handle_llm_review_response_async(self):
        """測試異步 LLM 審核響應處理"""
        # 創建一個測試工作流會話
        session_result = session_manager.create_session(
            workflow_type="echo",
            command="test",
            initial_data={}
        )
        
        if isinstance(session_result, str):
            session_id = session_result
            session = session_manager.get_workflow_session(session_id)
        else:
            session = session_result
            session_id = session.session_id
        
        # 創建簡單的測試工作流
        from modules.sys_module.workflows.test_workflows import create_echo_workflow
        engine = create_echo_workflow(session)
        
        # 註冊引擎
        self.sys_module.workflow_engines[session_id] = engine
        
        # 設置引擎為等待審核狀態
        engine.awaiting_llm_review = True
        engine.pending_review_result = StepResult.success("測試步驟完成")
        
        # 調用異步方法
        result = await self.sys_module.handle_llm_review_response_async(
            session_id=session_id,
            action="cancel"
        )
        
        assert result["status"] == "cancelled"
        assert session_id not in self.sys_module.workflow_engines
        
        # 清理
        session_manager.end_session(session_id, reason="測試結束")


class TestMCPServerLLMReviewIntegration:
    """測試 MCP Server 的 LLM 審核整合"""
    
    def setup_method(self):
        """每個測試前的設置"""
        from configs.config_loader import load_module_config
        config = load_module_config("sys_module")
        self.sys_module = SYSModule(config)
        self.sys_module.initialize()
        self.mcp_server = self.sys_module.get_mcp_server()
    
    def test_mcp_server_has_approve_step_tool(self):
        """測試 MCP Server 有 approve_step 工具"""
        tool = self.mcp_server.get_tool("approve_step")
        assert tool is not None
        assert tool.name == "approve_step"
    
    def test_mcp_server_has_modify_step_tool(self):
        """測試 MCP Server 有 modify_step 工具"""
        tool = self.mcp_server.get_tool("modify_step")
        assert tool is not None
        assert tool.name == "modify_step"
    
    def test_mcp_server_has_cancel_workflow_tool(self):
        """測試 MCP Server 有 cancel_workflow 工具"""
        tool = self.mcp_server.get_tool("cancel_workflow")
        assert tool is not None
        assert tool.name == "cancel_workflow"
    
    @pytest.mark.asyncio
    async def test_approve_step_tool_calls_llm_review_handler(self):
        """測試 approve_step 工具調用 LLM 審核處理器"""
        # 創建測試會話和工作流
        session_result = session_manager.create_session(
            workflow_type="echo",
            command="test",
            initial_data={}
        )
        
        if isinstance(session_result, str):
            session_id = session_result
            session = session_manager.get_workflow_session(session_id)
        else:
            session = session_result
            session_id = session.session_id
        
        from modules.sys_module.workflows.test_workflows import create_echo_workflow
        engine = create_echo_workflow(session)
        self.sys_module.workflow_engines[session_id] = engine
        
        # 設置引擎為等待審核狀態
        engine.awaiting_llm_review = True
        engine.pending_review_result = StepResult.success("測試")
        
        # 調用 approve_step 工具
        tool = self.mcp_server.get_tool("approve_step")
        result = await tool.execute({
            "session_id": session_id,
            "continue_data": {}
        })
        
        assert result.status == "success"
        
        # 清理
        if session_id in self.sys_module.workflow_engines:
            del self.sys_module.workflow_engines[session_id]
        session_manager.end_session(session_id, reason="測試結束")


class TestWorkflowDefinitionsUpdates:
    """測試工作流定義的更新"""
    
    def test_test_workflows_have_direct_mode(self):
        """測試測試工作流都設置為 DIRECT 模式"""
        from modules.sys_module.workflows import test_workflows
        
        session = session_manager.create_session(
            workflow_type="echo",
            command="test",
            initial_data={}
        )
        if isinstance(session, str):
            session_id = session
            session = session_manager.get_workflow_session(session_id)
        else:
            session_id = session.session_id
        
        # 測試 echo 工作流
        engine = test_workflows.create_echo_workflow(session)
        assert engine.definition.workflow_mode == WorkflowMode.DIRECT
        assert engine.definition.requires_llm_review == False
        
        # 清理
        session_manager.end_session(session_id, reason="測試結束")
    
    def test_file_workflows_have_background_mode_and_llm_review(self):
        """測試檔案工作流設置為 BACKGROUND 模式並啟用 LLM 審核"""
        from modules.sys_module.workflows import file_workflows
        
        session = session_manager.create_session(
            workflow_type="drop_and_read",
            command="test",
            initial_data={}
        )
        if isinstance(session, str):
            session_id = session
            session = session_manager.get_workflow_session(session_id)
        else:
            session_id = session.session_id
        
        # 測試 drop_and_read 工作流
        engine = file_workflows.create_drop_and_read_workflow(session)
        assert engine.definition.workflow_mode == WorkflowMode.BACKGROUND
        assert engine.definition.requires_llm_review == True
        assert engine.definition.auto_advance_on_approval == True
        
        # 清理
        session_manager.end_session(session_id, reason="測試結束")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
