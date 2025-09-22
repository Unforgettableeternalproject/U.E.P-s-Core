#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
測試 MEM 模組的舊 API 相容性
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.mem_module.mem_module import MEMModule

def test_legacy_api_compatibility():
    """測試舊 API 相容性"""
    print("🔄 測試 MEM 模組舊 API 相容性...")
    
    # 初始化 MEM 模組
    mem = MEMModule()
    if not mem.initialize():
        print("❌ MEM 模組初始化失敗")
        return False
    
    try:
        # 測試舊格式存儲
        print("\n1. 測試舊格式存儲...")
        legacy_store_data = {
            "mode": "store",
            "entry": {
                "user": "What's the capital of France?",
                "response": "The capital of France is Paris."
            }
        }
        
        store_result = mem.handle(legacy_store_data)
        print(f"   存儲結果: {store_result}")
        
        if store_result.get("status") == "stored":
            print("   ✅ 舊格式存儲成功")
        else:
            print("   ❌ 舊格式存儲失敗")
            return False
        
        # 測試舊格式查詢
        print("\n2. 測試舊格式查詢...")
        legacy_fetch_data = {
            "mode": "fetch",
            "text": "France capital",
            "top_k": 3
        }
        
        fetch_result = mem.handle(legacy_fetch_data)
        print(f"   查詢結果: {fetch_result}")
        
        if fetch_result.get("status") in ["success", "empty"]:
            print("   ✅ 舊格式查詢成功")
            results = fetch_result.get("results", [])
            print(f"   找到 {len(results)} 條結果")
            
            # 顯示結果
            for i, result in enumerate(results[:2]):
                print(f"   結果 {i+1}: {result.get('response', '')[:50]}...")
        else:
            print("   ❌ 舊格式查詢失敗")
            return False
        
        # 測試多個存儲和查詢
        print("\n3. 測試多個對話存儲...")
        conversations = [
            {"user": "What are we doing today?", "response": "We're working on the MEM module."},
            {"user": "What comes after MEM?", "response": "We'll handle the LLM integration next."},
            {"user": "Did we finish the STT part?", "response": "Yes, it's already tested."}
        ]
        
        for conv in conversations:
            store_data = {"mode": "store", "entry": conv}
            result = mem.handle(store_data)
            if result.get("status") != "stored":
                print(f"   ⚠️ 對話存儲失敗: {conv['user'][:30]}...")
        
        print("   ✅ 多個對話存儲完成")
        
        # 查詢特定內容
        print("\n4. 測試特定內容查詢...")
        specific_query = {
            "mode": "fetch",
            "text": "after MEM",
            "top_k": 2
        }
        
        specific_result = mem.handle(specific_query)
        print(f"   特定查詢結果: {specific_result.get('status')}")
        
        results = specific_result.get("results", [])
        found_llm = any("LLM" in result.get("response", "") for result in results)
        
        if found_llm:
            print("   ✅ 特定內容查詢成功，找到相關結果")
        else:
            print("   ⚠️ 特定內容查詢未找到預期結果")
        
        print("\n🎉 舊 API 相容性測試完成！")
        return True
        
    except Exception as e:
        print(f"❌ 測試過程發生錯誤: {e}")
        return False
    
    finally:
        mem.shutdown()

if __name__ == "__main__":
    success = test_legacy_api_compatibility()
    exit(0 if success else 1)