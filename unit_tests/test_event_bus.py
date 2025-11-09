# -*- coding: utf-8 -*-
"""
事件總線單元測試

測試目標：
1. 事件發布和訂閱機制
2. 同步/異步處理
3. 事件歷史記錄
4. 單一來源原則檢查
5. 事件處理順序
"""

import pytest
import time
from unittest.mock import Mock

from core.event_bus import EventBus, SystemEvent, Event


@pytest.mark.event
@pytest.mark.critical
class TestEventBusBasic:
    """事件總線基礎功能測試"""
    
    def test_event_bus_initialization(self, event_bus):
        """測試事件總線初始化"""
        assert event_bus is not None
        assert event_bus._handlers == {}
        assert event_bus._event_history == []
        assert event_bus._stats["total_published"] == 0
    
    def test_subscribe_event(self, event_bus):
        """測試事件訂閱"""
        handler = Mock()
        
        event_bus.subscribe(SystemEvent.CYCLE_STARTED, handler)
        
        # 驗證訂閱成功
        assert SystemEvent.CYCLE_STARTED in event_bus._handlers
        assert handler in event_bus._handlers[SystemEvent.CYCLE_STARTED]
    
    def test_unsubscribe_event(self, event_bus):
        """測試取消訂閱"""
        handler = Mock()
        
        event_bus.subscribe(SystemEvent.CYCLE_STARTED, handler)
        event_bus.unsubscribe(SystemEvent.CYCLE_STARTED, handler)
        
        # 驗證取消訂閱成功
        assert handler not in event_bus._handlers.get(SystemEvent.CYCLE_STARTED, [])
    
    def test_publish_sync_event(self, event_bus):
        """測試同步發布事件"""
        handler = Mock()
        
        event_bus.subscribe(SystemEvent.CYCLE_STARTED, handler)
        event_bus.publish(
            SystemEvent.CYCLE_STARTED,
            {"cycle_index": 1},
            source="test",
            sync=True
        )
        
        # 驗證處理器被調用
        assert handler.call_count == 1
        
        # 驗證事件數據正確
        call_args = handler.call_args[0][0]
        assert isinstance(call_args, Event)
        assert call_args.event_type == SystemEvent.CYCLE_STARTED
        assert call_args.data["cycle_index"] == 1
        assert call_args.source == "test"


@pytest.mark.event
class TestEventBusAsync:
    """事件總線異步處理測試"""
    
    def test_publish_async_event(self, event_bus):
        """測試異步發布事件"""
        handler = Mock()
        
        # 啟動事件處理線程
        event_bus.start()
        
        event_bus.subscribe(SystemEvent.CYCLE_COMPLETED, handler)
        event_bus.publish(
            SystemEvent.CYCLE_COMPLETED,
            {"cycle_index": 1},
            source="test",
            sync=False
        )
        
        # 等待異步處理
        time.sleep(0.1)
        
        # 驗證處理器被調用
        assert handler.call_count == 1
        
        # 清理
        event_bus.stop()
    
    def test_multiple_subscribers(self, event_bus):
        """測試多個訂閱者"""
        handler1 = Mock()
        handler2 = Mock()
        handler3 = Mock()
        
        event_bus.start()
        
        # 訂閱同一事件
        event_bus.subscribe(SystemEvent.STATE_CHANGED, handler1)
        event_bus.subscribe(SystemEvent.STATE_CHANGED, handler2)
        event_bus.subscribe(SystemEvent.STATE_CHANGED, handler3)
        
        event_bus.publish(
            SystemEvent.STATE_CHANGED,
            {"new_state": "WORK"},
            source="test",
            sync=False
        )
        
        # 等待異步處理
        time.sleep(0.1)
        
        # 驗證所有處理器都被調用
        assert handler1.call_count == 1
        assert handler2.call_count == 1
        assert handler3.call_count == 1
        
        event_bus.stop()


