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
        # print(f"[Controller] [X] 模組 '{name}' 未啟用，請檢查配置") # Ignored
        return None

    info_log(f"[Controller] 嘗試載入模組 '{name}'")

    try:
        mod = get_module(name)
        if mod is None:
            raise ImportError(f"{name} register() 回傳為 None")
        info_log(f"[Controller] [OK] 載入模組成功：{name}")
        return mod
    except NotImplementedError:
        error_log(f"[Controller] [X] 模組 '{name}' 尚未被實作")
        return None
    except Exception as e:
        error_log(f"[Controller] [X] 無法載入模組 '{name}': {e}")
        return None

modules = {
    "stt": safe_get_module("stt_module"),
    "nlp": safe_get_module("nlp_module"),
    "mem": safe_get_module("mem_module"),
    "llm": safe_get_module("llm_module"), 
    "tts": safe_get_module("tts_module"),
    "sysmod": safe_get_module("sys_module")
}

# 測試 STT 模組 - Phase 2 版本

def on_stt_result(result):
    """STT 結果回調函數 - 支援 Phase 2 格式"""
    # 首先檢查結果是否為 None 或非字典（處理錯誤情況）
    if result is None:
        print("❌ 語音識別失敗：沒有識別結果")
        return
        
    if isinstance(result, dict):
        # 提取基本信息
        text = result.get("text", "")
        stt_confidence = result.get("confidence", 0)  # 語音識別信心度
        speaker_info = result.get("speaker_info")
        activation_reason = result.get("activation_reason", "未提供判斷原因")
        should_activate = result.get("should_activate", False)  # 獲取是否應該啟動標誌
        error = result.get("error")  # 檢查是否有錯誤訊息
        
        # 處理錯誤情況
        if error:
            print(f"❌ 語音識別錯誤：{error}")
            return
            
        # 沒有識別出文字的情況
        if not text:
            print("🔇 未識別到有效語音內容")
            return
        
        # 拆分啟動原因中的信心度
        activation_confidence = 0
        # 安全檢查 activation_reason 是否為字符串類型
        if activation_reason and isinstance(activation_reason, str) and "智能判斷分數:" in activation_reason:
            try:
                confidence_part = activation_reason.split("智能判斷分數:")[1].strip()
                if confidence_part:
                    activation_confidence = float(confidence_part)
            except:
                pass
        
        # 顯示語音辨識結果，總是顯示識別到的文字
        print(f"\n📢 即時語音識別: 「{text}」")
        
        # 顯示結果，區分是否應該啟動
        if should_activate:
            print(f"✓ 智能啟動觸發！")
            print(f"   識別信心度：{stt_confidence:.2f}")
            print(f"   啟動原因：{activation_reason}")
        else:
            # 非啟動時只顯示簡略資訊，不干擾監聽流程
            if activation_confidence > 0:
                print(f"   (未觸發啟動，智能判斷分數: {activation_confidence:.2f})")
            else:
                print(f"   (未觸發啟動)")
                
        # 如果有說話人識別資訊，顯示說話人資訊
        if speaker_info:
        
        # 顯示說話人信息
        if speaker_info:
            speaker_id = speaker_info.get("speaker_id", "Unknown")
            confidence = speaker_info.get("confidence", 0)
            is_new = "(新說話人)" if speaker_info.get("is_new_speaker", False) else ""
            print(f"   🔊 說話人：{speaker_id} {is_new} (信心度: {confidence:.2f})")
            
        # 如果應該啟動，返回處理結果到下一步
        if should_activate:
            # 這裡可以觸發後續處理邏輯
            info_log(f"[Controller] 觸發後續處理：{text}")
            # TODO: 呼叫下一個處理模組
            
    else:
        # 舊版相容性
        print(f"✨ 回傳語音內容：{result}")

def stt_test_single(mode="manual", enable_speaker_id=True, language="en-US"):
    """單次 STT 測試 - Phase 2 版本"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return

    print(f"🎤 STT 測試模式: {mode}")
    
    # Phase 2 API 調用
    result = stt.handle({
        "mode": mode,
        "language": language,
        "enable_speaker_id": enable_speaker_id,
        "duration": 5
    })
    
    on_stt_result(result)
    return result

def stt_test_smart_activation():
    """智能啟動測試 - 手動錄音 + 智能判斷"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return

    print("🧠 智能啟動測試 - 錄音後智能判斷是否啟動")
    print("   試試說: 'UEP', 'help me', 'what is...', 'can you...' 等")
    
    result = stt.handle({
        "mode": "smart",
        "language": "en-US",  # 改為英文識別
        "enable_speaker_id": True,
        "context": "controller_test"
    })
    
    on_stt_result(result)
    return result

