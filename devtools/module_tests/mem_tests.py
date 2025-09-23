# -*- coding: utf-8 -*-
"""
MEM 模組測試函數
已重構模組 - 完整功能測試
"""

from utils.debug_helper import debug_log, info_log, error_log
from modules.mem_module.schemas import MEMInput, MEMOutput, MemoryType, MemoryImportance

# ===== 測試用預設資料 =====
DEFAULT_MEMORY_TOKEN = "mem_token_debug_2024"

# ===== 純MEM功能測試 =====

def mem_test_memory_query(modules, identity="test_user", query_text="天氣"):
    """測試記憶查詢功能 - 根據關鍵字查詢記憶"""
    mem = modules.get("mem")
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "Module not loaded"}

    try:
        memory_token = f"mem_token_{identity}"

        print(f"\n🔍 測試記憶查詢 - 關鍵字: '{query_text}'")
        print("=" * 60)
        print(f"👤 身份ID: {identity}")
        print(f"🗝️ 記憶令牌: {memory_token}")

        mem_input = MEMInput(
            operation_type="query_memory",
            memory_token=memory_token,
            query_text=query_text,
            max_results=10
        )

        result = mem.handle(mem_input)

        if isinstance(result, MEMOutput) and result.success:
            results_count = len(result.search_results) if hasattr(result, 'search_results') else 0
            print(f"✅ 查詢成功 - 找到 {results_count} 條相關記錄")

            if hasattr(result, 'search_results') and result.search_results:
                print(f"\n📋 查詢結果:")
                for i, search_result in enumerate(result.search_results[:5]):
                    content = search_result.get('content', '')[:80] + ('...' if len(search_result.get('content', '')) > 80 else '')
                    confidence = search_result.get('confidence', 0)
                    memory_type = search_result.get('memory_type', 'unknown')
                    print(f"   {i+1}. {content}")
                    print(f"       類型: {memory_type}, 相似度: {confidence:.3f}")

            return {"success": True, "results_count": results_count}
        else:
            error_msg = result.message if hasattr(result, 'message') else '未知錯誤'
            print(f"❌ 查詢失敗: {error_msg}")
            return {"success": False, "error": str(result)}

    except Exception as e:
        error_log(f"[MEM Test] 記憶查詢失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_conversation_snapshot(modules, identity="test_user", conversation="你好，今天天氣如何？"):
    """測試對話快照查詢功能 - 查詢對話快照"""
    mem = modules.get("mem")
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "Module not loaded"}

    try:
        memory_token = f"mem_token_{identity}"

        print(f"\n📸 測試對話快照查詢 - 對話: '{conversation}'")
        print("=" * 60)
        print(f"👤 身份ID: {identity}")
        print(f"🗝️ 記憶令牌: {memory_token}")

        mem_input = MEMInput(
            operation_type="query_memory",
            memory_token=memory_token,
            query_text=conversation,
            memory_types=[MemoryType.SNAPSHOT.value],
            max_results=10
        )

        result = mem.handle(mem_input)

        if isinstance(result, MEMOutput) and result.success:
            results_count = len(result.search_results) if hasattr(result, 'search_results') else 0
            print(f"✅ 快照查詢成功 - 找到 {results_count} 個快照")

            if hasattr(result, 'search_results') and result.search_results:
                print(f"\n📋 快照結果:")
                for i, snapshot in enumerate(result.search_results[:3]):
                    content = snapshot.get('content', '')[:100] + ('...' if len(snapshot.get('content', '')) > 100 else '')
                    confidence = snapshot.get('confidence', 0)
                    print(f"   {i+1}. {content}")
                    print(f"       相似度: {confidence:.3f}")

            return {"success": True, "snapshots_count": results_count}
        else:
            error_msg = result.message if hasattr(result, 'message') else '未知錯誤'
            print(f"❌ 快照查詢失敗: {error_msg}")
            return {"success": False, "error": str(result)}

    except Exception as e:
        error_log(f"[MEM Test] 快照查詢失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_memory_access_control(modules, memory_token=None):
    """測試記憶庫列表功能 - 列出記憶庫內容"""
    mem = modules.get("mem")
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "Module not loaded"}

    try:
        token = memory_token or DEFAULT_MEMORY_TOKEN

        print(f"\n🗃️ 測試記憶庫列表 - 令牌: {token}")
        print("=" * 60)

        mem_input = MEMInput(
            operation_type="query_memory",
            memory_token=token,
            query_text="",  # 空查詢以獲取所有記憶
            max_results=50
        )

        result = mem.handle(mem_input)

        if isinstance(result, MEMOutput) and result.success:
            results_count = len(result.search_results) if hasattr(result, 'search_results') else 0
            print(f"✅ 記憶庫列表成功 - 找到 {results_count} 條記憶")

            # 統計記憶類型
            memory_types = {}
            if hasattr(result, 'search_results') and result.search_results:
                for memory in result.search_results:
                    mem_type = memory.get('memory_type', 'unknown')
                    memory_types[mem_type] = memory_types.get(mem_type, 0) + 1

                print(f"\n📊 記憶類型統計:")
                for mem_type, count in memory_types.items():
                    print(f"   {mem_type}: {count} 條")

            return {"success": True, "total_memories": results_count, "memory_types": memory_types}
        else:
            error_msg = result.message if hasattr(result, 'message') else '未知錯誤'
            print(f"❌ 記憶庫列表失敗: {error_msg}")
            return {"success": False, "error": str(result)}

    except Exception as e:
        error_log(f"[MEM Test] 記憶庫列表失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_identity_manager_stats(modules, identity="test_user"):
    """測試記憶統計功能 - 統計記憶數量"""
    mem = modules.get("mem")
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "Module not loaded"}

    try:
        memory_token = f"mem_token_{identity}"

        print(f"\n📊 測試記憶統計 - 身份: {identity}")
        print("=" * 60)
        print(f"🗝️ 記憶令牌: {memory_token}")

        # 統計不同類型的記憶
        memory_types_to_check = [
            MemoryType.SNAPSHOT.value,
            MemoryType.LONG_TERM.value,
            MemoryType.PROFILE.value,
            MemoryType.PREFERENCE.value
        ]

        stats = {"memory_token": memory_token, "memory_counts": {}, "total_memories": 0}

        for mem_type in memory_types_to_check:
            mem_input = MEMInput(
                operation_type="query_memory",
                memory_token=memory_token,
                query_text="",
                memory_types=[mem_type],
                max_results=100
            )

            result = mem.handle(mem_input)

            if isinstance(result, MEMOutput) and result.success:
                count = len(result.search_results) if hasattr(result, 'search_results') else 0
                stats["memory_counts"][mem_type] = count
                stats["total_memories"] += count
            else:
                stats["memory_counts"][mem_type] = 0

        print(f"✅ 統計生成完成")
        print(f"📈 總記憶數量: {stats['total_memories']}")
        print(f"\n📋 各類型統計:")
        for mem_type, count in stats["memory_counts"].items():
            print(f"   {mem_type}: {count} 條")

        return {"success": True, "stats": stats}

    except Exception as e:
        error_log(f"[MEM Test] 記憶統計失敗: {e}")
        return {"success": False, "error": str(e)}

