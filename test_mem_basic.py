# test_mem_basic.py
"""
MEM模組基礎功能測試 - 驗證重構架構
使用registry系統載入模組
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from core.registry import get_module
from modules.mem_module.schemas import MEMInput, MemoryQuery, MemoryType
from core.schemas import MEMModuleData
from utils.debug_helper import debug_log, info_log, error_log

def test_mem_initialization():
    """測試MEM模組初始化"""
    print("=== 測試MEM模組初始化 ===")
    
    try:
        # 通過registry載入MEM模組
        print("正在通過registry載入MEM模組...")
        mem_module = get_module("mem_module")
        
        if not mem_module:
            print("❌ 無法通過registry載入MEM模組")
            return False
        
        print("✅ MEM模組載入成功")
        print(f"   - 模組類型: {type(mem_module).__name__}")
        print(f"   - 初始化狀態: {getattr(mem_module, 'is_initialized', 'Unknown')}")
        
        # 檢查模組基本屬性
        if hasattr(mem_module, 'embedding_model'):
            print(f"   - 嵌入模型: {mem_module.embedding_model}")
        if hasattr(mem_module, 'index_file'):
            print(f"   - 索引檔案: {mem_module.index_file}")
        if hasattr(mem_module, 'metadata_file'):
            print(f"   - 元資料檔案: {mem_module.metadata_file}")
            
        # 檢查新架構組件
        if hasattr(mem_module, 'memory_manager') and mem_module.memory_manager:
            print("   - 記憶管理器: ✅ 已載入")
        else:
            print("   - 記憶管理器: ❌ 未載入")
            
        if hasattr(mem_module, 'working_context_handler') and mem_module.working_context_handler:
            print("   - Working Context處理器: ✅ 已註冊")
        else:
            print("   - Working Context處理器: ❌ 未註冊")
            
    except Exception as e:
        print(f"❌ 初始化過程發生異常: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True

def test_mem_handle_core_schema():
    """測試MEM模組處理核心Schema"""
    print("\n=== 測試核心Schema處理 ===")
    
    try:
        # 通過registry載入MEM模組
        mem_module = get_module("mem_module")
        if not mem_module:
            print("❌ 無法載入MEM模組")
            return False
        
        # 測試查詢操作
        test_data = MEMModuleData(
            text="測試查詢",
            operation_type="query",
            query_text="今天天氣如何",
            max_results=5
        )
        
        print("測試查詢操作...")
        
        # 檢查模組是否有handle方法
        if not hasattr(mem_module, 'handle'):
            print("❌ MEM模組沒有handle方法")
            return False
            
        result = mem_module.handle(test_data)
        
        if result and result.get("success"):
            print("✅ 查詢操作成功")
            print(f"   - 操作類型: {result.get('operation_type')}")
            print(f"   - 結果數量: {result.get('total_results', 0)}")
        else:
            print("❌ 查詢操作失敗")
            if result:
                print(f"   - 錯誤: {result.get('error', 'Unknown error')}")
            return False
            
    except Exception as e:
        print(f"❌ 處理過程發生異常: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True

def test_mem_working_context():
    """測試Working Context整合"""
    print("\n=== 測試Working Context整合 ===")
    
    try:
        from core.working_context import working_context_manager, ContextType
        
        # 檢查Working Context管理器
        print("檢查Working Context管理器...")
        
        # 獲取所有上下文資訊
        contexts_info = working_context_manager.get_all_contexts_info()
        print(f"   - 當前上下文數量: {len(contexts_info)}")
        
        # 通過registry載入MEM模組並檢查處理器註冊
        mem_module = get_module("mem_module")
        if not mem_module:
            print("❌ 無法載入MEM模組")
            return False
            
        print("✅ MEM模組已透過registry載入")
        
        # 測試能否處理對話上下文
        if hasattr(mem_module, 'working_context_handler') and mem_module.working_context_handler:
            can_handle_conversation = mem_module.working_context_handler.can_handle(ContextType.CONVERSATION)
            can_handle_identity = mem_module.working_context_handler.can_handle(ContextType.IDENTITY_MANAGEMENT)
            
            print(f"   - 可處理對話上下文: {'✅' if can_handle_conversation else '❌'}")
            print(f"   - 可處理身份管理: {'✅' if can_handle_identity else '❌'}")
        else:
            print("❌ Working Context處理器未註冊")
            return False
            
    except Exception as e:
        print(f"❌ Working Context測試異常: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True

def test_schema_compatibility():
    """測試Schema兼容性"""
    print("\n=== 測試Schema兼容性 ===")
    
    try:
        # 測試新Schema
        mem_input = MEMInput(
            operation_type="query",
            identity_token="test_user_123",
            query_data=MemoryQuery(
                identity_token="test_user_123",
                query_text="測試查詢",
                memory_types=[MemoryType.SNAPSHOT],
                max_results=5
            )
        )
        
        print("✅ 新Schema創建成功")
        print(f"   - 操作類型: {mem_input.operation_type}")
        print(f"   - 身份令牌: {mem_input.identity_token}")
        print(f"   - 查詢文本: {mem_input.query_data.query_text}")
        
        # 測試核心Schema
        core_data = MEMModuleData(
            text="測試文本",
            operation_type="query",
            query_text="核心Schema測試",
            identity_token="test_user_456"
        )
        
        print("✅ 核心Schema創建成功")
        print(f"   - 文本: {core_data.text}")
        print(f"   - 操作類型: {core_data.operation_type}")
        print(f"   - 身份令牌: {core_data.identity_token}")
        
    except Exception as e:
        print(f"❌ Schema兼容性測試異常: {e}")
        return False
    
    return True

def main():
    """主測試函數"""
    print("MEM模組基礎功能測試開始...\n")
    
    tests = [
        test_schema_compatibility,
        test_mem_initialization,
        test_mem_handle_core_schema,
        test_mem_working_context
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
        else:
            print("測試失敗，跳過後續測試")
            break
    
    print(f"\n=== 測試結果 ===")
    print(f"通過: {passed}/{total}")
    
    if passed == total:
        print("🎉 所有測試都通過了！")
        return True
    else:
        print("❌ 部分測試失敗")
        return False

if __name__ == "__main__":
    main()
