from logging import config
from core.registry import get_module
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log
from module_tests.integration_tests import (
    integration_test_SN as itSN,
    integration_test_SM as itSM,
    integration_test_NM as itNM,
    integration_test_SNM as itSNM,
)
import time

config = load_config()
enabled = config.get("modules_enabled", {})

def safe_get_module(name):
    if not enabled.get(name, False):
        # print(f"[Controller] ❌ 模組 '{name}' 未啟用，請檢查配置") # Ignored
        return None

    info_log(f"[Controller] 嘗試載入模組 '{name}'")

    try:
        mod = get_module(name)
        if mod is None:
            raise ImportError(f"{name} register() 回傳為 None")
        info_log(f"[Controller] ✅ 載入模組成功：{name}")
        return mod
    except NotImplementedError:
        error_log(f"[Controller] ❌ 模組 '{name}' 尚未被實作")
        return None
    except Exception as e:
        error_log(f"[Controller] ❌ 無法載入模組 '{name}': {e}")
        return None

modules = {
    "stt": safe_get_module("stt_module"),
    "nlp": safe_get_module("nlp_module"),
    "llm": safe_get_module("llm_module"), 
    "mem": safe_get_module("mem_module"),
    "tts": safe_get_module("tts_module"),
    "sysmod": safe_get_module("sys_module")
}

# 模組載入

def load_module_test():
    pass

# 測試 STT 模組

def on_stt_result(text):
    print("✨ 回傳語音內容：", text)

def stt_test_single():
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return

    # 測試 STT 模組
    result = stt.handle()
    on_stt_result(result["text"])

def stt_test_realtime():
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return

    stt.start_realtime(on_result=on_stt_result)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        stt.stop_realtime()

# 測試 NLP 模組

def nlp_test(cases=""):
    nlp = modules["nlp"]

    if nlp is None:
        error_log("[Controller] ❌ 無法載入 NLP 模組")
        return

    test_cases = [cases] if cases != "" else [
        "Hello, it's me, your friend Bernie!",
        "Do a barrel roll.",
        "Do you like among us?",
        "gogogoog"
    ]

    debug_log(1, f"[NLP] 測試文本: {test_cases}")

    for text in test_cases:
        result = nlp.handle({"text": text})
        print(f"\n🧠 NLP 輸出結果：{result['text']} 對應的是 {result['label']}，程式決定進行 {result['intent']}\n")

# 測試 MEM 模組

def mem_fetch_test(text : str = ""):
    mem = modules["mem"]
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return

    result = mem.handle(
        {"mode": "fetch", "text": ("Test chat" if text == "" else text)})

    if result["status"] == "empty":
        print("\n🧠 MEM 回傳：查無相關記憶")
        return

    print(f"\n🧠 MEM 輸出結果：\n\n使用者: {result['results'][0]['user']} \n回應: {result['results'][0]['response']}")


def mem_store_test(user_text : str = "Test chat", response_text : str = "Test response"):
    mem = modules["mem"]
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return

    result = mem.handle(
        {"mode": "store", "entry": {"user": user_text, "response": response_text}})
    print("\n🧠 MEM 回傳：", "儲存" + ("成功" if result["status"] == "stored" else "失敗"))

def mem_clear_test(text : str = "ALL", top_k : int = 1):
    mem = modules["mem"]
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return

    result = mem.handle(
        {"mode": "clear_all" if text == "ALL" else "clear_by_text", "text": text, "top_k": top_k})
    print("\n🧠 MEM 回傳：", "清除" +
          ("成功" if result["status"] == "cleared" else "失敗"))

# 測試 LLM 模組

def llm_test_chat(text):
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
    print("⚙️ 系統指令：", result.get("sys_action"))

# 整合測試

def integration_test_SN():
    itSN(modules)

def integration_test_SM():
    itSM(modules)

def integration_test_NM():
    itNM(modules)

def integration_test_SNM():
    itSNM(modules)