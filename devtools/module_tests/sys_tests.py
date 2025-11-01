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


# TTS 測試工作流已移除，TTS 模組已重構
# 應在 TTS 模組測試 (devtools/module_tests/tts_tests.py) 中直接測試


# ===== 檔案工作流程測試 =====

def sys_test_file_read(modules):
    """測試檔案讀取工作流程"""
    sysmod = modules.get("sysmod")
    if sysmod is None:
        print("❌ SYS 模組未載入")
        return {"success": False, "error": "SYS 模組未載入"}

    print("\n📄 測試檔案讀取工作流程")
    print("=" * 60)

    try:
        # 啟動檔案讀取工作流程
        result = sysmod.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "drop_and_read",
                "command": "讀取檔案",
                "initial_data": {}
            }
        })

        session_id = result.get("session_id")
        print(f"✅ 工作流程已啟動 (ID: {session_id})")

        # 處理輸入
        if result.get("requires_input"):
            # 檢查是否需要檔案選擇
            prompt = result.get("message", "")
            if "檔案" in prompt or "file" in prompt.lower():
                print("🔍 開啟檔案選擇視窗...")
                try:
                    file_path = open_demo_window()
                    if file_path:
                        print(f"✅ 已選擇: {file_path}")
                        user_input = file_path
                    else:
                        print("❌ 未選擇檔案")
                        return {"success": False, "error": "未選擇檔案"}
                except Exception as e:
                    print(f"❌ 檔案選擇失敗: {e}")
                    return {"success": False, "error": str(e)}
            else:
                user_input = input(f"{prompt}: ")

            result = sysmod.handle({
                "mode": "continue_workflow",
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })

            # 繼續處理後續步驟
            while result.get("requires_input"):
                user_input = input(f"{result.get('message', '請輸入')}: ")
                result = sysmod.handle({
                    "mode": "continue_workflow",
                    "params": {
                        "session_id": session_id,
                        "user_input": user_input
                    }
                })

        if result.get("status") == "completed":
            print(f"\n✅ 檔案讀取完成！")
            if "data" in result and "content" in result["data"]:
                content = result["data"]["content"]
                print(f"📄 檔案內容預覽:")
                print(content[:500] + ("..." if len(content) > 500 else ""))
            return {"success": True, "data": result.get("data")}

    except Exception as e:
        print(f"❌ 測試異常: {e}")
        return {"success": False, "error": str(e)}


def sys_test_file_archive(modules):
    """測試智慧歸檔工作流程"""
    sysmod = modules.get("sysmod")
    if sysmod is None:
        print("❌ SYS 模組未載入")
        return {"success": False, "error": "SYS 模組未載入"}

    print("\n📁 測試智慧歸檔工作流程")
    print("=" * 60)

    try:
        # 啟動智慧歸檔工作流程
        result = sysmod.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "intelligent_archive",
                "command": "歸檔檔案",
                "initial_data": {}
            }
        })

        session_id = result.get("session_id")
        print(f"✅ 工作流程已啟動 (ID: {session_id})")

        # 互動循環
        while result.get("requires_input"):
            prompt = result.get("message", "")
            
            # 檢查是否需要檔案選擇
            if "檔案" in prompt and "確認" not in prompt and "y/n" not in prompt.lower():
                print("🔍 開啟檔案選擇視窗...")
                try:
                    file_path = open_demo_window()
                    if file_path:
                        print(f"✅ 已選擇: {file_path}")
                        user_input = file_path
                    else:
                        print("❌ 未選擇檔案")
                        return {"success": False, "error": "未選擇檔案"}
                except Exception as e:
                    print(f"❌ 檔案選擇失敗: {e}")
                    return {"success": False, "error": str(e)}
            else:
                user_input = input(f"{prompt}: ")

            result = sysmod.handle({
                "mode": "continue_workflow",
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })

        if result.get("status") == "completed":
            print(f"\n✅ 歸檔完成！")
            if "data" in result:
                print(f"📊 歸檔資訊:")
                for key, value in result["data"].items():
                    print(f"   {key}: {value}")
            return {"success": True, "data": result.get("data")}

    except Exception as e:
        print(f"❌ 測試異常: {e}")
        return {"success": False, "error": str(e)}


