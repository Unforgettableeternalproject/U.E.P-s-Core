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

    for group in chunks:
        text_block = "\n".join(group)
        summary = _summarizer(text_block, max_length=120, min_length=20, do_sample=False)[0]["summary_text"]
        summaries.append(summary)

    return "\n".join(summaries)

def build_prompt(user_input: str, memory: str = "", intent: str = "chat") -> str:
    config = load_module_config("llm_module")
    instructions = config.get("system_instruction", {})
    
    prompt_parts = []

    # 基本指示詞
    if "main" in instructions:
        prompt_parts.append(instructions["main"])
    if intent in instructions:
        prompt_parts.append(instructions[intent])

    debug_log(3, f"[LLM] 指示詞組合階段一: {prompt_parts}")

    # 加入記憶
    if memory:
        prompt_parts.append("這是你過去與使用者的對話摘要：\n" + memory)

    debug_log(3, f"[LLM] 指示詞組合階段二: {prompt_parts}")

    # 使用者輸入
    prompt_parts.append(f"使用者：{user_input}")

    debug_log(3, f"[LLM] 指示詞組合階段三: {prompt_parts}")

    return "\n\n".join(prompt_parts)
