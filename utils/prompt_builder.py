# utils/prompt_builder.py
from transformers import pipeline
from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log

_summarizer = pipeline("summarization", model="philschmid/bart-large-cnn-samsum")

def chunk_and_summarize_memories(memories: list[str], chunk_size: int = 3) -> str: # To be used later
    """
    Chunk and summarize multiple memories into prompt prefix.
    """
    chunks = [memories[i:i+chunk_size] for i in range(0, len(memories), chunk_size)]
    summaries = []

    debug_log_e(2, f"[LLM] 記憶切塊大小: {chunk_size}")
    debug_log_e(3, f"[LLM] 記憶切塊: {chunks}")

    for group in chunks:
        text_block = "\n".join(group)
        summary = _summarizer(text_block, max_length=120, min_length=20, do_sample=False)[0]["summary_text"]
        summaries.append(summary)

    debug_log(3, f"[LLM] 記憶摘要: {summaries}")

    return "\n".join(summaries)

def build_prompt(user_input: str, memory: str = "", intent: str = "chat", **kwargs) -> str:
    """
    Build LLM prompt text
    
    Args:
        user_input: User input text
        memory: Memory summary
        intent: Intent type (chat, command, etc.)
        **kwargs: Additional parameters, such as workflow-related information
            is_internal: Whether this is a system internal call (True/False)
    
    Returns:
        Complete prompt text
    """
    config = load_module_config("llm_module")
    instructions = config.get("system_instruction", {})
    is_internal = kwargs.get("is_internal", False)
    
    prompt_parts = []

    # System instructions are only added for external calls (UEP personality role)
    # Internal calls don't need system instructions, keeping purely functional
    if not is_internal:
        if "main" in instructions:
            prompt_parts.append(instructions["main"])
        if intent in instructions:
            prompt_parts.append(instructions[intent])

    debug_log(3, f"[LLM] 指示詞組合階段一: {prompt_parts}")

    # Add memory (only needed when communicating with users)
    if memory and not is_internal:
        prompt_parts.append("Here is a summary of your past conversations with the user:\n" + memory)

    debug_log(3, f"[LLM] 指示詞組合階段二: {prompt_parts}")
    
    # Specific intent handling
    if intent == "command":
        # If it's a command intent, use the dedicated command processing prompt template
        from utils.prompt_templates import build_command_prompt
        
        # Function list is prioritized from kwargs, if not available use static description
        available_functions = kwargs.get("available_functions", "")
        if not available_functions:
            # Use static function description as fallback (only file processing functions)
            available_functions = """
## Available System Functions

# File Processing
### drop_and_read
Read dragged or specified file content
Parameters:
  - file_path (required): Path to the file to read

### intelligent_archive  
Smart file archiving to appropriate folders
Parameters:
  - file_path (required): Source file path to archive
  - target_dir (optional): Target directory (auto-determined if not provided)

### summarize_tag
Generate file summary and tags
Parameters:
  - file_path (required): File path
  - tag_count (optional): Number of tags to generate (default: 3)
"""
        
        try:
            command_prompt = build_command_prompt(
                command=user_input,
                available_functions=available_functions
            )
            prompt_parts.append(command_prompt)
        except Exception as e:
            error_log(f"[LLM] 構建指令提示時發生錯誤: {e}")
            prompt_parts.append(f"User command: {user_input}")
    elif "workflow_step" in kwargs:
        # Workflow step processing
        from utils.prompt_templates import build_workflow_step_prompt
        
        step_info = kwargs.get("workflow_step", {})
        workflow_prompt = build_workflow_step_prompt(
            step_number=step_info.get("step_number", 1),
            total_steps=step_info.get("total_steps", 1),
            workflow_type=step_info.get("workflow_type", "unknown"),
            command=step_info.get("command", user_input),
            step_description=step_info.get("description", ""),
            user_input=user_input
        )
        prompt_parts.append(workflow_prompt)
    else:
        # General user input
        prompt_parts.append(f"User: {user_input}")

    debug_log(3, f"[LLM] 指示詞組合階段三: {prompt_parts}")

    return "\n\n".join(prompt_parts)
