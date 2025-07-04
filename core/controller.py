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

# 測試多步驟工作流程
def test_command_workflow(command_text: str = "幫我整理和摘要桌面上的文件"):
    """測試多步驟指令工作流程"""
    sysmod = modules["sysmod"]
    llm = modules["llm"]

    if sysmod is None or llm is None:
        error_log("[Controller] ❌ 無法載入 SYS 或 LLM 模組")
        return

    info_log(f"[Controller] 測試指令工作流程：'{command_text}'")
    
    # 第一步：LLM 分析指令
    llm_resp = llm.handle({
        "text": command_text,
        "intent": "command",
        "memory": ""
    })
    
    print("\n🧠 LLM 分析指令：", llm_resp.get("text", "[無回應]"))
    
    # 第二步：啟動工作流程（假設為檔案處理類型）
    workflow_resp = sysmod.handle({
        "mode": "start_workflow",
        "params": {
            "workflow_type": "file_processing",
            "command": command_text
        }
    })
    
    session_id = workflow_resp.get("session_id")
    if not session_id:
        error_log("[Controller] ❌ 工作流程啟動失敗")
        return
        
    print(f"\n🔄 工作流程已啟動，ID: {session_id}")
    print(f"🔹 系統提示：{workflow_resp.get('prompt')}")
    
    # 模擬用戶交互
    while workflow_resp.get("requires_input", False):
        # 請求用戶輸入
        user_input = input("\n✍️ 請輸入回應: ")
        
        if user_input.lower() in ("exit", "quit", "取消"):
            # 取消工作流程
            cancel_resp = sysmod.handle({
                "mode": "cancel_workflow",
                "params": {
                    "session_id": session_id,
                    "reason": "用戶取消"
                }
            })
            print(f"\n❌ 工作流程已取消：{cancel_resp.get('message')}")
            break
            
        # 繼續工作流程
        workflow_resp = sysmod.handle({
            "mode": "continue_workflow",
            "params": {
                "session_id": session_id,
                "user_input": user_input
            }
        })
        
        print(f"\n🔄 工作流程步驟 {workflow_resp.get('data', {}).get('step', '?')} 完成")
        print(f"🔹 系統訊息：{workflow_resp.get('message')}")
        
        if workflow_resp.get("requires_input", False):
            print(f"🔹 下一步提示：{workflow_resp.get('prompt')}")
        else:
            # 工作流程完成或異常終止
            status = workflow_resp.get("status")
            if status == "completed":
                print("\n✅ 工作流程成功完成！")
                result_data = workflow_resp.get("data", {})
                if result_data:
                    print("\n📊 工作流程結果:")
                    for key, value in result_data.items():
                        if isinstance(value, str) and len(value) > 100:
                            print(f"  {key}: {value[:100]}...")
                        else:
                            print(f"  {key}: {value}")
            else:
                print(f"\n⚠️ 工作流程異常結束，狀態: {status}")
    
    print("\n==== 工作流程測試結束 ====")

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
                case 5: # 測試檔案摘要標籤工作流程
                    print("=== 測試檔案摘要標籤工作流程 ===")
                    test_summarize_tag_workflow()
                case 6: # 測試一般多步驟工作流程
                    command = input("請輸入指令（如：幫我整理文件）：")
                    if command:
                        test_command_workflow(command)
                    else:
                        print("未輸入指令，取消測試")
                case _:
                    print("未知的子功能選項")
        case _:
            print("未知的功能選項")

def sys_test_workflows(workflow_type: int = 1):
    """測試各種測試工作流程
    
    Args:
        workflow_type: 工作流程類型
            1: simple_echo - 簡單回顯
            2: countdown - 倒數計時
            3: data_collector - 資料收集
            4: random_fail - 隨機失敗
    """
    sysmod = modules["sysmod"]
    if sysmod is None:
        error_log("[Controller] ❌ 無法載入 SYS 模組")
        return
        
    workflow_map = {
        1: "test_workflow_echo",
        2: "test_workflow_countdown",
        3: "test_workflow_data_collector",
        4: "test_workflow_random_fail"
    }
    
    workflow_display_name = {
        1: "簡單回顯",
        2: "倒數計時",
        3: "資料收集",
        4: "隨機失敗"
    }
    
    if workflow_type not in workflow_map:
        error_log(f"[Controller] ❌ 無效的工作流程類型: {workflow_type}")
        return
        
    mode = workflow_map[workflow_type]
    workflow_name = workflow_display_name[workflow_type]
    
    print(f"\n=== 開始測試 {workflow_name} 工作流程 ===")
    
    # 初始參數設定
    params = {}
    if workflow_type == 1:  # simple_echo
        message = input("請輸入要回顯的訊息: ")
        params = {"message": message}
    elif workflow_type == 2:  # countdown
        try:
            count = int(input("請輸入倒數的起始數值 (預設 5): ") or "5")
            params = {"count": count}
        except ValueError:
            print("輸入無效，使用預設值 5")
            params = {"count": 5}
    
    # 啟動工作流程
    resp = sysmod.handle({"mode": mode, "params": params})
    
    print("\n工作流程已啟動!")
    print(f"回應狀態: {resp.get('status', '未知')}")
    print(f"回應訊息: {resp.get('message', '無訊息')}")
    
    # 處理工作流程後續互動
    session_id = resp.get("session_id")
    if not session_id:
        print("無法獲取會話 ID，工作流程可能無法繼續")
        return
    
    # 進入互動循環
    while resp.get("status") == "processing":
        requires_input = resp.get("requires_input", False)
        prompt = resp.get("prompt", "請輸入")
        
        if requires_input:
            print(f"\n{prompt}")
            user_input = input("> ")
            
            # 繼續工作流程
            resp = sysmod.handle({
                "mode": "test_workflow_continue", 
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })
            
            print(f"\n回應狀態: {resp.get('status', '未知')}")
            print(f"回應訊息: {resp.get('message', '無訊息')}")
        else:
            # 工作流程已完成或失敗
            break
    
    print(f"\n=== {workflow_name} 工作流程結束 ===")
    print(f"最終狀態: {resp.get('status', '未知')}")
    print(f"最終訊息: {resp.get('message', '無訊息')}")
    
    # 顯示工作流程結果（如果有）
    if "data" in resp:
        print("\n工作流程結果:")
        print(resp["data"])

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

