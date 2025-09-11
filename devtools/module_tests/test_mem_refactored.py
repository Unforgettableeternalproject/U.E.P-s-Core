# test_mem_refactored.py
"""
測試重構後的MEM模組核心功能
包括Identity嵌入、快照功能、使用者記憶處理等
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from core.registry import get_module
from modules.mem_module.schemas import MEMInput, MemoryQuery, MemoryType
from core.schemas import MEMModuleData
from utils.debug_helper import debug_log, info_log, error_log

def test_refactored_initialization():
    """測試重構後的MEM模組初始化"""
    print("=== 測試重構後的MEM模組初始化 ===")
    
    try:
        # 通過registry載入MEM模組
        mem_module = get_module("mem_module")
        
        if mem_module and mem_module.is_initialized:
            print("✅ MEM模組載入並初始化成功")
            print(f"   - 模組類型: {type(mem_module).__name__}")
            print(f"   - 初始化狀態: {mem_module.is_initialized}")
            
            # 檢查新的子模組組件
            if hasattr(mem_module, 'memory_manager') and mem_module.memory_manager:
                print(f"   - 記憶管理器: ✅ 已載入 ({type(mem_module.memory_manager).__name__})")
                
                # 檢查子模組
                if hasattr(mem_module.memory_manager, 'identity_manager'):
                    print(f"   - 身份管理器: ✅ 已載入")
                if hasattr(mem_module.memory_manager, 'snapshot_manager'):
                    print(f"   - 快照管理器: ✅ 已載入")
                if hasattr(mem_module.memory_manager, 'semantic_retriever'):
                    print(f"   - 語義檢索器: ✅ 已載入")
                if hasattr(mem_module.memory_manager, 'memory_analyzer'):
                    print(f"   - 記憶分析器: ✅ 已載入")
            else:
                print("   - 記憶管理器: ❌ 未載入")
                return False
                
            return True
        else:
            print("❌ MEM模組載入失敗")
            return False
            
    except Exception as e:
        print(f"❌ 初始化測試異常: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_identity_token_from_working_context():
    """測試從Working Context獲取身份令牌功能"""
    print("\n=== 測試從Working Context獲取身份令牌功能 ===")
    
    try:
        mem_module = get_module("mem_module")
        if not mem_module or not mem_module.is_initialized:
            print("❌ MEM模組未正確初始化")
            return False
        
        # 測試身份令牌管理
        identity_manager = mem_module.memory_manager.identity_manager
        
        # 獲取當前記憶體令牌（從Working Context）
        current_token = identity_manager.get_current_memory_token()
        print(f"✅ 當前記憶體令牌獲取成功")
        print(f"   - 記憶體令牌: {current_token}")
        
        # 測試記憶體存取驗證
        can_read = identity_manager.validate_memory_access(current_token, "read")
        can_write = identity_manager.validate_memory_access(current_token, "write")
        print(f"   - 讀取權限: {'✅' if can_read else '❌'}")
        print(f"   - 寫入權限: {'✅' if can_write else '❌'}")
        
        # 測試系統令牌
        system_token = identity_manager.get_system_token()
        is_system = identity_manager.is_system_token(system_token)
        print(f"   - 系統令牌: {system_token}")
        print(f"   - 系統令牌驗證: {'✅' if is_system else '❌'}")
        
        # 獲取身份資訊
        identity_info = identity_manager.get_current_identity_info()
        if identity_info:
            print(f"   - 身份資訊: {identity_info.get('identity_id', 'Unknown')}")
        else:
            print(f"   - 身份資訊: 無當前身份")
        
        return True
            
    except Exception as e:
        print(f"❌ 身份令牌測試異常: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_conversation_snapshot():
    """測試對話快照功能"""
    print("\n=== 測試對話快照功能 ===")
    
    try:
        mem_module = get_module("mem_module")
        if not mem_module or not mem_module.is_initialized:
            print("❌ MEM模組未正確初始化")
            return False
        
        # 準備測試數據
        test_identity = "test_user_002"
        test_conversation = """
        User: How's the weather today?
        Assistant: Today's weather is sunny and clear, with a temperature around 25 degrees. It's perfect for going outside.
        User: Can I go for a walk in the park?
        Assistant: Absolutely! This weather is perfect for a park walk. Don't forget to bring a water bottle.
        User: Great, thanks for the advice.
        """
        test_topic = "weather_discussion"
        
        # 創建對話快照
        snapshot_manager = mem_module.memory_manager.snapshot_manager
        
        # 使用當前記憶體令牌
        current_token = mem_module.memory_manager.identity_manager.get_current_memory_token()
        
        snapshot = snapshot_manager.create_snapshot(
            identity_token=current_token,
            content=test_conversation,
            topic=test_topic
        )
        
        if snapshot:
            print(f"✅ 對話快照創建成功")
            print(f"   - 快照ID: {snapshot.memory_id}")
            print(f"   - 主題: {snapshot.topic}")
            print(f"   - 階段編號: {snapshot.stage_number}")
            print(f"   - 內容長度: {len(snapshot.content)} 字符")
            print(f"   - 重要性評分: {snapshot.importance_score}")
            
            # 測試快照檢索
            active_snapshots = snapshot_manager.get_active_snapshots(current_token)
            print(f"   - 活躍快照數量: {len(active_snapshots)}")
            
            return True
        else:
            print("❌ 對話快照創建失敗")
            return False
            
    except Exception as e:
        print(f"❌ 對話快照測試異常: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_semantic_retrieval():
    """測試語義檢索功能（增強RAG）"""
    print("\n=== 測試語義檢索功能 ===")
    
    try:
        mem_module = get_module("mem_module")
        if not mem_module or not mem_module.is_initialized:
            print("❌ MEM模組未正確初始化")
            return False
        
        # 先創建一些測試記憶
        test_memories = [
            "I like to walk in the park on sunny days",
            "Yesterday I went to a coffee shop and had a latte", 
            "I'm currently learning Python programming",
            "Planning to visit the library next week to read books"
        ]
        
        identity_token = mem_module.memory_manager.identity_manager.get_current_memory_token()
        
        # 添加測試記憶到系統
        for i, memory_content in enumerate(test_memories):
            mem_data = MEMModuleData(
                text=memory_content,
                operation_type="store",
                identity_token=identity_token,
                content=memory_content,
                memory_type="user_preference"
            )
            mem_module.handle(mem_data)
        
        # 測試語義檢索
        query_data = MEMModuleData(
            text="outdoor activities related memories",
            operation_type="query",
            query_text="outdoor activities related memories",
            identity_token=identity_token,
            max_results=3
        )
        
        result = mem_module.handle(query_data)
        
        if result and result.get("success"):
            print("✅ 語義檢索測試成功")
            print(f"   - 查詢文本: {query_data.query_text}")
            print(f"   - 結果數量: {result.get('total_results', 0)}")
            
            # 顯示檢索結果
            results = result.get('results', [])
            if isinstance(results, list):
                for i, res in enumerate(results[:3], 1):
                    if isinstance(res, dict):
                        content = res.get('content', 'N/A')
                        print(f"   - 結果{i}: {str(content)[:50]}...")
                    else:
                        print(f"   - 結果{i}: {str(res)[:50]}...")
            else:
                print(f"   - 結果類型: {type(results)}")
            
            return True
        else:
            print("❌ 語義檢索測試失敗")
            if result:
                print(f"   - 錯誤: {result.get('error', 'Unknown error')}")
            return False
            
    except Exception as e:
        print(f"❌ 語義檢索測試異常: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_memory_analysis():
    """測試記憶分析功能"""
    print("\n=== 測試記憶分析功能 ===")
    
    try:
        mem_module = get_module("mem_module")
        if not mem_module or not mem_module.is_initialized:
            print("❌ MEM模組未正確初始化")
            return False
        
        # 測試文本分析
        test_text = """
        Today is a beautiful day and I went for a walk in the park. I saw many beautiful flowers.
        The weather is great and sunny, which makes me feel happy. I also met an old friend,
        and we talked for a long time about recent life and work. Tonight I plan to go to a restaurant for dinner to celebrate.
        """
        
        memory_analyzer = mem_module.memory_manager.memory_analyzer
        
        # 提取關鍵詞
        keywords = memory_analyzer.extract_keywords(test_text)
        print(f"✅ 關鍵詞提取完成")
        print(f"   - 關鍵詞: {', '.join(keywords[:5])}...")
        
        # 提取主題
        topic = memory_analyzer.extract_topic(test_text)
        print(f"   - 主題: {topic}")
        
        # 生成摘要
        summary = memory_analyzer.generate_summary(test_text)
        print(f"   - 摘要: {summary[:100]}...")
        
        # 評估重要性
        importance_result = memory_analyzer.evaluate_importance(test_text)
        if isinstance(importance_result, dict):
            importance_score = importance_result.get("confidence", 0.0)
            importance_level = importance_result.get("level", "未知")
            print(f"   - 重要性評分: {importance_score:.2f}")
            print(f"   - 重要性等級: {importance_level}")
        else:
            print(f"   - 重要性評估: {importance_result}")
        
        return True
        
    except Exception as e:
        print(f"❌ 記憶分析測試異常: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """主測試函數"""
    print("MEM模組重構功能測試開始...\n")
    
    tests = [
        test_refactored_initialization,
        test_identity_token_from_working_context,
        test_conversation_snapshot,
        test_semantic_retrieval,
        test_memory_analysis
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
        else:
            print("測試失敗，繼續執行其他測試...")
    
    print(f"\n=== 測試結果 ===")
    print(f"通過: {passed}/{total}")
    
    if passed == total:
        print("🎉 所有重構功能測試都通過了！")
        print("✅ Identity嵌入功能正常")
        print("✅ 快照管理功能正常") 
        print("✅ 語義檢索功能正常")
        print("✅ 記憶分析功能正常")
        return True
    else:
        print("❌ 部分重構功能測試失敗")
        return False

if __name__ == "__main__":
    main()