def stt_test_background_smart(duration=60):
    """智能背景監聽測試 - 背景持續監聽 + 智能啟動"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return

    print(f"🔄 智能背景監聽測試 ({duration}秒)")
    print("說 'UEP', 'help me', 'what is', 'can you' 等觸發詞，系統會智能判斷是否啟動")
    
    def smart_background_callback(result):
        print(f"🤖 智能觸發:")
        on_stt_result(result)
    
    try:
        # 啟動背景監聽，確保正確傳遞持續時間
        stt.start_always_on(callback=smart_background_callback, duration=duration)
        
        # 等待指定時間 (由模組自己計時)
        try:
            # 只需要等待終止信號
            while getattr(stt, '_always_on_running', False):
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n⏹️ 用戶中斷")
            
    finally:
        stt.stop_always_on()

def stt_test_realtime():
    """舊版 realtime 測試 (保持相容性)"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return

    # 檢查是否有舊版 start_realtime 方法
    if hasattr(stt, 'start_realtime'):
        stt.start_realtime(on_result=on_stt_result)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            stt.stop_realtime()
    else:
        # 使用新版智能背景監聽代替
        print("⚠️ 使用新版智能背景監聽代替 realtime")
        stt_test_background_smart()

def stt_get_stats():
    """獲取 STT 統計信息"""
    stt = modules["stt"]

    if stt is None:
        error_log("[Controller] ❌ 無法載入 STT 模組")
        return

    if hasattr(stt, 'get_speaker_stats'):
        speaker_stats = stt.get_speaker_stats()
        activation_stats = stt.get_activation_stats()
        
        print("📊 STT 統計信息:")
        print("說話人統計:")
        for speaker_id, count in speaker_stats.items():
            print(f"  {speaker_id}: {count} 次")
        
        print("啟動統計:")
        for reason, count in activation_stats.items():
            print(f"  {reason}: {count} 次")
            
        return {"speaker_stats": speaker_stats, "activation_stats": activation_stats}
    else:
        print("⚠️ 當前版本不支援統計功能")

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
    # print("⚙️ 系統指令：", result.get("sys_action")) 因為是聊天測試所以這個應該不需要

