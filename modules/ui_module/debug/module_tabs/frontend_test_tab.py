# -*- coding: utf-8 -*-
"""
前端模組測試分頁
統合 UI、ANI、MOV 模組的測試功能
包含視覺化動畫預覽和即時測試功能
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

# AnimationPreviewWidget 已移至 Animation Tester


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
        # 建立分頁式介面
        self.test_tabs = QTabWidget()
        
        # 兩個子分頁：MOV測試、整合測試（ANI測試已移至 Animation Tester）
        self.mov_test_widget = self._create_mov_test_tab()
        self.integration_test_widget = self._create_integration_test_tab()
        
        self.test_tabs.addTab(self.mov_test_widget, "🚀 MOV 移動測試")
        self.test_tabs.addTab(self.integration_test_widget, "🔗 整合測試")
        
        # 添加提示：ANI 測試已移至 Animation Tester
        ani_note = QLabel("💡 ANI 動畫測試已整合到 Animation Tester，請點擊整合測試分頁中的按鈕開啟")
        ani_note.setWordWrap(True)
        ani_note.setStyleSheet("background-color: #e3f2fd; padding: 8px; border-radius: 4px; color: #1976d2; font-weight: bold;")
        main_layout.addWidget(ani_note)
        
        main_layout.addWidget(self.test_tabs)
    
    # ANI 測試分頁已移除，改用 Animation Tester
    # 如需測試動畫功能，請使用整合測試分頁中的「開啟 Animation Tester」按鈕
    
    def _create_mov_test_tab(self):
        """建立 MOV 移動測試分頁"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # MOV 狀態檢查
        status_group = QGroupBox("📊 MOV 模組狀態")
        status_layout = QVBoxLayout(status_group)
        
        self.mov_status_display = QLabel("MOV 模組: 檢查中...")
        self.mov_status_display.setWordWrap(True)
        status_layout.addWidget(self.mov_status_display)
        
        check_mov_status_btn = QPushButton("🔄 檢查 MOV 狀態")
        check_mov_status_btn.clicked.connect(self._check_mov_module_status)
        status_layout.addWidget(check_mov_status_btn)
        
        layout.addWidget(status_group)
        
        # MOV 行為測試
        behavior_group = QGroupBox("🎯 行為模式測試")
        behavior_layout = QVBoxLayout(behavior_group)
        
        behavior_hint = QLabel("💡 提示：MOV 模組負責控制 UEP 的移動和行為模式")
        behavior_hint.setWordWrap(True)
        behavior_hint.setStyleSheet("color: gray; font-size: 10px; padding: 5px;")
        behavior_layout.addWidget(behavior_hint)
        
        behavior_buttons = QHBoxLayout()
        
        idle_btn = QPushButton("😴 閒置狀態")
        idle_btn.clicked.connect(lambda: self._test_behavior_mode("idle"))
        behavior_buttons.addWidget(idle_btn)
        
        move_btn = QPushButton("🚶 移動狀態")
        move_btn.clicked.connect(lambda: self._test_behavior_mode("move"))
        behavior_buttons.addWidget(move_btn)
        
        behavior_layout.addLayout(behavior_buttons)
        
        # 開發中提示
        dev_note = QLabel("🚧 詳細的 MOV 測試功能開發中\n目前可用：狀態檢查、基本行為模式測試")
        dev_note.setWordWrap(True)
        dev_note.setStyleSheet("background-color: #fff3cd; padding: 10px; border-radius: 5px;")
        behavior_layout.addWidget(dev_note)
        
        layout.addWidget(behavior_group)
        layout.addStretch()
        
        return widget
    
    def _check_mov_module_status(self):
        """檢查 MOV 模組狀態"""
        self.add_result("🔍 檢查 MOV 模組狀態...", "INFO")
        
        try:
            mov_status = self.module_manager.get_module_status("mov")
            
            if mov_status.get('loaded', False):
                mov_module = mov_status.get('instance')
                status_text = f"MOV 模組: 已載入\n類型: {type(mov_module).__name__}"
                
                # 檢查可用方法
                available_methods = []
                for method in ['execute_behavior', 'set_behavior_mode', 'get_current_state']:
                    if hasattr(mov_module, method):
                        available_methods.append(method)
                
                if available_methods:
                    status_text += f"\n可用方法: {', '.join(available_methods)}"
                
                self.mov_status_display.setText(status_text)
                self.add_result("✅ MOV 模組已載入並就緒", "SUCCESS")
            else:
                self.mov_status_display.setText("MOV 模組: 未載入")
                self.add_result("❌ MOV 模組未載入，請先載入前端模組", "ERROR")
                
        except Exception as e:
            self.add_result(f"檢查 MOV 模組狀態時發生錯誤: {str(e)}", "ERROR")
    
    def _test_behavior_mode(self, mode: str):
        """測試行為模式"""
        self.add_result(f"🎯 測試行為模式: {mode}...", "INFO")
        
        try:
            mov_status = self.module_manager.get_module_status("mov")
            
            if not mov_status.get('loaded', False):
                self.add_result("❌ MOV 模組未載入，請先載入前端模組", "ERROR")
                return
            
            mov_module = mov_status.get('instance')
            
            # 檢查 MOV 模組是否支持行為模式設置
            if hasattr(mov_module, 'set_behavior_mode'):
                self.add_result(f"✅ MOV 模組支持行為模式設置", "SUCCESS")
                self.add_result("🚧 行為模式設置功能開發中", "WARNING")
            else:
                self.add_result("📋 MOV 模組當前實現不包含 set_behavior_mode 方法", "INFO")
                self.add_result("💡 可以透過整合測試分頁測試 MOV 功能", "INFO")
                
        except Exception as e:
            self.add_result(f"測試行為模式時發生錯誤: {str(e)}", "ERROR")
    
    def _create_integration_test_tab(self):
        """建立整合測試分頁"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        control_group = QGroupBox("整合測試控制")
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
        
        # Animation Tester 按鈕
        anim_tester_btn = QPushButton("🎬 開啟 Animation Tester")
        anim_tester_btn.clicked.connect(self.open_animation_tester)
        anim_tester_btn.setStyleSheet("""
            QPushButton {
                background-color: #4a148c;
                color: white;
                font-weight: bold;
                padding: 8px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #6a1b9a;
            }
            QPushButton:pressed {
                background-color: #38006b;
            }
        """)
        integration_buttons_layout.addWidget(anim_tester_btn)
        
        combo_test_btn = QPushButton("🎭 動畫+移動組合")
        combo_test_btn.clicked.connect(self.test_animation_movement_combo)
        integration_buttons_layout.addWidget(combo_test_btn)
        
        sync_test_btn = QPushButton("⚡ UI 同步測試")
        sync_test_btn.clicked.connect(self.test_ui_sync)
        integration_buttons_layout.addWidget(sync_test_btn)
        
        integration_layout.addLayout(integration_buttons_layout)
        control_layout.addWidget(integration_group)
        
        layout.addWidget(control_group)
        layout.addStretch()
        
        return widget
    
    # === ANI 測試功能已移至 Animation Tester ===
    
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
            
            # 檢查 ANI 模組是否已載入
            ani_status = self.module_manager.get_module_status("ani")
            if not ani_status.get('loaded', False):
                self.add_result("❌ ANI 模組未載入，請先載入前端模組", "ERROR")
                return
            
            # 取得 ANI 模組實例
            ani_module = ani_status.get('instance')
            if ani_module:
                self.add_result(f"📋 ANI 模組類型: {type(ani_module).__name__}", "INFO")
                
                # 檢查可用的播放方法
                if hasattr(ani_module, 'play'):
                    self.add_result("✅ ANI 模組已就緒，可以播放動畫", "SUCCESS")
                    self.add_result("💡 提示: 請在 ANI 測試分頁選擇並播放特定動畫", "INFO")
                else:
                    self.add_result("⚠️  ANI 模組介面可能已變更", "WARNING")
            else:
                self.add_result("❌ 無法取得 ANI 模組實例", "ERROR")
                
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
        
        try:
            # 檢查 MOV 模組是否已載入
            mov_status = self.module_manager.get_module_status("mov")
            if not mov_status.get('loaded', False):
                self.add_result("❌ MOV 模組未載入，請先載入前端模組", "ERROR")
                return
            
            # 檢查 MOV 模組的狀態和方法
            mov_module = mov_status.get('instance')
            if mov_module:
                # 顯示 MOV 模組的可用方法
                self.add_result(f"📋 MOV 模組類型: {type(mov_module).__name__}", "INFO")
                
                # 嘗試觸發一個簡單的移動
                if hasattr(mov_module, 'execute_behavior'):
                    self.add_result("🚧 MOV 移動執行功能開發中，請使用 MOV 測試分頁進行更詳細的測試", "WARNING")
                else:
                    self.add_result("⚠️  MOV 模組介面可能已變更，請檢查模組文檔", "WARNING")
            else:
                self.add_result("❌ 無法取得 MOV 模組實例", "ERROR")
                
        except Exception as e:
            self.add_result(f"執行移動時發生錯誤: {str(e)}", "ERROR")
    
    def move_to_center(self):
        """移動到螢幕中央"""
        self.add_result("📍 移動UEP到螢幕中央...", "INFO")
        try:
            # 檢查 UI 模組是否已載入
            ui_status = self.module_manager.get_module_status("ui")
            if not ui_status.get('loaded', False):
                self.add_result("❌ UI 模組未載入，請先載入前端模組", "ERROR")
                return
            
            # 獲取螢幕尺寸並計算中央位置
            from PyQt5.QtWidgets import QDesktopWidget
            desktop = QDesktopWidget()
            screen_geometry = desktop.screenGeometry()
            
            # 計算中央位置 (假設UEP大小為240x240)
            uep_size = 240
            center_x = (screen_geometry.width() - uep_size) // 2
            center_y = (screen_geometry.height() - uep_size) // 2
            
            # 直接通過UI模組來移動桌面寵物
            ui_module = ui_status.get('instance')
            if ui_module and hasattr(ui_module, 'handle_frontend_request'):
                result = ui_module.handle_frontend_request({
                    "command": "move_interface",
                    "interface": "main_desktop_pet",
                    "x": center_x,
                    "y": center_y
                })
                
                if result and result.get('success'):
                    self.add_result(f"✅ UEP已移動到中央位置 ({center_x}, {center_y})", "SUCCESS")
                else:
                    self.add_result(f"⚠️  移動命令已發送，但功能可能尚未完全實現", "WARNING")
                    self.add_result(f"   提示: 可以手動拖曳 UEP 視窗到想要的位置", "INFO")
            else:
                self.add_result("❌ UI 模組不支援前端請求介面", "ERROR")
                
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
        
        try:
            # 檢查所有前端模組狀態
            ui_status = self.module_manager.get_module_status("ui")
            ani_status = self.module_manager.get_module_status("ani")
            mov_status = self.module_manager.get_module_status("mov")
            
            results = []
            
            # UI 模組測試
            self.add_result("  📦 測試 UI 模組...", "INFO")
            if ui_status.get('loaded', False):
                self.add_result("    ✅ UI 模組已載入", "SUCCESS")
                results.append("UI: OK")
            else:
                self.add_result("    ❌ UI 模組未載入", "ERROR")
                results.append("UI: FAIL")
            
            # ANI 模組測試
            self.add_result("  📦 測試 ANI 模組...", "INFO")
            if ani_status.get('loaded', False):
                self.add_result("    ✅ ANI 模組已載入", "SUCCESS")
                results.append("ANI: OK")
            else:
                self.add_result("    ❌ ANI 模組未載入", "ERROR")
                results.append("ANI: FAIL")
            
            # MOV 模組測試
            self.add_result("  📦 測試 MOV 模組...", "INFO")
            if mov_status.get('loaded', False):
                self.add_result("    ✅ MOV 模組已載入", "SUCCESS")
                results.append("MOV: OK")
            else:
                self.add_result("    ❌ MOV 模組未載入", "ERROR")
                results.append("MOV: FAIL")
            
            # 總結
            success_count = sum(1 for r in results if "OK" in r)
            total_count = len(results)
            
            if success_count == total_count:
                self.add_result(f"✅ 完整前端測試完成: {success_count}/{total_count} 通過", "SUCCESS")
            else:
                self.add_result(f"⚠️  完整前端測試部分通過: {success_count}/{total_count}", "WARNING")
                
        except Exception as e:
            self.add_result(f"❌ 完整前端測試失敗: {str(e)}", "ERROR")
    
    def test_animation_movement_combo(self):
        """測試動畫+移動組合"""
        self.add_result("🎭 測試動畫+移動組合...", "INFO")
        
        try:
            # 檢查模組是否已載入
            ani_status = self.module_manager.get_module_status("ani")
            mov_status = self.module_manager.get_module_status("mov")
            
            if not ani_status.get('loaded', False):
                self.add_result("  ❌ ANI 模組未載入", "ERROR")
                return
            
            if not mov_status.get('loaded', False):
                self.add_result("  ❌ MOV 模組未載入", "ERROR")
                return
            
            self.add_result("  ✅ 前端模組已就緒", "SUCCESS")
            self.add_result("  ℹ️  MOV-ANI 整合測試功能開發中", "INFO")
            self.add_result("  💡 提示: 可以在 ANI 測試分頁播放動畫，在 MOV 測試分頁測試移動", "INFO")
            
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
            
            # 檢查 UI 模組是否已載入
            ui_status = self.module_manager.get_module_status("ui")
            if not ui_status.get('loaded', False):
                self.add_result("❌ UI 模組未載入，請先載入前端模組", "ERROR")
                return
            
            # 直接調用 UI 模組的方法
            ui_module = ui_status.get('instance')
            if ui_module and hasattr(ui_module, 'handle_frontend_request'):
                result = ui_module.handle_frontend_request({
                    "command": "show_interface",
                    "interface": "main_desktop_pet"
                })
                
                if result and result.get('success'):
                    self.add_result("✅ UEP 主程式顯示成功", "SUCCESS")
                else:
                    self.add_result(f"❌ UEP 主程式顯示失敗: {result.get('error', '未知錯誤') if result else '無回應'}", "ERROR")
            else:
                self.add_result("❌ UI 模組不支援前端請求介面", "ERROR")
                
        except Exception as e:
            self.add_result(f"顯示 UEP 主程式時發生錯誤: {str(e)}", "ERROR")
    
    def hide_uep_app(self):
        """隱藏 UEP 主程式"""
        try:
            self.add_result("👻 隱藏 UEP 主程式...", "INFO")
            
            # 檢查 UI 模組是否已載入
            ui_status = self.module_manager.get_module_status("ui")
            if not ui_status.get('loaded', False):
                self.add_result("❌ UI 模組未載入，請先載入前端模組", "ERROR")
                return
            
            # 直接調用 UI 模組的方法
            ui_module = ui_status.get('instance')
            if ui_module and hasattr(ui_module, 'handle_frontend_request'):
                result = ui_module.handle_frontend_request({
                    "command": "hide_interface",
                    "interface": "main_desktop_pet"
                })
                
                if result and result.get('success'):
                    self.add_result("✅ UEP 主程式隱藏成功", "SUCCESS")
                else:
                    self.add_result(f"❌ UEP 主程式隱藏失敗: {result.get('error', '未知錯誤') if result else '無回應'}", "ERROR")
            else:
                self.add_result("❌ UI 模組不支援前端請求介面", "ERROR")
                
        except Exception as e:
            self.add_result(f"隱藏 UEP 主程式時發生錯誤: {str(e)}", "ERROR")

    def open_animation_tester(self):
        """開啟 Animation Tester 獨立視窗"""
        import subprocess
        import sys
        from pathlib import Path
        
        try:
            # 獲取 animation_tester.py 的路徑
            project_root = Path(__file__).parent.parent.parent.parent.parent
            tester_path = project_root / "devtools" / "animation_tester.py"
            
            if not tester_path.exists():
                self.add_result(f"[錯誤] 找不到 Animation Tester: {tester_path}", "ERROR")
                return
            
            self.add_result(f"[啟動] 開啟 Animation Tester: {tester_path}", "INFO")
            
            # 使用 subprocess 啟動獨立進程
            subprocess.Popen(
                [sys.executable, str(tester_path)],
                cwd=str(project_root),
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == 'win32' else 0
            )
            
            self.add_result("[成功] Animation Tester 已在新視窗中啟動", "SUCCESS")
            
        except Exception as e:
            self.add_result(f"[錯誤] 啟動 Animation Tester 失敗: {e}", "ERROR")
            import traceback
            self.add_result(traceback.format_exc(), "ERROR")
    
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
