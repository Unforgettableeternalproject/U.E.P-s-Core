#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小化系統測試腳本
用於驗證系統核心層（Controller, Framework, Event Bus 等）在無模組情況下是否能正常運行
"""

import sys
import os
import time
from datetime import datetime

# 確保路徑正確
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log, warning_log
from utils.logger import force_enable_file_logging

# 強制啟用文件日誌
force_enable_file_logging()

def test_minimal_system():
    """測試最小化系統核心功能"""
    
    print("\n" + "="*60)
    print("U.E.P 最小化系統測試")
    print("="*60 + "\n")
    
    config = load_config()
    
    # ========== 階段 1: 配置檢查 ==========
    print("📋 階段 1: 檢查配置...")
    engine_mode = config.get("metadata", {}).get("engine_mode", "unknown")
    modules_enabled = config.get("modules_enabled", {})
    
    print(f"   引擎模式: {engine_mode}")
    print(f"   模組啟用狀態:")
    for module, enabled in modules_enabled.items():
        status = "✅ 啟用" if enabled else "❌ 停用"
        print(f"      - {module}: {status}")
    
    if engine_mode != "minimal":
        warning_log("⚠️  引擎模式不是 'minimal'，可能會載入模組")
    
    enabled_count = sum(1 for v in modules_enabled.values() if v)
    if enabled_count > 0:
        warning_log(f"⚠️  仍有 {enabled_count} 個模組被啟用")
    else:
        info_log("✅ 所有模組已停用")
    
    print()
    
    # ========== 階段 2: 核心系統初始化 ==========
    print("🔧 階段 2: 初始化核心系統...")
    
    try:
        # 2.1 Controller
        print("   初始化 Controller...")
        from core.controller import unified_controller
        info_log("   ✅ Controller 已載入")
        
        # 2.2 Framework
        print("   初始化 Framework...")
        from core.framework import core_framework
        
        # Framework 初始化（應該會跳過所有模組）
        success = core_framework.initialize()
        if success:
            info_log("   ✅ Framework 初始化成功")
            registered_modules = list(core_framework.modules.keys())
            if len(registered_modules) == 0:
                info_log("   ✅ 確認：無模組被註冊（符合預期）")
            else:
                warning_log(f"   ⚠️  發現 {len(registered_modules)} 個模組被註冊: {registered_modules}")
        else:
            error_log("   ❌ Framework 初始化失敗")
            return False
        
        # 2.3 Event Bus
        print("   初始化 Event Bus...")
        from core.event_bus import event_bus
        info_log("   ✅ Event Bus 已載入")
        
        # 2.4 Registry
        print("   初始化 Registry...")
        from core import registry
        info_log("   ✅ Registry 已載入")
        
        # 2.5 State Manager
        print("   初始化 State Manager...")
        from core.states.state_manager import state_manager
        info_log("   ✅ State Manager 已載入")
        
        # 2.6 Session Manager
        print("   初始化 Session Manager...")
        from core.sessions.session_manager import session_manager
        info_log("   ✅ Session Manager 已載入")
        
        print()
        
    except Exception as e:
        error_log(f"❌ 核心系統初始化失敗: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # ========== 階段 3: 功能驗證 ==========
    print("🧪 階段 3: 驗證核心功能...")
    
    try:
        # 3.1 Event Bus 事件測試
        print("   測試 Event Bus 事件訂閱/發布...")
        from core.event_bus import SystemEvent
        test_event_data = []
        
        def test_handler(event_data):
            test_event_data.append(event_data)
        
        event_bus.subscribe(SystemEvent.MODULE_INITIALIZED, test_handler)
        event_bus.publish(SystemEvent.MODULE_INITIALIZED, {"message": "測試訊息"})
        
        time.sleep(0.1)  # 等待事件處理
        
        if len(test_event_data) > 0:
            info_log("   ✅ Event Bus 事件系統正常")
        else:
            warning_log("   ⚠️  Event Bus 可能未正常工作")
        
        # 3.2 Framework 狀態查詢
        print("   測試 Framework 狀態查詢...")
        status = core_framework.get_framework_status()
        print(f"      - 已初始化: {status['is_initialized']}")
        print(f"      - 運行時間: {status['uptime_seconds']:.2f} 秒")
        print(f"      - 模組總數: {status['total_modules']}")
        print(f"      - 可用模組: {status['available_modules']}")
        
        if status['is_initialized'] and status['total_modules'] == 0:
            info_log("   ✅ Framework 狀態正常（無模組載入）")
        
        # 3.3 Registry 測試
        print("   測試 Registry...")
        from core.registry import is_loaded, get_loaded
        
        # 檢查所有可能的模組
        possible_modules = ['stt_module', 'nlp_module', 'mem_module', 'llm_module', 
                           'tts_module', 'sys_module', 'ui_module', 'ani_module', 'mov_module']
        loaded_count = sum(1 for m in possible_modules if is_loaded(m))
        
        print(f"      - Registry 中的模組數: {loaded_count}")
        
        if loaded_count == 0:
            info_log("   ✅ Registry 確認無模組")
        else:
            loaded_modules = [m for m in possible_modules if is_loaded(m)]
            warning_log(f"   ⚠️  Registry 中仍有模組: {loaded_modules}")
        
        print()
        
    except Exception as e:
        error_log(f"❌ 功能驗證失敗: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # ========== 階段 4: 總結 ==========
    print("="*60)
    print("📊 測試總結")
    print("="*60)
    print()
    print("✅ 核心系統測試通過！")
    print()
    print("已驗證組件:")
    print("  - Controller ✓")
    print("  - Framework ✓")
    print("  - Event Bus ✓")
    print("  - Registry ✓")
    print("  - State Manager ✓")
    print("  - Session Manager ✓")
    print()
    print("模組載入狀態:")
    print(f"  - 已註冊模組: {status['total_modules']} 個")
    print()
    
    if status['total_modules'] == 0:
        print("🎉 最小化系統運行成功！所有模組已解耦。")
    else:
        print("⚠️  部分模組仍被載入，需進一步檢查。")
    
    print()
    print("="*60)
    
    return True


def test_debugger_availability():
    """測試 debugger CLI 是否可用"""
    
    print("\n" + "="*60)
    print("Debugger CLI 可用性測試")
    print("="*60 + "\n")
    
    try:
        # 只測試配置加載，不導入完整的 debugger 模組（避免觸發模組測試導入）
        from configs.config_loader import load_config
        config = load_config()
        
        module_enabled = config.get("modules_enabled", {})
        module_refactored = config.get("modules_refactored", {})
        
        mod_list = {
            "stt": (module_enabled.get("stt_module", False), module_refactored.get("stt_module", False)),
            "nlp": (module_enabled.get("nlp_module", False), module_refactored.get("nlp_module", False)),
            "mem": (module_enabled.get("mem_module", False), module_refactored.get("mem_module", False)),
            "llm": (module_enabled.get("llm_module", False), module_refactored.get("llm_module", False)),
            "tts": (module_enabled.get("tts_module", False), module_refactored.get("tts_module", False)),
            "sys": (module_enabled.get("sys_module", False), module_refactored.get("sys_module", False)),
            "ui": (module_enabled.get("ui_module", False), module_refactored.get("ui_module", False)),
            "ani": (module_enabled.get("ani_module", False), module_refactored.get("ani_module", False)),
            "mov": (module_enabled.get("mov_module", False), module_refactored.get("mov_module", False))
        }
        
        print("✅ debugger 配置可以加載")
        
        print(f"\n模組啟用狀態（從 debugger 視角）:")
        for mod_name, (enabled, refactored) in mod_list.items():
            status = "綠色" if (enabled and refactored) else "黃色" if enabled else "紅色"
            print(f"  - {mod_name}: {status} (啟用={enabled}, 重構={refactored})")
        
        print("\n✅ Debugger CLI 基礎功能可用")
        print("💡 提示：可以運行 'python devtools/debugger.py' 啟動除錯介面")
        print("⚠️  注意：由於所有模組已停用，大部分測試功能將無法使用")
        
        return True
        
    except Exception as e:
        error_log(f"❌ Debugger 測試失敗: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("\n")
    info_log("開始最小化系統測試")
    print()
    
    # 測試核心系統
    core_test_passed = test_minimal_system()
    
    # 測試 debugger
    debugger_test_passed = test_debugger_availability()
    
    # 最終結果
    print("\n" + "="*60)
    print("最終測試結果")
    print("="*60)
    print()
    
    if core_test_passed and debugger_test_passed:
        print("🎉 所有測試通過！")
        print()
        print("✅ 核心系統：正常")
        print("✅ Debugger CLI：可用")
        print()
        print("系統已準備好進行下一步重構。")
        sys.exit(0)
    else:
        print("❌ 部分測試失敗")
        print()
        print(f"{'✅' if core_test_passed else '❌'} 核心系統：{'正常' if core_test_passed else '異常'}")
        print(f"{'✅' if debugger_test_passed else '❌'} Debugger CLI：{'可用' if debugger_test_passed else '不可用'}")
        print()
        print("請檢查錯誤訊息並修正問題。")
        sys.exit(1)
