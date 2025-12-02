# test_system_status.py
"""測試系統狀態視窗的日誌分頁動態顯示功能"""

import sys
import os

# 添加專案根目錄到路徑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication
from modules.ui_module.user.system_status import SystemStatusWindow
from modules.ui_module.user.theme_manager import theme_manager
from configs.user_settings_manager import get_user_setting

def test_system_status():
    """測試系統狀態視窗"""
    
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    
    # 應用主題
    if theme_manager:
        theme_manager.apply_app()
    
    # 檢查當前設定
    show_logs = get_user_setting("monitoring.logs.show_logs", False)
    print(f"[測試] 當前 show_logs 設定: {show_logs}")
    
    # 創建視窗
    window = SystemStatusWindow()
    window.show()
    
    print(f"[測試] 視窗已創建，分頁數量: {window.tab_widget.count()}")
    for i in range(window.tab_widget.count()):
        print(f"  - 分頁 {i}: {window.tab_widget.tabText(i)}")
    
    # 測試設定變更
    print("\n[測試] 模擬設定變更: show_logs = True")
    window.on_settings_changed("monitoring.logs.show_logs", True)
    
    print(f"[測試] 變更後分頁數量: {window.tab_widget.count()}")
    for i in range(window.tab_widget.count()):
        print(f"  - 分頁 {i}: {window.tab_widget.tabText(i)}")
    
    print("\n[測試] 模擬設定變更: show_logs = False")
    window.on_settings_changed("monitoring.logs.show_logs", False)
    
    print(f"[測試] 變更後分頁數量: {window.tab_widget.count()}")
    for i in range(window.tab_widget.count()):
        print(f"  - 分頁 {i}: {window.tab_widget.tabText(i)}")
    
    print("\n[測試] 視窗測試完成，啟動事件循環...")
    sys.exit(app.exec_())

if __name__ == "__main__":
    test_system_status()