def sys_test_file_summarize(modules):
    """測試摘要標籤工作流程"""
    sysmod = modules.get("sysmod")
    if sysmod is None:
        print("❌ SYS 模組未載入")
        return {"success": False, "error": "SYS 模組未載入"}

    print("\n🏷️ 測試摘要標籤工作流程")
    print("=" * 60)

    try:
        # 啟動摘要標籤工作流程
        result = sysmod.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "summarize_tag",
                "command": "生成摘要和標籤",
                "initial_data": {}
            }
        })

        session_id = result.get("session_id")
        print(f"✅ 工作流程已啟動 (ID: {session_id})")

        # 互動循環
        while result.get("requires_input"):
            prompt = result.get("message", "")
            
            # 檢查是否需要檔案選擇
            if "檔案" in prompt and "確認" not in prompt and "y/n" not in prompt.lower():
                print("🔍 開啟檔案選擇視窗...")
                try:
                    file_path = open_demo_window()
                    if file_path:
                        print(f"✅ 已選擇: {file_path}")
                        user_input = file_path
                    else:
                        print("❌ 未選擇檔案")
                        return {"success": False, "error": "未選擇檔案"}
                except Exception as e:
                    print(f"❌ 檔案選擇失敗: {e}")
                    return {"success": False, "error": str(e)}
            else:
                user_input = input(f"{prompt}: ")

            result = sysmod.handle({
                "mode": "continue_workflow",
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })

        if result.get("status") == "completed":
            print(f"\n✅ 摘要生成完成！")
            if "data" in result:
                print(f"📝 摘要: {result['data'].get('summary', '無')}")
                print(f"🏷️ 標籤: {result['data'].get('tags', '無')}")
            return {"success": True, "data": result.get("data")}

    except Exception as e:
        print(f"❌ 測試異常: {e}")
        return {"success": False, "error": str(e)}


# ===== 工作流程管理測試 =====

def sys_test_list_workflows(modules):
    """列出所有可用的工作流程"""
    print("\n📋 可用的工作流程")
    print("=" * 60)
    
    print("\n🧪 測試工作流程:")
    print("  • echo - 簡單回顯")
    print("  • countdown - 倒數計時")
    print("  • data_collector - 資料收集")
    print("  • random_fail - 隨機失敗（錯誤處理測試）")
    print("  • tts_test - TTS 文字轉語音測試")
    
    print("\n📄 檔案工作流程:")
    print("  • drop_and_read - 讀取檔案")
    print("  • intelligent_archive - 智慧歸檔")
    print("  • summarize_tag - 摘要標籤生成")
    
    return {"success": True}


def sys_test_active_workflows(modules):
    """查詢當前活躍的工作流程"""
    sysmod = modules.get("sysmod")
    if sysmod is None:
        print("❌ SYS 模組未載入")
        return {"success": False, "error": "SYS 模組未載入"}

    print("\n🔍 查詢活躍工作流程")
    print("=" * 60)

    try:
        result = sysmod.handle({
            "mode": "list_active_workflows",
            "params": {}
        })

        if result.get("status") == "success" and "data" in result:
            sessions = result["data"].get("sessions", [])
            if sessions:
                print(f"📊 找到 {len(sessions)} 個活躍工作流程:")
                for session in sessions:
                    print(f"  • ID: {session.get('session_id')}")
                    print(f"    類型: {session.get('workflow_type')}")
                    print(f"    狀態: {session.get('status')}")
                    print()
            else:
                print("📭 目前沒有活躍的工作流程")
            
            return {"success": True, "sessions": sessions}
        else:
            print(f"❌ 查詢失敗: {result.get('message', '未知錯誤')}")
            return result

    except Exception as e:
        print(f"❌ 查詢異常: {e}")
        return {"success": False, "error": str(e)}


def sys_test_workflow_status(modules, session_id: str = None):
    """查詢工作流程狀態"""
    sysmod = modules.get("sysmod")
    if sysmod is None:
        print("❌ SYS 模組未載入")
        return {"success": False, "error": "SYS 模組未載入"}

    if not session_id:
        session_id = input("請輸入工作流程 ID: ")

    print(f"\n🔍 查詢工作流程狀態 (ID: {session_id})")
    print("=" * 60)

    try:
        result = sysmod.handle({
            "mode": "get_workflow_status",
            "params": {
                "session_id": session_id
            }
        })

        if result.get("status") == "success" and "data" in result:
            info = result["data"]
            print("📊 工作流程資訊:")
            print(f"  ID: {info.get('session_id')}")
            print(f"  類型: {info.get('workflow_type')}")
            print(f"  狀態: {info.get('status')}")
            print(f"  當前步驟: {info.get('current_step')}")
            
            return {"success": True, "info": info}
        else:
            print(f"❌ 查詢失敗: {result.get('message', '未知錯誤')}")
            return result

    except Exception as e:
        print(f"❌ 查詢異常: {e}")
        return {"success": False, "error": str(e)}


def sys_test_cancel_workflow(modules, session_id: str = None):
    """取消工作流程"""
    sysmod = modules.get("sysmod")
    if sysmod is None:
        print("❌ SYS 模組未載入")
        return {"success": False, "error": "SYS 模組未載入"}

    if not session_id:
        session_id = input("請輸入要取消的工作流程 ID: ")

    print(f"\n❌ 取消工作流程 (ID: {session_id})")
    print("=" * 60)

    try:
        result = sysmod.handle({
            "mode": "cancel_workflow",
            "params": {
                "session_id": session_id,
                "reason": "使用者測試取消"
            }
        })

        if result.get("status") == "success":
            print(f"✅ 工作流程已取消")
            print(f"📝 訊息: {result.get('message', '')}")
            return {"success": True}
        else:
            print(f"❌ 取消失敗: {result.get('message', '未知錯誤')}")
            return result

    except Exception as e:
        print(f"❌ 取消異常: {e}")
        return {"success": False, "error": str(e)}
