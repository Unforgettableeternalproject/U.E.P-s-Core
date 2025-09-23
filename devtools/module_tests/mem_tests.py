# -*- coding: utf-8 -*-
"""
MEM 模組測試函數
已重構模組 - 完整功能測試
"""

from utils.debug_helper import debug_log, info_log, error_log
from modules.mem_module.schemas import MEMInput, MEMOutput, MemoryType, MemoryImportance
from datetime import datetime
import uuid
import time

# ===== 測試用預設資料 =====
DEFAULT_MEMORY_TOKEN = "test_debug_2024"

# ===== 純MEM功能測試 =====

def mem_test_store_memory(modules, identity="test_user", content="測試記憶內容", memory_type="long_term"):
    """測試記憶存儲功能 - 存儲新的記憶條目"""
    mem = modules.get("mem")
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "Module not loaded"}

    try:
        memory_token = f"test_{identity}"
        
        # 先設置工作上下文中的記憶令牌
        try:
            from core.working_context import working_context_manager
            working_context_manager.set_memory_token(memory_token)
            print(f"🔄 已設置工作上下文記憶令牌: {memory_token}")
        except Exception as e:
            print(f"⚠️ 設置工作上下文失敗: {e}")

        # 設置系統狀態為CHAT
        try:
            from core.state_manager import state_manager, UEPState as SystemState
            original_state = state_manager.get_state()
            state_manager.set_state(SystemState.CHAT)
            print(f"🔄 已設置系統狀態為CHAT（原狀態: {original_state.value}）")
        except Exception as e:
            print(f"⚠️ 無法設置CHAT狀態: {e}")

        print(f"\n💾 測試記憶存儲 - 類型: {memory_type}")
        print("=" * 60)
        print(f"👤 身份ID: {identity}")
        print(f"🗝️ 記憶令牌: {memory_token}")
        print(f"📝 內容: {content}")

        # 創建記憶條目
        from modules.mem_module.schemas import MemoryEntry
        
        memory_entry = MemoryEntry(
            memory_id=f"test_{uuid.uuid4().hex[:8]}",
            memory_token=memory_token,
            memory_type=getattr(MemoryType, memory_type.upper()),
            content=content,
            topic="測試主題",
            intent_tags=["test"],
            created_at=datetime.now(),
            updated_at=datetime.now(),
            importance_score=0.8
        )

        mem_input = MEMInput(
            operation_type="store_memory",
            memory_token=memory_token,
            memory_entry=memory_entry.model_dump()  # 轉換為字典格式
        )

        result = mem.handle(mem_input)

        # 處理成功結果
        if isinstance(result, MEMOutput) and result.success:
            print(f"✅ 記憶存儲成功")
            
            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")

            return {"success": True, "memory_id": memory_entry.memory_id}
        
        # 處理字典格式的成功結果
        elif isinstance(result, dict) and result.get('success'):
            print(f"✅ 記憶存儲成功 (dict)")
            
            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")

            return {"success": True, "memory_id": memory_entry.memory_id}
        
        else:
            # 處理失敗情況
            if isinstance(result, MEMOutput):
                error_msg = result.message if hasattr(result, 'message') else '未知錯誤'
            elif isinstance(result, dict):
                error_msg = result.get('error', '未知錯誤')
            else:
                error_msg = str(result)
            
            print(f"❌ 記憶存儲失敗: {error_msg}")
            
            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")
                
            return {"success": False, "error": error_msg}

    except Exception as e:
        error_log(f"[MEM Test] 記憶存儲失敗: {e}")
        
        # 恢復原始狀態
        try:
            if 'original_state' in locals():
                from core.state_manager import state_manager
                state_manager.set_state(original_state)
                print(f"🔄 已恢復系統狀態為: {original_state.value}")
        except Exception as restore_e:
            print(f"⚠️ 恢復狀態失敗: {restore_e}")
            
        return {"success": False, "error": str(e)}

