# -*- coding: utf-8 -*-
"""
前端模組測試分頁
統合 UI、ANI、MOV 模組的測試功能
"""

import sys
import os
import json
from typing import Dict, Any, Optional, List
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *

# 添加當前目錄以導入本地模組
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from base_test_tab import BaseTestTab


class FrontendTestTab(BaseTestTab):
    """
    前端測試分頁
    統合 UI、ANI、MOV 模組的測試功能
    """
    
    def __init__(self):
        super().__init__("frontend")
        self.MODULE_DISPLAY_NAME = "FRONTEND"
        self.module_display_name = "Frontend (UI+ANI+MOV)"
    
    def create_control_section(self, main_layout):
        """建立前端控制區域"""
        control_group = QGroupBox("Frontend 測試控制")
        control_layout = QVBoxLayout(control_group)
        
        # UI 模組區域
        ui_group = QGroupBox("🎨 UI 模組測試")
        ui_layout = QVBoxLayout(ui_group)
        
        # UI 基本操作按鈕
        ui_buttons_layout = QHBoxLayout()
        
        show_app_btn = QPushButton("🎈 顯示 UEP 主程式")
        show_app_btn.clicked.connect(self.show_uep_app)
        ui_buttons_layout.addWidget(show_app_btn)
        
        hide_app_btn = QPushButton("👻 隱藏 UEP 主程式")
        hide_app_btn.clicked.connect(self.hide_uep_app)
        ui_buttons_layout.addWidget(hide_app_btn)
        
        ui_layout.addLayout(ui_buttons_layout)
        
        # UI 控制操作
        ui_control_layout = QHBoxLayout()
        
        move_center_btn = QPushButton("📍 移動到中央")
        move_center_btn.clicked.connect(self.move_to_center)
        ui_control_layout.addWidget(move_center_btn)
        
        test_ui_btn = QPushButton("🔍 測試 UI 介面")
        test_ui_btn.clicked.connect(self.test_ui_interfaces)
        ui_control_layout.addWidget(test_ui_btn)
        
        ui_layout.addLayout(ui_control_layout)
        control_layout.addWidget(ui_group)
        
        # ANI 模組區域
        ani_group = QGroupBox("🎬 ANI 模組測試")
        ani_layout = QVBoxLayout(ani_group)
        
        ani_buttons_layout = QHBoxLayout()
        
        play_ani_btn = QPushButton("▶️ 播放動畫")
        play_ani_btn.clicked.connect(self.play_animation)
        ani_buttons_layout.addWidget(play_ani_btn)
        
        stop_ani_btn = QPushButton("⏹️ 停止動畫")
        stop_ani_btn.clicked.connect(self.stop_animation)
        ani_buttons_layout.addWidget(stop_ani_btn)
        
        check_ani_btn = QPushButton("📊 動畫狀態")
        check_ani_btn.clicked.connect(self.check_animation_status)
        ani_buttons_layout.addWidget(check_ani_btn)
        
        ani_layout.addLayout(ani_buttons_layout)
        control_layout.addWidget(ani_group)
        
        # MOV 模組區域
        mov_group = QGroupBox("🚀 MOV 模組測試")
        mov_layout = QVBoxLayout(mov_group)
        
        mov_buttons_layout = QHBoxLayout()
        
        execute_mov_btn = QPushButton("🎯 執行移動")
        execute_mov_btn.clicked.connect(self.execute_movement)
        mov_buttons_layout.addWidget(execute_mov_btn)
        
        check_mov_btn = QPushButton("📍 移動狀態")
        check_mov_btn.clicked.connect(self.check_movement_status)
        mov_buttons_layout.addWidget(check_mov_btn)
        
        mov_layout.addLayout(mov_buttons_layout)
        control_layout.addWidget(mov_group)
        
        # 整合測試區域
        integration_group = QGroupBox("🔗 整合測試")
        integration_layout = QVBoxLayout(integration_group)
        
        integration_buttons_layout = QHBoxLayout()
        
        full_test_btn = QPushButton("🚀 完整前端測試")
        full_test_btn.clicked.connect(self.run_full_frontend_test)
        integration_buttons_layout.addWidget(full_test_btn)
        
        combo_test_btn = QPushButton("🎭 動畫+移動組合")
        combo_test_btn.clicked.connect(self.test_animation_movement_combo)
        integration_buttons_layout.addWidget(combo_test_btn)
        
        sync_test_btn = QPushButton("⚡ UI 同步測試")
        sync_test_btn.clicked.connect(self.test_ui_sync)
        integration_buttons_layout.addWidget(sync_test_btn)
        
        integration_layout.addLayout(integration_buttons_layout)
        control_layout.addWidget(integration_group)
        
        main_layout.addWidget(control_group)
    
    def create_status_section(self, main_layout):
        """建立狀態顯示區域"""
        status_group = QGroupBox("📊 模組狀態")
        status_layout = QVBoxLayout(status_group)
        
        # 模組狀態顯示
        self.ui_status_label = QLabel("UI 模組: 檢查中...")
        self.ani_status_label = QLabel("ANI 模組: 檢查中...")
        self.mov_status_label = QLabel("MOV 模組: 檢查中...")
        
        status_layout.addWidget(self.ui_status_label)
        status_layout.addWidget(self.ani_status_label)
        status_layout.addWidget(self.mov_status_label)
        
        # 狀態重新整理按鈕
        refresh_status_btn = QPushButton("🔄 重新整理狀態")
        refresh_status_btn.clicked.connect(self.refresh_status)
        status_layout.addWidget(refresh_status_btn)
        
        # 模組管理按鈕
        module_management_layout = QHBoxLayout()
        
        load_modules_btn = QPushButton("📥 載入前端模組")
        load_modules_btn.clicked.connect(self.load_frontend_modules)
        module_management_layout.addWidget(load_modules_btn)
        
        unload_modules_btn = QPushButton("📤 卸載前端模組")
        unload_modules_btn.clicked.connect(self.unload_frontend_modules)
        module_management_layout.addWidget(unload_modules_btn)
        
        status_layout.addLayout(module_management_layout)
        
        main_layout.addWidget(status_group)
    
    def get_available_tests(self) -> Dict[str, str]:
        """取得可用的測試功能列表"""
        return {
            "ui_show_test": "UI 顯示測試",
            "ui_hide_test": "UI 隱藏測試", 
            "ui_control_test": "UI 控制測試",
            "ani_play_test": "動畫播放測試",
            "ani_stop_test": "動畫停止測試",
            "mov_execute_test": "移動執行測試",
            "frontend_test_full": "完整前端測試",
            "frontend_integration_test": "前端整合測試"
        }
    
    def refresh_status(self):
        """重新整理模組狀態"""
        self.add_result("🔄 重新整理前端模組狀態...", "INFO")
        
        # 檢查各個模組狀態
        ui_status = self.check_individual_module_status("ui")
        ani_status = self.check_individual_module_status("ani")
        mov_status = self.check_individual_module_status("mov")
        
        # 更新狀態標籤
        self.ui_status_label.setText(f"UI 模組: {ui_status}")
        self.ani_status_label.setText(f"ANI 模組: {ani_status}")
        self.mov_status_label.setText(f"MOV 模組: {mov_status}")
        
        # 根據個別模組狀態決定 Frontend 整體狀態
        all_loaded = ui_status == "已載入" and ani_status == "已載入" and mov_status == "已載入"
        overall_status = "已載入" if all_loaded else "部分載入"
        
        self.add_result(f"📊 前端模組狀態更新完成 - 整體狀態: {overall_status}", "INFO")
        
        # 如果不是全部載入，提供詳細資訊
        if not all_loaded:
            missing_modules = []
            if ui_status != "已載入":
                missing_modules.append("UI")
            if ani_status != "已載入": 
                missing_modules.append("ANI")
            if mov_status != "已載入":
                missing_modules.append("MOV")
            
            self.add_result(f"⚠️  未載入模組: {', '.join(missing_modules)}", "WARNING")
    
    def check_individual_module_status(self, module_name: str) -> str:
        """檢查個別模組狀態"""
        try:
            status = self.module_manager.get_module_status(module_name)
            if status.get('loaded', False):
                return "已載入"
            elif status.get('status') == 'disabled':
                return "已禁用"
            else:
                return "未載入"
        except Exception as e:
            return f"錯誤: {str(e)}"
    
    def check_ui_status(self):
        """檢查 UI 模組狀態"""
        self.add_result("🔍 檢查 UI 模組狀態...", "INFO")
        self.check_module_status_and_report("ui", "UI")
    
    def test_ui_interfaces(self):
        """測試 UI 介面"""
        self.add_result("🔍 測試 UI 介面功能...", "INFO")
        # TODO: 實現 UI 介面測試
        self.add_result("🚧 UI 介面測試功能開發中...", "WARNING")
    
    def test_access_widget(self):
        """測試訪問 Widget"""
        self.add_result("🔍 測試 Widget 訪問...", "INFO")
        # TODO: 實現 Widget 訪問測試
        self.add_result("🚧 Widget 訪問測試功能開發中...", "WARNING")
    
    def play_animation(self):
        """播放動畫"""
        try:
            self.add_result("▶️ 播放動畫...", "INFO")
            
            # 修正 background_worker 導入路徑
            import sys
            import os
            debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
            if debug_dir not in sys.path:
                sys.path.insert(0, debug_dir)
            
            from background_worker import get_worker_manager
            worker_manager = get_worker_manager()
            
            def run_ani_test_task():
                try:
                    # 使用 frontend 測試函數而不是直接調用 ani 模組
                    return self.module_manager.run_test_function("frontend", "frontend_test_animations", {})
                except Exception as e:
                    return {"success": False, "error": str(e)}
            
            task_id = "ani_play_test_" + str(id(self))
            worker_manager.start_task(task_id, run_ani_test_task)
            self.add_result("🔄 動畫播放測試正在背景執行，請稍候...", "INFO")
        except Exception as e:
            self.add_result(f"播放動畫時發生錯誤: {str(e)}", "ERROR")
    
    def stop_animation(self):
        """停止動畫"""
        self.add_result("⏹️ 停止動畫...", "INFO")
        # TODO: 實現動畫停止功能
        self.add_result("🚧 動畫停止功能開發中...", "WARNING")
    
    def check_animation_status(self):
        """檢查動畫狀態"""
        self.add_result("📊 檢查動畫狀態...", "INFO")
        self.check_module_status_and_report("ani", "ANI")
    
    def execute_movement(self):
        """執行移動"""
        self.add_result("🎯 執行移動...", "INFO")
        
        # 獲取移動參數
        params = {
            "action": "wave",  # 使用 control_desktop_pet 的動作參數
            "duration": 3  # 持續時間
        }
        
        try:
            # 使用 frontend 測試函數
            result = self.module_manager.run_test_function("frontend", "control_desktop_pet", params)
            
            if result.get('success', False):
                self.add_result("✅ 移動執行成功", "SUCCESS")
            else:
                self.add_result(f"❌ 移動執行失敗: {result.get('error', '未知錯誤')}", "ERROR")
        except Exception as e:
            self.add_result(f"執行移動時發生錯誤: {str(e)}", "ERROR")
    
    def move_to_center(self):
        """移動到螢幕中央"""
        self.add_result("📍 移動UEP到螢幕中央...", "INFO")
        try:
            # 獲取螢幕尺寸並計算中央位置
            from PyQt5.QtWidgets import QDesktopWidget
            desktop = QDesktopWidget()
            screen_geometry = desktop.screenGeometry()
            
            # 計算中央位置 (假設UEP大小為240x240)
            uep_size = 240
            center_x = (screen_geometry.width() - uep_size) // 2
            center_y = (screen_geometry.height() - uep_size) // 2
            
            # 直接通過UI模組來移動桌面寵物
            result = self.module_manager.run_test_function("frontend", "control_desktop_pet", {
                "action": "move_window",
                "x": center_x,
                "y": center_y
            })
            
            if result.get('success', False):
                self.add_result(f"✅ UEP已移動到中央位置 ({center_x}, {center_y})", "SUCCESS")
            else:
                self.add_result(f"❌ 移動到中央失敗: {result.get('error', '未知錯誤')}", "ERROR")
                
        except Exception as e:
            self.add_result(f"移動到中央時發生錯誤: {str(e)}", "ERROR")
    
    def check_movement_status(self):
        """檢查移動狀態"""
        self.add_result("📍 檢查移動狀態...", "INFO")
        self.check_module_status_and_report("mov", "MOV")
    
    # 整合測試方法
    def run_full_frontend_test(self):
        """執行完整前端測試"""
        self.add_result("🚀 啟動完整前端測試...", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        def run_full_test_task():
            try:
                return self.module_manager.run_test_function("frontend", "frontend_test_full", {})
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        # 設置任務完成後的回調
        def on_task_complete(task_id, result):
            if task_id != "frontend_full_test_" + str(id(self)):
                return
                
            if result.get('success', False):
                self.add_result(f"✅ 完整前端測試完成", "SUCCESS")
                if 'results' in result:
                    for sub_result in result['results']:
                        self.add_result(f"  └─ {sub_result}", "INFO")
            else:
                self.add_result(f"❌ 完整前端測試失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        task_id = "frontend_full_test_" + str(id(self))
        worker_manager.start_task(task_id, run_full_test_task)
        worker_manager.set_callback(task_id, on_task_complete)
        self.add_result("🔄 完整前端測試正在背景執行，請稍候...", "INFO")
    
    def test_animation_movement_combo(self):
        """測試動畫+移動組合"""
        self.add_result("🎭 測試動畫+移動組合...", "INFO")
        
        try:
            # 先播放動畫
            self.add_result("  ├─ 步驟 1: 啟動動畫", "INFO")
            ani_result = self.module_manager.run_test_function("frontend", "frontend_test_animations", {})
            
            if ani_result.get('success', False):
                self.add_result("  ├─ 動畫啟動成功", "SUCCESS")
                
                # 然後執行移動
                self.add_result("  ├─ 步驟 2: 執行移動", "INFO")
                mov_result = self.module_manager.run_test_function("frontend", "test_mov_ani_integration", {})
                
                if mov_result.get('success', False):
                    self.add_result("  └─ 組合測試完成", "SUCCESS")
                else:
                    self.add_result(f"  └─ 移動失敗: {mov_result.get('error', '未知錯誤')}", "ERROR")
            else:
                self.add_result(f"  └─ 動畫啟動失敗: {ani_result.get('error', '未知錯誤')}", "ERROR")
        except Exception as e:
            self.add_result(f"組合測試時發生錯誤: {str(e)}", "ERROR")
    
    def test_ui_sync(self):
        """測試 UI 同步"""
        self.add_result("⚡ 測試 UI 同步功能...", "INFO")
        # TODO: 實現 UI 同步測試
        self.add_result("🚧 UI 同步測試功能開發中...", "WARNING")
    
    def load_module(self):
        """載入模組"""
        self.add_result(f"🔄 載入 Frontend 模組群組...", "INFO")
        
        # 分別載入 UI、ANI、MOV 模組
        modules_to_load = ["ui"]
        success_count = 0
        
        for module_name in modules_to_load:
            try:
                result = self.module_manager.load_module(module_name)
                if result.get('success', False):
                    self.add_result(f"✅ {module_name.upper()} 模組載入成功", "SUCCESS")
                    success_count += 1
                else:
                    self.add_result(f"❌ {module_name.upper()} 模組載入失敗: {result.get('error', '未知錯誤')}", "ERROR")
            except Exception as e:
                self.add_result(f"❌ {module_name.upper()} 模組載入異常: {str(e)}", "ERROR")
        
        # 總結載入結果
        if success_count == len(modules_to_load):
            self.add_result(f"🎉 Frontend 模組群組載入完成 ({success_count}/{len(modules_to_load)})", "SUCCESS")
        else:
            self.add_result(f"⚠️  Frontend 模組群組部分載入 ({success_count}/{len(modules_to_load)})", "WARNING")
        
        # 更新狀態
        self.refresh_status()
    
    def check_module_status_and_report(self, module_name: str, display_name: str):
        """檢查模組狀態並報告"""
        try:
            status = self.module_manager.get_module_status(module_name)
            if status.get('loaded', False):
                self.add_result(f"✅ {display_name} 模組已載入", "SUCCESS")
                if 'instance' in status:
                    self.add_result(f"  └─ 實例類型: {type(status['instance']).__name__}", "INFO")
            elif status.get('status') == 'disabled':
                self.add_result(f"⚠️  {display_name} 模組已禁用", "WARNING")
            else:
                self.add_result(f"❌ {display_name} 模組未載入", "ERROR")
                if 'error' in status:
                    self.add_result(f"  └─ 錯誤: {status['error']}", "ERROR")
        except Exception as e:
            self.add_result(f"❌ 檢查 {display_name} 模組狀態時發生錯誤: {str(e)}", "ERROR")

    # === UEP 主程式控制方法 ===
    
    def show_uep_app(self):
        """顯示 UEP 主程式"""
        try:
            self.add_result("🎈 顯示 UEP 主程式...", "INFO")
            
            # 使用 debug_api 中的包裝函數
            result = self.module_manager.run_test_function("frontend", "show_desktop_pet", {})
            
            if result.get('success', False):
                self.add_result("✅ UEP 主程式顯示成功", "SUCCESS")
            else:
                self.add_result(f"❌ UEP 主程式顯示失敗: {result.get('error', '未知錯誤')}", "ERROR")
                
        except Exception as e:
            self.add_result(f"顯示 UEP 主程式時發生錯誤: {str(e)}", "ERROR")
    
    def hide_uep_app(self):
        """隱藏 UEP 主程式"""
        try:
            self.add_result("👻 隱藏 UEP 主程式...", "INFO")
            
            # 使用 debug_api 中的包裝函數
            result = self.module_manager.run_test_function("frontend", "hide_desktop_pet", {})
            
            if result.get('success', False):
                self.add_result("✅ UEP 主程式隱藏成功", "SUCCESS")
            else:
                self.add_result(f"❌ UEP 主程式隱藏失敗: {result.get('error', '未知錯誤')}", "ERROR")
                
        except Exception as e:
            self.add_result(f"隱藏 UEP 主程式時發生錯誤: {str(e)}", "ERROR")

    # === 模組管理方法 ===
    
    def load_frontend_modules(self):
        """載入前端模組 (UI, ANI, MOV)"""
        self.add_result("📥 開始載入前端模組...", "INFO")
        
        modules_to_load = ["ui", "ani", "mov"]
        loaded_count = 0
        
        for module_name in modules_to_load:
            try:
                self.add_result(f"  📦 正在載入 {module_name.upper()} 模組...", "INFO")
                result = self.module_manager.load_module(module_name)
                
                if result.get('success', False):
                    self.add_result(f"  ✅ {module_name.upper()} 模組載入成功", "SUCCESS")
                    loaded_count += 1
                else:
                    error_msg = result.get('error', '未知錯誤')
                    self.add_result(f"  ❌ {module_name.upper()} 模組載入失敗: {error_msg}", "ERROR")
                    
            except Exception as e:
                self.add_result(f"  ❌ 載入 {module_name.upper()} 模組時發生錯誤: {str(e)}", "ERROR")
        
        # 總結載入結果
        if loaded_count == len(modules_to_load):
            self.add_result(f"🎉 所有前端模組載入完成 ({loaded_count}/{len(modules_to_load)})", "SUCCESS")
        elif loaded_count > 0:
            self.add_result(f"⚠️  部分前端模組載入完成 ({loaded_count}/{len(modules_to_load)})", "WARNING")
        else:
            self.add_result("❌ 前端模組載入失敗", "ERROR")
        
        # 重新整理狀態
        self.refresh_status()
    
    def unload_frontend_modules(self):
        """卸載前端模組 (UI, ANI, MOV)"""
        self.add_result("📤 開始卸載前端模組...", "INFO")
        
        modules_to_unload = ["ui", "ani", "mov"]
        unloaded_count = 0
        
        for module_name in modules_to_unload:
            try:
                self.add_result(f"  📦 正在卸載 {module_name.upper()} 模組...", "INFO")
                result = self.module_manager.unload_module(module_name)
                
                if result.get('success', False):
                    self.add_result(f"  ✅ {module_name.upper()} 模組卸載成功", "SUCCESS")
                    unloaded_count += 1
                else:
                    error_msg = result.get('error', '未知錯誤')
                    self.add_result(f"  ❌ {module_name.upper()} 模組卸載失敗: {error_msg}", "ERROR")
                    
            except Exception as e:
                self.add_result(f"  ❌ 卸載 {module_name.upper()} 模組時發生錯誤: {str(e)}", "ERROR")
        
        # 總結卸載結果
        if unloaded_count == len(modules_to_unload):
            self.add_result(f"🎉 所有前端模組卸載完成 ({unloaded_count}/{len(modules_to_unload)})", "SUCCESS")
        elif unloaded_count > 0:
            self.add_result(f"⚠️  部分前端模組卸載完成 ({unloaded_count}/{len(modules_to_unload)})", "WARNING")
        else:
            self.add_result("❌ 前端模組卸載失敗", "ERROR")
        
        # 重新整理狀態
        self.refresh_status()
