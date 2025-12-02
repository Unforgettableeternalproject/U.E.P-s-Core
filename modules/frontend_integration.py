# modules/frontend_integration.py
"""
前端整合器 - 協調 UI、ANI、MOV 三個前端模組

負責：
- 初始化和管理三個前端模組
- 處理模組間的通信和協調
- 提供統一的前端介面
- 與核心框架整合
"""

import os
import sys
import time
import threading
from typing import Dict, Any, Optional

from core.bases.frontend_base import FrontendAdapter, BaseFrontendModule
from core.framework import CoreFramework
from utils.debug_helper import debug_log, info_log, error_log

# 導入前端模組
from modules.ui_module.ui_module import UIModule
from modules.ani_module.ani_module import ANIModule
from modules.mov_module.mov_module import MOVModule


class FrontendIntegrator:
    """
    前端整合器
    
    管理 UI、ANI、MOV 三個前端模組的生命周期和協調
    """
    
    def __init__(self, framework: CoreFramework, config: dict = None):
        self.framework = framework
        self.config = config or {}
        
        # 前端模組實例
        self.ui_module = None
        self.ani_module = None
        self.mov_module = None
        
        # 前端適配器
        self.frontend_adapter = None
        
        # 狀態
        self.is_initialized = False
        self.is_running = False
        
        info_log("[FrontendIntegrator] 前端整合器初始化")
    
    def initialize(self) -> bool:
        """初始化前端整合器"""
        try:
            # 創建前端適配器
            self.frontend_adapter = FrontendAdapter(self.framework)
            
            # 初始化前端模組
            if not self._initialize_modules():
                error_log("[FrontendIntegrator] 前端模組初始化失敗")
                return False
            
            # 註冊模組到適配器
            if not self._register_modules():
                error_log("[FrontendIntegrator] 前端模組註冊失敗")
                return False
            
            # 設置模組間連接
            self._setup_module_connections()
            
            # 啟動前端事件循環
            self.frontend_adapter.start_event_loop()
            
            self.is_initialized = True
            info_log("[FrontendIntegrator] 前端整合器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[FrontendIntegrator] 初始化失敗: {e}")
            return False
    
    def _initialize_modules(self) -> bool:
        """初始化前端模組
        
        注意：UI 模組會在初始化時自動創建和管理 ANI 和 MOV 模組，
        所以這裡只需要初始化 UI 模組即可。
        """
        try:
            # 創建並初始化 UI 模組
            # UI 模組會自動處理 ANI 和 MOV 的創建和連接
            ui_config = self.config.get('ui_module', {})
            self.ui_module = UIModule(ui_config)
            
            if not self.ui_module.initialize():
                error_log("[FrontendIntegrator] UI 模組初始化失敗")
                return False
            
            # 從 UI 模組獲取 ANI 和 MOV 模組引用
            self.ani_module = self.ui_module.ani_module
            self.mov_module = self.ui_module.mov_module
            
            if not self.ani_module or not self.mov_module:
                error_log("[FrontendIntegrator] 無法從 UI 模組獲取 ANI/MOV 引用")
                return False
            
            info_log("[FrontendIntegrator] 所有前端模組初始化完成")
            info_log(f"[FrontendIntegrator] UI 模組: {type(self.ui_module).__name__}")
            info_log(f"[FrontendIntegrator] ANI 模組: {type(self.ani_module).__name__}")
            info_log(f"[FrontendIntegrator] MOV 模組: {type(self.mov_module).__name__}")
            return True
            
        except Exception as e:
            error_log(f"[FrontendIntegrator] 模組初始化異常: {e}")
            import traceback
            error_log(traceback.format_exc())
            return False
    
    def _register_modules(self) -> bool:
        """註冊前端模組到適配器"""
        try:
            # 註冊 UI 模組
            if not self.frontend_adapter.register_frontend_module(self.ui_module):
                error_log("[FrontendIntegrator] UI 模組註冊失敗")
                return False
            
            # 註冊 ANI 模組
            if not self.frontend_adapter.register_frontend_module(self.ani_module):
                error_log("[FrontendIntegrator] ANI 模組註冊失敗")
                return False
            
            # 註冊 MOV 模組
            if not self.frontend_adapter.register_frontend_module(self.mov_module):
                error_log("[FrontendIntegrator] MOV 模組註冊失敗")
                return False
            
            info_log("[FrontendIntegrator] 所有前端模組註冊完成")
            return True
            
        except Exception as e:
            error_log(f"[FrontendIntegrator] 模組註冊異常: {e}")
            return False
    
    def _setup_module_connections(self):
        """設置模組間連接
        
        注意：UI 模組在初始化時已經完成了所有模組間的連接：
        - ANI 已注入到 MOV
        - MOV 和 ANI 已傳遞給 DesktopPetApp
        - 所有內部引用都已建立
        
        不需要額外的連接設置。
        """
        try:
            info_log("[FrontendIntegrator] 模組間連接已在 UI 初始化時完成")
            
        except Exception as e:
            error_log(f"[FrontendIntegrator] 設置模組連接異常: {e}")
    
    def start(self) -> bool:
        """啟動前端系統"""
        try:
            if not self.is_initialized:
                error_log("[FrontendIntegrator] 尚未初始化，無法啟動")
                return False
            
            self.is_running = True
            info_log("[FrontendIntegrator] 前端系統啟動成功")
            return True
                
        except Exception as e:
            error_log(f"[FrontendIntegrator] 啟動異常: {e}")
            return False
    
    def stop(self):
        """停止前端系統"""
        try:
            if self.is_running:
                # 隱藏主視窗
                self.ui_module.handle_frontend_request({
                    'command': 'hide_window'
                })
                
                self.is_running = False
                info_log("[FrontendIntegrator] 前端系統已停止")
                
        except Exception as e:
            error_log(f"[FrontendIntegrator] 停止異常: {e}")
    
    def shutdown(self):
        """關閉前端系統"""
        try:
            # 停止運行
            self.stop()
            
            # 停止前端事件循環
            if self.frontend_adapter:
                self.frontend_adapter.stop_event_loop()
            
            # 關閉所有模組
            if self.ui_module:
                self.ui_module.shutdown()
            if self.ani_module:
                self.ani_module.shutdown()
            if self.mov_module:
                self.mov_module.shutdown()
            
            # 關閉適配器
            if self.frontend_adapter:
                self.frontend_adapter.shutdown()
            
            self.is_initialized = False
            info_log("[FrontendIntegrator] 前端系統已關閉")
            
        except Exception as e:
            error_log(f"[FrontendIntegrator] 關閉異常: {e}")
    
    def get_frontend_status(self) -> Dict[str, Any]:
        """獲取前端狀態"""
        try:
            ui_status = self.ui_module.handle_frontend_request({'command': 'get_window_info'})
            ani_status = self.ani_module.handle_frontend_request({'command': 'get_current_animation'})
            mov_status = self.mov_module.handle_frontend_request({'command': 'get_status'})
            
            return {
                "is_initialized": self.is_initialized,
                "is_running": self.is_running,
                "ui_module": ui_status,
                "ani_module": ani_status,
                "mov_module": mov_status
            }
            
        except Exception as e:
            error_log(f"[FrontendIntegrator] 獲取狀態異常: {e}")
            return {"error": str(e)}
    
    def execute_frontend_command(self, module: str, command: dict) -> dict:
        """執行前端命令"""
        try:
            if module == "ui" and self.ui_module:
                return self.ui_module.handle_frontend_request(command)
            elif module == "ani" and self.ani_module:
                return self.ani_module.handle_frontend_request(command)
            elif module == "mov" and self.mov_module:
                return self.mov_module.handle_frontend_request(command)
            else:
                return {"error": f"未知模組或模組未初始化: {module}"}
                
        except Exception as e:
            error_log(f"[FrontendIntegrator] 執行命令異常: {e}")
            return {"error": str(e)}
    
    # ========== 事件處理器 ==========
    
    def _on_animation_finished(self, animation_name: str):
        """動畫完成回調"""
        debug_log(2, f"[FrontendIntegrator] 動畫完成: {animation_name}")
        
        # 根據動畫完成情況執行後續動作
        if animation_name == "turn_head":
            # 轉頭完成後回到正常動畫
            self.ani_module.handle_animation_request("static", {})
    
    # ========== 便利方法 ==========
    
    def show_pet(self):
        """顯示桌寵"""
        return self.execute_frontend_command("ui", {"command": "show_window"})
    
    def hide_pet(self):
        """隱藏桌寵"""
        return self.execute_frontend_command("ui", {"command": "hide_window"})
    
    def move_pet(self, x: int, y: int):
        """移動桌寵"""
        return self.execute_frontend_command("mov", {
            "command": "set_position",
            "x": x,
            "y": y
        })
    
    def play_animation(self, animation_type: str):
        """播放動畫"""
        return self.execute_frontend_command("ani", {
            "command": "play_animation",
            "animation_type": animation_type
        })
    
    def set_behavior(self, behavior: str):
        """設置行為"""
        return self.execute_frontend_command("mov", {
            "command": "set_behavior",
            "behavior": behavior
        })
    
    def enable_behavior(self, behavior_name: str, enabled: bool = True):
        """啟用/停用行為"""
        return self.execute_frontend_command("mov", {
            "command": "enable_behavior",
            "behavior_name": behavior_name,
            "enabled": enabled
        })


