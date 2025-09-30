#!/usr/bin/env python3
"""
測試新的系統架構 - SystemInitializer + SystemLoop + ProductionRunner
"""

import time
from utils.debug_helper import debug_log, info_log, error_log

def test_system_initializer():
    """測試系統初始化器"""
    print("🔧 測試 SystemInitializer...")
    print("=" * 50)
    
    try:
        from core.system_initializer import SystemInitializer
        
        # 創建初始化器
        initializer = SystemInitializer()
        print(f"✅ SystemInitializer 已創建")
        
        # 執行初始化
        print(f"\n🚀 開始系統初始化...")
        success = initializer.initialize_system(production_mode=False)
        
        if success:
            print(f"✅ 系統初始化成功")
            
            # 顯示初始化狀態
            status = initializer.get_initialization_status()
            print(f"📊 初始化階段: {status['phase']}")
            print(f"📦 已初始化模組: {status['initialized_modules']}")
            print(f"❌ 失敗模組: {status['failed_modules']}")
            print(f"⏱️ 啟動時間: {status['startup_time']:.2f}秒")
            
        else:
            print(f"❌ 系統初始化失敗")
            return False
            
        return True
        
    except Exception as e:
        print(f"❌ SystemInitializer 測試失敗: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_system_loop():
    """測試系統循環"""
    print(f"\n🔄 測試 SystemLoop...")
    print("=" * 50)
    
    try:
        from core.system_loop import SystemLoop
        
        # 創建系統循環
        loop = SystemLoop()
        print(f"✅ SystemLoop 已創建")
        
        # 啟動循環
        print(f"\n🚀 啟動系統循環...")
        success = loop.start()
        
        if success:
            print(f"✅ 系統循環啟動成功")
            
            # 運行幾秒鐘
            print(f"⏱️ 運行5秒鐘...")
            time.sleep(5)
            
            # 檢查狀態
            status = loop.get_status()
            print(f"📊 循環狀態: {status['status']}")
            print(f"🔢 循環次數: {status['loop_count']}")
            print(f"⏱️ 運行時間: {status['uptime']:.1f}秒")
            print(f"🧵 線程存活: {status['thread_alive']}")
            
            # 停止循環
            print(f"\n🛑 停止系統循環...")
            loop.stop()
            print(f"✅ 系統循環已停止")
            
        else:
            print(f"❌ 系統循環啟動失敗")
            return False
            
        return True
        
    except Exception as e:
        print(f"❌ SystemLoop 測試失敗: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_production_runner():
    """測試生產運行器（簡短版本）"""
    print(f"\n🚀 測試 ProductionRunner...")
    print("=" * 50)
    
    try:
        from core.production_runner import ProductionRunner
        
        # 創建運行器
        runner = ProductionRunner()
        print(f"✅ ProductionRunner 已創建")
        
        # 只測試初始化部分，不實際運行循環
        print(f"\n🔧 測試系統初始化...")
        success = runner._initialize_system(production_mode=False)
        
        if success:
            print(f"✅ 生產運行器系統初始化成功")
            
            # 顯示狀態
            status = runner.get_status()
            print(f"📊 運行狀態: {status['is_running']}")
            if status['initializer_status']:
                init_status = status['initializer_status']
                print(f"📦 已載入模組: {init_status['initialized_modules']}")
            
        else:
            print(f"❌ 生產運行器初始化失敗")
            return False
            
        return True
        
    except Exception as e:
        print(f"❌ ProductionRunner 測試失敗: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_system_architecture_integration():
    """測試完整系統架構整合"""
    print(f"\n🏗️ 測試完整系統架構整合...")
    print("=" * 50)
    
    try:
        # 測試架構組件存在
        components = [
            ("Controller", "core.controller", "unified_controller"),
            ("Framework", "core.framework", "core_framework"),
            ("Router", "core.router", "router"),
            ("State Manager", "core.states.state_manager", "state_manager"),
            ("Session Manager", "core.sessions.session_manager", "unified_session_manager"),
            ("Working Context", "core.working_context", "working_context_manager")
        ]
        
        for name, module_path, obj_name in components:
            try:
                module = __import__(module_path, fromlist=[obj_name])
                obj = getattr(module, obj_name)
                print(f"✅ {name}: 已載入")
            except Exception as e:
                print(f"❌ {name}: 載入失敗 - {e}")
                return False
        
        # 測試 Framework 模組註冊
        from core.framework import core_framework
        if core_framework.is_initialized:
            modules = list(core_framework.modules.keys())
            print(f"📦 Framework 已註冊模組: {modules}")
        else:
            print(f"⚠️ Framework 尚未初始化")
        
        # 測試效能監控
        if hasattr(core_framework, 'performance_monitoring_enabled'):
            monitoring_status = "啟用" if core_framework.performance_monitoring_enabled else "停用"
            print(f"📊 效能監控: {monitoring_status}")
        
        print(f"✅ 系統架構整合測試通過")
        return True
        
    except Exception as e:
        print(f"❌ 系統架構整合測試失敗: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """主測試函數"""
    print("🧪 UEP 新系統架構測試")
    print("=" * 60)
    print("測試範圍: SystemInitializer + SystemLoop + ProductionRunner")
    print("架構流程: Controller → Framework → Router → Managers → Context")
    print("=" * 60)
    
    # 激活虛擬環境提示
    print("確保已激活虛擬環境: .\\env\\Scripts\\activate")
    print()
    
    # 執行測試
    tests = [
        ("系統初始化器", test_system_initializer),
        ("系統循環", test_system_loop),
        ("生產運行器", test_production_runner),
        ("架構整合", test_system_architecture_integration)
    ]
    
    results = []
    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except KeyboardInterrupt:
            print(f"\n⚠️ 測試被用戶中斷")
            break
        except Exception as e:
            print(f"\n❌ {test_name} 測試異常: {e}")
            results.append((test_name, False))
    
    # 測試結果總結
    print(f"\n📋 測試結果總結")
    print("=" * 30)
    
    success_count = 0
    for test_name, result in results:
        status = "✅ 通過" if result else "❌ 失敗"
        print(f"{test_name}: {status}")
        if result:
            success_count += 1
    
    total_tests = len(results)
    print(f"\n📊 總計: {success_count}/{total_tests} 測試通過")
    
    if success_count == total_tests:
        print("🎉 所有測試通過！新系統架構已就緒")
    else:
        print("⚠️ 部分測試失敗，需要檢查系統組件")

if __name__ == "__main__":
    main()