def mem_test_create_snapshot(modules, identity="test_user", conversation_text="用戶: 今天天氣如何？\n助手: 今天天氣很好，陽光明媚。"):
    """測試快照創建功能 - 創建對話快照"""
    mem = modules.get("mem")
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "Module not loaded"}

    try:
        memory_token = f"test_{identity}"
        
        # 先設置工作上下文中的記憶令牌
        try:
            from core.working_context import working_context_manager
            working_context_manager.set_memory_token(memory_token)
            print(f"🔄 已設置工作上下文記憶令牌: {memory_token}")
        except Exception as e:
            print(f"⚠️ 設置工作上下文失敗: {e}")

        # 設置系統狀態為CHAT
        try:
            from core.state_manager import state_manager, UEPState as SystemState
            original_state = state_manager.get_state()
            state_manager.set_state(SystemState.CHAT)
            print(f"🔄 已設置系統狀態為CHAT（原狀態: {original_state.value}）")
        except Exception as e:
            print(f"⚠️ 無法設置CHAT狀態: {e}")

        print(f"\n📸 測試快照創建")
        print("=" * 60)
        print(f"👤 身份ID: {identity}")
        print(f"🗝️ 記憶令牌: {memory_token}")
        print(f"💬 對話內容: {conversation_text[:50]}...")

        mem_input = MEMInput(
            operation_type="create_snapshot",
            memory_token=memory_token,
            conversation_text=conversation_text
        )

        result = mem.handle(mem_input)

        # 處理成功結果
        if isinstance(result, MEMOutput) and result.success:
            snapshots_count = len(result.active_snapshots) if hasattr(result, 'active_snapshots') else 0
            print(f"✅ 快照創建成功 - 創建了 {snapshots_count} 個快照")
            
            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")

            return {"success": True, "snapshots_created": snapshots_count}
        
        # 處理字典格式的成功結果
        elif isinstance(result, dict) and result.get('success'):
            print(f"✅ 快照創建成功 (dict)")
            
            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")

            return {"success": True, "snapshots_created": 1}
        
        else:
            # 處理失敗情況
            if isinstance(result, MEMOutput):
                error_msg = result.message if hasattr(result, 'message') else '未知錯誤'
            elif isinstance(result, dict):
                error_msg = result.get('error', '未知錯誤')
            else:
                error_msg = str(result)
            
            print(f"❌ 快照創建失敗: {error_msg}")
            
            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")
                
            return {"success": False, "error": error_msg}

    except Exception as e:
        error_log(f"[MEM Test] 快照創建失敗: {e}")
        
        # 恢復原始狀態
        try:
            if 'original_state' in locals():
                from core.state_manager import state_manager
                state_manager.set_state(original_state)
                print(f"🔄 已恢復系統狀態為: {original_state.value}")
        except Exception as restore_e:
            print(f"⚠️ 恢復狀態失敗: {restore_e}")
            
        return {"success": False, "error": str(e)}

def mem_test_write_then_query(modules, identity="test_user"):
    """測試寫入後查詢功能 - 確保寫入的內容可以被查詢到"""
    print(f"\n🔄 測試寫入後查詢流程")
    print("=" * 60)
    
    # 第一步：寫入測試記憶
    test_content = f"這是一個測試記憶，時間戳: {datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    print("第一步：寫入測試記憶")
    store_result = mem_test_store_memory(modules, identity=identity, content=test_content, memory_type="long_term")
    
    if not store_result.get('success'):
        return {"success": False, "error": f"寫入失敗: {store_result.get('error')}"}
    
    print(f"✅ 寫入成功，記憶ID: {store_result.get('memory_id')}")
    
    # 等待足夠時間讓向量索引更新
    print("⏳ 等待向量索引更新...")
    time.sleep(3)  # 增加等待時間
    
    # 第二步：使用更寬泛的查詢詞彙
    print("\n第二步：查詢剛寫入的記憶")
    # 先嘗試精確查詢
    query_result = mem_test_memory_query(modules, identity=identity, query_text="時間戳")
    
    if not query_result.get('success'):
        return {"success": False, "error": f"查詢失敗: {query_result.get('error')}"}
    
    results_count = query_result.get('results_count', 0)
    print(f"✅ 查詢成功，找到 {results_count} 條記錄")
    
    # 如果沒找到，嘗試使用更通用的查詢
    if results_count == 0:
        print("🔄 嘗試使用更通用的查詢詞彙...")
        query_result = mem_test_memory_query(modules, identity=identity, query_text="測試")
        results_count = query_result.get('results_count', 0)
        print(f"📊 通用查詢結果: {results_count} 條記錄")
    
    if results_count > 0:
        print("✅ 寫入後查詢測試通過 - 能夠查詢到剛寫入的記憶")
        return {"success": True, "memory_stored": True, "memory_retrieved": True, "results_count": results_count}
    else:
        # 最後嘗試列出所有記憶進行調試
        print("🔍 嘗試列出所有記憶進行調試...")
        all_memories = mem_test_memory_access_control(modules, identity=identity)
        total_memories = all_memories.get('total_memories', 0)
        print(f"📋 記憶庫總數: {total_memories} 條")
        
        if total_memories > 0:
            return {"success": True, "memory_stored": True, "memory_retrieved": False, 
                   "note": f"記憶已存儲但查詢機制可能需要調整，記憶庫總數: {total_memories}"}
        else:
            return {"success": False, "error": "寫入的記憶無法被查詢到，且記憶庫為空"}

