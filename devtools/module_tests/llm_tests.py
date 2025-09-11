# -*- coding: utf-8 -*-
"""
LLM 模組測試函數
⚠️ 未重構模組 - 使用傳統模組呼叫方式
"""

from utils.debug_helper import debug_log, info_log, error_log

# ⚠️ 未重構模組標註
# 以下測試函數適用於尚未重構的 LLM 模組

def llm_test_chat(modules, text):
    llm = modules.get("llm")
    if llm is None:
        error_log("[Controller] ❌ 無法載入 LLM 模組")
        return

    memory = "No relevant memory found."  

    result = llm.handle({
        "text": text,
        "intent": "chat",
        "memory": memory
    })

    print("🧠 Gemini 回應：", result.get("text", "[無回應]"))
    print("🧭 心情標記（mood）：", result.get("mood", "neutral"))
    # print("⚙️ 系統指令：", result.get("sys_action")) 因為是聊天測試所以這個應該不需要

def llm_test_command(modules, text):
    llm = modules.get("llm")
    if llm is None:
        error_log("[Controller] ❌ 無法載入 LLM 模組")
        return

    memory = "No relevant memory found."  

    result = llm.handle({
        "text": text,
        "intent": "command",
        "memory": memory
    })

    print("🧠 Gemini 指令分析：", result.get("text", "[無回應]"))
    print("🧭 心情標記（mood）：", result.get("mood", "neutral"))
    print("⚙️ 系統指令：", result.get("sys_action"))
    print("📋 指令類型：", result.get("sys_action", {}).get("action", "無") if isinstance(result.get("sys_action"), dict) else "無")