#!/usr/bin/env python3
"""
U.E.P's Core 模組整合驗證腳本
用於驗證 Core Framework 和 STT→MEM 工作流程的狀態
"""

import sys
import os
import traceback
from datetime import datetime

def print_section(title):
    """打印區段標題"""
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}")

def test_dependencies():
    """測試依賴套件"""
    print_section("依賴套件檢查")
    
    dependencies = [
        ("sentence_transformers", "SentenceTransformer"),
        ("faiss", "IndexFlatL2"),
        ("numpy", "array"),
        ("torch", "tensor"),
        ("transformers", "AutoModel"),
    ]
    
    results = {}
    
    for module_name, test_import in dependencies:
        try:
            exec(f"import {module_name}")
            if test_import:
                exec(f"from {module_name} import {test_import}")
            results[module_name] = "✅ 可用"
        except ImportError as e:
            results[module_name] = f"❌ 缺失: {e}"
        except Exception as e:
            results[module_name] = f"⚠️ 問題: {e}"
    
    for module, status in results.items():
        print(f"  {module:<20}: {status}")
    
    return all("✅" in status for status in results.values())

def test_core_framework():
    """測試核心框架"""
    print_section("Core Framework 檢查")
    
    tests = []
    
    try:
        from core.framework import CoreFramework, core_framework
        tests.append(("Core Framework 導入", "✅"))
        
        from core.working_context import WorkingContextManager, ContextType
        tests.append(("Working Context 導入", "✅"))
        
        from core.state_manager import StateManager, UEPState
        tests.append(("State Manager 導入", "✅"))
        
        from core.strategies import smart_strategy, priority_strategy
        tests.append(("Routing Strategies 導入", "✅"))
        
        # 測試框架功能
        available_modules = core_framework.get_registered_modules()
        tests.append((f"已註冊模組數量", f"✅ {len(available_modules)} 個"))
        
        current_state = core_framework.get_current_state()
        tests.append(("當前系統狀態", f"✅ {current_state.name}"))
        
    except Exception as e:
        tests.append(("Core Framework", f"❌ 錯誤: {e}"))
    
    for test_name, result in tests:
        print(f"  {test_name:<25}: {result}")
    
    return all("✅" in result for _, result in tests)

def test_unified_controller():
    """測試統一控制器"""
    print_section("Unified Controller 檢查")
    
    tests = []
    
    try:
        from core.controller import unified_controller
        tests.append(("Controller 導入", "✅"))
        
        # 測試初始化
        print("  正在嘗試初始化...")
        success = unified_controller.initialize()
        tests.append(("Controller 初始化", "✅ 成功" if success else "❌ 失敗"))
        
        if success:
            status = unified_controller.get_system_status()
            enabled_modules = status.get("enabled_modules", [])
            tests.append((f"啟用模組", f"✅ {len(enabled_modules)} 個: {', '.join(enabled_modules)}"))
            
            health = status.get("system_health", "unknown")
            tests.append(("系統健康狀態", f"✅ {health}" if health == "healthy" else f"⚠️ {health}"))
        
    except Exception as e:
        tests.append(("Unified Controller", f"❌ 錯誤: {e}"))
        traceback.print_exc()
    
    for test_name, result in tests:
        print(f"  {test_name:<25}: {result}")
    
    return all("✅" in result for _, result in tests)

def test_stt_module():
    """測試 STT 模組"""
    print_section("STT Module 檢查")
    
    tests = []
    
    try:
        from modules.stt_module.stt_module import STTModule
        tests.append(("STT Module 導入", "✅"))
        
        from modules.stt_module.speaker_identification import SpeakerIdentification
        tests.append(("Speaker Identification 導入", "✅"))
        
        from modules.stt_module.speaker_context_handler import SpeakerContextHandler
        tests.append(("Speaker Context Handler 導入", "✅"))
        
        from modules.stt_module.schemas import STTInput, STTOutput, SpeakerInfo
        tests.append(("STT Schemas 導入", "✅"))
        
    except Exception as e:
        tests.append(("STT Module", f"❌ 錯誤: {e}"))
    
    for test_name, result in tests:
        print(f"  {test_name:<30}: {result}")
    
    return all("✅" in result for _, result in tests)

def test_mem_module():
    """測試 MEM 模組"""
    print_section("MEM Module 檢查")
    
    tests = []
    
    try:
        from modules.mem_module.mem_module import MEMModule
        tests.append(("MEM Module 導入", "✅"))
        
        from modules.mem_module.working_context_handler import MemoryContextHandler
        tests.append(("Memory Context Handler 導入", "✅"))
        
        from modules.mem_module.schemas import MemoryEntry, ConversationSnapshot, MemoryType
        tests.append(("MEM Schemas 導入", "✅"))
        
        # 測試 MEM 子模組
        try:
            from modules.mem_module.core.identity_manager import IdentityManager
            tests.append(("Identity Manager 導入", "✅"))
        except ImportError:
            tests.append(("Identity Manager 導入", "⚠️ 可能未實作"))
        
        try:
            from modules.mem_module.core.snapshot_manager import SnapshotManager
            tests.append(("Snapshot Manager 導入", "✅"))
        except ImportError:
            tests.append(("Snapshot Manager 導入", "⚠️ 可能未實作"))
        
    except Exception as e:
        tests.append(("MEM Module", f"❌ 錯誤: {e}"))
        print(f"    詳細錯誤: {e}")
    
    for test_name, result in tests:
        print(f"  {test_name:<30}: {result}")
    
    return all("✅" in result or "⚠️" in result for _, result in tests)