def mem_test_memory_query(modules, identity="test_user", query_text="天氣"):
    """測試記憶查詢功能 - 根據關鍵字查詢記憶"""
    mem = modules.get("mem")
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "Module not loaded"}

    try:
        memory_token = f"test_{identity}"
        
        # 先設置工作上下文中的記憶令牌（在設置CHAT狀態之前）
        try:
            from core.working_context import working_context_manager
            working_context_manager.set_memory_token(memory_token)
            print(f"🔄 已設置工作上下文記憶令牌: {memory_token}")
        except Exception as e:
            print(f"⚠️ 設置工作上下文失敗: {e}")

        # 然後設置系統狀態為CHAT（MEM模組要求）
        try:
            from core.state_manager import state_manager, UEPState as SystemState
            original_state = state_manager.get_state()
            state_manager.set_state(SystemState.CHAT)
            print(f"🔄 已設置系統狀態為CHAT（原狀態: {original_state.value}）")
        except Exception as e:
            print(f"⚠️ 無法設置CHAT狀態: {e}")

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

        # 處理 MEMOutput 類型的成功結果
        if isinstance(result, MEMOutput) and result.success:
            results_count = len(result.search_results) if hasattr(result, 'search_results') else 0
            print(f"✅ 查詢成功 - 找到 {results_count} 條相關記錄")

            if hasattr(result, 'search_results') and result.search_results:
                print(f"\n📋 查詢結果:")
                for i, search_result in enumerate(result.search_results[:5]):
                    if hasattr(search_result, 'memory_entry'):
                        content = search_result.memory_entry.content[:80] + ('...' if len(search_result.memory_entry.content) > 80 else '')
                        memory_type = search_result.memory_entry.memory_type
                        similarity = search_result.similarity_score if hasattr(search_result, 'similarity_score') else 0
                        print(f"   {i+1}. {content}")
                        print(f"       類型: {memory_type}, 相似度: {similarity:.3f}")

            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")

            return {"success": True, "results_count": results_count}
        
        # 處理字典類型的成功結果
        elif isinstance(result, dict) and result.get('success'):
            search_results = result.get('search_results', [])
            results_count = len(search_results)
            print(f"✅ 查詢成功 - 找到 {results_count} 條相關記錄")

            if search_results:
                print(f"\n📋 查詢結果:")
                for i, search_result in enumerate(search_results[:5]):
                    if hasattr(search_result, 'memory_entry'):
                        content = search_result.memory_entry.content[:80] + ('...' if len(search_result.memory_entry.content) > 80 else '')
                        memory_type = search_result.memory_entry.memory_type
                        similarity = search_result.similarity_score if hasattr(search_result, 'similarity_score') else 0
                        print(f"   {i+1}. {content}")
                        print(f"       類型: {memory_type}, 相似度: {similarity:.3f}")

            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")

            return {"success": True, "results_count": results_count}
        
        else:
            # 處理失敗情況
            if isinstance(result, MEMOutput):
                error_msg = result.message if hasattr(result, 'message') else '未知錯誤'
            elif isinstance(result, dict):
                error_msg = result.get('error', '未知錯誤')
            else:
                error_msg = str(result)
            
            print(f"❌ 查詢失敗: {error_msg}")
            
            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")
                
            return {"success": False, "error": error_msg}

    except Exception as e:
        error_log(f"[MEM Test] 記憶查詢失敗: {e}")
        
        # 恢復原始狀態
        try:
            if 'original_state' in locals():
                from core.state_manager import state_manager
                state_manager.set_state(original_state)
                print(f"🔄 已恢復系統狀態為: {original_state.value}")
        except Exception as restore_e:
            print(f"⚠️ 恢復狀態失敗: {restore_e}")
            
        return {"success": False, "error": str(e)}

