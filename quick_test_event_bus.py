# quick_test_event_bus.py
"""
快速測試事件總線功能
用於驗證事件驅動架構的基本功能
"""

import time
from core.event_bus import event_bus, SystemEvent


def test_basic_event_flow():
    """測試基本的事件流程"""
    print("=" * 60)
    print("🧪 測試事件總線基本功能")
    print("=" * 60)
    
    # 啟動事件總線
    print("\n1️⃣ 啟動事件總線...")
    event_bus.start()
    print("   ✅ 事件總線已啟動")
    
    # 創建事件接收計數器
    event_counters = {
        "input": 0,
        "processing": 0,
        "output": 0
    }
    
    # 定義處理器
    def on_input_complete(event):
        event_counters["input"] += 1
        print(f"   📥 收到輸入層完成事件: {event.event_id} from {event.source}")
    
    def on_processing_complete(event):
        event_counters["processing"] += 1
        print(f"   ⚙️ 收到處理層完成事件: {event.event_id} from {event.source}")
    
    def on_output_complete(event):
        event_counters["output"] += 1
        print(f"   📤 收到輸出層完成事件: {event.event_id} from {event.source}")
    
    # 訂閱事件
    print("\n2️⃣ 訂閱三層架構事件...")
    event_bus.subscribe(SystemEvent.INPUT_LAYER_COMPLETE, on_input_complete)
    event_bus.subscribe(SystemEvent.PROCESSING_LAYER_COMPLETE, on_processing_complete)
    event_bus.subscribe(SystemEvent.OUTPUT_LAYER_COMPLETE, on_output_complete)
    print("   ✅ 已訂閱所有層級事件")
    
    # 模擬三層流程
    print("\n3️⃣ 模擬三層架構流程...")
    
    print("\n   👉 模擬 NLP 發布輸入層完成事件...")
    event_bus.publish(
        SystemEvent.INPUT_LAYER_COMPLETE,
        {
            "text": "你好，UEP",
            "intent": "chat",
            "confidence": 0.95
        },
        source="nlp"
    )
    
    time.sleep(0.3)
    
    print("\n   👉 模擬 LLM 發布處理層完成事件...")
    event_bus.publish(
        SystemEvent.PROCESSING_LAYER_COMPLETE,
        {
            "response": "你好！我是 UEP，很高興為你服務。",
            "mode": "chat",
            "success": True
        },
        source="llm"
    )
    
    time.sleep(0.3)
    
    print("\n   👉 模擬 TTS 發布輸出層完成事件...")
    event_bus.publish(
        SystemEvent.OUTPUT_LAYER_COMPLETE,
        {
            "output_path": "/outputs/tts/response.wav",
            "duration": 2.5,
            "status": "success"
        },
        source="tts"
    )
    
    time.sleep(0.3)
    
    # 驗證結果
    print("\n4️⃣ 驗證事件接收...")
    all_received = all(count == 1 for count in event_counters.values())
    
    if all_received:
        print("   ✅ 所有事件都已正確接收！")
        print(f"   📊 輸入層: {event_counters['input']} 個事件")
        print(f"   📊 處理層: {event_counters['processing']} 個事件")
        print(f"   📊 輸出層: {event_counters['output']} 個事件")
    else:
        print("   ❌ 部分事件未接收")
        print(f"   📊 輸入層: {event_counters['input']}/1")
        print(f"   📊 處理層: {event_counters['processing']}/1")
        print(f"   📊 輸出層: {event_counters['output']}/1")
    
    # 檢查統計
    print("\n5️⃣ 事件總線統計...")
    stats = event_bus.get_stats()
    print(f"   📈 總發布: {stats['total_published']} 個事件")
    print(f"   📈 總處理: {stats['total_processed']} 個事件")
    print(f"   📈 錯誤: {stats['processing_errors']} 個")
    print(f"   📈 隊列大小: {stats['queue_size']}")
    
    # 停止事件總線
    print("\n6️⃣ 停止事件總線...")
    event_bus.stop()
    print("   ✅ 事件總線已停止")
    
    print("\n" + "=" * 60)
    if all_received:
        print("✅ 事件驅動架構測試通過！")
    else:
        print("❌ 事件驅動架構測試失敗")
    print("=" * 60)
    
    return all_received


def test_multiple_subscribers():
    """測試多個訂閱者"""
    print("\n" + "=" * 60)
    print("🧪 測試多訂閱者場景")
    print("=" * 60)
    
    event_bus.start()
    
    # 創建多個處理器
    handlers_called = []
    
    def handler_1(event):
        handlers_called.append("handler_1")
        print(f"   🔸 處理器1 收到事件: {event.event_type.value}")
    
    def handler_2(event):
        handlers_called.append("handler_2")
        print(f"   🔸 處理器2 收到事件: {event.event_type.value}")
    
    def handler_3(event):
        handlers_called.append("handler_3")
        print(f"   🔸 處理器3 收到事件: {event.event_type.value}")
    
    # 訂閱同一個事件
    print("\n1️⃣ 三個處理器訂閱同一個事件...")
    event_bus.subscribe(SystemEvent.MODULE_READY, handler_1)
    event_bus.subscribe(SystemEvent.MODULE_READY, handler_2)
    event_bus.subscribe(SystemEvent.MODULE_READY, handler_3)
    
    # 發布事件
    print("\n2️⃣ 發布 MODULE_READY 事件...")
    event_bus.publish(
        SystemEvent.MODULE_READY,
        {"module": "test_module"},
        source="test"
    )
    
    time.sleep(0.3)
    
    # 驗證
    print("\n3️⃣ 驗證所有處理器都收到...")
    if len(handlers_called) == 3:
        print("   ✅ 所有三個處理器都收到事件！")
        print(f"   📊 調用順序: {' -> '.join(handlers_called)}")
    else:
        print(f"   ❌ 只有 {len(handlers_called)}/3 個處理器收到")
    
    event_bus.stop()
    print("\n" + "=" * 60)
    
    return len(handlers_called) == 3


if __name__ == "__main__":
    print("\n🚀 開始測試事件驅動架構...\n")
    
    # 測試1: 基本事件流程
    test1_passed = test_basic_event_flow()
    
    # 測試2: 多訂閱者
    test2_passed = test_multiple_subscribers()
    
    # 總結
    print("\n" + "=" * 60)
    print("📊 測試總結")
    print("=" * 60)
    print(f"   基本事件流程: {'✅ 通過' if test1_passed else '❌ 失敗'}")
    print(f"   多訂閱者場景: {'✅ 通過' if test2_passed else '❌ 失敗'}")
    
    if test1_passed and test2_passed:
        print("\n🎉 所有測試通過！事件驅動架構工作正常！")
    else:
        print("\n⚠️ 部分測試失敗，請檢查事件總線實現")
    print("=" * 60 + "\n")
