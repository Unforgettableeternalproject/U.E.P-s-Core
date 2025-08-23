#!/usr/bin/env python3
# test_desktop_pet_mock.py
"""
測試 DesktopPetApp 的模擬環境
"""

import sys
import os

# 添加項目根目錄到 Python 路徑
sys.path.insert(0, os.path.abspath('.'))

# 強制模擬 PyQt5 導入失敗
sys.modules['PyQt5'] = None
sys.modules['PyQt5.QtWidgets'] = None
sys.modules['PyQt5.QtCore'] = None
sys.modules['PyQt5.QtGui'] = None

try:
    from modules.ui_module.main.desktop_pet_app import DesktopPetApp
    
    print("測試開始...")
    app = DesktopPetApp()
    print("DesktopPetApp 創建成功")
    
    print("測試 set_opacity...")
    try:
        app.set_opacity('0.8')
    except Exception as e:
        print(f"set_opacity 詳細錯誤: {e}")
        import traceback
        traceback.print_exc()
    print("set_opacity 測試完成")
    
    print("測試 set_size...")
    try:
        app.set_size('300', '300')
    except Exception as e:
        print(f"set_size 詳細錯誤: {e}")
        import traceback
        traceback.print_exc()
    print("set_size 測試完成")
    
    print("所有測試完成")
    
except Exception as e:
    print(f"測試出現異常: {e}")
    import traceback
    traceback.print_exc()
