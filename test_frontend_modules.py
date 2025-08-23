# test_frontend_modules.py
"""
前端模組測試腳本

測試三大前端模組 (UI, ANI, MOV) 的基本功能：
- 模組註冊
- 配置載入
- 初始化
- 基本通信
"""

import sys
import os
import traceback

# 添加項目根目錄到路徑
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from core.registry import get_module
from utils.debug_helper import debug_log, info_log, error_log

def test_module_registration():
    """測試模組註冊"""
    info_log("開始測試前端模組註冊...")
    
    modules_to_test = ["ui_module", "ani_module", "mov_module"]
    results = {}
    
    for module_name in modules_to_test:
        try:
            info_log(f"正在測試 {module_name}...")
            
            # 嘗試註冊模組
            module_instance = get_module(module_name)
            
            if module_instance is not None:
                info_log(f"✅ {module_name} 註冊成功")
                
                # 檢查模組是否有預期的方法
                expected_methods = ["initialize", "cleanup", "handle_frontend_request"]
                for method in expected_methods:
                    if hasattr(module_instance, method):
                        debug_log(1, f"  ✓ 方法 {method} 存在")
                    else:
                        debug_log(1, f"  ⚠ 方法 {method} 不存在")
                
                # 檢查配置是否載入
                if hasattr(module_instance, 'config') and module_instance.config:
                    debug_log(1, f"  ✓ 配置已載入: {len(module_instance.config)} 項設定")
                else:
                    debug_log(1, f"  ⚠ 配置未載入或為空")
                    
                results[module_name] = "成功"
            else:
                error_log(f"❌ {module_name} 註冊失敗")
                results[module_name] = "失敗"
                
        except Exception as e:
            error_log(f"❌ {module_name} 測試時發生錯誤: {e}")
            error_log(traceback.format_exc())
            results[module_name] = f"錯誤: {str(e)}"
    
    return results

def test_module_communication():
    """測試模組間通信"""
    info_log("開始測試模組間通信...")
    
    try:
        # 獲取模組實例
        ui_module = get_module("ui_module")
        ani_module = get_module("ani_module")
        mov_module = get_module("mov_module")
        
        if not all([ui_module, ani_module, mov_module]):
            error_log("無法獲取所有模組實例，跳過通信測試")
            return False
        
        # 測試基本請求處理
        test_request = {
            "command": "test",
            "data": {"message": "Hello from test script"}
        }
        
        for module_name, module in [("UI", ui_module), ("ANI", ani_module), ("MOV", mov_module)]:
            try:
                if hasattr(module, 'handle_frontend_request'):
                    response = module.handle_frontend_request(test_request)
                    info_log(f"✅ {module_name} 模組響應: {response}")
                else:
                    debug_log(1, f"⚠ {module_name} 模組沒有 handle_frontend_request 方法")
            except Exception as e:
                error_log(f"❌ {module_name} 模組通信測試失敗: {e}")
        
        return True
        
    except Exception as e:
        error_log(f"模組間通信測試失敗: {e}")
        return False

def print_test_summary(results):
    """打印測試摘要"""
    info_log("\n" + "="*50)
    info_log("前端模組測試摘要")
    info_log("="*50)
    
    for module_name, result in results.items():
        status_icon = "✅" if result == "成功" else "❌"
        info_log(f"{status_icon} {module_name}: {result}")
    
    success_count = sum(1 for r in results.values() if r == "成功")
    total_count = len(results)
    
    info_log(f"\n總計: {success_count}/{total_count} 個模組測試通過")
    
    if success_count == total_count:
        info_log("🎉 所有前端模組測試通過！")
    else:
        info_log("⚠ 部分模組測試失敗，請檢查錯誤訊息")

def main():
    """主測試函數"""
    info_log("=== 前端模組整合測試 ===")
    
    # 測試模組註冊
    registration_results = test_module_registration()
    
    # 測試模組間通信
    if any(r == "成功" for r in registration_results.values()):
        test_module_communication()
    
    # 打印摘要
    print_test_summary(registration_results)

if __name__ == "__main__":
    main()
