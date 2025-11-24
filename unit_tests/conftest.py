# -*- coding: utf-8 -*-
"""
Pytest fixtures for U.E.P unit tests.
提供所有單元測試共用的 fixtures。
"""

import pytest
from unittest.mock import Mock, AsyncMock, MagicMock
from pathlib import Path
import sys

# 確保可以導入專案模組
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


@pytest.fixture
def event_bus():
    """提供乾淨的事件總線實例"""
    from core.event_bus import EventBus
    
    bus = EventBus()
    yield bus
    # 清理：移除所有訂閱者
    bus._handlers.clear()
    bus._event_history.clear()


@pytest.fixture
def unified_session_manager():
    """提供統一會話管理器實例"""
    from core.sessions.session_manager import UnifiedSessionManager
    
    manager = UnifiedSessionManager()
    yield manager
    # 清理：記錄當前狀態但不強制終止（由各自管理器處理）
    try:
        # 清空會話記錄以避免測試間干擾
        manager.session_records.clear()
    except:
        pass


@pytest.fixture
def workflow_definition(mock_workflow_session):
    """提供測試用的工作流定義"""
    from modules.sys_module.workflows import WorkflowDefinition, StepTemplate, StepResult, WorkflowMode
    
    definition = WorkflowDefinition(
        workflow_type="test_workflow",
        name="測試工作流",
        description="用於單元測試的工作流",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=True
    )
    
    # 使用 StepTemplate 靜態方法創建步驟
    step_1 = StepTemplate.create_input_step(
        session=mock_workflow_session,
        step_id="step_1",
        prompt="輸入步驟1",
        description="測試步驟1"
    )
    step_2 = StepTemplate.create_processing_step(
        session=mock_workflow_session,
        step_id="step_2",
        processor=lambda session: StepResult.success("完成", {}),
        description="測試步驟2"
    )
    
    definition.add_step(step_1)
    definition.add_step(step_2)
    definition.add_transition("step_1", "step_2")
    definition.set_entry_point("step_1")
    
    yield definition


@pytest.fixture
def mock_workflow_session():
    """提供 mock 的工作流會話"""
    from core.sessions.workflow_session import WorkflowSession
    
    mock_session = Mock(spec=WorkflowSession)
    mock_session.session_id = "test-ws-123"
    mock_session.task_type = "test_task"
    mock_session.session_data = {}
    
    def get_data(key):
        return mock_session.session_data.get(key)
    
    def add_data(key, value):
        mock_session.session_data[key] = value
    
    mock_session.get_data = get_data
    mock_session.add_data = add_data
    
    yield mock_session


@pytest.fixture
def workflow_engine(workflow_definition, mock_workflow_session):
    """提供工作流引擎實例"""
    from modules.sys_module.workflows import WorkflowEngine
    
    engine = WorkflowEngine(workflow_definition, mock_workflow_session)
    yield engine


@pytest.fixture
def mock_sys_module():
    """提供 mock 的 SYS 模組"""
    mock_sys = Mock()
    mock_sys._current_session_id = "test-session-123"
    
    # Mock workflow engine
    mock_sys.workflow_engine = Mock()
    mock_sys.workflow_engine.start_workflow.return_value = "workflow-123"
    mock_sys.workflow_engine.get_workflow_status.return_value = {
        "workflow_id": "workflow-123",
        "status": "running",
        "current_step": "step1"
    }
    
    yield mock_sys


@pytest.fixture
def mcp_server(mock_sys_module):
    """提供 MCP 服務器實例"""
    from modules.sys_module.mcp_server.mcp_server import MCPServer
    
    server = MCPServer(sys_module=mock_sys_module)
    yield server


@pytest.fixture
def mcp_client(mcp_server):
    """提供 MCP 客戶端實例"""
    from modules.llm_module.mcp_client import MCPClient
    
    client = MCPClient(mcp_server=mcp_server)
    yield client


@pytest.fixture
def mock_gemini_wrapper():
    """提供 mock Gemini wrapper"""
    mock = MagicMock()
    
    # 預設回應
    mock.generate_content.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": "這是測試回應"
                }]
            }
        }]
    }
    
    return mock


@pytest.fixture
def llm_module(mock_gemini_wrapper):
    """提供 LLM 模組實例（使用 mock 服務）"""
    from modules.llm_module.llm_module import LLMModule
    
    module = LLMModule()
    # 替換為 mock Gemini wrapper
    module.model = mock_gemini_wrapper
    
    yield module


@pytest.fixture
def temp_workflow_file(tmp_path):
    """建立臨時工作流配置文件"""
    workflow_config = {
        "name": "測試工作流",
        "description": "用於測試的工作流",
        "steps": [
            {
                "step_id": "step1",
                "step_type": "INTERACTIVE",
                "description": "第一步：收集用戶輸入",
                "requires_llm_review": True
            },
            {
                "step_id": "step2",
                "step_type": "PROCESSING",
                "description": "第二步：處理數據",
                "requires_llm_review": False
            },
            {
                "step_id": "step3",
                "step_type": "SYSTEM",
                "description": "第三步：系統操作",
                "requires_llm_review": True
            }
        ]
    }
    
    import json
    file_path = tmp_path / "test_workflow.json"
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(workflow_config, f, ensure_ascii=False, indent=2)
    
    return file_path


@pytest.fixture
def sample_step_templates():
    """提供示例工作流步驟模板"""
    from modules.sys_module.workflows import StepTemplate
    
    return [
        StepTemplate(
            id="step1",
            name="第一步",
            description="收集用戶輸入",
            step_type="interactive"
        ),
        StepTemplate(
            id="step2",
            name="第二步",
            description="處理數據",
            step_type="processing"
        ),
        StepTemplate(
            id="step3",
            name="第三步",
            description="系統操作",
            step_type="system"
        )
    ]


@pytest.fixture
def mock_session():
    """提供 mock 會話物件"""
    session = Mock()
    session.session_id = "test-session-123"
    session.session_type = "WORKFLOW"
    session.is_active = True
    session.should_end = False
    session.metadata = {}
    
    return session


@pytest.fixture(autouse=True)
def reset_singletons():
    """在每個測試前重置單例"""
    # 某些模組可能使用單例模式，需要在測試間重置
    yield
    # 測試後清理
    pass


@pytest.fixture
def captured_events():
    """捕獲事件總線發出的所有事件"""
    events = []
    
    def capture(event_name, **kwargs):
        events.append({
            "event": event_name,
            "data": kwargs
        })
    
    return events, capture


# Pytest 配置
def pytest_configure(config):
    """註冊自定義標記"""
    config.addinivalue_line("markers", "critical: 關鍵功能測試，必須通過")
    config.addinivalue_line("markers", "workflow: 工作流相關測試")
    config.addinivalue_line("markers", "mcp: MCP 服務器相關測試")
    config.addinivalue_line("markers", "session: 會話管理相關測試")
    config.addinivalue_line("markers", "event: 事件總線相關測試")
    config.addinivalue_line("markers", "slow: 執行時間較長的測試")
