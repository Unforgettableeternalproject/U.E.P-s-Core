# -*- coding: utf-8 -*-
"""
MEM 模組測試函數 - 重構版本（工作流程集成測試）
✅ 針對新架構的測試函數，基於完整工作流程
"""

from utils.debug_helper import debug_log, info_log, error_log
from modules.mem_module.schemas import MEMInput, MEMOutput, MemoryQuery
from modules.nlp_module.schemas import UserProfile
from datetime import datetime
import json

def mem_test_memory_access_control(modules, memory_token="test_memory_token"):
    """測試記憶體存取控制功能 - 工作流程集成版本"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        print("🔒 測試記憶體存取控制功能（工作流程集成）...")
        
        # 使用新的 MEMInput 格式進行測試
        mem_input = MEMInput(
            operation_type="validate_token",
            memory_token=memory_token
        )
        
        result = mem.handle(mem_input)
        
        if isinstance(result, MEMOutput) and result.success:
            print(f"   ✅ 記憶令牌驗證成功: {result.message}")
            return {
                "success": True,
                "message": result.message,
                "memory_token": memory_token,
                "operation_result": result.data
            }
        else:
            print(f"   ❌ 記憶令牌驗證失敗: {result.message if hasattr(result, 'message') else '未知錯誤'}")
            return {"success": False, "error": str(result)}
            
    except Exception as e:
        error_log(f"[MEM Test] 記憶體存取控制測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_conversation_snapshot(modules, memory_token="test_user", conversation="你好，今天天氣如何？"):
    """測試對話快照功能 - 工作流程集成版本"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        print("📸 測試對話快照功能（工作流程集成）...")
        
        # 創建對話快照
        mem_input = MEMInput(
            operation_type="create_snapshot",
            memory_token=memory_token,
            conversation_text=conversation,
            intent_info={
                "primary_intent": "casual_conversation",
                "topic": "天氣詢問"
            }
        )
        
        result = mem.handle(mem_input)
        
        if isinstance(result, MEMOutput) and result.success:
            print(f"   ✅ 對話快照創建成功: {result.message}")
            
            # 嘗試查詢剛創建的快照
            query_input = MEMInput(
                operation_type="query_memory",
                memory_token=memory_token,
                query_text="天氣",
                memory_types=["snapshot"]
            )
            
            query_result = mem.handle(query_input)
            
            if isinstance(query_result, MEMOutput) and query_result.success:
                results_count = len(query_result.search_results) if hasattr(query_result, 'search_results') else 0
                print(f"   ✅ 快照查詢成功，找到 {results_count} 條記錄")
                
                return {
                    "success": True,
                    "snapshot_created": True,
                    "query_results": results_count,
                    "conversation": conversation
                }
            else:
                print(f"   ⚠️ 快照創建成功但查詢失敗: {query_result.message if hasattr(query_result, 'message') else '未知錯誤'}")
                return {
                    "success": True,
                    "snapshot_created": True,
                    "query_results": 0,
                    "conversation": conversation
                }
        else:
            print(f"   ❌ 對話快照創建失敗: {result.message if hasattr(result, 'message') else '未知錯誤'}")
            return {"success": False, "error": str(result)}
            
    except Exception as e:
        error_log(f"[MEM Test] 對話快照測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_memory_query(modules, memory_token="test_user", query_text="天氣"):
    """測試記憶查詢功能 - 工作流程集成版本"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        print("🔍 測試記憶查詢功能（工作流程集成）...")
        
        # 先存儲一些測試記憶
        test_memories = [
            {
                "content": "今天天氣很好，適合外出",
                "memory_type": "snapshot",
                "topic": "天氣",
                "importance": "medium"
            },
            {
                "content": "用戶喜歡在晴天進行戶外活動",
                "memory_type": "long_term",
                "topic": "用戶偏好",
                "importance": "high"
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
                print(f"   ⚠️ 測試記憶存儲失敗: {memory['content'][:20]}...")
        
        print(f"   ✅ 存儲了 {len(test_memories)} 條測試記憶")
        
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
            print(f"   ✅ 記憶查詢成功，找到 {results_count} 條相關記錄")
            
            # 顯示查詢結果
            if hasattr(result, 'search_results') and result.search_results:
                for i, search_result in enumerate(result.search_results[:3]):  # 顯示前3個結果
                    content = search_result.get('content', '')[:50] + ('...' if len(search_result.get('content', '')) > 50 else '')
                    confidence = search_result.get('confidence', 0)
                    print(f"   結果 {i+1}: {content} (相似度: {confidence:.3f})")
            
            return {
                "success": True,
                "query_text": query_text,
                "results_count": results_count,
                "search_results": result.search_results if hasattr(result, 'search_results') else []
            }
        else:
            print(f"   ❌ 記憶查詢失敗: {result.message if hasattr(result, 'message') else '未知錯誤'}")
            return {"success": False, "error": str(result)}
            
    except Exception as e:
        error_log(f"[MEM Test] 記憶查詢測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_identity_manager_stats(modules):
    """測試身份管理器統計功能 - 工作流程集成版本"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        print("📊 測試身份管理器統計功能...")
        
        # 檢查模組架構
        if hasattr(mem, 'memory_manager') and mem.memory_manager:
            if hasattr(mem.memory_manager, 'identity_manager'):
                identity_manager = mem.memory_manager.identity_manager
                
                # 獲取統計資訊
                stats = identity_manager.get_stats()
                print(f"   ✅ 身份管理器統計: {stats}")
                
                # 獲取當前記憶令牌
                current_token = identity_manager.get_current_memory_token()
                print(f"   當前記憶令牌: {current_token}")
                
                return {
                    "success": True,
                    "stats": stats,
                    "current_token": current_token
                }
            else:
                print("   ⚠️ 找不到身份管理器")
                return {"success": False, "error": "身份管理器未找到"}
        else:
            print("   ⚠️ 找不到記憶管理器")
            return {"success": False, "error": "記憶管理器未找到"}
            
    except Exception as e:
        error_log(f"[MEM Test] 身份管理器統計測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_nlp_integration(modules):
    """測試NLP整合功能 - 工作流程集成版本"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        print("🤝 測試NLP整合功能（工作流程集成）...")
        
        memory_token = f"test_nlp_integration_{int(datetime.now().timestamp())}"
        
        # 模擬NLP輸出
        nlp_output = {
            "intent_analysis": {
                "primary_intent": "learning_request",
                "confidence": 0.9,
                "entities": ["Python", "機器學習"],
                "sentiment": "positive"
            },
            "conversation_context": {
                "topic": "技術學習",
                "context_shift": False,
                "urgency": "normal"
            }
        }
        
        # 處理NLP輸出
        mem_input = MEMInput(
            operation_type="process_nlp_output",
            memory_token=memory_token,
            intent_info=nlp_output,
            conversation_text="我想學習Python和機器學習"
        )
        
        result = mem.handle(mem_input)
        
        if isinstance(result, MEMOutput) and result.success:
            print(f"   ✅ NLP整合測試成功: {result.message}")
            return {
                "success": True,
                "nlp_output_processed": True,
                "memory_token": memory_token,
                "result_data": result.data
            }
        else:
            print(f"   ❌ NLP整合測試失敗: {result.message if hasattr(result, 'message') else '未知錯誤'}")
            return {"success": False, "error": str(result)}
            
    except Exception as e:
        error_log(f"[MEM Test] NLP整合測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_llm_context_extraction(modules, memory_token="test_llm"):
    """測試LLM上下文提取功能 - 工作流程集成版本"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        print("🧠 測試LLM上下文提取功能（工作流程集成）...")
        
        # 生成LLM記憶指令
        mem_input = MEMInput(
            operation_type="generate_llm_instruction",
            memory_token=memory_token,
            query_text="如何幫助用戶學習程式設計",
            conversation_context="用戶正在尋求學習建議"
        )
        
        result = mem.handle(mem_input)
        
        if isinstance(result, MEMOutput) and result.success:
            print(f"   ✅ LLM上下文提取成功: {result.message}")
            
            # 檢查是否有LLM指令
            if hasattr(result, 'llm_instruction') and result.llm_instruction:
                print(f"   LLM指令已生成，類型: {type(result.llm_instruction)}")
                return {
                    "success": True,
                    "llm_instruction_generated": True,
                    "instruction_type": str(type(result.llm_instruction))
                }
            else:
                print(f"   ⚠️ LLM指令生成成功但無指令內容")
                return {
                    "success": True,
                    "llm_instruction_generated": False
                }
        else:
            print(f"   ❌ LLM上下文提取失敗: {result.message if hasattr(result, 'message') else '未知錯誤'}")
            return {"success": False, "error": str(result)}
            
    except Exception as e:
        error_log(f"[MEM Test] LLM上下文提取測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_full_workflow(modules):
    """測試完整工作流程 - 整合所有功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        print("🚀 測試完整MEM工作流程...")
        
        memory_token = f"test_full_workflow_{int(datetime.now().timestamp())}"
        
        # 1. 身分驗證
        print("   1. 身分驗證...")
        identity_result = mem_test_memory_access_control(modules, memory_token)
        if not identity_result.get("success"):
            return {"success": False, "error": "身分驗證失敗"}
        
        # 2. NLP整合
        print("   2. NLP整合...")
        nlp_result = mem_test_nlp_integration(modules)
        if not nlp_result.get("success"):
            return {"success": False, "error": "NLP整合失敗"}
        
        # 3. 對話快照
        print("   3. 對話快照...")
        snapshot_result = mem_test_conversation_snapshot(modules, memory_token, "這是完整工作流程測試")
        if not snapshot_result.get("success"):
            return {"success": False, "error": "對話快照失敗"}
        
        # 4. 記憶查詢
        print("   4. 記憶查詢...")
        query_result = mem_test_memory_query(modules, memory_token, "測試")
        if not query_result.get("success"):
            return {"success": False, "error": "記憶查詢失敗"}
        
        # 5. LLM上下文提取
        print("   5. LLM上下文提取...")
        llm_result = mem_test_llm_context_extraction(modules, memory_token)
        if not llm_result.get("success"):
            return {"success": False, "error": "LLM上下文提取失敗"}
        
        print("   🎉 完整工作流程測試成功！")
        
        return {
            "success": True,
            "memory_token": memory_token,
            "identity_test": identity_result,
            "nlp_integration": nlp_result,
            "snapshot_test": snapshot_result,
            "query_test": query_result,
            "llm_context": llm_result
        }
        
    except Exception as e:
        error_log(f"[MEM Test] 完整工作流程測試失敗: {e}")
        return {"success": False, "error": str(e)}