from logging import config
from os import error
from core.registry import get_module
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log
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

def handle_pipeline():
    stt = modules["stt"]
    nlp = modules["nlp"]
    llm = modules["llm"]
    mem = modules["mem"]
    tts = modules["tts"]
    sysmod = modules["sysmod"]

    if any(mod is None for mod in modules.values()):
        error_log("[Controller] ❌ 無法載入所有模組，請檢查模組註冊狀態")
        return "模組載入失敗"

    # Step 1: 取得語音輸入並轉為文字
    audio_text = stt.handle({})["text"]

    # Step 2: NLP 模組判斷 intent
    nlp_result = nlp.handle({"text": audio_text})
    intent = nlp_result.get("intent")
    detail = nlp_result.get("detail")

    # Step 3: 分流處理（聊天或指令）
    if intent == "chat":
        snapshot = mem.handle({"mode": "fetch", "text": audio_text})
        llm_result = llm.handle({
            "text": audio_text,
            "intent": "chat",
            "snapshot": snapshot
        })
        mem.handle({"mode": "store", "entry": llm_result["log"]})
    elif intent == "command":
        available_sys = sysmod.handle({"mode": "list_functions"})
        llm_result = llm.handle({
            "text": audio_text,
            "intent": "command",
            "available": available_sys,
            "detail": detail
        })
        sysmod.handle(llm_result.get("sys_action", {}))
    else:
        llm_result = {"text": "我不太明白你的意思..."}

    # Step 4: 輸出給 TTS 和 UI
    tts.handle({
        "text": llm_result.get("text"),
        "emotion": llm_result.get("emotion", "neutral")
    })

    return llm_result.get("text")

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
    # 測試 NLP 模組
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
    for text in test_cases:
        result = nlp.handle({"text": text})
        print(f"🧠 NLP 輸出結果：{result}\n")

# 統合測試

def integration_test_StN():
    stt = modules["stt"]
    nlp = modules["nlp"]

    if stt is None or nlp is None:
        error_log("[Controller] ❌ 無法載入 STT 或 NLP 模組")
        return

    # 測試STT到NLP的整合
    
    result = stt.handle()
    print("✨ 回傳語音內容：", result["text"])

    nlp_result = nlp.handle({"text": result["text"]})
    print("🧠 NLP 輸出結果：", nlp_result)