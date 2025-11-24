# tests/test_event_bus.py
"""
事件總線單元測試

測試事件驅動架構的核心功能
"""

import time
import unittest
from typing import List

from core.event_bus import EventBus, SystemEvent, Event


class TestEventBus(unittest.TestCase):
    """事件總線測試套件"""
    
    def setUp(self):
        """每個測試前的設置"""
        self.bus = EventBus()
        self.bus.start()
        self.received_events: List[Event] = []
    
    def tearDown(self):
        """每個測試後的清理"""
        self.bus.stop()
        self.received_events.clear()
    
    def test_publish_and_subscribe(self):
        """測試基本的發布訂閱功能"""
        def handler(event: Event):
            self.received_events.append(event)
        
        # 訂閱事件
        self.bus.subscribe(SystemEvent.INPUT_LAYER_COMPLETE, handler)
        
        # 發布事件
        test_data = {"test": "data", "value": 123}
        self.bus.publish(
            SystemEvent.INPUT_LAYER_COMPLETE,
            test_data,
            source="test"
        )
        
        # 等待異步處理
        time.sleep(0.5)
        
        # 驗證
        self.assertEqual(len(self.received_events), 1)
        self.assertEqual(self.received_events[0].event_type, SystemEvent.INPUT_LAYER_COMPLETE)
        self.assertEqual(self.received_events[0].data["test"], "data")
        self.assertEqual(self.received_events[0].source, "test")
    
    def test_multiple_subscribers(self):
        """測試多個訂閱者"""
        received_1 = []
        received_2 = []
        
        def handler1(event: Event):
            received_1.append(event)
        
        def handler2(event: Event):
            received_2.append(event)
        
        # 兩個訂閱者
        self.bus.subscribe(SystemEvent.PROCESSING_LAYER_COMPLETE, handler1)
        self.bus.subscribe(SystemEvent.PROCESSING_LAYER_COMPLETE, handler2)
        
        # 發布事件
        self.bus.publish(
            SystemEvent.PROCESSING_LAYER_COMPLETE,
            {"message": "test"},
            source="test"
        )
        
        time.sleep(0.5)
        
        # 兩個訂閱者都應該收到
        self.assertEqual(len(received_1), 1)
        self.assertEqual(len(received_2), 1)
    
    def test_event_filtering(self):
        """測試事件過濾（只接收訂閱的事件類型）"""
        def handler(event: Event):
            self.received_events.append(event)
        
        # 只訂閱輸入層完成
        self.bus.subscribe(SystemEvent.INPUT_LAYER_COMPLETE, handler)
        
        # 發布不同類型的事件
        self.bus.publish(SystemEvent.INPUT_LAYER_COMPLETE, {}, "test")
        self.bus.publish(SystemEvent.PROCESSING_LAYER_COMPLETE, {}, "test")
        self.bus.publish(SystemEvent.OUTPUT_LAYER_COMPLETE, {}, "test")
        
        time.sleep(0.5)
        
        # 只應該收到一個事件
        self.assertEqual(len(self.received_events), 1)
        self.assertEqual(
            self.received_events[0].event_type,
            SystemEvent.INPUT_LAYER_COMPLETE
        )
    
    def test_event_statistics(self):
        """測試事件統計功能"""
        def handler(event: Event):
            pass
        
        self.bus.subscribe(SystemEvent.INPUT_LAYER_COMPLETE, handler)
        
        # 發布多個事件
        for i in range(5):
            self.bus.publish(
                SystemEvent.INPUT_LAYER_COMPLETE,
                {"index": i},
                source="test"
            )
        
        time.sleep(0.5)
        
        # 檢查統計
        stats = self.bus.get_stats()
        self.assertEqual(stats["total_published"], 5)
        self.assertGreaterEqual(stats["total_processed"], 5)
    
    def test_sync_publish(self):
        """測試同步發布"""
        def handler(event: Event):
            self.received_events.append(event)
        
        self.bus.subscribe(SystemEvent.MODULE_READY, handler)
        
        # 同步發布
        self.bus.publish(
            SystemEvent.MODULE_READY,
            {"module": "test"},
            source="test",
            sync=True
        )
        
        # 同步發布應該立即處理，不需要 sleep
        self.assertEqual(len(self.received_events), 1)
    
    def test_error_handling(self):
        """測試錯誤處理（一個處理器錯誤不影響其他處理器）"""
        received_good = []
        
        def bad_handler(event: Event):
            raise ValueError("測試錯誤")
        
        def good_handler(event: Event):
            received_good.append(event)
        
        self.bus.subscribe(SystemEvent.STATE_CHANGED, bad_handler)
        self.bus.subscribe(SystemEvent.STATE_CHANGED, good_handler)
        
        # 發布事件
        self.bus.publish(
            SystemEvent.STATE_CHANGED,
            {"state": "test"},
            source="test"
        )
        
        time.sleep(0.5)
        
        # 好的處理器應該仍然收到事件
        self.assertEqual(len(received_good), 1)
        
        # 檢查統計中的錯誤計數
        stats = self.bus.get_stats()
        self.assertGreater(stats["processing_errors"], 0)
    
    def test_unsubscribe(self):
        """測試取消訂閱"""
        def handler(event: Event):
            self.received_events.append(event)
        
        # 訂閱
        self.bus.subscribe(SystemEvent.CYCLE_STARTED, handler)
        
        # 發布事件
        self.bus.publish(SystemEvent.CYCLE_STARTED, {}, "test")
        time.sleep(0.5)
        self.assertEqual(len(self.received_events), 1)
        
        # 取消訂閱
        self.bus.unsubscribe(SystemEvent.CYCLE_STARTED, handler)
        
        # 再次發布
        self.bus.publish(SystemEvent.CYCLE_STARTED, {}, "test")
        time.sleep(0.5)
        
        # 不應該收到新事件
        self.assertEqual(len(self.received_events), 1)
    
    def test_event_history(self):
        """測試事件歷史記錄"""
        # 發布多個事件
        for i in range(5):
            self.bus.publish(
                SystemEvent.INPUT_LAYER_COMPLETE,
                {"index": i},
                source="test"
            )
        
        time.sleep(0.5)
        
        # 獲取歷史
        recent = self.bus.get_recent_events(count=5)
        self.assertEqual(len(recent), 5)
        
        # 獲取特定類型的歷史
        specific = self.bus.get_recent_events(
            count=10,
            event_type=SystemEvent.INPUT_LAYER_COMPLETE
        )
        self.assertEqual(len(specific), 5)


if __name__ == "__main__":
    unittest.main()
