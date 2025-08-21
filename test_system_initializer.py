#!/usr/bin/env python3
"""
測試系統初始化器
測試系統啟動時清空狀態佇列的功能
"""

import time
from core.state_queue import get_state_queue_manager, SystemState
from core.state_manager import state_manager, UEPState
from core.system_initializer import system_initializer

def test_system_initialization_queue_cleanup():
    """測試系統初始化時清空狀態佇列"""
    print("=" * 60)
    print("🧪 測試系統初始化時清空狀態佇列")
    print("=" * 60)
    
    # 步驟1：獲取狀態佇列管理器
    state_queue = get_state_queue_manager()
    print(f"📊 初始佇列長度: {len(state_queue.queue)}")
    print(f"📊 初始系統狀態: {state_manager.get_state().name}")
    print(f"📊 初始佇列狀態: {state_queue.current_state.value}")
    
    # 步驟2：手動添加一些測試狀態項目
    print("\n🔧 添加測試狀態項目...")
    
    # 添加工作狀態
    state_queue.add_state(
        SystemState.WORK, 
        "測試工作項目1 - 發送郵件", 
        context_content="發送郵件給客戶",
        trigger_user="test_user_001"
    )
    
    state_queue.add_state(
        SystemState.CHAT, 
        "測試聊天項目1 - 問候", 
        context_content="Hello, how are you?",
        trigger_user="test_user_002"
    )
    
    state_queue.add_state(
        SystemState.WORK, 
        "測試工作項目2 - 設置提醒", 
        context_content="設置明天的會議提醒",
        trigger_user="test_user_001"
    )
    
    print(f"✅ 添加完成！佇列長度: {len(state_queue.queue)}")
    
    # 顯示佇列內容
    print("\n📋 當前佇列內容:")
    for i, item in enumerate(state_queue.queue, 1):
        print(f"  {i}. {item.state.value} (優先級: {item.priority})")
        print(f"     觸發: {item.trigger_content}")
        print(f"     上下文: {item.context_content}")
        print(f"     用戶: {item.trigger_user}")
    
    # 步驟3：執行系統初始化
    print(f"\n🚀 開始系統初始化...")
    print(f"初始化前 - 佇列長度: {len(state_queue.queue)}")
    print(f"初始化前 - 系統狀態: {state_manager.get_state().name}")
    print(f"初始化前 - 佇列狀態: {state_queue.current_state.value}")
    
    # 執行初始化
    start_time = time.time()
    try:
        result = system_initializer.initialize_system(production_mode=False)
        end_time = time.time()
        
        print(f"\n📊 初始化結果: {'✅ 成功' if result else '❌ 失敗'}")
        print(f"📊 耗時: {end_time - start_time:.2f} 秒")
        
    except Exception as e:
        print(f"\n❌ 初始化過程中發生錯誤: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # 步驟4：檢查初始化後的狀態
    print(f"\n🔍 檢查初始化後的狀態:")
    print(f"佇列長度: {len(state_queue.queue)}")
    print(f"系統狀態: {state_manager.get_state().name}")
    print(f"佇列狀態: {state_queue.current_state.value}")
    
    # 獲取系統狀態
    status = system_initializer.get_system_status()
    print(f"\n📈 系統狀態報告:")
    print(f"  階段: {status['phase']}")
    print(f"  系統狀態: {status['system_state']}")
    print(f"  佇列狀態: {status['state_queue']['current_state']}")
    print(f"  佇列長度: {status['state_queue']['queue_length']}")
    print(f"  待處理狀態: {status['state_queue']['pending_states']}")
    print(f"  已初始化模組: {status['initialized_modules']}")
    print(f"  失敗模組: {status['failed_modules']}")
    print(f"  系統就緒: {status['is_ready']}")
    
    # 步驟5：驗證結果
    print(f"\n✅ 驗證結果:")
    
    # 檢查佇列是否被清空
    queue_cleared = len(state_queue.queue) == 0
    print(f"  佇列已清空: {'✅' if queue_cleared else '❌'}")
    
    # 檢查系統狀態是否為 IDLE
    system_idle = state_manager.get_state() == UEPState.IDLE
    print(f"  系統狀態為 IDLE: {'✅' if system_idle else '❌'}")
    
    # 檢查佇列狀態是否為 idle
    queue_idle = state_queue.current_state.value == 'idle'
    print(f"  佇列狀態為 idle: {'✅' if queue_idle else '❌'}")
    
    # 檢查是否有模組被初始化
    modules_initialized = len(status['initialized_modules']) > 0
    print(f"  有模組被初始化: {'✅' if modules_initialized else '⚠️'}")
    
    # 總結
    all_checks_passed = queue_cleared and system_idle and queue_idle
    print(f"\n🎯 整體測試結果: {'✅ 全部通過' if all_checks_passed else '❌ 部分失敗'}")
    
    if all_checks_passed:
        print("🎉 系統初始化正確地清空了狀態佇列！")
    else:
        print("⚠️ 系統初始化可能存在問題，請檢查日誌。")
    
    return all_checks_passed


def test_add_states_after_initialization():
    """測試初始化後添加新狀態"""
    print("\n" + "=" * 60)
    print("🧪 測試初始化後添加新狀態")
    print("=" * 60)
    
    state_queue = get_state_queue_manager()
    
    print(f"📊 初始化後佇列長度: {len(state_queue.queue)}")
    
    # 添加新的狀態
    print("🔧 添加新的測試狀態...")
    success1 = state_queue.add_state(
        SystemState.CHAT,
        "初始化後的聊天測試",
        context_content="How is the system working?",
        trigger_user="test_user_post_init"
    )
    
    success2 = state_queue.add_state(
        SystemState.WORK,
        "初始化後的工作測試", 
        context_content="Run system diagnostics",
        trigger_user="test_user_post_init"
    )
    
    print(f"添加狀態1結果: {'✅' if success1 else '❌'}")
    print(f"添加狀態2結果: {'✅' if success2 else '❌'}")
    print(f"新的佇列長度: {len(state_queue.queue)}")
    
    # 顯示新的佇列內容
    if state_queue.queue:
        print("\n📋 新的佇列內容:")
        for i, item in enumerate(state_queue.queue, 1):
            print(f"  {i}. {item.state.value} (優先級: {item.priority})")
            print(f"     上下文: {item.context_content}")
    
    # 清空測試佇列
    print("\n🧹 清空測試佇列...")
    state_queue.clear_queue()
    print(f"清空後佇列長度: {len(state_queue.queue)}")
    
    return success1 and success2


if __name__ == "__main__":
    print("🚀 開始測試系統初始化器")
    print(f"測試時間: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    try:
        # 測試1：系統初始化清空佇列
        test1_result = test_system_initialization_queue_cleanup()
        
        # 測試2：初始化後添加狀態
        test2_result = test_add_states_after_initialization()
        
        # 總結
        print("\n" + "=" * 60)
        print("📊 測試總結")
        print("=" * 60)
        print(f"測試1 - 系統初始化清空佇列: {'✅ 通過' if test1_result else '❌ 失敗'}")
        print(f"測試2 - 初始化後添加狀態: {'✅ 通過' if test2_result else '❌ 失敗'}")
        
        if test1_result and test2_result:
            print("\n🎉 所有測試通過！系統初始化器工作正常。")
        else:
            print("\n⚠️ 部分測試失敗，請檢查系統配置。")
            
    except Exception as e:
        print(f"\n❌ 測試過程中發生錯誤: {e}")
        import traceback
        traceback.print_exc()
