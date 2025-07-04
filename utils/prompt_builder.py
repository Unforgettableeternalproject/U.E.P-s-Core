# utils/prompt_builder.py
from transformers import pipeline
from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log

_summarizer = pipeline("summarization", model="philschmid/bart-large-cnn-samsum")

def chunk_and_summarize_memories(memories: list[str], chunk_size: int = 3) -> str: # 之後才會用到
    """
    將多筆記憶切塊並摘要整合成 prompt 前段。
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
    構建 LLM 提示文本
    
    Args:
        user_input: 使用者輸入文本
        memory: 記憶摘要
        intent: 意圖類型 (chat, command 等)
        **kwargs: 額外參數，例如工作流程相關資訊
            is_internal: 是否是系統內部呼叫 (True/False)
    
    Returns:
        完整的提示文本
    """
    config = load_module_config("llm_module")
    instructions = config.get("system_instruction", {})
    is_internal = kwargs.get("is_internal", False)
    
    prompt_parts = []

    # 只有與用戶溝通時才加入系統指示詞，內部系統呼叫時不需要
    if not is_internal:
        # 基本指示詞
        if "main" in instructions:
            prompt_parts.append(instructions["main"])
        if intent in instructions:
            prompt_parts.append(instructions[intent])

    debug_log(3, f"[LLM] 指示詞組合階段一: {prompt_parts}")

    # 加入記憶 (只有與用戶溝通時才需要)
    if memory and not is_internal:
        prompt_parts.append("這是你過去與使用者的對話摘要：\n" + memory)

    debug_log(3, f"[LLM] 指示詞組合階段二: {prompt_parts}")
    
    # 特定意圖處理
    if intent == "command":
        # 如果是指令意圖，使用專用的指令處理提示模板
        from utils.prompt_templates import build_command_prompt
        from modules.sys_module.sys_module import SYSModule
        
        # 獲取可用功能列表
        try:
            # 簡單描述可用功能列表，而不是實際創建模組實例
            available_functions = """
以下是系統可執行的功能：
1. 檔案操作
   - 讀取檔案內容 (drop_and_read)
   - 智能歸檔檔案 (intelligent_archive)
   - 為檔案生成摘要與標籤 (summarize_tag)

2. 視窗控制
   - 移動視窗位置 (push_window)
   - 摺疊視窗 (fold_window)
   - 切換工作區 (switch_workspace)
   - 螢幕截圖與標註 (screenshot_and_annotate)

3. 文字處理
   - 剪貼簿追蹤與搜尋 (clipboard_tracker)
   - 快速文字範本 (quick_phrases)
   - 圖像OCR文字提取 (ocr_extract)

4. 自動化輔助
   - 設定提醒 (set_reminder)
   - 生成備份腳本 (generate_backup_script)
   - 資料夾監控 (monitor_folder)

5. 整合功能
   - 新聞摘要 (news_summary)
   - 天氣查詢 (get_weather)
   - 世界時間查詢 (get_world_time)
   - 程式碼分析 (code_analysis)
   - 媒體控制 (media_control)
"""
            command_prompt = build_command_prompt(
                command=user_input,
                available_functions=available_functions
            )
            prompt_parts.append(command_prompt)
        except Exception as e:
            error_log(f"[LLM] 構建指令提示時發生錯誤: {e}")
            prompt_parts.append(f"使用者指令：{user_input}")
    elif "workflow_step" in kwargs:
        # 工作流程步驟處理
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
        # 一般用戶輸入
        prompt_parts.append(f"使用者：{user_input}")

    debug_log(3, f"[LLM] 指示詞組合階段三: {prompt_parts}")

    return "\n\n".join(prompt_parts)
