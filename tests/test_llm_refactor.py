#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 模組重構測試腳本
測試新的 CHAT/WORK 模式和所有新功能
"""

import sys
import os
from pathlib import Path

# 添加專案根目錄到路徑
project_root = Path(__file__).parent.parent  # tests/ 的上一層才是專案根目錄
sys.path.insert(0, str(project_root))

# 導入必要模組
from modules.llm_module.llm_module import LLMModule
from modules.llm_module.schemas import LLMInput, LLMOutput
from core.status_manager import status_manager
from utils.debug_helper import info_log, debug_log, error_log

def test_status_manager():
    """測試 StatusManager 整合"""
    print("\n=== 測試 StatusManager 整合 ===")
    
    # 獲取當前狀態
    status = status_manager.get_status_dict()
    print(f"當前系統狀態: {status}")
    
    # 測試狀態更新
    status_manager.update_mood(0.1, "測試心情提升")
    status_manager.update_pride(0.05, "測試自豪感提升")
    
    # 獲取個性修飾符
    modifiers = status_manager.get_personality_modifiers()
    print(f"個性修飾符: {modifiers}")
    
    return True

def test_llm_chat_mode():
    """測試 CHAT 模式"""
    print("\n=== 測試 CHAT 模式 ===")
    
    try:
        # 初始化 LLM 模組
        llm = LLMModule()
        if not llm.initialize():
            error_log("LLM 模組初始化失敗")
            return False
            
        # 準備 CHAT 模式輸入
        chat_input = LLMInput(
            mode="chat",
            text="你好，今天天氣如何？",
            memory_context="昨天我們聊過天氣話題",
            identity_context={"name": "測試用戶", "preferences": ["友善對話"]}
        )
        
        print(f"輸入: {chat_input.text}")
        print(f"模式: {chat_input.mode}")
        
        # 處理請求 
        result = llm.handle(chat_input.model_dump())
        print(f"回應: {result.get('text', 'No response')}")
        print(f"處理時間: {result.get('processing_time', 0):.3f}s")
        print(f"成功: {result.get('success', False)}")
        
        # 測試快取功能
        print("\n--- 測試快取功能 ---")
        result2 = llm.handle(chat_input.model_dump())
        print(f"第二次回應: {result2.get('text', 'No response')}")
        print(f"處理時間: {result2.get('processing_time', 0):.3f}s")
        
        return result.get('success', False)
        
    except Exception as e:
        error_log(f"CHAT 模式測試失敗: {e}")
        return False

def test_llm_work_mode():
    """測試 WORK 模式"""
    print("\n=== 測試 WORK 模式 ===")
    
    try:
        llm = LLMModule()
        
        # 準備 WORK 模式輸入
        work_input = LLMInput(
            mode="work",
            text="幫我分析系統性能並提供優化建議",
            available_functions="系統性能分析功能",
            workflow_context={"task_type": "performance_analysis", "priority": "high"}
        )
        
        print(f"輸入: {work_input.text}")
        print(f"模式: {work_input.mode}")
        print(f"工作內容: {work_input.workflow_context}")
        
        # 處理請求
        result = llm.handle(work_input.model_dump())
        print(f"回應: {result.get('text', 'No response')}")
        print(f"處理時間: {result.get('processing_time', 0):.3f}s")
        print(f"系統動作: {result.get('system_action', 'None')}")
        
        return result.get('success', False)
        
    except Exception as e:
        error_log(f"WORK 模式測試失敗: {e}")
        return False

def test_legacy_compatibility():
    """測試向後兼容性"""
    print("\n=== 測試向後兼容性 ===")
    
    try:
        llm = LLMModule()
        
        # 使用舊的 intent 格式
        legacy_input = {
            "text": "這是舊版格式的測試",
            "intent": "chat",
            "memory": "一些記憶內容",
            "is_internal": False
        }
        
        print(f"舊版輸入: {legacy_input}")
        
        result = llm.handle(legacy_input)
        print(f"回應: {result.get('text', 'No response')}")
        print(f"狀態: {result.get('status', 'unknown')}")
        
        return result.get('status') == 'ok'
        
    except Exception as e:
        error_log(f"向後兼容性測試失敗: {e}")
        return False

def test_module_status():
    """測試模組狀態查詢"""
    print("\n=== 測試模組狀態 ===")
    
    try:
        llm = LLMModule()
        status = llm.get_module_status()
        
        print("模組狀態:")
        for key, value in status.items():
            print(f"  {key}: {value}")
            
        return True
        
    except Exception as e:
        error_log(f"模組狀態測試失敗: {e}")
        return False

def main():
    """主測試函數"""
    print("開始 LLM 模組重構測試...")
    
    tests = [
        ("StatusManager 整合", test_status_manager),
        ("CHAT 模式", test_llm_chat_mode),
        ("WORK 模式", test_llm_work_mode), 
        ("向後兼容性", test_legacy_compatibility),
        ("模組狀態", test_module_status)
    ]
    
    results = []
    for test_name, test_func in tests:
        try:
            print(f"\n{'='*50}")
            result = test_func()
            results.append((test_name, result))
            print(f"{test_name}: {'✅ 通過' if result else '❌ 失敗'}")
        except Exception as e:
            error_log(f"{test_name} 測試異常: {e}")
            results.append((test_name, False))
    
    # 總結
    print(f"\n{'='*50}")
    print("測試結果總結:")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ 通過" if result else "❌ 失敗" 
        print(f"  {test_name}: {status}")
    
    print(f"\n總計: {passed}/{total} 測試通過")
    
    if passed == total:
        print("🎉 所有測試通過！LLM 模組重構成功！")
    else:
        print("⚠️  有測試失敗，需要檢查和修復。")
    
    return passed == total

if __name__ == "__main__":
    main()