@pytest.mark.event
class TestEventHistory:
    """事件歷史記錄測試"""
    
    def test_event_history_recording(self, event_bus):
        """測試事件歷史記錄"""
        event_bus.publish(
            SystemEvent.CYCLE_STARTED,
            {"cycle_index": 1},
            source="test",
            sync=True
        )
        
        # 驗證事件被記錄
        assert len(event_bus._event_history) == 1
        
        event = event_bus._event_history[0]
        assert event.event_type == SystemEvent.CYCLE_STARTED
        assert event.data["cycle_index"] == 1
        assert event.source == "test"
    
    def test_get_recent_events(self, event_bus):
        """測試獲取最近事件"""
        # 發布多個事件
        for i in range(5):
            event_bus.publish(
                SystemEvent.CYCLE_STARTED,
                {"cycle_index": i},
                source="test",
                sync=True
            )
        
        # 獲取最近3個事件
        recent = event_bus.get_recent_events(count=3)
        assert len(recent) == 3
        assert recent[-1].data["cycle_index"] == 4  # 最新的
        assert recent[0].data["cycle_index"] == 2   # 第三新的
    
    def test_get_events_by_type(self, event_bus):
        """測試按類型獲取事件"""
        # 發布不同類型的事件
        event_bus.publish(SystemEvent.CYCLE_STARTED, {}, source="test", sync=True)
        event_bus.publish(SystemEvent.CYCLE_COMPLETED, {}, source="test", sync=True)
        event_bus.publish(SystemEvent.CYCLE_STARTED, {}, source="test", sync=True)
        
        # 只獲取 CYCLE_STARTED 事件
        started_events = event_bus.get_recent_events(
            count=10,
            event_type=SystemEvent.CYCLE_STARTED
        )
        
        assert len(started_events) == 2
        assert all(e.event_type == SystemEvent.CYCLE_STARTED for e in started_events)
    
    def test_history_size_limit(self, event_bus):
        """測試歷史記錄大小限制"""
        # 發布超過最大限制的事件
        for i in range(150):
            event_bus.publish(
                SystemEvent.CYCLE_STARTED,
                {"index": i},
                source="test",
                sync=True
            )
        
        # 驗證歷史記錄不超過限制
        assert len(event_bus._event_history) <= event_bus._max_history
        
        # 驗證保留的是最新的事件
        assert event_bus._event_history[-1].data["index"] == 149
    
    def test_clear_history(self, event_bus):
        """測試清空歷史記錄"""
        event_bus.publish(SystemEvent.CYCLE_STARTED, {}, source="test", sync=True)
        event_bus.publish(SystemEvent.CYCLE_COMPLETED, {}, source="test", sync=True)
        
        assert len(event_bus._event_history) > 0
        
        event_bus.clear_history()
        
        assert len(event_bus._event_history) == 0


@pytest.mark.event
class TestEventBusStats:
    """事件總線統計信息測試"""
    
    def test_publish_stats(self, event_bus):
        """測試發布統計"""
        initial_stats = event_bus.get_stats()
        
        event_bus.publish(SystemEvent.CYCLE_STARTED, {}, source="test", sync=True)
        event_bus.publish(SystemEvent.CYCLE_COMPLETED, {}, source="test", sync=True)
        
        stats = event_bus.get_stats()
        
        # 驗證統計更新
        assert stats["total_published"] == initial_stats["total_published"] + 2
        assert SystemEvent.CYCLE_STARTED.value in stats["by_event_type"]
        assert SystemEvent.CYCLE_COMPLETED.value in stats["by_event_type"]
    
    def test_processing_stats(self, event_bus):
        """測試處理統計"""
        handler = Mock()
        
        event_bus.start()
        event_bus.subscribe(SystemEvent.CYCLE_STARTED, handler)
        
        event_bus.publish(SystemEvent.CYCLE_STARTED, {}, source="test", sync=False)
        
        # 等待處理完成
        time.sleep(0.1)
        
        stats = event_bus.get_stats()
        assert stats["total_processed"] > 0
        
        event_bus.stop()
    
    def test_error_stats(self, event_bus):
        """測試錯誤統計"""
        # 創建會拋出異常的處理器
        def error_handler(event):
            raise ValueError("測試錯誤")
        
        event_bus.subscribe(SystemEvent.CYCLE_STARTED, error_handler)
        event_bus.publish(SystemEvent.CYCLE_STARTED, {}, source="test", sync=True)
        
        stats = event_bus.get_stats()
        assert stats["processing_errors"] > 0


