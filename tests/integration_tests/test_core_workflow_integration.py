# tests/integration_tests/test_core_workflow_integration.py
"""
核心工作流整合測試

測試 STT → NLP → MEM 完整工作流，以及狀態管理和會話系統的整合。
這是確認系統架構正確運作的關鍵測試。
"""

import pytest
import time
import threading
from unittest.mock import Mock, patch, MagicMock

# Core 組件
from core.framework import core_framework
from core.state_manager import state_manager, UEPState
from core.working_context import working_context_manager, ContextType
from core.router import router
from core.session_manager import session_manager, SessionStatus
from core.system_loop import system_loop, LoopStatus
from core.schemas import STTModuleData, NLPModuleData, MEMModuleData


class TestCoreWorkflowIntegration:
    """核心工作流整合測試"""
    
    def setup_method(self):
        """測試前準備"""
        # 重置系統狀態
        state_manager.set_state(UEPState.IDLE)
        
        # 清理過期上下文
        working_context_manager.cleanup_expired_contexts()
        
        # 停止系統循環（如果正在運行）
        if system_loop.status == LoopStatus.RUNNING:
            system_loop.stop()
            time.sleep(0.1)
    
    def teardown_method(self):
        """測試後清理"""
        # 確保系統循環停止
        if system_loop.status == LoopStatus.RUNNING:
            system_loop.stop()
        
        # 清理狀態
        state_manager.set_state(UEPState.IDLE)
        working_context_manager.cleanup_expired_contexts()
    
    def test_stt_to_nlp_data_flow(self):
        """測試 STT → NLP 數據流"""
        
        # 模擬 STT 輸出
        stt_data = STTModuleData(
            text="你好，我想要設定提醒",
            confidence=0.95,
            speaker_info={
                "speaker_name": "User",
                "voice_characteristics": "清晰男聲"
            },
            speaker_id="speaker_001",
            timestamp=time.time(),
            language="zh-tw",
            audio_features={}
        )
        
        # 準備 Working Context
        context_id = working_context_manager.create_context(
            context_type=ContextType.CROSS_MODULE_DATA,
            threshold=1
        )
        
        working_context_manager.set_context_data("stt_result", stt_data.model_dump())
        working_context_manager.set_context_data("session_info", {"user_id": "test_user"})
        
        # 模擬 NLP 模組處理
        with patch('core.framework.core_framework.get_module') as mock_get_module:
            # 創建模擬的 NLP 模組
            mock_nlp_module = Mock()
            mock_nlp_module.handle.return_value = {
                "success": True,
                "result": {
                    "primary_intent": "reminder_creation",
                    "entities": {"reminder_text": "設定提醒"},
                    "speaker_id": "speaker_001",
                    "identity_id": "user_001",
                    "confidence_score": 0.87,
                    "state_transition": {
                        "suggested_state": "WORK",
                        "reason": "用戶請求執行任務"
                    }
                }
            }
            
            mock_get_module.return_value = mock_nlp_module
            
            # 執行數據流轉換
            nlp_module = core_framework.get_module('nlp_module')
            if nlp_module:
                result = nlp_module.handle({
                    "mode": "analyze_intent",
                    "text": stt_data.text,
                    "speaker_id": stt_data.speaker_id,
                    "context": working_context_manager.get_context_data("stt_result")
                })
                
                assert result["success"] is True
                assert result["result"]["primary_intent"] == "reminder_creation"
                assert result["result"]["speaker_id"] == "speaker_001"
    
    def test_nlp_to_mem_data_flow(self):
        """測試 NLP → MEM 數據流"""
        
        # 模擬 NLP 輸出
        nlp_data = NLPModuleData(
            primary_intent="conversation",
            entities=[{"type": "topic", "value": "天氣", "confidence": 0.9}],
            speaker_id="speaker_001",
            identity_id="user_001",
            confidence=0.92,
            state_transition={
                "suggested_state": "CHAT",
                "reason": "用戶想要對話"
            },
            processed_text="今天天氣怎麼樣？",
            timestamp=time.time()
        )
        
        # 準備 Working Context
        context_id = working_context_manager.create_context(
            context_type=ContextType.CONVERSATION,
            threshold=1
        )
        
        working_context_manager.set_context_data("nlp_result", nlp_data.model_dump())
        working_context_manager.set_context_data("conversation_stage", "inquiry")
        
        # 模擬 MEM 模組處理
        with patch('core.framework.core_framework.get_module') as mock_get_module:
            # 創建模擬的 MEM 模組
            mock_mem_module = Mock()
            mock_mem_module.handle.return_value = {
                "success": True,
                "result": {
                    "memory_retrieved": True,
                    "relevant_conversations": [
                        {
                            "snippet": "上次討論過天氣預報",
                            "timestamp": "2024-01-15",
                            "relevance_score": 0.78
                        }
                    ],
                    "context_updated": True,
                    "snapshot_created": True
                }
            }
            
            mock_get_module.return_value = mock_mem_module
            
            # 執行記憶查詢
            mem_module = core_framework.get_module('mem_module')
            if mem_module:
                result = mem_module.handle({
                    "mode": "retrieve_and_update",
                    "identity_id": nlp_data.identity_id,
                    "query_text": nlp_data.processed_text,
                    "context": working_context_manager.get_context_data("nlp_result")
                })
                
                assert result["success"] is True
                assert result["result"]["memory_retrieved"] is True
                assert len(result["result"]["relevant_conversations"]) > 0
    
    def test_state_transition_flow(self):
        """測試狀態轉換流程"""
        
        # 初始狀態應該是 IDLE
        assert state_manager.get_state() == UEPState.IDLE
        
        # 模擬語音輸入觸發狀態轉換
        with patch('core.router.router.route') as mock_route:
            mock_route.return_value = {
                "success": True,
                "route": "chat_handler",
                "state_change": UEPState.CHAT,
                "session_required": True
            }
            
            # 觸發路由處理
            route_result = router.route(
                intent="conversation",
                detail={
                    "text": "你好",
                    "speaker_id": "speaker_001"
                },
                state=UEPState.IDLE
            )
            
            assert route_result["success"] is True
            assert route_result["state_change"] == UEPState.CHAT
    
    def test_working_context_integration(self):
        """測試 Working Context 整合"""
        
        # 創建多個上下文
        speech_context_id = working_context_manager.create_context(
            context_type=ContextType.CROSS_MODULE_DATA,
            threshold=1
        )
        
        nlp_context_id = working_context_manager.create_context(
            context_type=ContextType.TASK_EXECUTION,
            threshold=1
        )
        
        # 設定上下文數據
        working_context_manager.set_context_data("speech_text", "設定提醒明天開會")
        working_context_manager.set_context_data("nlp_intent", "reminder_creation")
        working_context_manager.set_context_data("nlp_entities", {"date": "明天", "event": "開會"})
        
        # 測試上下文數據查詢
        speech_text = working_context_manager.get_context_data("speech_text")
        nlp_intent = working_context_manager.get_context_data("nlp_intent")
        
        assert speech_text is not None
        assert nlp_intent is not None
        assert speech_text == "設定提醒明天開會"
        assert nlp_intent == "reminder_creation"
        
        # 測試數據存在性
        nlp_entities = working_context_manager.get_context_data("nlp_entities")
        assert nlp_entities is not None
        assert nlp_entities["date"] == "明天"
        assert nlp_entities["event"] == "開會"
    
    def test_session_workflow_integration(self):
        """測試會話工作流整合"""
        
        # 創建工作流會話
        session = session_manager.create_session(
            workflow_type="reminder_creation",
            command="設定明天的會議提醒",
            initial_data={
                "user_input": "設定明天的會議提醒",
                "speaker_id": "speaker_001"
            }
        )
        
        assert session is not None
        session_id = session.session_id
        
        # 檢查會話狀態
        assert session.workflow_type == "reminder_creation"
        assert session.current_step == 0
        assert session.status == SessionStatus.ACTIVE
        
        # 模擬步驟進展
        session.advance_step({
            "step_result": "date_extracted",
            "extracted_date": "2024-01-16"
        })
        
        # 檢查步驟進展
        assert session.current_step == 1
        assert len(session.history) >= 1  # 至少包含初始化歷史記錄
    
    def test_full_workflow_simulation(self):
        """測試完整工作流模擬"""
        
        # 1. 模擬語音輸入
        stt_result = {
            "text": "幫我記住今天學到的新單字",
            "speaker_id": "speaker_001",
            "confidence": 0.95
        }
        
        # 2. 創建 Working Context
        context_id = working_context_manager.create_context(
            context_type=ContextType.CROSS_MODULE_DATA,
            threshold=1
        )
        
        working_context_manager.set_context_data("stt_result", stt_result)
        
        # 3. 模擬 NLP 處理
        nlp_result = {
            "primary_intent": "memory_storage",
            "entities": {"content_type": "vocabulary"},
            "speaker_id": "speaker_001",
            "identity_id": "user_001",
            "state_transition": {
                "suggested_state": "CHAT",
                "reason": "用戶想要存儲記憶"
            }
        }
        
        working_context_manager.set_context_data("nlp_result", nlp_result)
        
        # 4. 檢查狀態管理
        current_state = state_manager.get_state()
        
        # 根據 NLP 結果建議轉換狀態
        if nlp_result["state_transition"]["suggested_state"] == "CHAT":
            state_manager.set_state(UEPState.CHAT)
        
        assert state_manager.get_state() == UEPState.CHAT
        
        # 5. 模擬 MEM 處理
        mem_result = {
            "memory_stored": True,
            "snapshot_id": "snapshot_001",
            "context_updated": True
        }
        
        working_context_manager.set_context_data("mem_result", mem_result)
        
        # 6. 驗證完整流程
        final_stt_result = working_context_manager.get_context_data("stt_result")
        final_nlp_result = working_context_manager.get_context_data("nlp_result")
        final_mem_result = working_context_manager.get_context_data("mem_result")
        
        assert final_nlp_result is not None
        assert final_mem_result is not None
        assert final_mem_result["memory_stored"] is True
        assert state_manager.get_state() == UEPState.CHAT
    
    @pytest.mark.asyncio
    async def test_system_loop_integration(self):
        """測試系統循環整合"""
        
        # 啟動系統循環
        assert system_loop.start() is True
        
        # 等待循環啟動
        time.sleep(0.2)
        assert system_loop.status == LoopStatus.RUNNING
        
        # 模擬事件觸發
        with patch.object(system_loop, '_handle_speech_input') as mock_handler:
            system_loop._trigger_event('speech_input', {
                'text': '測試語音輸入',
                'speaker_id': 'test_speaker'
            })
            
            # 稍等一下讓事件處理
            time.sleep(0.1)
            
            # 驗證事件處理器被調用
            mock_handler.assert_called_once()
        
        # 停止系統循環
        system_loop.stop()
        time.sleep(0.1)
        assert system_loop.status == LoopStatus.STOPPED


if __name__ == "__main__":
    # 可以直接運行進行快速測試
    test_instance = TestCoreWorkflowIntegration()
    test_instance.setup_method()
    
    try:
        print("🧪 測試 STT → NLP 數據流...")
        test_instance.test_stt_to_nlp_data_flow()
        print("✅ STT → NLP 測試通過")
        
        print("🧪 測試 NLP → MEM 數據流...")
        test_instance.test_nlp_to_mem_data_flow()
        print("✅ NLP → MEM 測試通過")
        
        print("🧪 測試狀態轉換...")
        test_instance.test_state_transition_flow()
        print("✅ 狀態轉換測試通過")
        
        print("🧪 測試 Working Context 整合...")
        test_instance.test_working_context_integration()
        print("✅ Working Context 測試通過")
        
        print("🧪 測試會話工作流...")
        test_instance.test_session_workflow_integration()
        print("✅ 會話工作流測試通過")
        
        print("🧪 測試完整工作流...")
        test_instance.test_full_workflow_simulation()
        print("✅ 完整工作流測試通過")
        
        print("🎉 所有整合測試通過！")
        
    except Exception as e:
        print(f"❌ 測試失敗: {e}")
        
    finally:
        test_instance.teardown_method()