def create_frontend_system(framework: CoreFramework, config: dict = None) -> FrontendIntegrator:
    """
    創建前端系統的便利函數
    
    Args:
        framework: 核心框架實例
        config: 前端配置
        
    Returns:
        前端整合器實例
    """
    integrator = FrontendIntegrator(framework, config)
    return integrator


# ========== 測試和示例代碼 ==========

def test_frontend_integration():
    """測試前端整合功能"""
    try:
        # 這裡需要一個核心框架實例
        from core.framework import CoreFramework
        from core.working_context import WorkingContextManager
        from core.states.state_manager import StateManager
        
        # 創建核心組件
        context_manager = WorkingContextManager()
        state_manager = StateManager()
        framework = CoreFramework(context_manager, state_manager)
        
        # 創建前端系統
        frontend_config = {
            'ui_module': {
                'window_size': 250
            },
            'ani_module': {
                'frame_interval': 100
            },
            'mov_module': {
                'ground_speed': 2.2,
                'walking_enabled': True,
                'floating_enabled': True
            }
        }
        
        integrator = create_frontend_system(framework, frontend_config)
        
        # 初始化和啟動
        if integrator.initialize():
            info_log("前端系統初始化成功")
            
            if integrator.start():
                info_log("前端系統啟動成功")
                
                # 測試一些功能
                status = integrator.get_frontend_status()
                info_log(f"前端狀態: {status}")
                
                # 播放動畫
                integrator.play_animation("talking")
                
                # 等待一會兒
                time.sleep(5)
                
                # 關閉
                integrator.shutdown()
                info_log("前端系統測試完成")
            else:
                error_log("前端系統啟動失敗")
        else:
            error_log("前端系統初始化失敗")
            
    except Exception as e:
        error_log(f"前端整合測試異常: {e}")


if __name__ == "__main__":
    test_frontend_integration()
