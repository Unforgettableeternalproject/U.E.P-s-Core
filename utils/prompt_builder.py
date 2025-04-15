# utils/prompt_builder.py

from configs.config_loader import load_module_config

def build_prompt(user_input: str, memory: str = "") -> str:

    config = load_module_config("llm_module")
    sys_instructions = config.get("system_instruction", {})

    system_prompt = sys_instructions.get("chat", "你是一個智慧助理，請友善且有邏輯地回應。")

    final_prompt = f"{system_prompt}\n\n"

    if memory:
        final_prompt += f"這是你之前與使用者的對話紀錄摘要：\n{memory}\n\n"

    final_prompt += f"使用者：{user_input}\n\n請用完整語句回應他："

    return final_prompt
