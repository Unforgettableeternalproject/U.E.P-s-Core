"""
測試腳本：使用者存取小工具 (Access Widget)

這個腳本用於單獨測試浮動球體選單介面
"""
import sys
import os

# 添加專案根目錄到 Python 路徑
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from PyQt5.QtWidgets import QApplication
from modules.ui_module.user.access_widget import MainButton, ControllerBridge
from modules.ui_module.user.theme_manager import theme_manager

def main():
    """主測試函數"""
    print("=" * 60)
    print("🧪 使用者存取小工具測試")
    print("=" * 60)
    print("\n功能說明：")
    print("  - 浮動圓形按鈕（可拖曳）")
    print("  - 點擊展開放射狀選單")
    print("  - ⚙️ 使用者設定")
    print("  - 🖼️ 系統背景")
    print("  - 📊 狀態檔案")
    print("  - 🌙/☀️ 主題切換按鈕")
    print("\n操作提示：")
    print("  - 滑鼠拖曳主按鈕可移動位置")
    print("  - 游標接近螢幕邊緣時，小工具會自動滑入")
    print("  - 按 ESC 或關閉視窗退出測試")
    print("=" * 60)
    print()
    
    # 創建應用程式
    app = QApplication(sys.argv)
    
    # 應用主題
    theme_manager.apply_app()
    print(f"✅ 主題管理器已初始化：{theme_manager.theme.value} 模式")
    
    # 創建控制器橋接（模擬控制器）
    class MockController:
        """模擬控制器用於測試"""
        def process_input(self, command, data):
            print(f"[MockController] 收到命令: {command}")
            print(f"[MockController] 數據: {data}")
    
    mock_controller = MockController()
    bridge = ControllerBridge(mock_controller)
    
    print("✅ 控制器橋接已創建")
    
    # 創建並顯示主按鈕小工具
    widget = MainButton(bridge=bridge)
    widget.show()
    
    print("✅ 使用者小工具已顯示")
    print("\n🎯 測試進行中... (按 Ctrl+C 或關閉視窗結束)")
    
    # 運行事件循環
    exit_code = app.exec_()
    
    print("\n" + "=" * 60)
    print("✅ 測試結束")
    print("=" * 60)
    
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
