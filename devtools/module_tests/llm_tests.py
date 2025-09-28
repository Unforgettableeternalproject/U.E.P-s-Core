# -*- coding: utf-8 -*-
"""
LLM 模組測試函數
純功能測試 - 不依賴其他模組協作
使用統一測試環境管理
"""

from utils.debug_helper import debug_log, info_log, error_log
import time

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

def llm_test_cache_functionality(modules):
    """測試 Context Caching 功能"""
    llm = modules.get("llm")
    if llm is None:
        print("❌ LLM 模組未載入")
        return {"success": False, "error": "LLM 模組未載入"}

    print(f"\n🗄️ 測試 Context Caching 功能")
    print("=" * 60)

    try:
        # 測試相同內容的多次請求（應該使用快取）
        test_text = "測試快取功能，這是一個重複的查詢。"
        
        # 第一次請求
        print("📤 第一次請求（建立快取）...")
        input_data = {
            "text": test_text,
            "mode": "chat",
            "source": "debug_test"
        }

        start_time = time.time()
        result1 = llm.handle(input_data)
        first_time = time.time() - start_time
        
        if not result1.get("success", False):
            return {"success": False, "error": f"第一次請求失敗: {result1.get('error')}"}

        print(f"⏱️ 第一次處理時間: {first_time:.3f}s")
        
        # 等待一秒確保快取生效
        time.sleep(1)
        
        # 第二次相同請求（應該使用快取）
        print("📥 第二次相同請求（使用快取）...")
        start_time = time.time()
        result2 = llm.handle(input_data)
        second_time = time.time() - start_time
        
        if not result2.get("success", False):
            return {"success": False, "error": f"第二次請求失敗: {result2.get('error')}"}

        print(f"⏱️ 第二次處理時間: {second_time:.3f}s")
        
        # 檢查快取統計
        cache_stats = {}
        if hasattr(llm, 'cache_manager'):
            cache_stats = llm.cache_manager.get_cache_statistics()
            print(f"📊 快取統計: {cache_stats}")

        # 分析效能提升
        speed_improvement = (first_time - second_time) / first_time * 100 if first_time > 0 else 0
        print(f"🚀 速度提升: {speed_improvement:.1f}%")

        return {
            "success": True,
            "first_time": first_time,
            "second_time": second_time,
            "speed_improvement": speed_improvement,
            "cache_stats": cache_stats
        }

    except Exception as e:
        print(f"❌ 快取測試異常: {e}")
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