from logging import config
from core.registry import get_module
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log
from utils.debug_file_dropper import open_demo_window, open_folder_dialog
from module_tests.integration_tests import *
import tkinter as tk
from tkinterdnd2 import TkinterDnD
import time
import asyncio

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

    result = asyncio.run(tts.handle({
        "text": text,
        "mood": mood,
        "save": save
    }))
    
    if result["status"] == "error":
        print("\n❌ TTS 錯誤：", result["message"])
    elif result["status"] == "processing":
        print("\n⏳ TTS 處理中，分為", result.get("chunk_count", "未知"), "個區塊...")
    else:
        if save:
            print("\n✅ TTS 成功，音檔已經儲存到", result["output_path"])
        else: 
            print("\n✅ TTS 成功，音檔已經被撥放\n")

# 測試 SYS 模組

def sys_list_functions():
    sysmod = modules["sysmod"]

    if sysmod is None:
        error_log("[Controller] ❌ 無法載入 SYS 模組")
        return

    resp = sysmod.handle({"mode": "list_functions", "params": {}})

    print("=== SYS 功能清單 ===")
    import json
    print(json.dumps(resp.get("data", {}), ensure_ascii=False, indent=2))


def sys_test_functions(mode : int = 1, sub : int = 1): 
    sysmod = modules["sysmod"]
    if sysmod is None:
        error_log("[Controller] ❌ 無法載入 SYS 模組")
        return

    match mode:
        case 1: # 檔案互動功能
            info_log("[Controller] 開啟檔案互動功能")
            match sub:
                case 1: # Drop and Read
                    file_path = open_demo_window()
                    resp = sysmod.handle({"mode": "drop_and_read", "params": {"file_path": file_path}})
                    print(resp.get("data", {}))
                case 2: # Intelligent Archive
                    file_path = open_demo_window()
                    resp = sysmod.handle({"mode": "intelligent_archive", "params": {"file_path": file_path}})
                    print("=== SYS 智能歸檔功能 ===")
                    print(f"檔案已歸檔至: {resp.get('data', '未知位置')}")                
                case 3: # Intelligent Archive with target directory
                    file_path = open_demo_window()
                    print("請選擇目標資料夾...")
                    target_dir = open_folder_dialog()
                    params = {"file_path": file_path}
                    if target_dir:
                        params["target_dir"] = target_dir
                        print(f"已選擇目標資料夾: {target_dir}")
                    else:
                        print("未選擇目標資料夾，將使用系統自動選擇")
                    resp = sysmod.handle({"mode": "intelligent_archive", "params": params})
                    print("=== SYS 智能歸檔功能 (指定目錄) ===")
                    print(f"檔案已歸檔至: {resp.get('data', '未知位置')}")
                case 4: # Summarize Tag
                    file_path = open_demo_window()
                    resp = sysmod.handle({"mode": "summarize_tag", "params": {"file_path": file_path}})
                    print("=== SYS 檔案摘要標記功能 ===")
                    result = resp.get("data", {})
                    if isinstance(result, dict):
                        print(f"摘要檔案位置: {result.get('summary_file', '未生成')}")
                        print(f"生成標籤: {', '.join(result.get('tags', ['無標籤']))}")
                    else:
                        print(f"結果: {result}")
                case _:
                    print("未知的子功能選項")
        case _:
            print("未知的功能選項")

# 整合測試

def integration_test_SN():
    itSN(modules)

def integration_test_SM():
    itSM(modules)

def integration_test_SL():
    itSL(modules)

def integration_test_ST():
    itST(modules)

def integration_test_NM():
    itNM(modules)

def integration_test_NL():
    itNL(modules)

def integration_test_NT():
    itNT(modules)

def integration_test_ML():
    itML(modules)

def integration_test_LT():
    itLT(modules)

def integration_test_SNM():
    itSNM(modules)

def integration_test_SNL():
    itSNL(modules)

def integration_test_NML():
    itNML(modules)

def integration_test_SNML():
    itSNML(modules)

def integration_test_NMLT():
    itNMLT(modules)

def integration_test_SNMLT():
    itSNMLT(modules)

# 額外測試

def test_summrize():
    test_chunk_and_summarize()

# 測試 SYS 模組