@pytest.mark.event
@pytest.mark.critical
class TestEventOrdering:
    """事件處理順序測試"""
    
    def test_sync_event_order(self, event_bus):
        """測試同步事件處理順序"""
        call_order = []
        
        def handler1(event):
            call_order.append("handler1")
        
        def handler2(event):
            call_order.append("handler2")
        
        def handler3(event):
            call_order.append("handler3")
        
        # 按順序訂閱
        event_bus.subscribe(SystemEvent.CYCLE_STARTED, handler1)
        event_bus.subscribe(SystemEvent.CYCLE_STARTED, handler2)
        event_bus.subscribe(SystemEvent.CYCLE_STARTED, handler3)
        
        event_bus.publish(SystemEvent.CYCLE_STARTED, {}, source="test", sync=True)
        
        # 驗證處理器按訂閱順序執行
        assert call_order == ["handler1", "handler2", "handler3"]
    
    def test_multiple_events_order(self, event_bus):
        """測試多個事件的處理順序"""
        event_bus.start()
        
        received_events = []
        
        def handler(event):
            received_events.append(event.data["index"])
        
        event_bus.subscribe(SystemEvent.CYCLE_STARTED, handler)
        
        # 發布多個事件
        for i in range(5):
            event_bus.publish(
                SystemEvent.CYCLE_STARTED,
                {"index": i},
                source="test",
                sync=False
            )
        
        # 等待處理完成
        time.sleep(0.2)
        
        # 驗證事件按發布順序處理
        assert received_events == [0, 1, 2, 3, 4]
        
        event_bus.stop()


@pytest.mark.event
class TestEventBusSingleSource:
    """事件單一來源原則測試"""
    
    def test_event_source_recorded(self, event_bus):
        """測試事件來源記錄"""
        event_bus.publish(
            SystemEvent.CYCLE_STARTED,
            {"data": "test"},
            source="NLP_MODULE",
            sync=True
        )
        
        event = event_bus._event_history[-1]
        assert event.source == "NLP_MODULE"
    
    def test_different_sources(self, event_bus):
        """測試不同來源的事件"""
        event_bus.publish(
            SystemEvent.INPUT_LAYER_COMPLETE,
            {},
            source="NLP_MODULE",
            sync=True
        )
        
        event_bus.publish(
            SystemEvent.PROCESSING_LAYER_COMPLETE,
            {},
            source="LLM_MODULE",
            sync=True
        )
        
        # 驗證每個事件都記錄了正確的來源
        assert event_bus._event_history[-2].source == "NLP_MODULE"
        assert event_bus._event_history[-1].source == "LLM_MODULE"
    
    def test_workflow_event_sources(self, event_bus):
        """測試工作流事件來源"""
        # 模擬 LLM 批准步驟
        event_bus.publish(
            SystemEvent.WORKFLOW_STEP_APPROVED,
            {"step_id": "step1"},
            source="LLM_MODULE",
            sync=True
        )
        
        # 模擬 SYS 完成步驟
        event_bus.publish(
            SystemEvent.WORKFLOW_STEP_COMPLETED,
            {"step_id": "step1"},
            source="SYS_MODULE",
            sync=True
        )
        
        # 驗證來源正確
        assert event_bus._event_history[-2].source == "LLM_MODULE"
        assert event_bus._event_history[-1].source == "SYS_MODULE"


@pytest.mark.event
class TestEventBusThreadSafety:
    """事件總線線程安全測試"""
    
    def test_concurrent_subscriptions(self, event_bus):
        """測試並發訂閱"""
        import threading
        
        def subscribe_handler():
            for i in range(10):
                handler = Mock()
                event_bus.subscribe(SystemEvent.CYCLE_STARTED, handler)
        
        # 創建多個線程同時訂閱
        threads = [threading.Thread(target=subscribe_handler) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # 驗證所有訂閱都成功
        assert len(event_bus._handlers[SystemEvent.CYCLE_STARTED]) == 50
    
    def test_concurrent_publishes(self, event_bus):
        """測試並發發布"""
        import threading
        
        event_bus.start()
        
        def publish_events():
            for i in range(10):
                event_bus.publish(
                    SystemEvent.CYCLE_STARTED,
                    {"index": i},
                    source="test",
                    sync=False
                )
        
        # 創建多個線程同時發布
        threads = [threading.Thread(target=publish_events) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # 等待處理完成
        time.sleep(0.3)
        
        stats = event_bus.get_stats()
        assert stats["total_published"] == 50
        
        event_bus.stop()