def test_summarize_tag_workflow(file_path=None):
    """測試檔案摘要標籤的多步驟工作流程"""
    sysmod = modules["sysmod"]
    llm = modules["llm"]

    if sysmod is None or llm is None:
        error_log("[Controller] ❌ 無法載入 SYS 或 LLM 模組")
        return
    
    # 詢問文件路徑（如果未提供）
    if not file_path:
        from utils.debug_file_dropper import open_demo_window
        print("請選擇要摘要的檔案...")
        file_path = open_demo_window()
        if not file_path:
            print("未選擇檔案，取消測試")
            return

    info_log(f"[Controller] 測試檔案摘要標籤工作流程，檔案路徑：'{file_path}'")
    
    # 建立工作流程會話
    session_id = f"summarize-{int(time.time())}"
    
    # 初始化會話數據
    session_data = {
        "step": 1,
        "file_path": file_path,
        "tag_count": 3
    }
    
    # 啟動工作流程
    print("\n🔄 檔案摘要標籤工作流程啟動\n")
    
    # 模擬工作流程的步驟執行
    from modules.sys_module.actions.file_interaction import summarize_tag_workflow
    
    # 循環執行直到工作流程完成或失敗
    while True:
        # 執行當前步驟
        step_result = summarize_tag_workflow(session_data, llm)
        status = step_result.get("status", "")
        message = step_result.get("message", "")
        prompt = step_result.get("prompt", "")
        requires_input = step_result.get("requires_input", False)
        
        # 更新會話數據
        if "session_data" in step_result:
            session_data = step_result["session_data"]
            
        # 顯示步驟結果
        print(f"步驟 {session_data.get('step', '?')} - {message}")
        
        # 檢查是否需要用戶輸入
        if requires_input:
            print(f"\n{prompt}")
            user_input = input("► ")
            
            if user_input.lower() in ("exit", "quit", "取消", "否"):
                print("\n❌ 工作流程已取消")
                break
                
            # 處理用戶輸入
            if "file_path" in prompt.lower():
                session_data["file_path"] = user_input
            elif "重試" in prompt:
                if user_input.lower() in ("y", "yes", "是", "是的"):
                    # 重新執行當前步驟
                    continue
                else:
                    print("\n❌ 工作流程已取消")
                    break
        
        # 檢查工作流程狀態
        if status == "completed":
            print(f"\n✅ 檔案摘要標籤工作流程成功完成！")
            if "result" in step_result:
                result = step_result["result"]
                print(f"\n📊 摘要檔案：{result.get('summary_file', '未生成')}")
                print(f"🏷️ 標籤：{', '.join(result.get('tags', ['無標籤']))}")
            break
        elif status == "error":
            print(f"\n⚠️ 工作流程出錯：{message}")
            if not requires_input:
                break
        
    print("\n==== 檔案摘要標籤工作流程測試結束 ====")

def sys_list_test_workflows():
    """列出所有可用的測試工作流程"""
    print("\n=== 可用的測試工作流程 ===")
    print("1. simple_echo - 簡單回顯工作流程")
    print("   - 單步驟工作流程")
    print("   - 測試工作流程機制的基本功能")
    print("   - 接受一個訊息並回顯它")
    print()
    print("2. countdown - 倒數計時工作流程")
    print("   - 多步驟工作流程")
    print("   - 測試工作流程中的狀態保持")
    print("   - 從指定數字開始倒數計時直到零")
    print()
    print("3. data_collector - 資料收集工作流程")
    print("   - 多步驟工作流程")
    print("   - 測試工作流程中的用戶輸入處理")
    print("   - 收集各種用戶資訊並在最後匯總")
    print()
    print("4. random_fail - 隨機失敗工作流程")
    print("   - 多步驟工作流程")
    print("   - 測試工作流程的錯誤處理")
    print("   - 在隨機步驟可能失敗，以測試錯誤恢復機制")