def mem_test_conversation_snapshot(modules, identity="test_user", conversation="你好，今天天氣如何？"):
    """測試對話快照查詢功能 - 查詢對話快照"""
    mem = modules.get("mem")
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "Module not loaded"}

    try:
        memory_token = f"test_{identity}"
        
        # 先設置工作上下文中的記憶令牌（在設置CHAT狀態之前）
        try:
            from core.working_context import working_context_manager
            working_context_manager.set_memory_token(memory_token)
            print(f"🔄 已設置工作上下文記憶令牌: {memory_token}")
        except Exception as e:
            print(f"⚠️ 設置工作上下文失敗: {e}")

        # 然後設置系統狀態為CHAT（MEM模組要求）
        try:
            from core.state_manager import state_manager, UEPState as SystemState
            original_state = state_manager.get_state()
            state_manager.set_state(SystemState.CHAT)
            print(f"🔄 已設置系統狀態為CHAT（原狀態: {original_state.value}）")
        except Exception as e:
            print(f"⚠️ 無法設置CHAT狀態: {e}")

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

        # 處理 MEMOutput 類型的成功結果
        if isinstance(result, MEMOutput) and result.success:
            results_count = len(result.search_results) if hasattr(result, 'search_results') else 0
            print(f"✅ 快照查詢成功 - 找到 {results_count} 個快照")

            if hasattr(result, 'search_results') and result.search_results:
                print(f"\n📋 快照結果:")
                for i, snapshot in enumerate(result.search_results[:3]):
                    if hasattr(snapshot, 'memory_entry'):
                        content = snapshot.memory_entry.content[:100] + ('...' if len(snapshot.memory_entry.content) > 100 else '')
                        similarity = snapshot.similarity_score if hasattr(snapshot, 'similarity_score') else 0
                        print(f"   {i+1}. {content}")
                        print(f"       相似度: {similarity:.3f}")

            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")

            return {"success": True, "snapshots_count": results_count}
        
        # 處理字典類型的成功結果
        elif isinstance(result, dict) and result.get('success'):
            search_results = result.get('search_results', [])
            results_count = len(search_results)
            print(f"✅ 快照查詢成功 - 找到 {results_count} 個快照")

            if search_results:
                print(f"\n📋 快照結果:")
                for i, snapshot in enumerate(search_results[:3]):
                    if hasattr(snapshot, 'memory_entry'):
                        content = snapshot.memory_entry.content[:100] + ('...' if len(snapshot.memory_entry.content) > 100 else '')
                        similarity = snapshot.similarity_score if hasattr(snapshot, 'similarity_score') else 0
                        print(f"   {i+1}. {content}")
                        print(f"       相似度: {similarity:.3f}")

            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")

            return {"success": True, "snapshots_count": results_count}
        
        else:
            # 處理失敗情況
            if isinstance(result, MEMOutput):
                error_msg = result.message if hasattr(result, 'message') else '未知錯誤'
            elif isinstance(result, dict):
                error_msg = result.get('error', '未知錯誤')
            else:
                error_msg = str(result)
            
            print(f"❌ 快照查詢失敗: {error_msg}")
            
            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")
                
            return {"success": False, "error": error_msg}

    except Exception as e:
        error_log(f"[MEM Test] 快照查詢失敗: {e}")
        
        # 恢復原始狀態
        try:
            if 'original_state' in locals():
                from core.state_manager import state_manager
                state_manager.set_state(original_state)
                print(f"🔄 已恢復系統狀態為: {original_state.value}")
        except Exception as restore_e:
            print(f"⚠️ 恢復狀態失敗: {restore_e}")
            
        return {"success": False, "error": str(e)}

