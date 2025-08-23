# test_debug_interface.py
"""
除錯介面測試腳本
"""

import sys
import os

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log


def test_debug_interface_import():
    """測試除錯介面導入"""
    print("=== 除錯介面導入測試 ===")
    
    try:
        from modules.ui_module.debug import launch_debug_interface
        print("✅ launch_debug_interface 導入成功")
    except ImportError as e:
        print(f"❌ launch_debug_interface 導入失敗: {e}")
        return False
    
    try:
        from modules.ui_module.debug.debug_main_window import DebugMainWindow
        print("✅ DebugMainWindow 導入成功")
    except ImportError as e:
        print(f"❌ DebugMainWindow 導入失敗: {e}")
        return False
    
    try:
        from modules.ui_module.debug.module_test_tabs import BaseTestTab, STTTestTab
        print("✅ 模組測試分頁導入成功")
    except ImportError as e:
        print(f"❌ 模組測試分頁導入失敗: {e}")
        return False
    
    try:
        from modules.ui_module.debug.integration_test_tab import IntegrationTestTab
        print("✅ 整合測試分頁導入成功")
    except ImportError as e:
        print(f"❌ 整合測試分頁導入失敗: {e}")
        return False
    
    try:
        from modules.ui_module.debug.system_monitor_tab import SystemMonitorTab
        print("✅ 系統監控分頁導入成功")
    except ImportError as e:
        print(f"❌ 系統監控分頁導入失敗: {e}")
        return False
    
    try:
        from modules.ui_module.debug.log_viewer_tab import LogViewerTab
        print("✅ 日誌檢視分頁導入成功")
    except ImportError as e:
        print(f"❌ 日誌檢視分頁導入失敗: {e}")
        return False
    
    return True


def test_pyqt5_availability():
    """測試 PyQt5 可用性"""
    print("\n=== PyQt5 可用性測試 ===")
    
    try:
        from PyQt5.QtWidgets import QApplication, QMainWindow
        from PyQt5.QtCore import Qt, QTimer
        from PyQt5.QtGui import QFont
        print("✅ PyQt5 可用")
        return True
    except ImportError as e:
        print(f"❌ PyQt5 不可用: {e}")
        return False


def test_debug_interface_creation():
    """測試除錯介面建立"""
    print("\n=== 除錯介面建立測試 ===")
    
    pyqt5_available = test_pyqt5_availability()
    
    if not pyqt5_available:
        print("⚠️  PyQt5 不可用，跳過圖形介面測試")
        return True
    
    try:
        from PyQt5.QtWidgets import QApplication
        from modules.ui_module.debug.debug_main_window import DebugMainWindow
        
        # 建立 QApplication 實例（如果不存在）
        app = QApplication.instance()
        if app is None:
            app = QApplication([])
        
        # 不實際顯示視窗，只測試建立
        debug_window = DebugMainWindow()
        print("✅ DebugMainWindow 建立成功")
        
        # 測試基本屬性
        if hasattr(debug_window, 'tab_widget'):
            print("✅ 標籤頁小工具已建立")
        
        if hasattr(debug_window, 'test_tabs'):
            print(f"✅ 測試分頁已建立 ({len(debug_window.test_tabs)} 個)")
        
        # 清理
        debug_window.close()
        
        return True
        
    except Exception as e:
        print(f"❌ 除錯介面建立失敗: {e}")
        return False


def test_launch_function():
    """測試啟動函數"""
    print("\n=== 啟動函數測試 ===")
    
    try:
        from modules.ui_module.debug import launch_debug_interface
        
        # 測試函數可呼叫性（不實際啟動）
        print("✅ launch_debug_interface 函數可用")
        
        # 測試參數處理
        result = launch_debug_interface(prefer_gui=False)
        print("✅ 非圖形模式測試完成")
        
        return True
        
    except Exception as e:
        print(f"❌ 啟動函數測試失敗: {e}")
        return False


def main():
    """主測試函數"""
    print("🚀 開始除錯介面測試\n")
    
    tests = [
        ("導入測試", test_debug_interface_import),
        ("PyQt5 測試", test_pyqt5_availability),
        ("介面建立測試", test_debug_interface_creation),
        ("啟動函數測試", test_launch_function)
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        try:
            if test_func():
                passed += 1
                print(f"✅ {test_name} 通過")
            else:
                print(f"❌ {test_name} 失敗")
        except Exception as e:
            print(f"❌ {test_name} 異常: {e}")
        
        print("-" * 50)
    
    print(f"\n📊 測試結果: {passed}/{total} 通過")
    
    if passed == total:
        print("🎉 所有測試通過！除錯介面系統準備就緒")
        
        # 提供使用說明
        print("\n📖 使用方式:")
        print("1. 從 Python 腳本中:")
        print("   from modules.ui_module.debug import launch_debug_interface")
        print("   window = launch_debug_interface()")
        
        print("\n2. 從 Entry.py 中:")
        print("   添加參數 --debug-gui 啟動圖形除錯介面")
        
        print("\n3. 從舊版 debugger.py 中:")
        print("   輸入 'gui' 切換到圖形介面")
        
    else:
        print("⚠️  部分測試失敗，請檢查問題並修復")
    
    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
