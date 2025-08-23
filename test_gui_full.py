#!/usr/bin/env python3
"""
完整的圖形除錯介面啟動測試
"""

import sys
import os

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

def main():
    print("🚀 啟動完整圖形除錯介面")
    print("=" * 50)
    
    try:
        print("🔍 導入除錯介面模組...")
        from modules.ui_module.debug import launch_debug_interface
        
        print("🖥️ 啟動圖形介面（完整模式）...")
        print("⚠️  關閉視窗來結束程式")
        
        # 啟動介面（阻塞模式，含事件循環）
        launch_debug_interface(ui_module=None, prefer_gui=True, blocking=True)
        
    except KeyboardInterrupt:
        print("\n⌨️ 用戶中斷")
    except ImportError as e:
        print(f"❌ 導入錯誤: {e}")
        print("💡 提示: 確認 PyQt5 已安裝")
    except Exception as e:
        print(f"❌ 其他錯誤: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n🏁 程式結束")

if __name__ == "__main__":
    main()
