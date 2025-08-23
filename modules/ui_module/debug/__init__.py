# debug/__init__.py
"""
Debug Interface Package

統一的除錯介面入口點
"""

def launch_debug_interface(ui_module=None, prefer_gui=True, blocking=True):
    """
    啟動除錯介面
    
    Args:
        ui_module: UI 模組實例
        prefer_gui: 是否優先使用圖形介面
        blocking: 是否阻塞執行（對圖形介面啟動事件循環）
    
    Returns:
        除錯介面實例或 None
    """
    # 檢查運行環境 - 避免與生產環境衝突
    from configs.config_loader import load_config
    config = load_config()
    is_debug_mode = config.get("debug", {}).get("enabled", False)
    
    # 如果在生產模式但有UI模組，將進入獨立調試模式
    standalone_debug = not is_debug_mode
    if standalone_debug:
        print("⚠️ 在生產環境下啟動獨立除錯界面 - 注意資源使用")
    
    # 檢查是否已經有運行中的界面實例
    existing_interface = None
    if ui_module and hasattr(ui_module, "interfaces"):
        existing_interface = ui_module.interfaces.get("debug_interface")
        
    # 如果存在運行中界面，優先使用
    if existing_interface:
        try:
            print("使用已存在的除錯界面")
            if hasattr(existing_interface, "show"):
                existing_interface.show()
                
                # 嘗試提升窗口優先級以確保可見
                try:
                    if hasattr(existing_interface, "activateWindow"):
                        existing_interface.activateWindow()
                    if hasattr(existing_interface, "raise_"):
                        existing_interface.raise_()
                except:
                    pass
                    
                return existing_interface
        except Exception as e:
            print(f"無法重用現有介面: {e}")
            existing_interface = None
    
    if prefer_gui:
        try:
            # 設置低優先級，避免佔用太多系統資源
            try:
                import os
                if hasattr(os, 'nice'):  # Linux/Unix
                    os.nice(10)  # 調低優先級
            except:
                pass
                
            from .debug_main_window import launch_debug_interface as launch_gui
            return launch_gui(ui_module, blocking)
        except ImportError as e:
            print(f"⚠️  無法載入圖形介面: {e}")
            print("回退到命令行介面...")
        except Exception as e:
            print(f"❌ 圖形介面啟動失敗: {e}")
            print("回退到命令行介面...")
    
    # 回退到命令行介面
    try:
        # 避免循環導入，直接呼叫舊版除錯功能
        print("使用命令行除錯介面...")
        print("請使用 devtools.debugger.debug_interactive() 來存取完整功能")
        return None
    except Exception as e:
        print(f"❌ 無法載入除錯介面: {e}")
        return None


# 向後相容的導入
try:
    from .debug_main_window import DebugMainWindow
    from .module_test_tabs import *
    from .integration_test_tab import IntegrationTestTab
    from .system_monitor_tab import SystemMonitorTab
    from .log_viewer_tab import LogViewerTab
except ImportError:
    # PyQt5 未安裝時的處理
    DebugMainWindow = None
    IntegrationTestTab = None
    SystemMonitorTab = None
    LogViewerTab = None
"""
Debug Interface Package

開發用除錯介面包
"""