# 保留以下兩個函數以維持與debug_api的兼容性，但移除整合相關內容
def mem_test_nlp_integration(modules, nlp_text="今天天氣很好", identity_token="test_user"):
    """簡化版NLP整合測試 - 實際上只做記憶查詢"""
    return mem_test_memory_query(modules, identity_token, nlp_text)

def mem_test_llm_context_extraction(modules, conversation_text="用戶詢問天氣資訊", identity_token="test_user"):
    """簡化版LLM上下文提取測試 - 實際上只做記憶查詢"""
    return mem_test_memory_query(modules, identity_token, conversation_text)

def mem_test_full_workflow(modules, identity="test_user"):
    """測試完整MEM工作流程 - 整合所有核心功能"""
    print("🚀 開始 MEM 模組基礎測試")
    print("="*60)

    try:
        results = {}

        # 1. 記憶查詢測試
        print("\n1. 🔍 記憶查詢測試")
        query_result = mem_test_memory_query(modules, identity, "天氣")
        results["memory_query"] = query_result

        # 2. 快照查詢測試
        print("\n2. 📸 對話快照查詢測試")
        snapshot_result = mem_test_conversation_snapshot(modules, identity, "今天天氣如何")
        results["snapshot_query"] = snapshot_result

        # 3. 記憶庫列表測試
        print("\n3. 🗃️ 記憶庫列表測試")
        access_result = mem_test_memory_access_control(modules, f"mem_token_{identity}")
        results["memory_listing"] = access_result

        # 4. 統計測試
        print("\n4. 📊 記憶統計測試")
        stats_result = mem_test_identity_manager_stats(modules, identity)
        results["statistics"] = stats_result

        print("\n" + "="*60)
        print("📊 測試總結")

        success_count = sum(1 for result in results.values() if result["success"])
        total_tests = len(results)

        for test_name, result in results.items():
            status = "✅ 通過" if result["success"] else "❌ 失敗"
            test_names = {
                "memory_query": "記憶查詢",
                "snapshot_query": "快照查詢",
                "memory_listing": "記憶庫列表",
                "statistics": "記憶統計"
            }
            print(f"   {test_names.get(test_name, test_name)}: {status}")

        print(f"\n📈 最終結果: {success_count}/{total_tests} 項測試通過")

        return {"success": success_count == total_tests, "results": results}

    except Exception as e:
        error_log(f"[MEM Test] 工作流程測試失敗: {e}")
        return {"success": False, "error": str(e)}

# 使用範例
if __name__ == "__main__":
    print("MEM 模組純功能測試套件")
    print("僅測試 MEM 模組記憶操作功能")
    print(f"預設記憶令牌: {DEFAULT_MEMORY_TOKEN}")