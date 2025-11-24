# -*- coding: utf-8 -*-
"""
LLM 模組測試函數
純功能測試 - 不依賴其他模組協作
使用統一測試環境管理
"""

from utils.debug_helper import debug_log, info_log, error_log
import time
import json

# ===== 純LLM功能測試 =====

def llm_test_chat(modules, text: str):
    """測試 CHAT 模式對話功能 - 基本聊天回應"""
    llm = modules.get("llm")
    if llm is None:
        print("❌ LLM 模組未載入")
        return {"success": False, "error": "LLM 模組未載入"}

    print(f"\n💬 測試 CHAT 對話 - 文本: '{text}'")
    print("=" * 60)

    try:
        # 創建 LLM 輸入數據
        input_data = {
            "text": text,
            "mode": "chat",
            "source": "debug_test"
        }
        
        start_time = time.time()
        result = llm.handle(input_data)
        processing_time = time.time() - start_time
        
        if isinstance(result, dict) and result.get("success", False):
            print("✅ CHAT 處理成功")
            print(f"🧠 Gemini 回應: {result.get('text', '[無回應]')}")
            print(f"⏱️ 處理時間: {processing_time:.2f}s")
            
            debug_log(3, f"[LLM 測試] 原始輸出: {result}")
            
            # 顯示學習數據（如果有）
            learning_data = result.get("learning_data")
            if learning_data:
                print(f"🧠 學習數據: {learning_data}")
            else:
                print("🧠 學習數據: None")
            
            return {"success": True, "response": result.get('text'), "processing_time": processing_time}
        else:
            error_msg = result.get('error', '未知錯誤') if isinstance(result, dict) else '非預期回應格式'
            print(f"❌ CHAT 處理失敗: {error_msg}")
            return {"success": False, "error": error_msg}
            
    except Exception as e:
        print(f"❌ CHAT 測試異常: {e}")
        return {"success": False, "error": str(e)}

def llm_test_command(modules, text: str):
    """測試 WORK 模式指令功能 - 系統指令分析和執行"""
    llm = modules.get("llm")
    if llm is None:
        print("❌ LLM 模組未載入")
        return {"success": False, "error": "LLM 模組未載入"}

    print(f"\n⚙️ 測試 WORK 指令 - 文本: '{text}'")
    print("=" * 60)

    try:
        # 創建 LLM 輸入數據
        input_data = {
            "text": text,
            "mode": "work",
            "source": "debug_test"
        }

        start_time = time.time()
        result = llm.handle(input_data)
        processing_time = time.time() - start_time

        if isinstance(result, dict) and result.get("success", False):
            print("✅ WORK 處理成功")
            print(f"🧠 Gemini 指令分析: {result.get('text', '[無回應]')}")
            print(f"⏱️ 處理時間: {processing_time:.2f}s")
            
            debug_log(3, f"[LLM 測試] 原始輸出: {result}")
            
            # 顯示系統指令
            if "system_action" in result:
                sys_action = result["system_action"]
                if isinstance(sys_action, dict):
                    print(f"⚙️ 系統指令: {sys_action}")
                    print(f"📋 指令類型: {sys_action.get('action', '無')}")
                    if "parameters" in sys_action:
                        print(f"🔧 參數: {sys_action['parameters']}")
                else:
                    print(f"⚙️ 系統指令: {sys_action}")
            
            # 顯示學習數據
            learning_data = result.get("learning_data")
            if learning_data:
                print(f"🧠 學習數據: {learning_data}")
            else:
                print("🧠 學習數據: None")
                    
            return {"success": True, "response": result.get('text'), "system_action": result.get("system_action"), "processing_time": processing_time}
        else:
            error_msg = result.get('error', '未知錯誤') if isinstance(result, dict) else '非預期回應格式'
            print(f"❌ WORK 處理失敗: {error_msg}")
            return {"success": False, "error": error_msg}
            
    except Exception as e:
        print(f"❌ WORK 測試異常: {e}")
        return {"success": False, "error": str(e)}

def llm_test_learning_engine(modules):
    """測試 Learning Engine 功能"""
    llm = modules.get("llm")
    if llm is None:
        print("❌ LLM 模組未載入")
        return {"success": False, "error": "LLM 模組未載入"}

    print(f"\n🧠 測試 Learning Engine 功能")
    print("=" * 60)

    try:
        # 測試學習功能
        test_conversations = [
            "你好，我喜歡簡潔的回應",
            "謝謝你的幫助，你的回答很清楚",
            "我希望得到更詳細的說明"
        ]
        
        learning_results = []
        
        for i, text in enumerate(test_conversations, 1):
            print(f"📝 測試對話 {i}: {text}")
            
            input_data = {
                "text": text,
                "mode": "chat",
                "source": "debug_test"
            }
            
            result = llm.handle(input_data)
            
            if result.get("success", False):
                learning_data = result.get("learning_data")
                if learning_data:
                    print(f"🧠 學習到的數據: {learning_data}")
                    learning_results.append(learning_data)
                else:
                    print("🧠 本次對話無學習數據")
            else:
                print(f"❌ 對話 {i} 處理失敗")
        
        # 嘗試獲取學習統計
        if hasattr(llm, 'learning_engine'):
            try:
                stats = llm.learning_engine.get_learning_statistics()
                print(f"📊 學習引擎統計: {stats}")
            except Exception as stats_e:
                print(f"⚠️ 無法獲取學習統計: {stats_e}")

        return {
            "success": True,
            "learning_results": learning_results,
            "total_conversations": len(test_conversations)
        }

    except Exception as e:
        print(f"❌ 學習引擎測試異常: {e}")
        return {"success": False, "error": str(e)}

