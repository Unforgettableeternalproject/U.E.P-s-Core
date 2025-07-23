"""
utils/prompt_templates.py
Prompt templates for different LLM interaction scenarios
"""

# System action instruction template - Enhanced English version
SYSTEM_ACTION_TEMPLATE = """
# System Action Instructions

When you identify that the user needs the system to execute specific actions, please include clear system action instructions in your response.

## Available System Functions

Currently, UEP supports the following file interaction functions:

### File Processing Workflows

1. **drop_and_read** - Read file content
   - Supports: .txt, .md, .pdf formats
   - Purpose: Read and display file contents
   - Usage: When user wants to read or view file contents

2. **intelligent_archive** - Smart file archiving
   - Purpose: Automatically categorize and archive files based on type and history
   - Usage: When user wants to organize or archive files

3. **summarize_tag** - Generate file summary and tags
   - Purpose: Create summary and tags for files using LLM analysis
   - Usage: When user wants to summarize document content

## Response Format

Your response should contain two parts:
1. Natural language response to the user
2. System action instruction (JSON format)

## System Action JSON Format
```json
{
  "action": "[action_type]",
  "workflow_type": "[workflow_type]", 
  "function_name": "[specific_function_name]",
  "params": {
    "[param1]": "[value1]",
    "[param2]": "[value2]"
  },
  "reason": "[explanation_for_execution]"
}
```

## Supported Action Types

1. **start_workflow**: Start a new workflow
   - workflow_type: Type of workflow (drop_and_read, intelligent_archive, summarize_tag)
   - params: Initialization parameters
   - reason: Why this workflow was chosen

2. **execute_function**: Execute system function directly
   - function_name: Function name (for simple operations)
   - params: Function parameters
   - reason: Why this function was chosen

3. **continue_workflow**: Continue existing workflow
   - session_id: Workflow session ID
   - params: Continue parameters
   - reason: Reason for continuation

4. **cancel_workflow**: Cancel workflow
   - session_id: Workflow session ID
   - reason: Cancellation reason

## Important Guidelines

- **CRITICAL**: Only output system actions in command mode
- **CRITICAL**: If the requested functionality is NOT in the available functions list above, tell the user that you currently doesn't have that capability and DO NOT generate any system action JSON
- Always prefer workflows over direct function execution unless it's a simple operation
- Parameter values should be specific and clear, avoid vague expressions
- If you cannot find a suitable function from the available list, explain this limitation to the user

## Response Examples

### Example 1: Valid Request
User input: "Please read this document for me"

Response:
I'll help you read the document. I'll start the file reading workflow which will allow you to select a file and display its contents.

```json
{
  "action": "start_workflow",
  "workflow_type": "drop_and_read",
  "params": {},
  "reason": "User wants to read a document, using drop_and_read workflow"
}
```

### Example 2: Unavailable Function
User input: "Can you check the weather for me?"

Response:
I'm sorry, but I currently don't have the capability to check weather information. UEP currently only supports file processing functions like reading documents, archiving files, and generating summaries. Is there anything file-related I can help you with instead?

(No JSON output since the function is not available)

### Example 3: File Organization
User input: "Help me organize the files on my desktop"

Response:
I'll help you organize your desktop files. I'll start the intelligent archiving workflow which will categorize and move your files to appropriate folders based on their type and your previous archiving patterns.

```json
{
  "action": "start_workflow", 
  "workflow_type": "intelligent_archive",
  "params": {
    "source_path": "Desktop",
    "auto_categorize": true
  },
  "reason": "User wants to organize desktop files, intelligent archiving is most suitable"
}
```
"""

def build_command_prompt(command: str, available_functions: str = "") -> str:
    """
    Build a prompt for command intent handling
    
    Args:
        command: The user's command
        available_functions: Description of available system functions (optional, defaults to template)
        
    Returns:
        The formatted prompt with system action template
    """
    return f"{SYSTEM_ACTION_TEMPLATE}\n\n## User Command\n{command}\n\nPlease analyze this command and respond appropriately."

def build_workflow_step_prompt(
    step_number: int,
    total_steps: int,
    workflow_type: str,
    command: str,
    step_description: str,
    user_input: str
) -> str:
    """
    Build a prompt for a workflow step (simplified English version)
    
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
    return f"""
# Workflow Step {step_number}

## Current Workflow
Type: {workflow_type}
Operation: {command}
Current Step: {step_number}/{total_steps}

## Step Description
{step_description}

## User Response
{user_input}

## Step Processing Requirements
Please analyze the user's response and execute the following based on the current workflow step:
1. Determine if the user's response meets the step requirements
2. If it meets requirements, extract needed information and proceed to next step
3. If it doesn't meet requirements, explain why and provide specific guidance

## Output Format
Please output your analysis in the following format:

Response Evaluation: [Valid/Invalid]
Extracted Information:
- [Info1]: [Value]
- [Info2]: [Value]
Next Step: [Description of next step]

Response to User:
[Response text to user, explaining next operation or requirements]
"""

def format_sys_functions_for_prompt(functions_spec: dict) -> str:
    """
    Format system functions specification for inclusion in prompts
    Currently only supports file interaction functions
    
    Args:
        functions_spec: The functions specification dictionary from SYS module
        
    Returns:
        A formatted string describing available file interaction functions
    """
    # Only include file_interaction functions
    file_functions = {
        "drop_and_read": {
            "category": "File Processing",
            "description": "Read file content (supports .txt, .md, .pdf formats)",
            "params": {
                "file_path": {"required": True, "description": "Path to the file to read"}
            }
        },
        "intelligent_archive": {
            "category": "File Processing", 
            "description": "Smart file archiving based on type and history",
            "params": {
                "file_path": {"required": True, "description": "Path to the file to archive"},
                "target_dir": {"required": False, "description": "Target directory (optional, auto-determined if not provided)"}
            }
        },
        "summarize_tag": {
            "category": "File Processing",
            "description": "Generate file summary and tags using LLM analysis", 
            "params": {
                "file_path": {"required": True, "description": "Path to the file to summarize"},
                "tag_count": {"required": False, "description": "Number of tags to generate (default: 3)"}
            }
        }
    }
    
    formatted_lines = ["## Available System Functions"]
    
    # Group functions by category
    categories = {}
    for func_name, func_data in file_functions.items():
        category = func_data.get("category", "Other")
        if category not in categories:
            categories[category] = []
        
        description = func_data.get("description", "")
        params_info = []
        for param_name, param_data in func_data.get("params", {}).items():
            required = "required" if param_data.get("required", False) else "optional"
            param_desc = param_data.get("description", "")
            params_info.append(f"  - {param_name} ({required}): {param_desc}")
        
        func_info = [
            f"### {func_name}",
            f"{description}",
        ]
        
        if params_info:
            func_info.append("Parameters:")
            func_info.extend(params_info)
        
        categories[category].append("\n".join(func_info))
    
    # Format by category
    for category, funcs in categories.items():
        formatted_lines.append(f"\n# {category}")
        formatted_lines.extend(funcs)
    
    return "\n\n".join(formatted_lines)
