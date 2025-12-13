"""
測試效能指標收集功能

此腳本驗證所有模組的效能指標接口是否正確實作
"""

import sys
import os

# 添加專案根目錄到 Python 路徑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.logging_helper import info_log, debug_log, error_log
from configs.config_loader import load_module_config

def test_module_performance_window(module_class, module_name):
    """測試模組的 get_performance_window 方法"""
    try:
        info_log(f"\n{'='*60}")
        info_log(f"測試 {module_name} 模組效能窗口")
        info_log(f"{'='*60}")
        
        # 載入模組配置
        config = load_module_config(module_name)
        
        # 創建模組實例
        module = module_class(config)
        
        # 檢查是否有 get_performance_window 方法
        if not hasattr(module, 'get_performance_window'):
            error_log(f"❌ {module_name} 缺少 get_performance_window 方法")
            return False
        
        # 檢查是否有 update_custom_metric 方法
        if not hasattr(module, 'update_custom_metric'):
            error_log(f"❌ {module_name} 缺少 update_custom_metric 方法")
            return False
        
        # 測試 update_custom_metric
        module.update_custom_metric('test_metric', 123)
        info_log(f"✓ update_custom_metric 測試通過")
        
        # 獲取效能窗口
        window = module.get_performance_window()
        
        # 驗證基本欄位
        required_fields = ['total_requests', 'successful_requests', 'failed_requests']
        for field in required_fields:
            if field not in window:
                error_log(f"❌ {module_name} 效能窗口缺少欄位: {field}")
                return False
        
        info_log(f"✓ 基本欄位檢查通過")
        
        # 顯示效能窗口內容
        info_log(f"\n效能窗口內容:")
        for key, value in window.items():
            if isinstance(value, dict):
                info_log(f"  {key}:")
                for sub_key, sub_value in value.items():
                    info_log(f"    {sub_key}: {sub_value}")
            else:
                info_log(f"  {key}: {value}")
        
        info_log(f"\n✅ {module_name} 模組效能指標測試通過")
        return True
        
    except Exception as e:
        error_log(f"❌ {module_name} 測試失敗: {e}")
        import traceback
        error_log(traceback.format_exc())
        return False

def main():
    """主測試函數"""
    info_log("開始測試所有模組的效能指標接口")
    info_log("="*80)
    
    # 定義要測試的模組（只測試後端模組，不測試需要 Qt 的前端模組）
    test_modules = [
        ("STTModule", "stt_module", "modules.stt_module.stt_module"),
        ("NLPModule", "nlp_module", "modules.nlp_module.nlp_module"),
        ("LLMModule", "llm_module", "modules.llm_module.llm_module"),
        ("TTSModule", "tts_module", "modules.tts_module.tts_module"),
        ("MEMModule", "mem_module", "modules.mem_module.mem_module"),
        ("SYSModule", "sys_module", "modules.sys_module.sys_module"),
    ]
    
    results = {}
    
    for class_name, module_name, import_path in test_modules:
        try:
            # 動態導入模組類別
            module_path, class_name_only = import_path.rsplit('.', 1)
            exec(f"from {module_path} import {class_name_only}")
            module_class = eval(class_name_only)
            
            # 測試模組
            results[module_name] = test_module_performance_window(module_class, module_name)
            
        except Exception as e:
            error_log(f"❌ 無法載入或測試 {module_name}: {e}")
            results[module_name] = False
    
    # 顯示總結
    info_log("\n" + "="*80)
    info_log("測試結果總結")
    info_log("="*80)
    
    passed = sum(1 for result in results.values() if result)
    total = len(results)
    
    for module_name, result in results.items():
        status = "✅ 通過" if result else "❌ 失敗"
        info_log(f"{module_name:20s} : {status}")
    
    info_log(f"\n總計: {passed}/{total} 個模組測試通過")
    
    if passed == total:
        info_log("\n🎉 所有模組效能指標測試通過！")
        return 0
    else:
        error_log(f"\n⚠️ {total - passed} 個模組測試失敗")
        return 1

if __name__ == "__main__":
    sys.exit(main())