def llm_test_system_status_monitoring(modules):
    """測試系統狀態變化監控 - 觀察LLM操作對系統狀態的影響 (互動式)"""
    llm = modules.get("llm")
    if llm is None:
        print("❌ LLM 模組未載入")
        return {"success": False, "error": "LLM 模組未載入"}

    print(f"\n📊 測試系統狀態監控功能 (互動式)")
    print("=" * 60)

    try:
        # 嘗試導入狀態管理器
        try:
            from core.status_manager import StatusManager
            status_manager = StatusManager()
            print("✅ 狀態管理器載入成功")
        except ImportError:
            print("⚠️ 無法載入 StatusManager，使用替代方案")
            return {"success": False, "error": "無法載入 StatusManager"}

        # 記錄初始狀態
        initial_status = {}
        if status_manager:
            try:
                initial_status = status_manager.get_status_dict()
                print("📋 初始系統狀態:")
                for key, value in initial_status.items():
                    print(f"  {key}: {value}")
            except Exception as e:
                print(f"⚠️ 無法獲取初始狀態: {e}")

        print("\n💬 互動式測試模式 (僅限 CHAT 模式)")
        print("輸入對話內容來測試系統狀態變化，輸入 'quit' 結束測試")
        print("-" * 60)

        status_changes = []
        test_count = 0
        
        while True:
            try:
                # 獲取用戶輸入
                user_input = input(f"\n[測試 {test_count + 1}] 請輸入對話內容: ").strip()
                
                if user_input.lower() in ['quit', 'exit', 'q', '退出']:
                    print("📝 結束互動式測試")
                    break
                
                if not user_input:
                    print("⚠️ 輸入不能為空，請重新輸入")
                    continue
                
                test_count += 1
                print(f"\n🔄 測試步驟 {test_count}: '{user_input}' (模式: chat)")
                
                # 記錄操作前狀態
                pre_status = {}
                if status_manager:
                    try:
                        pre_status = status_manager.get_status_dict()
                    except Exception:
                        pass

                # 執行LLM操作
                input_data = {
                    "text": user_input,
                    "mode": "chat",
                    "source": "status_monitor_test"
                }
                
                start_time = time.time()
                result = llm.handle(input_data)
                processing_time = time.time() - start_time

                # 記錄操作後狀態
                post_status = {}
                if status_manager:
                    try:
                        post_status = status_manager.get_status_dict()
                    except Exception:
                        pass

                # 比較狀態變化
                changes = {}
                if pre_status and post_status:
                    for key in set(pre_status.keys()) | set(post_status.keys()):
                        pre_val = pre_status.get(key, "N/A")
                        post_val = post_status.get(key, "N/A")
                        if pre_val != post_val:
                            changes[key] = {"before": pre_val, "after": post_val}

                # 顯示結果
                if isinstance(result, dict) and result.get("success", False):
                    print(f"✅ 處理成功: {result.get('text', '[無回應]')}")
                else:
                    error_msg = result.get('error', '未知錯誤') if isinstance(result, dict) else '非預期回應格式'
                    print(f"❌ 處理失敗: {error_msg}")

                # 記錄結果
                step_result = {
                    "step": test_count,
                    "input": user_input,
                    "mode": "chat",
                    "success": result.get("success", False) if isinstance(result, dict) else False,
                    "processing_time": processing_time,
                    "status_changes": changes
                }
                
                status_changes.append(step_result)

                # 顯示此步驟的狀態變化
                if changes:
                    print("📈 檢測到狀態變化:")
                    for key, change in changes.items():
                        print(f"  {key}: {change['before']} → {change['after']}")
                else:
                    print("📊 本步驟無狀態變化")

                print(f"⏱️ 處理時間: {processing_time:.2f}s")
                
            except KeyboardInterrupt:
                print("\n🛑 用戶中斷測試")
                break
            except Exception as e:
                print(f"❌ 步驟 {test_count} 測試異常: {e}")
                continue

        # 記錄最終狀態
        final_status = {}
        if status_manager:
            try:
                final_status = status_manager.get_status_dict()
                print("\n📋 最終系統狀態:")
                for key, value in final_status.items():
                    print(f"  {key}: {value}")
            except Exception as e:
                print(f"⚠️ 無法獲取最終狀態: {e}")

        # 彙總報告
        if status_changes:
            total_changes = sum(1 for step in status_changes if step["status_changes"])
            successful_operations = sum(1 for step in status_changes if step["success"])
            
            print(f"\n📊 狀態監控摘要:")
            print(f"  總測試步驟: {len(status_changes)}")
            print(f"  成功操作: {successful_operations}")
            print(f"  發生狀態變化的步驟: {total_changes}")

            return {
                "success": True,
                "initial_status": initial_status,
                "final_status": final_status,
                "status_changes": status_changes,
                "summary": {
                    "total_steps": len(status_changes),
                    "successful_operations": successful_operations,
                    "steps_with_changes": total_changes
                }
            }
        else:
            print("\n📊 未進行任何測試")
            return {"success": True, "message": "未進行任何測試"}

    except Exception as e:
        print(f"❌ 狀態監控測試異常: {e}")
        return {"success": False, "error": str(e)}