def integration_test_Y():
    """測試 SYS 模組的檔案操作功能"""
    if modules["sysmod"] is None:
        error_log("[Test] SYS 模組未啟用，無法進行測試")
        return False
    
    info_log("[Test] 開始測試 SYS 模組")
    
    # 測試參數
    import os
    import tempfile
    
    # 建立測試檔案
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode='w', encoding='utf-8') as tmp:
        tmp.write("這是一個測試檔案，用於測試 SYS 模組的檔案操作功能。\n")
        tmp.write("系統模組應該能夠正確讀取這個檔案並進行處理。\n")
        test_file_path = tmp.name
    
    info_log(f"[Test] 測試檔案已建立：{test_file_path}")
    
    try:
        # 測試 drop_and_read 功能
        info_log("[Test] 測試 drop_and_read 功能")
        content = modules["sysmod"].handle({
            "mode": "drop_and_read",
            "params": {
                "file_path": test_file_path
            }
        })
        
        if "content" in content and len(content["content"]) > 0:
            info_log("[Test] drop_and_read 測試成功")
        else:
            error_log("[Test] drop_and_read 測試失敗")
        
        # 測試 summarize_tag 功能
        info_log("[Test] 測試 summarize_tag 功能")
        summary_result = modules["sysmod"].handle({
            "mode": "summarize_tag",
            "params": {
                "file_path": test_file_path,
                "tag_count": 2
            }
        })
        
        if "summary_file" in summary_result:
            info_log(f"[Test] summarize_tag 測試成功，生成摘要檔案：{summary_result['summary_file']}")
        else:
            error_log("[Test] summarize_tag 測試失敗")
        
        # 測試 intelligent_archive 功能
        info_log("[Test] 測試 intelligent_archive 功能")
        archive_result = modules["sysmod"].handle({
            "mode": "intelligent_archive",
            "params": {
                "file_path": test_file_path
            }
        })
        
        if "new_path" in archive_result:
            info_log(f"[Test] intelligent_archive 測試成功，檔案已歸檔至：{archive_result['new_path']}")
        else:
            error_log("[Test] intelligent_archive 測試失敗")
            
        info_log("[Test] SYS 模組測試完成")
        return True
        
    except Exception as e:
        error_log(f"[Test] SYS 模組測試失敗：{e}")
        return False
    finally:
        # 清理測試檔案
        try:
            if os.path.exists(test_file_path):
                os.remove(test_file_path)
            info_log("[Test] 測試檔案已清理")
        except:
            error_log("[Test] 無法清理測試檔案")

def integration_test_TY():
    """測試 TTS + SYS 模組整合：檔案朗讀功能"""
    if modules["tts"] is None or modules["sysmod"] is None:
        error_log("[Test] TTS 或 SYS 模組未啟用，無法進行整合測試")
        return False
    
    info_log("[Test] 開始測試 TTS+SYS 模組整合")
    
    # 測試參數
    import os
    import tempfile
    
    # 建立測試檔案
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode='w', encoding='utf-8') as tmp:
        tmp.write("這是一個整合測試檔案。\n")
        tmp.write("系統模組讀取檔案後，語音合成模組應該能夠將內容轉換為語音。\n")
        test_file_path = tmp.name
    
    info_log(f"[Test] 測試檔案已建立：{test_file_path}")
    
    try:
        # 先使用 SYS 模組讀取檔案
        file_content = modules["sysmod"].handle({
            "mode": "drop_and_read",
            "params": {
                "file_path": test_file_path
            }
        })
        
        if "content" not in file_content or not file_content["content"]:
            error_log("[Test] SYS 模組讀取檔案失敗")
            return False
            
        info_log("[Test] SYS 模組成功讀取檔案")
        
        # 再使用 TTS 模組將內容轉換為語音
        tts_result = modules["tts"].handle({
            "text": file_content["content"],
            "options": {
                "speed": 1.0,
                "volume": 1.0
            }
        })
        
        if "audio_path" in tts_result:
            info_log(f"[Test] TTS 模組成功生成語音檔案：{tts_result['audio_path']}")
            info_log("[Test] TTS+SYS 模組整合測試成功")
            return True
        else:
            error_log("[Test] TTS 模組生成語音失敗")
            return False
            
    except Exception as e:
        error_log(f"[Test] TTS+SYS 模組整合測試失敗：{e}")
        return False
    finally:
        # 清理測試檔案
        try:
            if os.path.exists(test_file_path):
                os.remove(test_file_path)
            info_log("[Test] 測試檔案已清理")
        except:
            error_log("[Test] 無法清理測試檔案")