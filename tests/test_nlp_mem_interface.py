# -*- coding: utf-8 -*-
"""
測試 NLP 與 MEM 模組之間的數據接口相容性
"""

import sys
import os
import json
from datetime import datetime

# 添加項目根目錄到系統路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.nlp_module.nlp_module import NLPModule
from modules.nlp_module.schemas import NLPInput, NLPOutput
from modules.mem_module.mem_module import MEMModule
from modules.mem_module.schemas import MEMInput, MEMOutput

def test_nlp_to_mem_interface():
    """測試 NLP 輸出是否符合 MEM 輸入要求"""
    
    print("=== 測試 NLP 與 MEM 數據接口相容性 ===\n")
    
    # 1. 初始化 NLP 模組
    print("1. 初始化 NLP 模組...")
    nlp_module = NLPModule()
    nlp_success = nlp_module.initialize()
    print(f"   NLP 初始化: {'成功' if nlp_success else '失敗'}")
    
    # 2. 創建測試輸入
    print("\n2. 創建測試輸入...")
    nlp_input_data = {
        "text": "我想學習Python和機器學習，可以推薦一些實作專案嗎？",
        "speaker_id": "test_speaker_001",
        "speaker_confidence": 0.9,
        "speaker_status": "existing",
        "enable_segmentation": True,
        "enable_entity_extraction": True,
        "enable_identity_processing": True
    }
    print(f"   測試輸入: {nlp_input_data['text']}")
    
    # 3. 獲取 NLP 輸出
    print("\n3. 獲取 NLP 輸出...")
    try:
        nlp_result = nlp_module.handle(nlp_input_data)
        print(f"   NLP 處理: 成功")
        print(f"   輸出類型: {type(nlp_result)}")
        
        # 顯示 NLP 輸出結構
        print("\n   NLP 輸出結構:")
        for key, value in nlp_result.items():
            if isinstance(value, dict):
                print(f"     {key}: {type(value)} (dict)")
            elif isinstance(value, list):
                print(f"     {key}: {type(value)} (list, length={len(value)})")
            else:
                print(f"     {key}: {type(value)} - {value}")
                
    except Exception as e:
        print(f"   NLP 處理失敗: {e}")
        return False
    
    # 4. 初始化 MEM 模組
    print("\n4. 初始化 MEM 模組...")
    mem_module = MEMModule()
    mem_success = mem_module.initialize()
    print(f"   MEM 初始化: {'成功' if mem_success else '失敗'}")
    
    # 5. 設置 Working Context 中的身份信息 (模擬 NLP 設置)
    print("\n5. 設置 Working Context 身份信息...")
    from core.working_context import working_context_manager
    
    # 模擬身份數據
    test_identity = {
        "identity_id": "test_user_001",
        "speaker_id": "test_speaker_001",
        "display_name": "測試用戶",
        "memory_token": f"test_token_{int(datetime.now().timestamp())}",
        "preferences": {"language": "zh-tw"},
        "voice_preferences": {"speed": 1.0, "tone": "warm"},
        "conversation_style": {"formality": "casual"}
    }
    
    working_context_manager.set_current_identity(test_identity)
    working_context_manager.set_memory_token(test_identity["memory_token"])
    
    print(f"   設置身份: {test_identity['identity_id']}")
    print(f"   設置記憶令牌: {test_identity['memory_token']}")
    
    # 6. 測試 MEM 從 Working Context 獲取身份
    print("\n6. 測試 MEM 從 Working Context 獲取身份...")
    try:
        mem_input = MEMInput(
            operation_type="process_identity"
        )
        
        mem_result = mem_module.handle(mem_input)
        print(f"   結果: {'成功' if mem_result.success else '失敗'}")
        if mem_result.success:
            data = mem_result.data or {}
            print(f"   獲取的記憶令牌: {data.get('memory_token')}")
            print(f"   獲取的身份ID: {data.get('user_profile', {}).get('identity_id')}")
        else:
            print(f"   錯誤: {mem_result.message}")
            
    except Exception as e:
        print(f"   處理失敗: {e}")
    
    # 7. 測試直接使用 process_nlp_output 方法
    print("\n7. 測試直接處理 NLP 輸出...")
    try:
        mem_result = mem_module.process_nlp_output(nlp_result)
        if mem_result:
            print(f"   結果: {'成功' if mem_result.success else '失敗'}")
            if mem_result.success:
                print(f"   訊息: {mem_result.message}")
                if mem_result.data:
                    print(f"   處理數據: {mem_result.data}")
            else:
                print(f"   錯誤: {mem_result.errors}")
        else:
            print("   結果: 無返回值")
            
    except Exception as e:
        print(f"   處理失敗: {e}")
    
    # 8. 測試使用 MEMInput 格式處理 NLP 輸出
    print("\n8. 測試使用 MEMInput 格式...")
    try:
        mem_input = MEMInput(
            operation_type="process_nlp_output",
            intent_info=nlp_result,
            conversation_text=nlp_input_data["text"]
        )
        
        mem_result = mem_module.handle(mem_input)
        print(f"   結果: {'成功' if mem_result.success else '失敗'}")
        if mem_result.success:
            print(f"   訊息: {mem_result.message}")
            if mem_result.data:
                print(f"   處理數據: {mem_result.data}")
        else:
            print(f"   錯誤: {mem_result.errors}")
            
    except Exception as e:
        print(f"   處理失敗: {e}")
    
    # 9. 驗證 Working Context 中的數據
    print("\n9. 驗證 Working Context 數據...")
    current_identity = working_context_manager.get_current_identity()
    current_token = working_context_manager.get_memory_token()
    
    print(f"   當前身份: {current_identity.get('identity_id') if current_identity else None}")
    print(f"   當前記憶令牌: {current_token}")
    
    # 10. 架構建議
    print("\n10. 架構建議:")
    print("   ✅ NLP 模組應該:")
    print("      - 檢測身份是否存在（identity_action）")
    print("      - 將身份實體存放到 Working Context")
    print("      - 僅在輸出中提供身份狀態信息")
    print()
    print("   ✅ MEM 模組應該:")
    print("      - 優先從 Working Context 獲取身份信息")
    print("      - 使用 working_context_manager.get_current_identity()")
    print("      - 使用 working_context_manager.get_memory_token()")
    print("      - NLP 輸出主要用於意圖分析和記憶創建決策")
    
    print("\n=== 測試完成 ===")
    return True

if __name__ == "__main__":
    test_nlp_to_mem_interface()