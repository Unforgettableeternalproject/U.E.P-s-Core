# -*- coding: utf-8 -*-
"""
SYS 模組測試函數
簡單的功能測試 - 專注於使用者可見的工作流程功能
"""

from utils.debug_helper import debug_log, info_log, error_log
from utils.debug_file_dropper import open_demo_window
import time

# ===== 測試工作流程測試 =====

def sys_test_echo(modules):
    """測試簡單回顯工作流程"""
    sysmod = modules.get("sysmod")
    if sysmod is None:
        print("❌ SYS 模組未載入")
        return {"success": False, "error": "SYS 模組未載入"}

    print("\n🔄 測試 Echo 工作流程")
    print("=" * 60)

    try:
        # 啟動 echo 工作流程
        result = sysmod.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "echo",
                "command": "測試回顯工作流程",
                "initial_data": {}
            }
        })

        if not result.get("success") and result.get("status") not in ["success", "ok", "pending"]:
            print(f"❌ 工作流程啟動失敗: {result.get('message', '未知錯誤')}")
            return result

        session_id = result.get("session_id")
        print(f"✅ 工作流程已啟動 (ID: {session_id})")
        print(f"📝 系統提示: {result.get('message', '')}")

        # 如果需要輸入
        if result.get("requires_input"):
            user_input = input("\n請輸入訊息: ")
            
            result = sysmod.handle({
                "mode": "continue_workflow",
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })

            if result.get("status") == "completed":
                print(f"\n✅ 工作流程完成！")
                if "data" in result:
                    print(f"📤 回顯訊息: {result['data'].get('echo_message', '無')}")
                return {"success": True, "data": result.get("data")}
            else:
                print(f"❌ 工作流程異常: {result.get('message', '未知')}")
                return result
        
        return result

    except Exception as e:
        print(f"❌ 測試異常: {e}")
        return {"success": False, "error": str(e)}


def sys_test_countdown(modules):
    """測試倒數計時工作流程"""
    sysmod = modules.get("sysmod")
    if sysmod is None:
        print("❌ SYS 模組未載入")
        return {"success": False, "error": "SYS 模組未載入"}

    print("\n⏰ 測試 Countdown 工作流程")
    print("=" * 60)

    try:
        # 啟動倒數工作流程
        result = sysmod.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "countdown",
                "command": "測試倒數計時",
                "initial_data": {}
            }
        })

        session_id = result.get("session_id")
        print(f"✅ 工作流程已啟動 (ID: {session_id})")

        # 輸入起始數字
        if result.get("requires_input"):
            start_num = input("\n請輸入起始數字 (建議3-5): ")
            
            result = sysmod.handle({
                "mode": "continue_workflow",
                "params": {
                    "session_id": session_id,
                    "user_input": start_num
                }
            })

            print(f"\n⏳ 倒數開始...")
            
            # 等待倒數完成
            while result.get("status") == "waiting":
                time.sleep(0.5)
                result = sysmod.handle({
                    "mode": "continue_workflow",
                    "params": {
                        "session_id": session_id,
                        "user_input": None  # 不傳入輸入，只查詢狀態
                    }
                })
                print(".", end="", flush=True)

            print(f"\n✅ 倒數完成！")
            if "data" in result:
                print(f"📊 結果: {result['data']}")
            
            return {"success": True, "data": result.get("data")}

    except Exception as e:
        print(f"❌ 測試異常: {e}")
        return {"success": False, "error": str(e)}


def sys_test_data_collector(modules):
    """測試資料收集工作流程"""
    sysmod = modules.get("sysmod")
    if sysmod is None:
        print("❌ SYS 模組未載入")
        return {"success": False, "error": "SYS 模組未載入"}

    print("\n📊 測試 Data Collector 工作流程")
    print("=" * 60)

    try:
        # 啟動資料收集工作流程
        result = sysmod.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "data_collector",
                "command": "測試資料收集",
                "initial_data": {}
            }
        })

        session_id = result.get("session_id")
        print(f"✅ 工作流程已啟動 (ID: {session_id})")
        print(f"📝 這個工作流程會詢問你一系列問題\n")

        # 互動循環
        while result.get("requires_input"):
            # 優先使用 prompt（下一步的提示），如果沒有則使用 message
            prompt = result.get("prompt") or result.get("message", "請輸入")
            
            # 如果有確認訊息（且與提示不同），先顯示它
            message = result.get("message")
            if message and message != prompt:
                print(f"\n{message}")
            
            user_input = input(f"{prompt}: ")
            
            result = sysmod.handle({
                "mode": "continue_workflow",
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })

        if result.get("status") == "completed":
            print(f"\n✅ 資料收集完成！")
            if "data" in result:
                print(f"📊 收集的資料:")
                for key, value in result["data"].items():
                    print(f"   {key}: {value}")
            return {"success": True, "data": result.get("data")}

    except Exception as e:
        print(f"❌ 測試異常: {e}")
        return {"success": False, "error": str(e)}


def sys_test_random_fail(modules):
    """測試隨機失敗工作流程（測試錯誤處理）"""
    sysmod = modules.get("sysmod")
    if sysmod is None:
        print("❌ SYS 模組未載入")
        return {"success": False, "error": "SYS 模組未載入"}

    print("\n🎲 測試 Random Fail 工作流程")
    print("=" * 60)

    try:
        # 啟動隨機失敗工作流程
        result = sysmod.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "random_fail",
                "command": "測試錯誤處理",
                "initial_data": {}
            }
        })

        session_id = result.get("session_id")
        print(f"✅ 工作流程已啟動 (ID: {session_id})")
        print(f"📝 這個工作流程可能會隨機失敗，用於測試錯誤處理\n")

        # 互動循環
        while result.get("requires_input") or result.get("status") == "waiting":
            if result.get("requires_input"):
                prompt = result.get("message", "請輸入")
                user_input = input(f"{prompt}: ")
                
                result = sysmod.handle({
                    "mode": "continue_workflow",
                    "params": {
                        "session_id": session_id,
                        "user_input": user_input
                    }
                })
            elif result.get("status") == "waiting":
                time.sleep(0.5)
                result = sysmod.handle({
                    "mode": "continue_workflow",
                    "params": {
                        "session_id": session_id,
                        "user_input": ""
                    }
                })

        if result.get("status") == "completed":
            print(f"\n✅ 測試完成！")
            if "data" in result:
                print(f"📊 測試結果: {result['data']}")
            return {"success": True, "data": result.get("data")}
        else:
            print(f"\n⚠️ 工作流程結束，狀態: {result.get('status')}")
            return result

    except Exception as e:
        print(f"❌ 測試異常: {e}")
        return {"success": False, "error": str(e)}
