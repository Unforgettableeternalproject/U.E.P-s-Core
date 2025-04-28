from logging import config
from core.registry import get_module
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log
from module_tests.integration_tests import *
import time, asyncio

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
    "mem": safe_get_module("mem_module"),
    "llm": safe_get_module("llm_module"), 
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


def mem_list_all_test(page : int = 1):
    mem = modules["mem"]
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return

    result = mem.handle({"mode": "list_all", "page": page})

    if result["status"] == "empty":
        print("\n🧠 MEM 回傳：查無相關記憶")
        return

    if result["status"] == "failed":
        print("\n🧠 MEM 回傳：記憶查詢有誤 (也許是頁碼問題)")
        return
    
    for i, record in enumerate(result["records"], start=1):
        print(f"記錄 {i}: 使用者: {record['user']}，回應: {record['response']}")

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

# 測試 TTS 模組

def tts_test(text, mood="neutral", save=False):
    tts = modules["tts"]
    if tts is None:
        error_log("[Controller] ❌ 無法載入 TTS 模組")
        return
    if not text:
        error_log("[Controller] ❌ TTS 測試文本為空")
        return
    
    # 获取或创建事件循环
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    # 同步调用异步方法
    if hasattr(tts, 'handle_async'):
        # 如果 TTS 模块有异步 handle 方法
        result = loop.run_until_complete(tts.handle_async({"text": text, "mood": mood, "save": save}))
    else:
        # 如果 TTS 模块仍然是同步方法
        result = tts.handle({"text": text, "mood": mood, "save": save})
    
    if result["status"] == "error":
        print("\n❌ TTS 錯誤：", result["message"])
    elif result["status"] == "processing":
        print("\n⏳ TTS 處理中，分為", result.get("chunk_count", "未知"), "個區塊...")
        
        # 如果是异步处理中，可以选择等待完成或直接返回
        if result.get("is_chunked", False) and hasattr(tts, 'get_queue_status'):
            print("等待处理完成...")
            while True:
                status = tts.get_queue_status()
                if status["queue_length"] == 0 and not status["is_playing"]:
                    print("处理完成！")
                    break
                time.sleep(0.5)
    else:
        if save:
            print("\n✅ TTS 成功，音檔已經儲存到", result["output_path"])
        else: 
            print("\n✅ TTS 成功，音檔已經被撥放\n")

# 整合測試

def integration_test_SN():
    itSN(modules)

def integration_test_SM():
    itSM(modules)

def integration_test_SL():
    itSL(modules)

def integration_test_NM():
    itNM(modules)

def integration_test_NL():
    itNL(modules)

def integration_test_ML():
    itML(modules)

def integration_test_SNM():
    itSNM(modules)

def integration_test_SNL():
    itSNL(modules)

def integration_test_NML():
    itNML(modules)

def integration_test_SNML():
    itSNML(modules)

# 額外測試

def test_summrize():
    test_chunk_and_summarize()