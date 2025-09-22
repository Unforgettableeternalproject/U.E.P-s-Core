"""
MEM 模組測試 - 簡化版本，專注於核心記憶操作功能
使用英文內容進行記憶存儲，以便與用戶的英文互動兼容
"""

from datetime import datetime
from modules.mem_module.mem_module import MEMInput, MEMOutput
from utils.logger import error_log

def mem_test_memory_creation(modules, memory_token="test_user", content="Today is a beautiful sunny day, perfect for outdoor activities"):
    """測試記憶建立功能 - 英文內容"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ Unable to load MEM module")
        return {"success": False, "error": "Module not loaded"}

    try:
        print("💾 Testing memory creation functionality...")
        
        # 建立記憶輸入
        memory_entry = {
            "content": content,
            "memory_type": "episodic",  # 情節記憶
            "topic": "daily_experience", 
            "importance": "medium",
            "timestamp": datetime.now().isoformat()
        }
        
        mem_input = MEMInput(
            operation_type="store_memory",
            memory_token=memory_token,
            memory_entry=memory_entry
        )
        
        result = mem.handle(mem_input)
        
        if isinstance(result, MEMOutput) and result.success:
            print(f"   ✅ Memory creation successful: {result.message}")
            return {
                "success": True,
                "memory_content": content,
                "memory_token": memory_token,
                "stored_at": memory_entry["timestamp"]
            }
        else:
            print(f"   ❌ Memory creation failed: {result.message if hasattr(result, 'message') else 'Unknown error'}")
            return {"success": False, "error": str(result)}
            
    except Exception as e:
        error_log(f"[MEM Test] Memory creation test failed: {e}")
        return {"success": False, "error": str(e)}

def mem_test_snapshot_creation(modules, memory_token="test_user", conversation="Hello, how are you today? I'm looking forward to our conversation."):
    """測試快照建立功能 - 英文對話內容"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ Unable to load MEM module")
        return {"success": False, "error": "Module not loaded"}

    try:
        print("📸 Testing snapshot creation functionality...")
        
        # 建立對話快照
        snapshot_entry = {
            "content": conversation,
            "memory_type": "snapshot",
            "topic": "conversation_record",
            "importance": "high",
            "timestamp": datetime.now().isoformat(),
            "context": "Daily conversation session"
        }
        
        mem_input = MEMInput(
            operation_type="store_memory",
            memory_token=memory_token,
            memory_entry=snapshot_entry
        )
        
        result = mem.handle(mem_input)
        
        if isinstance(result, MEMOutput) and result.success:
            print(f"   ✅ Snapshot creation successful: {result.message}")
            return {
                "success": True,
                "snapshot_content": conversation,
                "memory_token": memory_token,
                "created_at": snapshot_entry["timestamp"]
            }
        else:
            print(f"   ❌ Snapshot creation failed: {result.message if hasattr(result, 'message') else 'Unknown error'}")
            return {"success": False, "error": str(result)}
            
    except Exception as e:
        error_log(f"[MEM Test] Snapshot creation test failed: {e}")
        return {"success": False, "error": str(e)}

