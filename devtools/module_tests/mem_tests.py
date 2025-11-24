# -*- coding: utf-8 -*-
"""
MEM 模組測試函數
純功能測試 - 不依賴其他模組協作
使用統一測試環境管理
"""

from utils.debug_helper import debug_log, info_log, error_log
import time
import uuid

# ===== 純MEM功能測試 =====

def mem_test_store_memory(modules, identity="test_user", content="測試記憶內容", memory_type="long_term"):
    """測試記憶存儲功能 - 存儲新的記憶條目"""
    mem = modules.get("mem")
    if mem is None:
        print("❌ MEM 模組未載入")
        return {"success": False, "error": "MEM 模組未載入"}

    print(f"\n💾 測試記憶存儲 - 類型: {memory_type}")
    print("=" * 60)
    print(f"👤 身份ID: {identity}")
    print(f"📝 內容: {content}")

    try:
        # 創建記憶輸入數據
        input_data = {
            "identity_id": identity,
            "content": content,
            "memory_type": memory_type,
            "source": "debug_test"
        }
        
        start_time = time.time()
        result = mem.handle(input_data)
        processing_time = time.time() - start_time
        
        if isinstance(result, dict) and result.get("success", False):
            print("✅ 記憶存儲成功")
            print(f"🆔 記憶ID: {result.get('memory_id', '[無ID]')}")
            print(f"⏱️ 處理時間: {processing_time:.2f}s")
            
            return {"success": True, "memory_id": result.get('memory_id'), "processing_time": processing_time}
        else:
            error_msg = result.get('error', '未知錯誤') if isinstance(result, dict) else '非預期回應格式'
            print(f"❌ 記憶存儲失敗: {error_msg}")
            return {"success": False, "error": error_msg}
            
    except Exception as e:
        print(f"❌ 記憶存儲測試異常: {e}")
        return {"success": False, "error": str(e)}

def mem_test_memory_query(modules, identity="test_user", query_text="天氣"):
    """測試記憶查詢功能 - 搜尋相關記憶"""
    mem = modules.get("mem")
    if mem is None:
        print("❌ MEM 模組未載入")
        return {"success": False, "error": "MEM 模組未載入"}

    print(f"\n🔍 測試記憶查詢 - 查詢: '{query_text}'")
    print("=" * 60)
    print(f"👤 身份ID: {identity}")

    try:
        # 創建查詢輸入數據
        input_data = {
            "identity_id": identity,
            "query": query_text,
            "action": "search",
            "source": "debug_test"
        }
        
        start_time = time.time()
        result = mem.handle(input_data)
        processing_time = time.time() - start_time
        
        if isinstance(result, dict) and result.get("success", False):
            memories = result.get("memories", [])
            print("✅ 記憶查詢成功")
            print(f"📊 找到 {len(memories)} 條相關記憶")
            print(f"⏱️ 處理時間: {processing_time:.2f}s")
            
            # 顯示前幾條記憶
            for i, memory in enumerate(memories[:3], 1):
                if isinstance(memory, dict):
                    print(f"   {i}. {memory.get('content', '[無內容]')[:50]}...")
                else:
                    print(f"   {i}. {str(memory)[:50]}...")
            
            return {"success": True, "memories": memories, "processing_time": processing_time}
        else:
            error_msg = result.get('error', '未知錯誤') if isinstance(result, dict) else '非預期回應格式'
            print(f"❌ 記憶查詢失敗: {error_msg}")
            return {"success": False, "error": error_msg}
            
    except Exception as e:
        print(f"❌ 記憶查詢測試異常: {e}")
        return {"success": False, "error": str(e)}

