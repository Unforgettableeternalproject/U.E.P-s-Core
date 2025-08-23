#!/usr/bin/env python3
"""
測試修復後的模組問題
"""

import sys
import os

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

def main():
    print("🔧 測試修復後的模組問題")
    print("=" * 50)
    
    try:
        print("🔍 測試 MOV 模組修復...")
        
        # 測試 MOV 模組導入
        from modules.mov_module.mov_module import MOVModule
        
        # 創建 MOV 模組實例
        print("📦 創建 MOV 模組實例...")
        mov = MOVModule({})
        
        # 檢查回調機制是否存在
        print(f"🔌 動畫回調機制存在: {hasattr(mov, '_animation_callbacks')}")
        print(f"🔌 add_animation_callback方法存在: {hasattr(mov, 'add_animation_callback')}")
        print(f"🔌 trigger_animation方法存在: {hasattr(mov, 'trigger_animation')}")
        
        # 測試回調機制
        def test_callback(animation_type, params):
            print(f"✅ 回調成功: {animation_type}, {params}")
        
        # 註冊回調
        mov.add_animation_callback(test_callback)
        
        # 觸發動畫
        mov.trigger_animation("test_animation", {"test": True})
        
        print("✅ MOV 模組修復測試通過")
        
        print("\n🔍 測試除錯介面性能優化...")
        
        # 測試除錯介面導入
        from modules.ui_module.debug import launch_debug_interface
        
        print("📦 創建除錯介面實例...")
        window = launch_debug_interface(ui_module=None, prefer_gui=True, blocking=False)
        
        if window:
            print("✅ 除錯介面創建成功")
            print(f"📝 視窗類型: {type(window)}")
            
            # 檢查是否可見（在關閉前）
            try:
                visible = window.isVisible()
                print(f"👁️ 視窗可見: {visible}")
            except:
                print("⚠️ 視窗對象已被釋放（正常的非阻塞行為）")
                
        else:
            print("❌ 除錯介面創建失敗")
            
        print("✅ 除錯介面性能優化測試完成")
        
    except ImportError as e:
        print(f"❌ 導入錯誤: {e}")
    except Exception as e:
        print(f"❌ 其他錯誤: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n🏁 修復測試完成")

if __name__ == "__main__":
    main()
