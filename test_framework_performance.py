#!/usr/bin/env python3
"""
測試 Framework 效能監控功能
"""

import time
import random
from core.framework import CoreFramework

def test_performance_monitoring():
    """測試效能監控功能"""
    print("🔍 Framework 效能監控測試")
    print("=" * 50)
    
    # 初始化框架
    framework = CoreFramework()
    framework.initialize()
    
    print(f"📊 Framework 狀態:")
    print(f"   初始化: {framework.is_initialized}")
    print(f"   監控啟用: {framework.performance_monitoring_enabled}")
    print(f"   已註冊模組: {len(framework.modules)}")
    
    # 模擬模組效能指標更新
    modules = ['stt', 'nlp', 'mem', 'llm']
    
    print(f"\n🎯 模擬模組效能指標更新...")
    for i in range(5):
        for module_id in modules:
            # 模擬處理時間和結果
            processing_time = random.uniform(0.1, 2.0)
            memory_usage = random.uniform(50, 200)
            request_result = 'success' if random.random() > 0.1 else 'failed'
            
            metrics_data = {
                'processing_time': processing_time,
                'memory_usage': memory_usage,
                'request_result': request_result,
                'custom_metrics': {
                    'cpu_usage': random.uniform(10, 80),
                    'queue_size': random.randint(0, 10)
                }
            }
            
            framework.update_module_metrics(module_id, metrics_data)
        
        print(f"   第 {i+1} 輪指標更新完成")
        time.sleep(0.1)
    
    # 蒐集系統效能快照
    print(f"\n📸 蒐集系統效能快照...")
    snapshot = framework.collect_system_performance_snapshot()
    
    print(f"   快照時間: {time.strftime('%H:%M:%S', time.localtime(snapshot.timestamp))}")
    print(f"   總模組數: {snapshot.total_modules}")
    print(f"   活躍模組: {snapshot.active_modules}")
    print(f"   系統運行時間: {snapshot.system_uptime:.2f} 秒")
    print(f"   總請求數: {snapshot.total_system_requests}")
    print(f"   系統成功率: {snapshot.system_success_rate:.2%}")
    print(f"   平均響應時間: {snapshot.system_average_response_time:.3f} 秒")
    
    # 顯示各模組指標
    print(f"\n📈 模組效能指標:")
    for module_id in modules:
        metrics = framework.get_module_metrics(module_id)
        if metrics:
            print(f"   {module_id}:")
            print(f"     總請求: {metrics.total_requests}")
            print(f"     成功率: {metrics.success_rate:.2%}")
            print(f"     平均處理時間: {metrics.average_processing_time:.3f}s")
            print(f"     峰值處理時間: {metrics.peak_processing_time:.3f}s")
            print(f"     當前記憶體: {metrics.memory_usage:.1f}MB")
            print(f"     峰值記憶體: {metrics.peak_memory_usage:.1f}MB")
            print(f"     錯誤次數: {metrics.error_count}")
    
    # 獲取效能摘要
    print(f"\n📋 效能摘要:")
    summary = framework.get_performance_summary()
    
    print(f"   Framework 狀態:")
    for key, value in summary["framework_status"].items():
        if key == "uptime":
            print(f"     {key}: {value:.2f}秒")
        else:
            print(f"     {key}: {value}")
    
    print(f"   監控統計:")
    for key, value in summary["monitoring_stats"].items():
        print(f"     {key}: {value}")
    
    # 測試多次快照蒐集
    print(f"\n🔄 測試連續快照蒐集...")
    for i in range(3):
        snapshot = framework.collect_system_performance_snapshot()
        print(f"   快照 {i+1}: {snapshot.total_system_requests} 總請求")
        time.sleep(0.5)
    
    # 獲取歷史記錄
    history = framework.get_performance_history(count=5)
    print(f"\n📚 效能歷史記錄 (最近5個):")
    for i, snapshot in enumerate(history, 1):
        timestamp_str = time.strftime('%H:%M:%S', time.localtime(snapshot.timestamp))
        print(f"   {i}. {timestamp_str} - 請求數: {snapshot.total_system_requests}, 成功率: {snapshot.system_success_rate:.2%}")
    
    print(f"\n✅ Framework 效能監控測試完成")

def test_performance_monitoring_integration():
    """測試與系統循環的整合"""
    print(f"\n🔄 系統循環整合測試")
    print("=" * 30)
    
    framework = CoreFramework()
    framework.initialize()
    
    # 模擬系統循環中的效能監控
    print(f"   模擬 3 次系統循環...")
    for cycle in range(3):
        print(f"   系統循環 {cycle + 1}:")
        
        # 模擬模組處理
        for module_id in ['stt', 'nlp']:
            processing_start = time.time()
            time.sleep(random.uniform(0.05, 0.15))  # 模擬處理時間
            processing_time = time.time() - processing_start
            
            framework.update_module_metrics(module_id, {
                'processing_time': processing_time,
                'request_result': 'success'
            })
        
        # 在系統循環末尾蒐集快照
        snapshot = framework.collect_system_performance_snapshot()
        print(f"     -> 快照: {snapshot.active_modules} 活躍模組, 平均響應: {snapshot.system_average_response_time:.3f}s")
    
    print(f"   系統循環整合測試完成")

if __name__ == "__main__":
    # 激活虛擬環境提示
    print("確保已激活虛擬環境: .\\env\\Scripts\\activate")
    
    try:
        test_performance_monitoring()
        test_performance_monitoring_integration()
    except KeyboardInterrupt:
        print(f"\n測試被用戶中斷")
    except Exception as e:
        print(f"\n❌ 測試失敗: {e}")
        import traceback
        traceback.print_exc()