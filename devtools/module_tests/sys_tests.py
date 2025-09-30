# -*- coding: utf-8 -*-
"""
SYS 模組測試函數
⚠️ 未重構模組 - 使用傳統模組呼叫方式
"""

from utils.debug_helper import debug_log, info_log, error_log
from utils.debug_file_dropper import open_demo_window, open_folder_dialog
import psutil
import platform
import time

# ⚠️ 未重構模組標註
# 以下測試函數適用於尚未重構的 SYS 模組
# 使用傳統的模組呼叫方式而非統一的 handle 介面

def sys_list_functions(modules):
    sysmod = modules.get("sysmod")

    if sysmod is None:
        error_log("[Controller] ❌ 無法載入 SYS 模組")
        return

    resp = sysmod.handle({"mode": "list_functions", "params": {}})

    print("=== SYS 功能清單 ===")
    import json
    print(json.dumps(resp.get("data", {}), ensure_ascii=False, indent=2))

# 測試多步驟工作流程
def test_command_workflow(modules, command_text: str = "幫我整理和摘要桌面上的文件"):
    """測試多步驟指令工作流程"""
    sysmod = modules.get("sysmod")
    llm = modules.get("llm")

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

def sys_test_functions(modules, mode : int = 1, sub : int = 1): 
    sysmod = modules.get("sysmod")
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

def sys_test_workflows(modules, workflow_type: int = 1):
    """測試各種測試工作流程
    
    Args:
        workflow_type: 工作流程類型
            1: echo - 簡單回顯
            2: countdown - 倒數計時
            3: data_collector - 資料收集
            4: random_fail - 隨機失敗
            5: tts_test - TTS文字轉語音測試
    """
    sysmod = modules.get("sysmod")
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

def sys_list_test_workflows(modules):
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

def test_file_workflow(modules, workflow_type: str):
    """測試檔案工作流程
    
    Args:
        workflow_type: 工作流程類型 ('drop_and_read', 'intelligent_archive', 'summarize_tag')
    """
    sysmod = modules.get("sysmod")
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