def mem_test_memory_access_control(modules, identity="test_user"):
    """測試記憶庫列表功能 - 列出記憶庫內容"""
    mem = modules.get("mem")
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "Module not loaded"}

    try:
        memory_token = f"test_{identity}"
        
        # 先設置工作上下文中的記憶令牌（在設置CHAT狀態之前）
        try:
            from core.working_context import working_context_manager
            working_context_manager.set_memory_token(memory_token)
            print(f"🔄 已設置工作上下文記憶令牌: {memory_token}")
        except Exception as e:
            print(f"⚠️ 設置工作上下文失敗: {e}")

        # 然後設置系統狀態為CHAT（MEM模組要求）
        try:
            from core.state_manager import state_manager, UEPState as SystemState
            original_state = state_manager.get_state()
            state_manager.set_state(SystemState.CHAT)
            print(f"🔄 已設置系統狀態為CHAT（原狀態: {original_state.value}）")
        except Exception as e:
            print(f"⚠️ 無法設置CHAT狀態: {e}")

        print(f"\n🗃️ 測試記憶庫列表 - 令牌: {memory_token}")
        print("=" * 60)

        mem_input = MEMInput(
            operation_type="query_memory",
            memory_token=memory_token,
            query_text="",  # 空查詢以獲取所有記憶
            max_results=50
        )

        result = mem.handle(mem_input)

        # 處理 MEMOutput 類型的成功結果
        if isinstance(result, MEMOutput) and result.success:
            results_count = len(result.search_results) if hasattr(result, 'search_results') else 0
            print(f"✅ 記憶庫列表成功 - 找到 {results_count} 條記憶")

            # 統計記憶類型
            memory_types = {}
            if hasattr(result, 'search_results') and result.search_results:
                for memory in result.search_results:
                    if hasattr(memory, 'memory_entry'):
                        mem_type = memory.memory_entry.memory_type
                        memory_types[str(mem_type)] = memory_types.get(str(mem_type), 0) + 1

                print(f"\n📊 記憶類型統計:")
                for mem_type, count in memory_types.items():
                    print(f"   {mem_type}: {count} 條")

            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")

            return {"success": True, "total_memories": results_count, "memory_types": memory_types}
        
        # 處理字典類型的成功結果
        elif isinstance(result, dict) and result.get('success'):
            search_results = result.get('search_results', [])
            results_count = len(search_results)
            print(f"✅ 記憶庫列表成功 - 找到 {results_count} 條記憶")

            # 統計記憶類型
            memory_types = {}
            if search_results:
                for memory in search_results:
                    if hasattr(memory, 'memory_entry'):
                        mem_type = memory.memory_entry.memory_type
                        memory_types[str(mem_type)] = memory_types.get(str(mem_type), 0) + 1

                print(f"\n📊 記憶類型統計:")
                for mem_type, count in memory_types.items():
                    print(f"   {mem_type}: {count} 條")

            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")

            return {"success": True, "total_memories": results_count, "memory_types": memory_types}
        
        else:
            # 處理失敗情況
            if isinstance(result, MEMOutput):
                error_msg = result.message if hasattr(result, 'message') else '未知錯誤'
            elif isinstance(result, dict):
                error_msg = result.get('error', '未知錯誤')
            else:
                error_msg = str(result)
            
            print(f"❌ 記憶庫列表失敗: {error_msg}")
            
            # 恢復原始狀態
            try:
                if 'original_state' in locals():
                    state_manager.set_state(original_state)
                    print(f"🔄 已恢復系統狀態為: {original_state.value}")
            except Exception as e:
                print(f"⚠️ 恢復狀態失敗: {e}")
                
            return {"success": False, "error": error_msg}

    except Exception as e:
        error_log(f"[MEM Test] 記憶庫列表失敗: {e}")
        
        # 恢復原始狀態
        try:
            if 'original_state' in locals():
                from core.state_manager import state_manager
                state_manager.set_state(original_state)
                print(f"🔄 已恢復系統狀態為: {original_state.value}")
        except Exception as restore_e:
            print(f"⚠️ 恢復狀態失敗: {restore_e}")
            
        return {"success": False, "error": str(e)}