def llm_test_command(text):
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
        case 1: # 檔案互動功能 (僅工作流程模式)
            info_log("[Controller] 開啟檔案互動功能 (工作流程模式)")
            match sub:
                case 1: # 測試檔案工作流程 - Drop and Read
                    print("=== 測試檔案讀取工作流程 ===")
                    test_file_workflow("drop_and_read")
                case 2: # 測試檔案工作流程 - Intelligent Archive
                    print("=== 測試智慧歸檔工作流程 ===")
                    test_file_workflow("intelligent_archive")
                case 3: # 測試檔案工作流程 - Summarize Tag
                    print("=== 測試摘要標籤工作流程 ===")
                    test_file_workflow("summarize_tag")
                case 4: # 測試一般多步驟工作流程
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
            1: echo - 簡單回顯
            2: countdown - 倒數計時
            3: data_collector - 資料收集
            4: random_fail - 隨機失敗
            5: tts_test - TTS文字轉語音測試
    """
    sysmod = modules["sysmod"]
    if sysmod is None:
        error_log("[Controller] ❌ 無法載入 SYS 模組")
        return
        
    workflow_map = {
        1: "echo",
        2: "countdown", 
        3: "data_collector",
        4: "random_fail",
        5: "tts_test"
    }
    
    workflow_display_name = {
        1: "簡單回顯",
        2: "倒數計時",
        3: "資料收集",
        4: "隨機失敗",
        5: "TTS文字轉語音"
    }
    
    if workflow_type not in workflow_map:
        error_log(f"[Controller] ❌ 無效的工作流程類型: {workflow_type}")
        return
        
    workflow_name = workflow_display_name[workflow_type]
    workflow_type_name = workflow_map[workflow_type]
    
    print(f"\n=== 開始測試 {workflow_name} 工作流程 ===")
    
    # 啟動工作流程（使用統一的 start_workflow 模式）
    resp = sysmod.handle({
        "mode": "start_workflow", 
        "params": {
            "workflow_type": workflow_type_name,
            "command": f"測試 {workflow_name} 工作流程"
        }
    })
    
    print("\n工作流程已啟動!")
    print(f"回應狀態: {resp.get('status', '未知')}")
    print(f"回應訊息: {resp.get('message', '無訊息')}")
    
    # 處理工作流程後續互動
    session_id = resp.get("session_id")
    if not session_id:
        print("無法獲取會話 ID，工作流程可能無法繼續")
        return
    
    # 進入互動循環
    while resp.get("requires_input", False) or resp.get("status") == "waiting":
        requires_input = resp.get("requires_input", False)
        prompt = resp.get("prompt", "請輸入")
        
        if requires_input:
            print(f"\n{prompt}")
            user_input = input("> ")
            
            # 如果用戶輸入 exit 或 quit，取消工作流程
            if user_input.lower() in ["exit", "quit", "取消"]:
                cancel_resp = sysmod.handle({
                    "mode": "cancel_workflow",
                    "params": {
                        "session_id": session_id,
                        "reason": "用戶取消"
                    }
                })
                print(f"\n❌ 工作流程已取消：{cancel_resp.get('message', '已取消')}")
                break
            
            # 繼續工作流程（使用統一的 continue_workflow 模式）
            resp = sysmod.handle({
                "mode": "continue_workflow", 
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })
            
            print(f"\n回應狀態: {resp.get('status', '未知')}")
            print(f"回應訊息: {resp.get('message', '無訊息')}")
            
            # 如果狀態是 waiting，繼續自動推進
            while resp.get("status") == "waiting" and not resp.get("requires_input", False):
                import time
                time.sleep(0.5)  # 短暫延遲
                resp = sysmod.handle({
                    "mode": "continue_workflow", 
                    "params": {
                        "session_id": session_id,
                        "user_input": ""  # 自動推進不需要輸入
                    }
                })
                print(f"回應狀態: {resp.get('status', '未知')}")
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
        data = resp["data"]
        print(data)
        
        # 特殊處理資料收集工作流程的結果
        if workflow_type == 3 and data and "enhanced_summary" in data:
            print("\n========== LLM 增強摘要 ==========")
            print(data["enhanced_summary"])
            print("========== 摘要結束 ==========")

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

def integration_test_LY():
    itLY(modules)

def integration_test_SNM():
    itSNM(modules)

def integration_test_SNL():
    itSNL(modules)

def integration_test_NML():
    itNML(modules)

def integration_test_NLY():
    itNLY(modules)

def integration_test_SNML():
    itSNML(modules)

def integration_test_NMLT():
    itNMLT(modules)

def integration_test_SNMLT():
    itSNMLT(modules)

def integration_test_SNMLTY():
    itSNMLTY(modules)

def pipeline_test():
    itSNMLTY(modules)

# 額外測試

def test_summrize():
    test_chunk_and_summarize()

def sys_list_test_workflows():
    """列出所有可用的測試工作流程"""
    print("\n=== 可用的測試工作流程 ===")
    print("1. echo - 簡單回顯工作流程")
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
    print()
    print("5. tts_test - TTS文字轉語音測試工作流程")
    print("   - 多步驟工作流程")
    print("   - 測試與TTS模組的整合")
    print("   - 讓用戶輸入文字、情緒，並將其轉換成語音")
    print()
    print("=== 可用的文件工作流程 ===")
    print("drop_and_read - 檔案讀取工作流程")
    print("   - 多步驟工作流程")
    print("   - 等待檔案路徑輸入，確認後讀取檔案內容")
    print()
    print("intelligent_archive - 智慧歸檔工作流程")
    print("   - 多步驟工作流程")
    print("   - 根據檔案類型和歷史記錄智慧歸檔檔案")
    print()
    print("summarize_tag - 摘要標籤工作流程")
    print("   - 多步驟工作流程")
    print("   - 使用LLM為檔案生成摘要和標籤")

def test_file_workflow(workflow_type: str):
    """測試檔案工作流程
    
    Args:
        workflow_type: 工作流程類型 ('drop_and_read', 'intelligent_archive', 'summarize_tag')
    """
    sysmod = modules["sysmod"]
    if sysmod is None:
        error_log("[Controller] ❌ 無法載入 SYS 模組")
        return
        
    workflow_display_names = {
        "drop_and_read": "檔案讀取",
        "intelligent_archive": "智慧歸檔", 
        "summarize_tag": "摘要標籤"
    }
    
    workflow_name = workflow_display_names.get(workflow_type, workflow_type)
    
    print(f"\n=== 開始測試 {workflow_name} 工作流程 ===")
    
    # 啟動工作流程
    resp = sysmod.handle({
        "mode": "start_workflow",
        "params": {
            "workflow_type": workflow_type,
            "command": f"測試 {workflow_name} 工作流程"
        }
    })
    
    print("\n工作流程已啟動!")
    print(f"回應狀態: {resp.get('status', '未知')}")
    print(f"回應訊息: {resp.get('message', '無訊息')}")
    
    # 處理工作流程後續互動
    session_id = resp.get("session_id")
    if not session_id:
        print("無法獲取會話 ID，工作流程可能無法繼續")
        return
    
    # 進入互動循環
    while resp.get("requires_input", False) or resp.get("status") == "waiting":
        requires_input = resp.get("requires_input", False)
        prompt = resp.get("prompt", "請輸入")
        
        if requires_input:
            print(f"\n{prompt}")
            
            # 檢查是否需要檔案選擇（更精確的判斷）
            # 只有當提示明確要求選擇檔案，且不是確認步驟時，才開啟檔案選擇視窗
            needs_file_selection = (
                any(keyword in prompt.lower() for keyword in [
                    "請輸入要讀取的檔案路徑", 
                    "請選擇要歸檔的檔案路徑",
                    "請輸入要生成摘要的檔案路徑",
                    "請選擇檔案", 
                    "請輸入檔案路徑", 
                    "file path"
                ]) and
                "確認" not in prompt.lower() and
                "是否" not in prompt.lower() and
                "y/n" not in prompt.lower()
            )
            
            if needs_file_selection:
                print("🔍 正在開啟檔案選擇視窗...")
                try:
                    file_path = open_demo_window()
                    if file_path:
                        print(f"✅ 已選擇檔案: {file_path}")
                        user_input = file_path
                    else:
                        print("❌ 未選擇檔案，取消測試")
                        break
                except Exception as e:
                    error_log(f"[Controller] 檔案選擇出現錯誤: {e}")
                    print("❌ 檔案選擇失敗，取消測試")
                    break
            else:
                # 一般文字輸入或確認步驟
                user_input = input("> ")
                
                # 如果用戶輸入 exit 或 quit，取消工作流程
                if user_input.lower() in ["exit", "quit", "取消"]:
                    cancel_resp = sysmod.handle({
                        "mode": "cancel_workflow",
                        "params": {
                            "session_id": session_id,
                            "reason": "用戶取消"
                        }
                    })
                    print(f"\n❌ 工作流程已取消：{cancel_resp.get('message', '已取消')}")
                    break
            
            # 繼續工作流程
            resp = sysmod.handle({
                "mode": "continue_workflow",
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })
            
            print(f"\n回應狀態: {resp.get('status', '未知')}")
            print(f"回應訊息: {resp.get('message', '無訊息')}")
            
            # 如果狀態是 waiting，繼續自動推進
            while resp.get("status") == "waiting" and not resp.get("requires_input", False):
                import time
                time.sleep(0.5)  # 短暫延遲
                resp = sysmod.handle({
                    "mode": "continue_workflow", 
                    "params": {
                        "session_id": session_id,
                        "user_input": ""  # 自動推進不需要輸入
                    }
                })
                print(f"自動推進 - 回應狀態: {resp.get('status', '未知')}")
                print(f"自動推進 - 回應訊息: {resp.get('message', '無訊息')}")
        else:
            # 工作流程已完成或失敗
            break
    
    print(f"\n=== {workflow_name} 工作流程結束 ===")
    print(f"最終狀態: {resp.get('status', '未知')}")
    print(f"最終訊息: {resp.get('message', '無訊息')}")
    
    # 顯示工作流程結果（如果有）
    if "data" in resp:
        print("\n🎯 工作流程結果:")
        data = resp["data"]
        
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and len(value) > 200:
                    print(f"  {key}: {value[:200]}...")
                elif isinstance(value, list) and len(value) > 5:
                    print(f"  {key}: {value[:5]}... (總共 {len(value)} 項)")
                else:
                    print(f"  {key}: {value}")
        else:
            print(f"  結果: {data}")
            
        # 特殊處理不同類型的檔案工作流程結果
        if workflow_type == "drop_and_read" and isinstance(data, dict):
            if "content" in data:
                print(f"\n📄 檔案內容預覽:")
                content = data["content"]
                if len(content) > 500:
                    print(f"{content[:500]}...")
                else:
                    print(content)
                    
        elif workflow_type == "intelligent_archive" and isinstance(data, dict):
            if "archive_path" in data:
                print(f"\n📁 檔案已歸檔至: {data['archive_path']}")
            if "category" in data:
                print(f"📂 分類: {data['category']}")
                
        elif workflow_type == "summarize_tag" and isinstance(data, dict):
            if "summary" in data:
                print(f"\n📝 摘要: {data['summary']}")
            if "tags" in data:
                print(f"🏷️ 標籤: {', '.join(data['tags'])}")