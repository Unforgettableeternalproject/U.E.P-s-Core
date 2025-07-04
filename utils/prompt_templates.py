"""
utils/prompt_templates.py
Prompt templates for different LLM interaction scenarios
"""

# Command workflow prompt template
COMMAND_WORKFLOW_TEMPLATE = """
# 指令處理 (Command Processing)

## 指令文本
{command}

## 可用系統功能
{available_functions}

## 指令分析要求
請分析上述指令，並執行以下步驟：
1. 判斷用戶想要執行的操作類型
2. 選擇最適合的系統功能來實現此操作
3. 說明需要哪些參數來執行此功能
4. 如果需要多步驟互動，請設計清晰的工作流程

## 輸出格式
請以以下格式輸出您的分析：

操作類型：[操作類型]
適用功能：[選擇的系統功能]
所需參數：
- [參數1名稱]：[參數說明]
- [參數2名稱]：[參數說明]
互動步驟：
1. [步驟1]
2. [步驟2]
...

回應給用戶：
[給用戶的回應文本，說明您將如何處理他的請求]
"""

# Command workflow step prompt template
COMMAND_WORKFLOW_STEP_TEMPLATE = """
# 指令工作流程 - 步驟 {step_number}

## 當前工作流程
類型：{workflow_type}
操作：{command}
當前步驟：{step_number}/{total_steps}

## 步驟說明
{step_description}

## 用戶回應
{user_input}

## 步驟處理要求
請分析用戶的回應，並根據工作流程的當前步驟執行以下操作：
1. 判斷用戶的回應是否符合步驟要求
2. 如果符合，提取需要的資訊並推進到下一步
3. 如果不符合，說明原因並給予具體指導

## 輸出格式
請以以下格式輸出您的分析：

回應評估：[有效/無效]
提取資訊：
- [資訊1]：[值]
- [資訊2]：[值]
下一步驟：[下一步驟的說明]

回應給用戶：
[給用戶的回應文本，說明接下來的操作或要求]
"""

# System action instruction template
SYSTEM_ACTION_TEMPLATE = """
# 系統動作指示

當你識別出用戶需要系統執行特定動作時，請在回應中包含明確的系統動作指示。

## 格式
在回應文本的最後，添加以下格式的系統動作指示：

```system_action
{
  "action": "[動作名稱]",
  "params": {
    "[參數1]": "[值1]",
    "[參數2]": "[值2]",
    ...
  }
}
```

## 支援的動作類型
- file_operation: 檔案操作 (params: operation, file_path, target_path)
- start_workflow: 啟動工作流程 (params: workflow_type, command)
- continue_workflow: 繼續工作流程 (params: session_id, user_input)
- cancel_workflow: 取消工作流程 (params: session_id, reason)
- execute_command: 執行系統指令 (params: command, args)

## 範例
```system_action
{
  "action": "file_operation",
  "params": {
    "operation": "move",
    "file_path": "C:/Users/docs/report.txt", 
    "target_path": "C:/Users/docs/archives/"
  }
}
```
"""

def build_command_prompt(command: str, available_functions: str) -> str:
    """
    Build a prompt for command intent handling
    
    Args:
        command: The user's command
        available_functions: Description of available system functions
        
    Returns:
        The formatted prompt
    """
    return COMMAND_WORKFLOW_TEMPLATE.format(
        command=command,
        available_functions=available_functions
    )

def build_workflow_step_prompt(
    step_number: int,
    total_steps: int,
    workflow_type: str,
    command: str,
    step_description: str,
    user_input: str
) -> str:
    """
    Build a prompt for a workflow step
    
    Args:
        step_number: The current step number
        total_steps: The total number of steps
        workflow_type: The type of workflow
        command: The original command
        step_description: Description of the current step
        user_input: The user's input for this step
        
    Returns:
        The formatted prompt
    """
    return COMMAND_WORKFLOW_STEP_TEMPLATE.format(
        step_number=step_number,
        total_steps=total_steps,
        workflow_type=workflow_type,
        command=command,
        step_description=step_description,
        user_input=user_input
    )

def format_sys_functions_for_prompt(functions_spec: dict) -> str:
    """
    Format system functions specification for inclusion in prompts
    
    Args:
        functions_spec: The functions specification dictionary from SYS module
        
    Returns:
        A formatted string describing available functions
    """
    formatted_lines = ["## 可用系統功能"]
    
    # Group functions by category
    categories = {}
    for func_name, func_data in functions_spec.items():
        category = func_data.get("category", "其他")
        if category not in categories:
            categories[category] = []
        
        description = func_data.get("description", "")
        params_info = []
        for param_name, param_data in func_data.get("params", {}).items():
            required = "必填" if param_data.get("required", False) else "選填"
            param_desc = param_data.get("description", "")
            params_info.append(f"  - {param_name} ({required}): {param_desc}")
        
        func_info = [
            f"### {func_name}",
            f"{description}",
        ]
        
        if params_info:
            func_info.append("參數:")
            func_info.extend(params_info)
        
        categories[category].append("\n".join(func_info))
    
    # Format by category
    for category, funcs in categories.items():
        formatted_lines.append(f"\n# {category}")
        formatted_lines.extend(funcs)
    
    return "\n\n".join(formatted_lines)
