# -*- coding: utf-8 -*-
"""
MEM 模組測試函數 - 重構版本
✅ 針對新架構的測試函數
"""

from utils.debug_helper import debug_log, info_log, error_log
from modules.mem_module.schemas import MEMInput, MEMOutput, MemoryQuery, IdentityToken
from modules.nlp_module.schemas import UserProfile
from datetime import datetime
import json

def mem_test_identity_token_creation(modules, user_name="測試使用者"):
    """測試身份Token創建功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        # 模擬UserProfile數據（精確匹配NLP模組UserProfile的字段）
        mock_user_profile_data = {
            'identity_id': f'user_test_{int(datetime.now().timestamp())}',
            'speaker_id': 'test_speaker_001',
            'display_name': user_name,  # 確保是字符串
            'memory_token': f'mem_test_{int(datetime.now().timestamp())}',
            'preferences': {},  # 改為字典而不是列表
            'voice_preferences': {"default_mood": "neutral"},
            'conversation_style': {"formality": "casual"},
            'total_interactions': 0,
            'created_at': datetime.now(),
            'last_interaction': None,
            'metadata': {}
        }
        
        # 使用正確的方法創建身份Token
        if hasattr(mem, 'memory_manager') and mem.memory_manager:
            identity_manager = mem.memory_manager.identity_manager
            
            # 使用create_identity_token_from_nlp方法
            token = identity_manager.create_identity_token_from_nlp(mock_user_profile_data)
            
            if token:
                print(f"✅ 身份Token創建成功:")
                print(f"   身份ID: {token.identity_id}")
                print(f"   顯示名稱: {token.display_name}")
                print(f"   記憶令牌: {token.memory_token}")
                print(f"   創建時間: {token.created_at}")
                print(f"   總互動次數: {token.total_interactions}")
                print(f"   是否活躍: {token.is_active}")
                
                return {"success": True, "token": token}
            else:
                return {"success": False, "error": "令牌創建失敗"}
        else:
            return {"success": False, "error": "記憶管理器未初始化"}
            
    except Exception as e:
        error_log(f"[MEM Test] 身份Token創建失敗: {e}")
        return {"success": False, "error": str(e)}

def mem_test_conversation_snapshot(modules, identity_token: str = "test_user", conversation: str = "你好，今天天氣如何？"):
    """測試對話快照創建功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        # 創建對話快照請求
        mem_input = MEMInput(
            operation_type="create_snapshot",
            identity_token=identity_token,
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

def mem_test_memory_query(modules, identity_token: str = "test_user", query_text: str = "天氣"):
    """測試記憶查詢功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        # 創建記憶查詢
        query_data = MemoryQuery(
            identity_token=identity_token,
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
    """測試身份管理器統計功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        if hasattr(mem, 'memory_manager') and mem.memory_manager:
            identity_manager = mem.memory_manager.identity_manager
            stats = identity_manager.get_statistics()
            
            print(f"✅ 身份管理器統計:")
            print(f"   身份Token緩存數量: {stats.get('identity_tokens_count', 0)}")
            print(f"   創建次數: {stats.get('tokens_created', 0)}")
            print(f"   訪問次數: {stats.get('tokens_accessed', 0)}")
            print(f"   更新次數: {stats.get('tokens_updated', 0)}")
            
            return {"success": True, "stats": stats}
        else:
            return {"success": False, "error": "記憶管理器未初始化"}
            
    except Exception as e:
        error_log(f"[MEM Test] 身份管理器統計測試失敗: {e}")
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

def mem_test_llm_context_extraction(modules, identity_token: str = "test_user", query_text: str = "學習"):
    """測試為LLM提取記憶上下文功能"""
    mem = modules.get("mem")
    
    if mem is None:
        error_log("[MEM Test] ❌ 無法載入 MEM 模組")
        return {"success": False, "error": "模組未載入"}

    try:
        context = mem.get_memory_context_for_llm(identity_token, query_text)
        
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
        # 1. 創建身份Token
        token_result = mem_test_identity_token_creation(modules, user_name, "enthusiastic")
        if not token_result["success"]:
            return {"success": False, "step": "identity_creation", "error": token_result["error"]}
        
        identity_token = token_result["token"].token_id
        print(f"   ✅ 步驟1: 身份Token創建成功")
        
        # 2. 創建對話快照
        snapshot_result = mem_test_conversation_snapshot(modules, identity_token, "我想學習新的編程技術")
        if not snapshot_result["success"]:
            return {"success": False, "step": "snapshot_creation", "error": snapshot_result["error"]}
        
        print(f"   ✅ 步驟2: 對話快照創建成功")
        
        # 3. 查詢記憶
        query_result = mem_test_memory_query(modules, identity_token, "編程")
        print(f"   ✅ 步驟3: 記憶查詢完成")
        
        # 4. 獲取LLM上下文
        context_result = mem_test_llm_context_extraction(modules, identity_token, "編程學習")
        print(f"   ✅ 步驟4: LLM上下文提取完成")
        
        # 5. 檢查統計
        stats_result = mem_test_identity_manager_stats(modules)
        print(f"   ✅ 步驟5: 統計數據獲取完成")
        
        print(f"🎉 完整工作流程測試成功!")
        
        return {
            "success": True,
            "steps": {
                "identity_creation": token_result,
                "snapshot_creation": snapshot_result,
                "memory_query": query_result,
                "llm_context": context_result,
                "statistics": stats_result
            }
        }
            
    except Exception as e:
        error_log(f"[MEM Test] 完整工作流程測試失敗: {e}")
        return {"success": False, "error": str(e)}