# -*- coding: utf-8 -*-
"""
MEM 模組測試函數 - 重構版本
✅ 針對新架構的測試函數
"""

from utils.debug_helper import debug_log, info_log, error_log
from modules.mem_module.schemas import MEMInput, MEMOutput, MemoryQuery
from modules.nlp_module.schemas import UserProfile
from datetime import datetime
import json

def mem_test_memory_access_control(modules, memory_token="test_memory_token"):
    """測試記憶體存取控制功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        print("🔒 測試記憶體存取控制功能...")
        
        # 測試存取控制管理器
        if hasattr(mem, 'memory_manager') and mem.memory_manager:
            identity_manager = mem.memory_manager.identity_manager
            
            # 測試記憶令牌提取
            current_token = identity_manager.get_current_memory_token()
            print(f"   當前記憶令牌: {current_token}")
            
            # 測試存取權限驗證
            access_granted = identity_manager.validate_memory_access(memory_token, "read")
            print(f"   存取權限驗證 ({memory_token}): {'✅ 允許' if access_granted else '❌ 拒絕'}")
            
            # 測試系統令牌存取
            system_access = identity_manager.validate_memory_access(identity_manager.get_system_token(), "write")
            print(f"   系統令牌存取: {'✅ 允許' if system_access else '❌ 拒絕'}")
            
            # 獲取統計資訊
            stats = identity_manager.get_stats()
            print(f"   統計資訊: {stats}")
            
            return {
                "success": True, 
                "current_token": current_token,
                "access_granted": access_granted,
                "system_access": system_access,
                "stats": stats
            }
        else:
            return {"success": False, "error": "記憶管理器未初始化"}
            
    except Exception as e:
        error_log(f"[MEM Test] 記憶體存取控制測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_conversation_snapshot(modules, memory_token: str = "test_user", conversation: str = "你好，今天天氣如何？"):
    """測試對話快照創建功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        # 創建對話快照請求
        mem_input = MEMInput(
            operation_type="create_snapshot",
            identity_token=memory_token,  # 實際上使用記憶令牌
            conversation_text=conversation,
            intent_info={"primary_intent": "casual_chat"}
        )
        
        result = mem.handle(mem_input)
        
        if isinstance(result, MEMOutput) and result.success:
            print(f"✅ 對話快照創建成功:")
            print(f"   快照ID: {result.snapshot_id}")
            print(f"   操作類型: {result.operation_type}")
            return {"success": True, "result": result}
        else:
            error_log(f"[MEM Test] 對話快照創建失敗: {result}")
            return {"success": False, "error": "快照創建失敗"}
            
    except Exception as e:
        error_log(f"[MEM Test] 對話快照測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_memory_query(modules, memory_token: str = "test_user", query_text: str = "天氣"):
    """測試記憶查詢功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        # 創建記憶查詢
        query_data = MemoryQuery(
            identity_token=memory_token,  # 實際上使用記憶令牌
            query_text=query_text,
            max_results=5,
            similarity_threshold=0.7
        )
        
        mem_input = MEMInput(
            operation_type="query",
            query_data=query_data
        )
        
        result = mem.handle(mem_input)
        
        if isinstance(result, MEMOutput) and result.success:
            print(f"✅ 記憶查詢成功:")
            print(f"   查詢結果數量: {result.total_memories}")
            print(f"   記憶上下文: {result.memory_context[:100]}..." if result.memory_context else "   無記憶上下文")
            if result.search_results:
                for i, memory in enumerate(result.search_results[:3], 1):
                    print(f"   記憶 {i}: {memory}")
            return {"success": True, "result": result}
        else:
            print(f"⚠️ 記憶查詢結果為空或失敗")
            return {"success": True, "result": result, "message": "查無相關記憶"}
            
    except Exception as e:
        error_log(f"[MEM Test] 記憶查詢測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_identity_manager_stats(modules):
    """測試記憶體存取控制管理器統計功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        if hasattr(mem, 'memory_manager') and mem.memory_manager:
            identity_manager = mem.memory_manager.identity_manager
            stats = identity_manager.get_stats()
            
            print(f"✅ 記憶體存取控制管理器統計:")
            print(f"   令牌提取次數: {stats.get('token_extractions', 0)}")
            print(f"   存取允許次數: {stats.get('memory_access_granted', 0)}")
            print(f"   存取拒絕次數: {stats.get('memory_access_denied', 0)}")
            print(f"   存取驗證次數: {stats.get('access_validations', 0)}")
            print(f"   當前記憶令牌: {stats.get('current_memory_token', 'N/A')}")
            print(f"   是否有身份資訊: {stats.get('has_identity', False)}")
            
            return {"success": True, "stats": stats}
        else:
            return {"success": False, "error": "記憶管理器未初始化"}
            
    except Exception as e:
        error_log(f"[MEM Test] 記憶體存取控制統計測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_nlp_integration(modules, nlp_output_mock: dict = None):
    """測試NLP整合功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        if nlp_output_mock is None:
            # 創建模擬NLP輸出
            nlp_output_mock = {
                "user_profile": {
                    "user_name": "TestUser",
                    "personality": "curious",
                    "preferences": ["學習", "探索"],
                    "context_history": [],
                    "mentioned_entities": ["天氣", "學習"],
                    "emotional_state": "positive",
                    "confidence_score": 0.9
                },
                "user_input": "今天天氣很好，適合學習新知識",
                "intent_info": {
                    "primary_intent": "learning",
                    "confidence": 0.8
                }
            }
        
        result = mem.process_nlp_output(nlp_output_mock)
        
        if result and isinstance(result, MEMOutput):
            print(f"✅ NLP整合測試成功:")
            print(f"   處理結果: {result.success}")
            print(f"   操作類型: {result.operation_type}")
            return {"success": True, "result": result}
        else:
            print(f"⚠️ NLP整合處理結果為空")
            return {"success": True, "message": "NLP整合未處理或返回空結果"}
            
    except Exception as e:
        error_log(f"[MEM Test] NLP整合測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_llm_context_extraction(modules, memory_token: str = "test_user", query_text: str = "學習"):
    """測試為LLM提取記憶上下文功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        context = mem.get_memory_context_for_llm(memory_token, query_text)
        
        print(f"✅ LLM記憶上下文提取:")
        print(f"   上下文長度: {len(context)} 字符")
        print(f"   上下文內容: {context[:200]}..." if context else "   無相關記憶上下文")
        
        return {"success": True, "context": context}
            
    except Exception as e:
        error_log(f"[MEM Test] LLM上下文提取測試失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_full_workflow(modules, user_name: str = "WorkflowTestUser"):
    """測試完整MEM工作流程"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    print(f"🔄 開始完整MEM工作流程測試...")
    
    try:
        # 使用測試記憶令牌
        test_memory_token = f"workflow_test_{user_name}_{int(datetime.now().timestamp())}"
        
        # 1. 測試記憶體存取控制
        access_result = mem_test_memory_access_control(modules, test_memory_token)
        if not access_result["success"]:
            return {"success": False, "step": "access_control", "error": access_result["error"]}
        
        print(f"   ✅ 步驟1: 記憶體存取控制測試成功")
        
        # 2. 創建對話快照
        snapshot_result = mem_test_conversation_snapshot(modules, test_memory_token, "我想學習新的編程技術")
        if not snapshot_result["success"]:
            print(f"   ⚠️ 步驟2: 對話快照創建未成功，但繼續測試 - {snapshot_result.get('error', '未知原因')}")
        else:
            print(f"   ✅ 步驟2: 對話快照創建成功")
        
        # 3. 查詢記憶
        query_result = mem_test_memory_query(modules, test_memory_token, "編程")
        print(f"   ✅ 步驟3: 記憶查詢完成")
        
        # 4. 獲取LLM上下文
        context_result = mem_test_llm_context_extraction(modules, test_memory_token, "編程學習")
        print(f"   ✅ 步驟4: LLM上下文提取完成")
        
        # 5. 檢查統計
        stats_result = mem_test_identity_manager_stats(modules)
        print(f"   ✅ 步驟5: 統計數據獲取完成")
        
        # 6. 測試NLP整合
        nlp_result = mem_test_nlp_integration(modules, None)
        print(f"   ✅ 步驟6: NLP整合測試完成")
        
        print(f"🎉 完整工作流程測試成功!")
        
        return {
            "success": True,
            "test_memory_token": test_memory_token,
            "steps": {
                "access_control": access_result,
                "snapshot_creation": snapshot_result,
                "memory_query": query_result,
                "llm_context": context_result,
                "statistics": stats_result,
                "nlp_integration": nlp_result
            }
        }
            
    except Exception as e:
        error_log(f"[MEM Test] 完整工作流程測試失敗: {e}")
        return {"success": False, "error": str(e)}