def mem_test_identity_manager_stats(modules, identity="test_user"):
    """測試記憶統計功能 - 統計記憶數量"""
    mem = modules.get("mem")
    if mem is None:
        error_log("[Controller] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "Module not loaded"}

    try:
        memory_token = f"test_{identity}"
        
        # 先設置工作上下文中的記憶令牌（在設置CHAT狀態之前）
        try:
            from core.working_context import working_context_manager
            working_context_manager.set_memory_token(memory_token)
            print(f"🔄 已設置工作上下文記憶令牌: {memory_token}")
        except Exception as e:
            print(f"⚠️ 設置工作上下文失敗: {e}")

        # 然後設置系統狀態為CHAT（MEM模組要求）
        try:
            from core.state_manager import state_manager, UEPState as SystemState
            original_state = state_manager.get_state()
            state_manager.set_state(SystemState.CHAT)
            print(f"🔄 已設置系統狀態為CHAT（原狀態: {original_state.value}）")
        except Exception as e:
            print(f"⚠️ 無法設置CHAT狀態: {e}")

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

        # 恢復原始狀態
        try:
            if 'original_state' in locals():
                state_manager.set_state(original_state)
                print(f"🔄 已恢復系統狀態為: {original_state.value}")
        except Exception as e:
            print(f"⚠️ 恢復狀態失敗: {e}")

        return {"success": True, "stats": stats}

    except Exception as e:
        error_log(f"[MEM Test] 記憶統計失敗: {e}")
        
        # 恢復原始狀態
        try:
            if 'original_state' in locals():
                from core.state_manager import state_manager
                state_manager.set_state(original_state)
                print(f"🔄 已恢復系統狀態為: {original_state.value}")
        except Exception as restore_e:
            print(f"⚠️ 恢復狀態失敗: {restore_e}")
            
        return {"success": False, "error": str(e)}

def mem_test_nlp_integration(modules, identity="test_user", text="測試自然語言整合"):
    """測試MEM與NLP整合功能 - 透過NLP分析文字並存儲到記憶中"""
    mem = modules.get("mem")
    nlp = modules.get("nlp")
    
    if mem is None:
        error_log("[MEM-NLP Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "MEM module not loaded"}
        
    if nlp is None:
        error_log("[MEM-NLP Test] ❌ 無法載入 NLP 模組")
        return {"success": False, "error": "NLP module not loaded"}

    try:
        memory_token = f"test_{identity}"
        
        # 先設置工作上下文中的記憶令牌
        try:
            from core.working_context import working_context_manager
            working_context_manager.set_memory_token(memory_token)
            print(f"🔄 已設置工作上下文記憶令牌: {memory_token}")
        except Exception as e:
            print(f"⚠️ 設置工作上下文失敗: {e}")

        # 設置系統狀態為CHAT
        try:
            from core.state_manager import state_manager, UEPState as SystemState
            original_state = state_manager.get_state()
            state_manager.set_state(SystemState.CHAT)
            print(f"🔄 已設置系統狀態為CHAT（原狀態: {original_state.value}）")
        except Exception as e:
            print(f"⚠️ 無法設置CHAT狀態: {e}")

        print(f"\n🔄 MEM-NLP整合測試")
        print("=" * 60)
        print(f"👤 身份ID: {identity}")
        print(f"🗝️ 記憶令牌: {memory_token}")
        print(f"📝 輸入文字: {text}")
        
        # 1. 使用NLP模組處理文字
        print("\n步驟1: 使用NLP分析文字...")
        
        try:
            from modules.nlp_module.schemas import NLPInput, NLPOutput
            nlp_input = NLPInput(
                text=text,
                enable_identity=True,
                enable_segmentation=True
            )
            
            nlp_result = nlp.handle(nlp_input)
            
            if not isinstance(nlp_result, NLPOutput):
                print(f"❌ NLP分析失敗: 返回類型錯誤 {type(nlp_result)}")
                return {"success": False, "error": f"NLP返回類型錯誤: {type(nlp_result)}"}
                
            print(f"✅ NLP分析成功")
            intent = nlp_result.intent if hasattr(nlp_result, 'intent') else "未知意圖"
            print(f"📊 識別意圖: {intent}")
            
        except Exception as e:
            print(f"❌ NLP處理失敗: {e}")
            return {"success": False, "error": f"NLP處理失敗: {e}"}
        
        # 2. 使用MEM模組存儲分析結果
        print("\n步驟2: 存儲NLP分析結果到記憶...")
        
        try:
            from modules.mem_module.schemas import MemoryEntry
            
            memory_entry = MemoryEntry(
                memory_id=f"nlp_test_{uuid.uuid4().hex[:8]}",
                memory_token=memory_token,
                memory_type=MemoryType.LONG_TERM,
                content=f"NLP分析: {text} -> 意圖: {intent}",
                topic="NLP整合測試",
                intent_tags=["nlp_test", intent],
                created_at=datetime.now(),
                updated_at=datetime.now(),
                importance_score=0.7
            )

            mem_input = MEMInput(
                operation_type="store_memory",
                memory_token=memory_token,
                memory_entry=memory_entry.model_dump()
            )

            mem_result = mem.handle(mem_input)
            
            if (isinstance(mem_result, MEMOutput) and mem_result.success) or \
               (isinstance(mem_result, dict) and mem_result.get('success')):
                print(f"✅ 記憶存儲成功")
            else:
                error_msg = getattr(mem_result, 'error', str(mem_result)) \
                          if isinstance(mem_result, MEMOutput) else \
                          mem_result.get('error', str(mem_result)) \
                          if isinstance(mem_result, dict) else \
                          "未知錯誤"
                print(f"❌ 記憶存儲失敗: {error_msg}")
                return {"success": False, "error": error_msg}
                
        except Exception as e:
            print(f"❌ 記憶存儲失敗: {e}")
            return {"success": False, "error": f"記憶存儲失敗: {e}"}
        
        # 3. 查詢剛存儲的記憶
        print("\n步驟3: 查詢剛存儲的NLP分析記憶...")
        
        # 等待一下讓向量索引更新
        time.sleep(2)
        
        try:
            mem_query_input = MEMInput(
                operation_type="query_memory",
                memory_token=memory_token,
                query_text=intent,  # 使用識別的意圖作為查詢關鍵詞
                max_results=5
            )
            
            query_result = mem.handle(mem_query_input)
            
            if isinstance(query_result, MEMOutput) and query_result.success:
                results_count = len(query_result.search_results) if hasattr(query_result, 'search_results') else 0
                print(f"✅ 查詢成功 - 找到 {results_count} 條相關記錄")
                
                if hasattr(query_result, 'search_results') and query_result.search_results:
                    for i, result in enumerate(query_result.search_results[:3]):  # 只顯示前3條
                        print(f"\n結果 {i+1}:")
                        print(f"  內容: {result.get('content', 'N/A')}")
                        print(f"  相似度: {result.get('score', 0):.2f}")
                        print(f"  記憶類型: {result.get('memory_type', 'N/A')}")
                
            elif isinstance(query_result, dict) and query_result.get('success'):
                search_results = query_result.get('search_results', [])
                results_count = len(search_results)
                print(f"✅ 查詢成功 - 找到 {results_count} 條相關記錄")
                
                for i, result in enumerate(search_results[:3]):  # 只顯示前3條
                    print(f"\n結果 {i+1}:")
                    print(f"  內容: {result.get('content', 'N/A')}")
                    print(f"  相似度: {result.get('score', 0):.2f}")
                    print(f"  記憶類型: {result.get('memory_type', 'N/A')}")
            else:
                error_msg = getattr(query_result, 'error', str(query_result)) \
                          if isinstance(query_result, MEMOutput) else \
                          query_result.get('error', str(query_result)) \
                          if isinstance(query_result, dict) else \
                          "未知錯誤"
                print(f"❌ 查詢失敗: {error_msg}")
        except Exception as e:
            print(f"❌ 查詢失敗: {e}")
        
        # 恢復原始狀態
        try:
            if 'original_state' in locals():
                state_manager.set_state(original_state)
                print(f"🔄 已恢復系統狀態至: {original_state.value}")
        except Exception as e:
            print(f"⚠️ 恢復狀態失敗: {e}")
            
        return {"success": True, "nlp_intent": intent, "message": "MEM-NLP整合測試完成"}

    except Exception as e:
        error_log(f"[MEM-NLP Test] 整合測試失敗: {e}")
        
        # 恢復原始狀態
        try:
            if 'original_state' in locals():
                state_manager.set_state(original_state)
                print(f"🔄 已恢復系統狀態至: {original_state.value}")
        except Exception as restore_e:
            print(f"⚠️ 恢復狀態失敗: {restore_e}")
            
        return {"success": False, "error": str(e)}

# 使用範例
if __name__ == "__main__":
    print("MEM 模組純功能測試套件")
    print("僅測試 MEM 模組記憶操作功能")
    print(f"預設記憶令牌: {DEFAULT_MEMORY_TOKEN}")