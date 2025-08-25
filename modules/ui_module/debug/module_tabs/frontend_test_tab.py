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
    """前端模組測試分頁 - 統合 UI、ANI、MOV"""
    
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
        
        # UI 基本測試
        ui_basic_layout = QHBoxLayout()
        
        ui_status_btn = QPushButton("📊 UI 狀態檢查")
        ui_status_btn.clicked.connect(self.check_ui_status)
        ui_basic_layout.addWidget(ui_status_btn)
        
        ui_interface_btn = QPushButton("🖼️ 介面測試")
        ui_interface_btn.clicked.connect(self.test_ui_interfaces)
        ui_basic_layout.addWidget(ui_interface_btn)
        
        ui_widget_btn = QPushButton("🔧 Access Widget 測試")
        ui_widget_btn.clicked.connect(self.test_access_widget)
        ui_basic_layout.addWidget(ui_widget_btn)
        
        ui_layout.addLayout(ui_basic_layout)
        control_layout.addWidget(ui_group)
        
        # ANI 模組區域
        ani_group = QGroupBox("🎬 ANI 模組測試")
        ani_layout = QVBoxLayout(ani_group)
        
        # 動畫選擇
        ani_selection_layout = QHBoxLayout()
        
        ani_selection_layout.addWidget(QLabel("動畫類型:"))
        self.animation_combo = QComboBox()
        self.animation_combo.addItems(["idle", "thinking", "speaking", "listening", "happy", "sad"])
        ani_selection_layout.addWidget(self.animation_combo)
        
        # 動畫參數
        self.animation_duration = QSpinBox()
        self.animation_duration.setRange(1, 10)
        self.animation_duration.setValue(3)
        self.animation_duration.setSuffix(" 秒")
        ani_selection_layout.addWidget(QLabel("持續時間:"))
        ani_selection_layout.addWidget(self.animation_duration)
        
        ani_layout.addLayout(ani_selection_layout)
        
        # ANI 測試按鈕
        ani_test_layout = QHBoxLayout()
        
        ani_play_btn = QPushButton("▶️ 播放動畫")
        ani_play_btn.clicked.connect(self.play_animation)
        ani_test_layout.addWidget(ani_play_btn)
        
        ani_stop_btn = QPushButton("⏹️ 停止動畫")
        ani_stop_btn.clicked.connect(self.stop_animation)
        ani_test_layout.addWidget(ani_stop_btn)
        
        ani_status_btn = QPushButton("📊 動畫狀態")
        ani_status_btn.clicked.connect(self.check_animation_status)
        ani_test_layout.addWidget(ani_status_btn)
        
        ani_layout.addLayout(ani_test_layout)
        control_layout.addWidget(ani_group)
        
        # MOV 模組區域
        mov_group = QGroupBox("🏃 MOV 模組測試")
        mov_layout = QVBoxLayout(mov_group)
        
        # 移動參數設置
        mov_params_layout = QGridLayout()
        
        # 位置設置
        mov_params_layout.addWidget(QLabel("目標X:"), 0, 0)
        self.target_x = QSpinBox()
        self.target_x.setRange(-2000, 2000)
        self.target_x.setValue(100)
        mov_params_layout.addWidget(self.target_x, 0, 1)
        
        mov_params_layout.addWidget(QLabel("目標Y:"), 0, 2)
        self.target_y = QSpinBox()
        self.target_y.setRange(-2000, 2000)
        self.target_y.setValue(100)
        mov_params_layout.addWidget(self.target_y, 0, 3)
        
        # 移動類型
        mov_params_layout.addWidget(QLabel("移動類型:"), 1, 0)
        self.movement_type = QComboBox()
        self.movement_type.addItems(["linear", "smooth", "bounce", "spring"])
        mov_params_layout.addWidget(self.movement_type, 1, 1)
        
        # 移動速度
        mov_params_layout.addWidget(QLabel("速度:"), 1, 2)
        self.movement_speed = QSpinBox()
        self.movement_speed.setRange(1, 10)
        self.movement_speed.setValue(5)
        mov_params_layout.addWidget(self.movement_speed, 1, 3)
        
        mov_layout.addLayout(mov_params_layout)
        
        # MOV 測試按鈕
        mov_test_layout = QHBoxLayout()
        
        mov_execute_btn = QPushButton("🎯 執行移動")
        mov_execute_btn.clicked.connect(self.execute_movement)
        mov_test_layout.addWidget(mov_execute_btn)
        
        mov_center_btn = QPushButton("🏠 回到中心")
        mov_center_btn.clicked.connect(self.move_to_center)
        mov_test_layout.addWidget(mov_center_btn)
        
        mov_status_btn = QPushButton("📊 移動狀態")
        mov_status_btn.clicked.connect(self.check_movement_status)
        mov_test_layout.addWidget(mov_status_btn)
        
        mov_layout.addLayout(mov_test_layout)
        control_layout.addWidget(mov_group)
        
        # 整合測試區域
        integration_group = QGroupBox("🔗 整合測試")
        integration_layout = QVBoxLayout(integration_group)
        
        integration_test_layout = QHBoxLayout()
        
        full_frontend_btn = QPushButton("🚀 完整前端測試")
        full_frontend_btn.clicked.connect(self.run_full_frontend_test)
        full_frontend_btn.setStyleSheet("QPushButton { background-color: #1976d2; font-size: 14px; padding: 10px; }")
        integration_test_layout.addWidget(full_frontend_btn)
        
        ani_mov_btn = QPushButton("🎬🏃 動畫+移動組合")
        ani_mov_btn.clicked.connect(self.test_animation_movement_combo)
        integration_test_layout.addWidget(ani_mov_btn)
        
        ui_sync_btn = QPushButton("🔄 UI 同步測試")
        ui_sync_btn.clicked.connect(self.test_ui_sync)
        integration_test_layout.addWidget(ui_sync_btn)
        
        integration_layout.addLayout(integration_test_layout)
        control_layout.addWidget(integration_group)
        
        main_layout.addWidget(control_group)
    
    def refresh_status(self):
        """刷新前端模組狀態"""
        try:
            # 檢查各個前端模組的狀態
            ui_status = self.module_manager.get_module_status("ui")
            ani_status = self.module_manager.get_module_status("ani") 
            mov_status = self.module_manager.get_module_status("mov")
            
            # 構建狀態信息
            statuses = []
            if ui_status.get('status') == 'enabled':
                statuses.append("UI:✅")
            else:
                statuses.append("UI:❌")
                
            if ani_status.get('status') == 'enabled':
                statuses.append("ANI:✅")
            else:
                statuses.append("ANI:❌")
                
            if mov_status.get('status') == 'enabled':
                statuses.append("MOV:✅")
            else:
                statuses.append("MOV:❌")
            
            status_text = "前端狀態: " + " | ".join(statuses)
            
            # 如果所有模組都啟用則顯示綠色，否則橙色
            all_enabled = all(status.get('status') == 'enabled' for status in [ui_status, ani_status, mov_status])
            if all_enabled:
                self.status_label.setText(status_text)
                self.status_label.setStyleSheet("color: green; font-weight: bold; padding: 5px;")
                self.setEnabled(True)
            else:
                self.status_label.setText(status_text + " (部分模組未啟用)")
                self.status_label.setStyleSheet("color: orange; font-weight: bold; padding: 5px;")
                self.setEnabled(True)  # 仍然允許測試
                
        except Exception as e:
            self.status_label.setText(f"狀態獲取失敗: {str(e)}")
            self.status_label.setStyleSheet("color: red; font-weight: bold; padding: 5px;")
    
    # UI 測試方法
    def check_ui_status(self):
        """檢查 UI 狀態"""
        self.add_result("🎨 檢查 UI 模組狀態...", "INFO")
        self.run_test("frontend_test_ui_status")
    
    def test_ui_interfaces(self):
        """測試 UI 介面"""
        self.add_result("🖼️ 測試 UI 介面...", "INFO")
        self.run_test("frontend_test_ui_interfaces")
    
    def test_access_widget(self):
        """測試 Access Widget"""
        self.add_result("🔧 測試 Access Widget...", "INFO")
        self.run_test("frontend_test_access_widget")
    
    # ANI 測試方法
    def play_animation(self):
        """播放動畫"""
        animation_type = self.animation_combo.currentText()
        duration = self.animation_duration.value()
        
        self.add_result(f"▶️ 播放動畫: {animation_type} (持續 {duration} 秒)", "INFO")
        
        params = {
            "animation_type": animation_type,
            "duration": duration
        }
        self.run_test("frontend_test_animation_play", params)
    
    def stop_animation(self):
        """停止動畫"""
        self.add_result("⏹️ 停止動畫...", "INFO")
        self.run_test("frontend_test_animation_stop")
    
    def check_animation_status(self):
        """檢查動畫狀態"""
        self.add_result("📊 檢查動畫狀態...", "INFO")
        self.run_test("frontend_test_animation_status")
    
    # MOV 測試方法
    def execute_movement(self):
        """執行移動"""
        x = self.target_x.value()
        y = self.target_y.value()
        movement_type = self.movement_type.currentText()
        speed = self.movement_speed.value()
        
        self.add_result(f"🎯 執行移動: 到 ({x}, {y}), 類型: {movement_type}, 速度: {speed}", "INFO")
        
        params = {
            "target_x": x,
            "target_y": y,
            "movement_type": movement_type,
            "speed": speed
        }
        self.run_test("frontend_test_movement_execute", params)
    
    def move_to_center(self):
        """移動到中心"""
        self.add_result("🏠 移動到螢幕中心...", "INFO")
        self.run_test("frontend_test_movement_center")
    
    def check_movement_status(self):
        """檢查移動狀態"""
        self.add_result("📊 檢查移動狀態...", "INFO")
        self.run_test("frontend_test_movement_status")
    
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
                if 'data' in result:
                    self.add_result(f"結果數據: {json.dumps(result['data'], ensure_ascii=False, indent=2)}", "INFO")
            else:
                self.add_result(f"❌ 完整前端測試失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        # 啟動背景任務
        task_id = "frontend_full_test_" + str(id(self))
        worker_manager.signals.finished.connect(on_task_complete)
        worker_manager.start_task(task_id, run_full_test_task)
        
        self.add_result("🔄 完整前端測試正在背景執行，請稍候...", "INFO")
    
    def test_animation_movement_combo(self):
        """測試動畫+移動組合"""
        animation_type = self.animation_combo.currentText()
        x = self.target_x.value()
        y = self.target_y.value()
        
        self.add_result(f"🎬🏃 執行動畫+移動組合: {animation_type} + 移動到 ({x}, {y})", "INFO")
        
        params = {
            "animation_type": animation_type,
            "target_x": x,
            "target_y": y,
            "movement_type": self.movement_type.currentText(),
            "speed": self.movement_speed.value()
        }
        self.run_test("frontend_test_animation_movement_combo", params)
    
    def test_ui_sync(self):
        """測試 UI 同步"""
        self.add_result("🔄 測試 UI 同步機制...", "INFO")
        self.run_test("frontend_test_ui_sync")
    
    def load_module(self):
        """載入前端模組"""
        try:
            self.add_result("正在載入前端模組群組 (UI+ANI+MOV)...", "INFO")
            
            # 逐一載入各個前端模組
            modules = ["ui", "ani", "mov"]
            success_count = 0
            
            for module_name in modules:
                try:
                    result = self.module_manager.load_module(module_name)
                    if result.get('success', False):
                        self.add_result(f"✅ {module_name.upper()} 模組載入成功", "SUCCESS")
                        success_count += 1
                    else:
                        self.add_result(f"❌ {module_name.upper()} 模組載入失敗: {result.get('error', '未知錯誤')}", "ERROR")
                except Exception as e:
                    self.add_result(f"❌ {module_name.upper()} 模組載入異常: {str(e)}", "ERROR")
            
            if success_count == len(modules):
                self.add_result("🎉 所有前端模組載入完成", "SUCCESS")
            elif success_count > 0:
                self.add_result(f"⚠️ 部分前端模組載入完成 ({success_count}/{len(modules)})", "WARNING")
            else:
                self.add_result("❌ 所有前端模組載入失敗", "ERROR")
                
        except Exception as e:
            self.add_result(f"載入前端模組時發生錯誤: {str(e)}", "ERROR")
        finally:
            self.refresh_status()
    
    def unload_module(self):
        """卸載前端模組"""
        try:
            self.add_result("正在卸載前端模組群組 (UI+ANI+MOV)...", "INFO")
            
            # 逐一卸載各個前端模組
            modules = ["ui", "ani", "mov"]
            success_count = 0
            
            for module_name in modules:
                try:
                    result = self.module_manager.unload_module(module_name)
                    if result.get('success', False):
                        self.add_result(f"✅ {module_name.upper()} 模組卸載成功", "SUCCESS")
                        success_count += 1
                    else:
                        self.add_result(f"❌ {module_name.upper()} 模組卸載失敗: {result.get('error', '未知錯誤')}", "ERROR")
                except Exception as e:
                    self.add_result(f"❌ {module_name.upper()} 模組卸載異常: {str(e)}", "ERROR")
            
            if success_count == len(modules):
                self.add_result("🎉 所有前端模組卸載完成", "SUCCESS")
            elif success_count > 0:
                self.add_result(f"⚠️ 部分前端模組卸載完成 ({success_count}/{len(modules)})", "WARNING")
            else:
                self.add_result("❌ 所有前端模組卸載失敗", "ERROR")
                
        except Exception as e:
            self.add_result(f"卸載前端模組時發生錯誤: {str(e)}", "ERROR")
        finally:
            self.refresh_status()
    
    def reload_module(self):
        """重載前端模組"""
        try:
            self.add_result("正在重載前端模組群組 (UI+ANI+MOV)...", "INFO")
            
            # 逐一重載各個前端模組
            modules = ["ui", "ani", "mov"]
            success_count = 0
            
            for module_name in modules:
                try:
                    result = self.module_manager.reload_module(module_name)
                    if result.get('success', False):
                        self.add_result(f"✅ {module_name.upper()} 模組重載成功", "SUCCESS")
                        success_count += 1
                    else:
                        self.add_result(f"❌ {module_name.upper()} 模組重載失敗: {result.get('error', '未知錯誤')}", "ERROR")
                except Exception as e:
                    self.add_result(f"❌ {module_name.upper()} 模組重載異常: {str(e)}", "ERROR")
            
            if success_count == len(modules):
                self.add_result("🎉 所有前端模組重載完成", "SUCCESS")
            elif success_count > 0:
                self.add_result(f"⚠️ 部分前端模組重載完成 ({success_count}/{len(modules)})", "WARNING")
            else:
                self.add_result("❌ 所有前端模組重載失敗", "ERROR")
                
        except Exception as e:
            self.add_result(f"重載前端模組時發生錯誤: {str(e)}", "ERROR")
        finally:
            self.refresh_status()