def test_working_context_integration():
    """測試 Working Context 整合"""
    print_section("Working Context 整合檢查")
    
    tests = []
    
    try:
        from core.working_context import working_context_manager, ContextType
        
        # 測試上下文類型
        context_types = list(ContextType)
        tests.append((f"上下文類型數量", f"✅ {len(context_types)} 種"))
        
        # 測試添加上下文數據
        test_context_id = f"test_context_{int(datetime.now().timestamp())}"
        working_context_manager.add_data(
            context_id=test_context_id,
            context_type=ContextType.SPEAKER_ACCUMULATION,
            data={"test": "data"},
            threshold=1
        )
        tests.append(("上下文數據添加", "✅"))
        
        # 檢查上下文狀態
        contexts_info = working_context_manager.get_all_contexts_info()
        tests.append((f"活躍上下文數量", f"✅ {len(contexts_info)} 個"))
        
        # 清理測試上下文
        working_context_manager.cleanup_expired_contexts()
        tests.append(("上下文清理", "✅"))
        
    except Exception as e:
        tests.append(("Working Context", f"❌ 錯誤: {e}"))
    
    for test_name, result in tests:
        print(f"  {test_name:<25}: {result}")
    
    return all("✅" in result for _, result in tests)

def test_configuration():
    """測試配置檔案"""
    print_section("配置檔案檢查")
    
    tests = []
    
    try:
        from configs.config_loader import load_config
        config = load_config()
        tests.append(("配置檔案載入", "✅"))
        
        # 檢查模組啟用狀態
        enabled_modules = config.get("modules_enabled", {})
        enabled_count = sum(1 for enabled in enabled_modules.values() if enabled)
        tests.append((f"啟用模組數量", f"✅ {enabled_count} 個"))
        
        # 檢查重構狀態
        refactored_modules = config.get("modules_refactored", {})
        refactored_count = sum(1 for refactored in refactored_modules.values() if refactored)
        tests.append((f"已重構模組數量", f"✅ {refactored_count} 個"))
        
        # 檢查除錯模式
        debug_enabled = config.get("debug", {}).get("enabled", False)
        tests.append(("除錯模式", f"✅ {'啟用' if debug_enabled else '關閉'}"))
        
        # 檢查配置一致性
        inconsistencies = []
        for module_name in enabled_modules:
            if enabled_modules.get(module_name) and not refactored_modules.get(module_name):
                inconsistencies.append(f"{module_name} (啟用但未重構)")
            elif not enabled_modules.get(module_name) and refactored_modules.get(module_name):
                inconsistencies.append(f"{module_name} (重構但未啟用)")
        
        if inconsistencies:
            tests.append(("配置一致性", f"⚠️ 不一致: {', '.join(inconsistencies)}"))
        else:
            tests.append(("配置一致性", "✅"))
            
    except Exception as e:
        tests.append(("配置檔案", f"❌ 錯誤: {e}"))
    
    for test_name, result in tests:
        print(f"  {test_name:<25}: {result}")
    
    return all("✅" in result or "⚠️" in result for _, result in tests)

def generate_report():
    """生成完整報告"""
    print(f"\n{'='*60}")
    print(f" U.E.P's Core 系統驗證報告")
    print(f" 檢查時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    results = {
        "依賴套件": test_dependencies(),
        "Core Framework": test_core_framework(),
        "Unified Controller": test_unified_controller(),
        "STT Module": test_stt_module(),
        "MEM Module": test_mem_module(),
        "Working Context": test_working_context_integration(),
        "配置檔案": test_configuration(),
    }
    
    print_section("整體檢查結果")
    
    passed = 0
    total = len(results)
    
    for test_name, success in results.items():
        status = "✅ 通過" if success else "❌ 失敗"
        print(f"  {test_name:<20}: {status}")
        if success:
            passed += 1
    
    print(f"\n總結: {passed}/{total} 項檢查通過")
    
    if passed == total:
        print("\n🎉 所有檢查都通過！系統狀態良好。")
        print("✅ 核心工作流程已經可以確實進行到 MEM 的部分")
    elif passed >= total * 0.7:
        print("\n⚠️ 大部分檢查通過，但仍有一些問題需要解決。")
        print("🔧 請參考 IMMEDIATE_FIX_PLAN.md 進行修復")
    else:
        print("\n❌ 多項檢查失敗，需要進行重大修復。")
        print("📋 請按照 IMMEDIATE_FIX_PLAN.md 逐步解決問題")
    
    return passed == total

if __name__ == "__main__":
    try:
        success = generate_report()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n⌨️ 檢查被用戶中斷")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 檢查過程中發生未預期的錯誤: {e}")
        traceback.print_exc()
        sys.exit(1)