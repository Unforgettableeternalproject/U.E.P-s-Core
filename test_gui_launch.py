#!/usr/bin/env python3
"""
簡單的圖形除錯介面啟動測試腳本
"""

import sys
import os

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

def main():
    print("🚀 測試圖形除錯介面啟動")
    print("=" * 50)
    
    try:
        print("🔍 導入除錯介面模組...")
        from modules.ui_module.debug import launch_debug_interface
        
        print("🖥️ 啟動圖形介面...")
        print("⚠️  關閉視窗來結束測試")
        
        # 啟動介面（非阻塞模式以便測試）
        window = launch_debug_interface(ui_module=None, prefer_gui=True, blocking=False)
        
        if window:
            print("✅ 圖形介面啟動成功！")
            print("📝 視窗物件:", type(window))
            print("👁️ 視窗可見:", window.isVisible())
            
            # 手動啟動事件循環進行測試
            try:
                from PyQt5.QtWidgets import QApplication
                app = QApplication.instance()
                if app:
                    print("🔄 進入 Qt 事件循環...")
                    print("   (關閉視窗或按 Ctrl+C 來結束)")
                    app.exec_()
                else:
                    print("❌ 無法取得 QApplication 實例")
            except KeyboardInterrupt:
                print("\n⌨️ 用戶中斷")
            except Exception as e:
                print(f"❌ 事件循環異常: {e}")
                
        else:
            print("❌ 圖形介面啟動失敗")
            
    except ImportError as e:
        print(f"❌ 導入錯誤: {e}")
        print("💡 提示: 確認 PyQt5 已安裝")
    except Exception as e:
        print(f"❌ 其他錯誤: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n🏁 測試結束")

if __name__ == "__main__":
    main()
