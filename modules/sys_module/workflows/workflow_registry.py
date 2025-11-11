"""
Workflow Registry - 工作流註冊中心

集中管理所有工作流的 MCP 工具註冊
從 workflow_definitions.yaml 讀取配置並動態生成 MCP 工具
"""

import yaml
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Any
from modules.sys_module.mcp_server.tool_definitions import MCPTool, ToolParameter, ToolParameterType, ToolResult
from utils.debug_helper import debug_log, error_log

if TYPE_CHECKING:
    from modules.sys_module.mcp_server.mcp_server import MCPServer


async def _wrap_workflow_handler(workflow_type: str, params: dict, sys_module) -> ToolResult:
    """
    包裝工作流 handler，將字典結果轉換為 ToolResult
    
    Args:
        workflow_type: 工作流類型
        params: 工具參數
        sys_module: SYS 模組實例
        
    Returns:
        ToolResult 對象
    """
    import json
    
    # 解析 initial_data（如果是 JSON 字串則解析，否則直接使用）
    initial_data_raw = params.get("initial_data", "{}")
    if isinstance(initial_data_raw, str):
        try:
            initial_data = json.loads(initial_data_raw) if initial_data_raw else {}
        except json.JSONDecodeError:
            return ToolResult.error(f"initial_data 格式錯誤: 無效的 JSON 字串")
    else:
        initial_data = initial_data_raw if isinstance(initial_data_raw, dict) else {}
    
    result = await sys_module.start_workflow_async(
        workflow_type, 
        params["command"], 
        initial_data
    )
    
    # 將字典結果轉換為 ToolResult
    if result.get("status") == "error":
        return ToolResult.error(result.get("message", "工作流啟動失敗"))
    else:
        return ToolResult.success(
            message=result.get("message", f"工作流 '{workflow_type}' 已啟動"),
            data=result
        )


def _load_workflow_definitions() -> Dict[str, Any]:
    """
    從 YAML 文件載入工作流定義
    
    Returns:
        工作流定義字典
    """
    yaml_path = Path(__file__).parent / "workflow_definitions.yaml"
    
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            debug_log(2, f"[WorkflowRegistry] 已載入 {len(config.get('workflows', {}))} 個工作流定義")
            return config.get('workflows', {})
    except Exception as e:
        error_log(f"[WorkflowRegistry] 載入工作流定義失敗: {e}")
        return {}


def _build_tool_description(workflow_name: str, workflow_def: Dict[str, Any]) -> str:
    """
    根據工作流定義構建工具描述
    
    包含：
    - 基本描述
    - 參數提取提示（如果有 initial_params）
    
    Args:
        workflow_name: 工作流名稱
        workflow_def: 工作流定義
        
    Returns:
        完整的工具描述
    """
    base_description = workflow_def.get('description', f'{workflow_name} workflow')
    initial_params = workflow_def.get('initial_params', {})
    
    if not initial_params:
        return base_description
    
    # 構建參數提取指導
    extraction_guide = "\n\nParameter Extraction (if available in user input):"
    for param_name, param_def in initial_params.items():
        optional = param_def.get('optional', False)
        param_type = param_def.get('type', 'string')
        extraction_hint = param_def.get('extraction_hint', '')
        
        required_text = "optional" if optional else "required"
        extraction_guide += f"\n- {param_name} ({param_type}, {required_text}): {extraction_hint}"
    
    return base_description + extraction_guide


def _build_initial_data_description(initial_params: Dict[str, Any]) -> str:
    """
    構建 initial_data 參數的描述
    
    Args:
        initial_params: 初始參數定義
        
    Returns:
        initial_data 參數描述
    """
    if not initial_params:
        return 'Initial workflow data as JSON string (optional). Provide "{}" if no parameters to extract.'
    
    param_descriptions = []
    for param_name, param_def in initial_params.items():
        param_type = param_def.get('type', 'string')
        optional = param_def.get('optional', False)
        description = param_def.get('description', '')
        required_text = "optional" if optional else "required"
        
        param_descriptions.append(f"  - {param_name} ({param_type}, {required_text}): {description}")
    
    params_text = "\n".join(param_descriptions)
    
    # 提供 JSON 格式範例
    example_json = "{"
    example_fields = []
    for param_name, param_def in initial_params.items():
        param_type = param_def.get('type', 'string')
        if param_type == 'str':
            example_fields.append(f'"{param_name}": "value"')
        elif param_type == 'int':
            example_fields.append(f'"{param_name}": 0')
        else:
            example_fields.append(f'"{param_name}": null')
    example_json += ", ".join(example_fields) + "}"
    
    return (
        f"JSON string containing initial workflow data. Extract from user input if available:\n{params_text}\n"
        f'Example format: {example_json}\n'
        f'If no parameters can be extracted, provide empty JSON string "{{}}"'
    )


def register_all_workflows(mcp_server: 'MCPServer', sys_module) -> None:
    """
    註冊所有工作流到 MCP Server
    
    從 workflow_definitions.yaml 讀取配置並動態生成 MCP 工具
    
    Args:
        mcp_server: MCP Server 實例
        sys_module: SYS Module 實例（用於 handler）
    """
    workflow_definitions = _load_workflow_definitions()
    
    if not workflow_definitions:
        error_log("[WorkflowRegistry] 無工作流定義可註冊")
        return
    
    registered_count = 0
    for workflow_name, workflow_def in workflow_definitions.items():
        try:
            # 構建工具描述（包含參數提取指導）
            tool_description = _build_tool_description(workflow_name, workflow_def)
            
            # 構建 initial_data 參數描述
            initial_params = workflow_def.get('initial_params', {})
            initial_data_description = _build_initial_data_description(initial_params)
            
            # 創建工具參數
            parameters = [
                ToolParameter(
                    name="command",
                    type=ToolParameterType.STRING,
                    description="User's original command",
                    required=True
                ),
                ToolParameter(
                    name="initial_data",
                    type=ToolParameterType.STRING,  # 改為 STRING 類型，內容為 JSON
                    description=initial_data_description,
                    required=False
                )
            ]
            
            # 註冊工具
            mcp_server.register_tool(MCPTool(
                name=workflow_name,
                description=tool_description,
                parameters=parameters,
                handler=lambda params, wf_name=workflow_name: _wrap_workflow_handler(wf_name, params, sys_module)
            ))
            
            registered_count += 1
            debug_log(3, f"[WorkflowRegistry] 已註冊工作流工具: {workflow_name}")
            
        except Exception as e:
            error_log(f"[WorkflowRegistry] 註冊工作流 {workflow_name} 失敗: {e}")
    
    debug_log(1, f"[WorkflowRegistry] 成功註冊 {registered_count}/{len(workflow_definitions)} 個工作流工具")