def mem_test_memory_query(modules, memory_token="test_user", query_text="weather"):
    """測試記憶查詢功能 - 英文查詢"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ Unable to load MEM module")
        return {"success": False, "error": "Module not loaded"}

    try:
        print("🔍 Testing memory query functionality...")
        
        # 先建立一些英文測試記憶
        test_memories = [
            {
                "content": "Today's weather is sunny and perfect for outdoor activities",
                "memory_type": "snapshot",
                "topic": "weather",
                "importance": "medium"
            },
            {
                "content": "User prefers sunny days for hiking and walking",
                "memory_type": "long_term",
                "topic": "user_preferences",
                "importance": "high"
            },
            {
                "content": "Yesterday it was raining, user stayed indoors reading",
                "memory_type": "episodic",
                "topic": "daily_activities",
                "importance": "low"
            }
        ]
        
        for memory in test_memories:
            store_input = MEMInput(
                operation_type="store_memory",
                memory_token=memory_token,
                memory_entry=memory
            )
            
            store_result = mem.handle(store_input)
            if not (isinstance(store_result, MEMOutput) and store_result.success):
                print(f"   ⚠️ Failed to store test memory: {memory['content'][:30]}...")
        
        print(f"   ✅ Stored {len(test_memories)} test memories")
        
        # 執行查詢
        query_input = MEMInput(
            operation_type="query_memory",
            memory_token=memory_token,
            query_text=query_text,
            max_results=10
        )
        
        result = mem.handle(query_input)
        
        if isinstance(result, MEMOutput) and result.success:
            results_count = len(result.search_results) if hasattr(result, 'search_results') else 0
            print(f"   ✅ Memory query successful, found {results_count} relevant records")
            
            # 顯示查詢結果
            if hasattr(result, 'search_results') and result.search_results:
                for i, search_result in enumerate(result.search_results[:3]):  # 顯示前3個結果
                    content = search_result.get('content', '')[:50] + ('...' if len(search_result.get('content', '')) > 50 else '')
                    confidence = search_result.get('confidence', 0)
                    print(f"   Result {i+1}: {content} (similarity: {confidence:.3f})")
            
            return {
                "success": True,
                "query_text": query_text,
                "results_count": results_count,
                "search_results": result.search_results if hasattr(result, 'search_results') else []
            }
        else:
            print(f"   ❌ Memory query failed: {result.message if hasattr(result, 'message') else 'Unknown error'}")
            return {"success": False, "error": str(result)}
            
    except Exception as e:
        error_log(f"[MEM Test] Memory query test failed: {e}")
        return {"success": False, "error": str(e)}

def mem_test_database_listing(modules, memory_token="test_user"):
    """測試資料庫內容條列功能 - 英文內容"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ Unable to load MEM module")
        return {"success": False, "error": "Module not loaded"}

    try:
        print("📋 Testing database content listing functionality...")
        
        # 先確保有一些測試記憶
        test_memories = [
            {
                "content": "User's favorite color is blue",
                "memory_type": "long_term",
                "topic": "user_preferences",
                "importance": "medium"
            },
            {
                "content": "Meeting scheduled for tomorrow at 2 PM",
                "memory_type": "snapshot",
                "topic": "schedule",
                "importance": "high"
            }
        ]
        
        # 存儲測試記憶
        for memory in test_memories:
            store_input = MEMInput(
                operation_type="store_memory",
                memory_token=memory_token,
                memory_entry=memory
            )
            mem.handle(store_input)
        
        # 查詢所有記憶（透過空白查詢或最大結果數）
        list_input = MEMInput(
            operation_type="query_memory",
            memory_token=memory_token,
            query_text="",  # 空白查詢來取得所有記憶
            max_results=100  # 取得更多結果
        )
        
        result = mem.handle(list_input)
        
        if isinstance(result, MEMOutput) and result.success:
            memories_count = len(result.search_results) if hasattr(result, 'search_results') else 0
            print(f"   ✅ Database listing successful, found {memories_count} total memories")
            
            # 顯示資料庫內容摘要
            if hasattr(result, 'search_results') and result.search_results:
                memory_types = {}
                topics = {}
                
                for memory in result.search_results[:10]:  # 顯示前10條記錄
                    content = memory.get('content', '')[:40] + ('...' if len(memory.get('content', '')) > 40 else '')
                    memory_type = memory.get('memory_type', 'unknown')
                    topic = memory.get('topic', 'unknown')
                    importance = memory.get('importance', 'unknown')
                    
                    print(f"   • {content} (type: {memory_type}, topic: {topic}, importance: {importance})")
                    
                    # 統計
                    memory_types[memory_type] = memory_types.get(memory_type, 0) + 1
                    topics[topic] = topics.get(topic, 0) + 1
                
                print(f"   📊 Memory types distribution: {dict(memory_types)}")
                print(f"   📊 Topics distribution: {dict(topics)}")
            
            return {
                "success": True,
                "total_memories": memories_count,
                "sample_memories": result.search_results[:10] if hasattr(result, 'search_results') else []
            }
        else:
            print(f"   ❌ Database listing failed: {result.message if hasattr(result, 'message') else 'Unknown error'}")
            return {"success": False, "error": str(result)}
            
    except Exception as e:
        error_log(f"[MEM Test] Database listing test failed: {e}")
        return {"success": False, "error": str(e)}

def mem_test_full_workflow(modules):
    """測試核心MEM功能 - 專注於基本記憶操作"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ Unable to load MEM module")
        return {"success": False, "error": "Module not loaded"}

    try:
        print("🚀 Testing core MEM functionality workflow...")
        
        memory_token = f"test_core_workflow_{int(datetime.now().timestamp())}"
        
        # 1. 記憶建立
        print("   1. Memory creation...")
        creation_result = mem_test_memory_creation(modules, memory_token)
        if not creation_result.get("success"):
            return {"success": False, "error": "Memory creation failed"}
        
        # 2. 記憶查詢
        print("   2. Memory query...")
        query_result = mem_test_memory_query(modules, memory_token, "weather")
        if not query_result.get("success"):
            return {"success": False, "error": "Memory query failed"}
        
        # 3. 快照建立
        print("   3. Snapshot creation...")
        snapshot_result = mem_test_snapshot_creation(modules, memory_token, "Core functionality test session")
        if not snapshot_result.get("success"):
            return {"success": False, "error": "Snapshot creation failed"}
        
        # 4. 資料庫內容條列
        print("   4. Database content listing...")
        listing_result = mem_test_database_listing(modules, memory_token)
        if not listing_result.get("success"):
            return {"success": False, "error": "Database listing failed"}
        
        print("   🎉 Core MEM functionality test completed successfully!")
        
        return {
            "success": True,
            "memory_token": memory_token,
            "creation_test": creation_result,
            "query_test": query_result,
            "snapshot_test": snapshot_result,
            "listing_test": listing_result
        }
        
    except Exception as e:
        error_log(f"[MEM Test] Core functionality test failed: {e}")
        return {"success": False, "error": str(e)}