def mem_test_conversation_snapshot(modules, identity="test_user", conversation="你好，今天天氣如何？"):
    """測試對話快照功能 - 創建對話記錄"""
    mem = modules.get("mem")
    if mem is None:
        print("❌ MEM 模組未載入")
        return {"success": False, "error": "MEM 模組未載入"}

    print(f"\n📸 測試對話快照")
    print("=" * 60)
    print(f"👤 身份ID: {identity}")
    print(f"💬 對話: {conversation}")

    try:
        # 創建快照輸入數據
        input_data = {
            "identity_id": identity,
            "conversation": conversation,
            "action": "create_snapshot",
            "source": "debug_test"
        }
        
        start_time = time.time()
        result = mem.handle(input_data)
        processing_time = time.time() - start_time
        
        if isinstance(result, dict) and result.get("success", False):
            print("✅ 對話快照創建成功")
            print(f"🆔 快照ID: {result.get('snapshot_id', '[無ID]')}")
            print(f"⏱️ 處理時間: {processing_time:.2f}s")
            
            return {"success": True, "snapshot_id": result.get('snapshot_id'), "processing_time": processing_time}
        else:
            error_msg = result.get('error', '未知錯誤') if isinstance(result, dict) else '非預期回應格式'
            print(f"❌ 對話快照失敗: {error_msg}")
            return {"success": False, "error": error_msg}
            
    except Exception as e:
        print(f"❌ 對話快照測試異常: {e}")
        return {"success": False, "error": str(e)}

def mem_test_identity_stats(modules, identity="test_user"):
    """測試身份統計功能 - 獲取使用者記憶統計"""
    mem = modules.get("mem")
    if mem is None:
        print("❌ MEM 模組未載入")
        return {"success": False, "error": "MEM 模組未載入"}

    print(f"\n📊 測試身份統計")
    print("=" * 60)
    print(f"👤 身份ID: {identity}")

    try:
        # 創建統計輸入數據
        input_data = {
            "identity_id": identity,
            "action": "get_statistics",
            "source": "debug_test"
        }
        
        start_time = time.time()
        result = mem.handle(input_data)
        processing_time = time.time() - start_time
        
        if isinstance(result, dict) and result.get("success", False):
            stats = result.get("statistics", {})
            print("✅ 身份統計獲取成功")
            print(f"⏱️ 處理時間: {processing_time:.2f}s")
            
            # 顯示統計資訊
            for key, value in stats.items():
                print(f"   📈 {key}: {value}")
            
            return {"success": True, "statistics": stats, "processing_time": processing_time}
        else:
            error_msg = result.get('error', '未知錯誤') if isinstance(result, dict) else '非預期回應格式'
            print(f"❌ 身份統計失敗: {error_msg}")
            return {"success": False, "error": error_msg}
            
    except Exception as e:
        print(f"❌ 身份統計測試異常: {e}")
        return {"success": False, "error": str(e)}

def mem_test_write_then_query(modules, identity="test_user"):
    """測試寫入後查詢功能 - 綜合測試記憶存儲和查詢"""
    mem = modules.get("mem")
    if mem is None:
        print("❌ MEM 模組未載入")
        return {"success": False, "error": "MEM 模組未載入"}

    print(f"\n🔄 測試寫入後查詢")
    print("=" * 60)

    try:
        # 1. 先存儲一些測試記憶
        test_memories = [
            "今天天氣很好，陽光明媚",
            "我最喜歡喝咖啡",
            "週末計劃去公園散步"
        ]
        
        stored_ids = []
        print("📝 存儲測試記憶...")
        
        for i, content in enumerate(test_memories, 1):
            store_result = mem_test_store_memory(modules, identity, content, "short_term")
            if store_result["success"]:
                stored_ids.append(store_result.get("memory_id"))
                print(f"   ✅ 記憶 {i} 存儲成功")
            else:
                print(f"   ❌ 記憶 {i} 存儲失敗")
        
        # 2. 等待一秒確保存儲完成
        time.sleep(1)
        
        # 3. 測試查詢
        print("\n🔍 查詢相關記憶...")
        query_result = mem_test_memory_query(modules, identity, "天氣")
        
        if query_result["success"]:
            found_memories = query_result.get("memories", [])
            print(f"✅ 查詢成功，找到 {len(found_memories)} 條記憶")
            
            return {
                "success": True,
                "stored_count": len(stored_ids),
                "found_count": len(found_memories),
                "stored_ids": stored_ids
            }
        else:
            print("❌ 查詢失敗")
            return {"success": False, "error": "查詢階段失敗"}
        
    except Exception as e:
        print(f"❌ 寫入後查詢測試異常: {e}")
        return {"success": False, "error": str(e)}