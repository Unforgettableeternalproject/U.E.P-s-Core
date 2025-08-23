# debug/system_monitor_tab.py
"""
System Monitor Tab

系統監控分頁
提供系統狀態監控和資源使用情況檢視
"""

import os
import sys
from typing import Dict, Any, Optional, List

try:
    from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                QPushButton, QTextEdit, QLabel, QProgressBar,
                                QTableWidget, QTableWidgetItem, QHeaderView,
                                QFrame, QSplitter, QTabWidget, QGridLayout)
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal
    from PyQt5.QtGui import QFont, QColor, QPalette
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    QWidget = object
    pyqtSignal = None

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log, KEY_LEVEL, OPERATION_LEVEL, SYSTEM_LEVEL, ELABORATIVE_LEVEL

# 導入背景工作線程管理器
from .background_worker import get_worker_manager


class SystemMonitorTab(QWidget):
    """
    系統監控分頁
    
    特性：
    - 系統資源監控
    - 模組狀態監控
    - 效能指標顯示
    - 即時更新
    """
    
    refresh_requested = pyqtSignal() if pyqtSignal else None
    
    def __init__(self, ui_module=None):
        super().__init__()
        self.ui_module = ui_module
        self.system_info = {}
        self.module_status = {}
        self.worker_manager = get_worker_manager()
        self.resource_task_id = "system_monitor_resources"
        self.module_task_id = "system_monitor_modules"
        self.network_task_id = "system_monitor_network"
        self.debug_main_window = None
        
        if PYQT5_AVAILABLE:
            self.init_ui()
            self.setup_timer()
            self.setup_worker_signals()
            self.refresh_all_info()
            # 尋找父視窗中的 debug_main_window
            self.find_debug_main_window()
        
        debug_log(SYSTEM_LEVEL, "[SystemMonitorTab] 系統監控分頁初始化完成")
        
    def setup_worker_signals(self):
        """設置背景工作線程的信號連接"""
        if not PYQT5_AVAILABLE or not self.worker_manager or not hasattr(self.worker_manager, 'signals'):
            debug_log(KEY_LEVEL, "[SystemMonitorTab] 工作線程管理器不可用或無信號屬性，跳過信號連接")
            return
            
        # 處理背景工作結果
        if self.worker_manager.signals.finished:
            # 斷開舊連接防止重複
            try:
                self.worker_manager.signals.finished.disconnect(self._handle_worker_result)
            except:
                pass
            # 重新連接
            self.worker_manager.signals.finished.connect(self._handle_worker_result)
            debug_log(KEY_LEVEL, "[SystemMonitorTab] 工作完成信號已連接")
            
        # 處理背景工作錯誤
        if self.worker_manager.signals.error:
            # 斷開舊連接防止重複
            try:
                self.worker_manager.signals.error.disconnect(self._handle_worker_error)
            except:
                pass
            # 重新連接
            self.worker_manager.signals.error.connect(self._handle_worker_error)
            debug_log(KEY_LEVEL, "[SystemMonitorTab] 工作錯誤信號已連接")
    
    def _handle_worker_result(self, task_id, result):
        """處理背景工作線程的結果"""
        debug_log(KEY_LEVEL, f"[SystemMonitorTab] 收到任務結果: {task_id}")
        
        # 根據任務ID分發結果
        if task_id == self.resource_task_id:
            debug_log(KEY_LEVEL, "[SystemMonitorTab] 處理系統資源結果")
            self._update_resource_ui(result)
        elif task_id == self.module_task_id:
            debug_log(KEY_LEVEL, f"[SystemMonitorTab] 處理模組狀態結果: {type(result)}")
            if result:
                debug_log(KEY_LEVEL, f"[SystemMonitorTab] 模組數量: {len(result) if isinstance(result, dict) else '非字典類型'}")
            self._update_module_ui(result)
        elif task_id == self.network_task_id:
            debug_log(KEY_LEVEL, "[SystemMonitorTab] 處理網路狀態結果")
            self._update_network_ui(result)
        else:
            debug_log(KEY_LEVEL, f"[SystemMonitorTab] 未知任務ID: {task_id}")
    
    def _handle_worker_error(self, task_id, error_msg):
        """處理背景工作線程的錯誤"""
        error_log(KEY_LEVEL, f"[SystemMonitorTab] 背景任務 {task_id} 錯誤: {error_msg}")
    
    def init_ui(self):
        """初始化介面"""
        layout = QVBoxLayout(self)
        
        # 建立上下分割
        splitter = QSplitter(Qt.Vertical)
        
        # 上半部：系統資訊和模組狀態
        self.create_top_section(splitter)
        
        # 下半部：詳細監控資訊
        self.create_bottom_section(splitter)
        
        layout.addWidget(splitter)
        
        # 設置樣式
        self.setup_styles()
    
    def create_top_section(self, parent):
        """建立上半部區域"""
        top_widget = QWidget()
        top_layout = QHBoxLayout(top_widget)
        
        # 系統資訊
        self.create_system_info_section(top_layout)
        
        # 模組狀態
        self.create_module_status_section(top_layout)
        
        parent.addWidget(top_widget)
    
    def create_system_info_section(self, parent_layout):
        """建立系統資訊區域"""
        system_group = QGroupBox("系統資訊")
        system_layout = QVBoxLayout(system_group)
        
        # 基本資訊
        info_layout = QGridLayout()
        
        self.os_label = QLabel("作業系統: 檢測中...")
        info_layout.addWidget(QLabel("🖥️"), 0, 0)
        info_layout.addWidget(self.os_label, 0, 1)
        
        self.python_label = QLabel("Python: 檢測中...")
        info_layout.addWidget(QLabel("🐍"), 1, 0)
        info_layout.addWidget(self.python_label, 1, 1)
        
        self.uptime_label = QLabel("執行時間: 檢測中...")
        info_layout.addWidget(QLabel("⏱️"), 2, 0)
        info_layout.addWidget(self.uptime_label, 2, 1)
        
        system_layout.addLayout(info_layout)
        
        # 資源使用情況
        resource_layout = QVBoxLayout()
        
        # CPU 使用率
        cpu_layout = QHBoxLayout()
        cpu_layout.addWidget(QLabel("CPU:"))
        self.cpu_progress = QProgressBar()
        self.cpu_progress.setRange(0, 100)
        cpu_layout.addWidget(self.cpu_progress)
        self.cpu_label = QLabel("0%")
        cpu_layout.addWidget(self.cpu_label)
        resource_layout.addLayout(cpu_layout)
        
        # 記憶體使用率
        memory_layout = QHBoxLayout()
        memory_layout.addWidget(QLabel("記憶體:"))
        self.memory_progress = QProgressBar()
        self.memory_progress.setRange(0, 100)
        memory_layout.addWidget(self.memory_progress)
        self.memory_label = QLabel("0%")
        memory_layout.addWidget(self.memory_label)
        resource_layout.addLayout(memory_layout)
        
        system_layout.addLayout(resource_layout)
        
        # 網路狀態
        network_layout = QHBoxLayout()
        self.network_status = QLabel("🌐 網路狀態: 檢測中...")
        network_layout.addWidget(self.network_status)
        system_layout.addLayout(network_layout)
        
        parent_layout.addWidget(system_group)
    
    def create_module_status_section(self, parent_layout):
        """建立模組狀態區域"""
        module_group = QGroupBox("模組狀態")
        module_layout = QVBoxLayout(module_group)
        
        # 模組狀態表格
        self.module_table = QTableWidget()
        self.module_table.setColumnCount(4)
        self.module_table.setHorizontalHeaderLabels(["模組", "狀態", "載入時間", "記憶體"])
        self.module_table.setMaximumHeight(200)
        
        if QHeaderView:
            header = self.module_table.horizontalHeader()
            header.setStretchLastSection(True)
        
        module_layout.addWidget(self.module_table)
        
        # 控制按鈕
        button_layout = QHBoxLayout()
        
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self.refresh_module_status)
        button_layout.addWidget(refresh_btn)
        
        reload_btn = QPushButton("♻️ 重載模組")
        reload_btn.clicked.connect(self.reload_modules)
        button_layout.addWidget(reload_btn)
        
        button_layout.addStretch()
        module_layout.addLayout(button_layout)
        
        parent_layout.addWidget(module_group)
    
    def create_bottom_section(self, parent):
        """建立下半部區域"""
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        
        # 建立詳細監控標籤頁
        detail_tabs = QTabWidget()
        
        # 效能監控分頁
        self.create_performance_tab(detail_tabs)
        
        # 日誌監控分頁
        self.create_log_monitor_tab(detail_tabs)
        
        # 連線監控分頁
        self.create_connection_tab(detail_tabs)
        
        bottom_layout.addWidget(detail_tabs)
        parent.addWidget(bottom_widget)
    
    def create_performance_tab(self, tab_widget):
        """建立效能監控分頁"""
        performance_widget = QWidget()
        layout = QVBoxLayout(performance_widget)
        
        # U.E.P 特定效能指標
        uep_group = QGroupBox("U.E.P 效能指標")
        uep_layout = QGridLayout(uep_group)
        
        # 模組回應時間
        uep_layout.addWidget(QLabel("STT 回應時間:"), 0, 0)
        self.stt_response_label = QLabel("N/A")
        uep_layout.addWidget(self.stt_response_label, 0, 1)
        
        uep_layout.addWidget(QLabel("NLP 處理時間:"), 1, 0)
        self.nlp_response_label = QLabel("N/A")
        uep_layout.addWidget(self.nlp_response_label, 1, 1)
        
        uep_layout.addWidget(QLabel("LLM 回應時間:"), 2, 0)
        self.llm_response_label = QLabel("N/A")
        uep_layout.addWidget(self.llm_response_label, 2, 1)
        
        uep_layout.addWidget(QLabel("TTS 生成時間:"), 3, 0)
        self.tts_response_label = QLabel("N/A")
        uep_layout.addWidget(self.tts_response_label, 3, 1)
        
        # 前端效能
        uep_layout.addWidget(QLabel("動畫 FPS:"), 0, 2)
        self.animation_fps_label = QLabel("N/A")
        uep_layout.addWidget(self.animation_fps_label, 0, 2)
        
        uep_layout.addWidget(QLabel("UI 回應時間:"), 1, 2)
        self.ui_response_label = QLabel("N/A")
        uep_layout.addWidget(self.ui_response_label, 1, 3)
        
        layout.addWidget(uep_group)
        
        # 資源使用詳情
        resource_group = QGroupBox("資源使用詳情")
        resource_layout = QVBoxLayout(resource_group)
        
        self.resource_details = QTextEdit()
        self.resource_details.setReadOnly(True)
        self.resource_details.setMaximumHeight(150)
        resource_layout.addWidget(self.resource_details)
        
        layout.addWidget(resource_group)
        
        tab_widget.addTab(performance_widget, "📈 效能")
    
    def create_log_monitor_tab(self, tab_widget):
        """建立日誌監控分頁"""
        log_widget = QWidget()
        layout = QVBoxLayout(log_widget)
        
        # 日誌統計
        stats_group = QGroupBox("日誌統計")
        stats_layout = QGridLayout(stats_group)
        
        stats_layout.addWidget(QLabel("INFO:"), 0, 0)
        self.info_count_label = QLabel("0")
        stats_layout.addWidget(self.info_count_label, 0, 1)
        
        stats_layout.addWidget(QLabel("WARNING:"), 0, 2)
        self.warning_count_label = QLabel("0")
        stats_layout.addWidget(self.warning_count_label, 0, 3)
        
        stats_layout.addWidget(QLabel("ERROR:"), 1, 0)
        self.error_count_label = QLabel("0")
        stats_layout.addWidget(self.error_count_label, 1, 1)
        
        stats_layout.addWidget(QLabel("DEBUG:"), 1, 2)
        self.debug_count_label = QLabel("0")
        stats_layout.addWidget(self.debug_count_label, 1, 3)
        
        layout.addWidget(stats_group)
        
        # 最近錯誤
        error_group = QGroupBox("最近錯誤")
        error_layout = QVBoxLayout(error_group)
        
        self.recent_errors = QTextEdit()
        self.recent_errors.setReadOnly(True)
        self.recent_errors.setMaximumHeight(200)
        error_layout.addWidget(self.recent_errors)
        
        layout.addWidget(error_group)
        
        tab_widget.addTab(log_widget, "📋 日誌")
    
    def create_connection_tab(self, tab_widget):
        """建立連線監控分頁"""
        connection_widget = QWidget()
        layout = QVBoxLayout(connection_widget)
        
        # 網路連線狀態
        network_group = QGroupBox("網路連線")
        network_layout = QGridLayout(network_group)
        
        network_layout.addWidget(QLabel("網際網路:"), 0, 0)
        self.internet_status_label = QLabel("檢測中...")
        network_layout.addWidget(self.internet_status_label, 0, 1)
        
        network_layout.addWidget(QLabel("DNS 解析:"), 1, 0)
        self.dns_status_label = QLabel("檢測中...")
        network_layout.addWidget(self.dns_status_label, 1, 1)
        
        layout.addWidget(network_group)
        
        # 外部服務連線
        service_group = QGroupBox("外部服務")
        service_layout = QVBoxLayout(service_group)
        
        self.service_table = QTableWidget()
        self.service_table.setColumnCount(3)
        self.service_table.setHorizontalHeaderLabels(["服務", "狀態", "延遲"])
        
        if QHeaderView:
            header = self.service_table.horizontalHeader()
            header.setStretchLastSection(True)
        
        service_layout.addWidget(self.service_table)
        layout.addWidget(service_group)
        
        tab_widget.addTab(connection_widget, "🌐 連線")
    
    def setup_styles(self):
        """設置樣式"""
        self.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                border: 1px solid #404040;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
                color: #ffffff;
            }
            
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 8px 0 8px;
                color: #0078d4;
                font-weight: bold;
            }
            
            QProgressBar {
                border: 1px solid #404040;
                border-radius: 4px;
                background-color: #2d2d2d;
                text-align: center;
                color: #ffffff;
            }
            
            QProgressBar::chunk {
                background-color: #0078d4;
                border-radius: 2px;
            }
            
            QTableWidget {
                background-color: #2d2d2d;
                border: 1px solid #404040;
                border-radius: 4px;
                gridline-color: #404040;
            }
            
            QTableWidget::item {
                padding: 4px;
                border-bottom: 1px solid #404040;
            }
            
            QTableWidget::item:selected {
                background-color: #404040;
            }
            
            QHeaderView::section {
                background-color: #404040;
                color: #ffffff;
                padding: 4px;
                border: 1px solid #606060;
                font-weight: bold;
            }
        """)
    
    def setup_timer(self):
        """設置更新定時器"""
        if not QTimer:
            debug_log(KEY_LEVEL, "[SystemMonitorTab] QTimer 不可用，跳過定時器設置")
            return
        
        debug_log(KEY_LEVEL, "[SystemMonitorTab] 開始設置更新定時器")
        
        # 系統資源更新定時器
        self.resource_timer = QTimer()
        self.resource_timer.timeout.connect(self.update_system_resources)
        self.resource_timer.start(5000)  # 每5秒更新，降低頻率以減輕負擔
        debug_log(KEY_LEVEL, "[SystemMonitorTab] 系統資源定時器已啟動 (5秒間隔)")
        
        # 模組狀態更新定時器
        self.module_timer = QTimer()
        self.module_timer.timeout.connect(self.refresh_module_status)
        self.module_timer.start(10000)  # 每10秒更新，降低頻率以減輕負擔
        debug_log(KEY_LEVEL, "[SystemMonitorTab] 模組狀態定時器已啟動 (10秒間隔)")
        
        # 網路狀態更新定時器
        self.network_timer = QTimer()
        self.network_timer.timeout.connect(self.update_network_status)
        self.network_timer.start(20000)  # 每20秒更新，降低頻率以減輕負擔
        debug_log(KEY_LEVEL, "[SystemMonitorTab] 網路狀態定時器已啟動 (20秒間隔)")
        
        # 日誌統計更新定時器
        self.log_stats_timer = QTimer()
        self.log_stats_timer.timeout.connect(self.update_log_statistics)
        self.log_stats_timer.start(3000)  # 每3秒更新日誌統計資訊
        debug_log(KEY_LEVEL, "[SystemMonitorTab] 日誌統計定時器已啟動 (3秒間隔)")
        
        # 效能指標更新定時器
        self.performance_timer = QTimer()
        self.performance_timer.timeout.connect(self.update_performance_metrics)
        self.performance_timer.start(5000)  # 每5秒更新效能指標
        debug_log(KEY_LEVEL, "[SystemMonitorTab] 效能指標定時器已啟動 (5秒間隔)")
        
        debug_log(KEY_LEVEL, "[SystemMonitorTab] 所有定時器設置完成")
    
    def refresh_all_info(self):
        """刷新所有資訊"""
        debug_log(KEY_LEVEL, "[SystemMonitorTab] 刷新所有資訊")
        self.update_system_info()
        self.update_system_resources()
        self.refresh_module_status()
        self.update_network_status()
        self.update_performance_metrics()
        self.update_log_statistics()
    
    def update_system_info(self):
        """更新系統資訊"""
        try:
            import platform
            import datetime
            
            # 作業系統資訊
            os_info = f"{platform.system()} {platform.release()}"
            self.os_label.setText(f"作業系統: {os_info}")
            
            # Python 版本
            python_info = f"Python {platform.python_version()}"
            self.python_label.setText(f"Python: {python_info}")
            
            # 執行時間（模擬）
            self.uptime_label.setText("執行時間: 正常運行")
            
        except Exception as e:
            error_log(f"[SystemMonitorTab] 更新系統資訊失敗: {e}")
    
    def update_system_resources(self):
        """更新系統資源"""
        # 只有當分頁可見時才更新資源使用情況，以減少系統負載
        if not self.isVisible():
            return
            
        # 使用背景工作線程收集資源資訊
        def collect_resources():
            try:
                import psutil
                # 這些操作現在在背景線程中執行，不會阻塞UI
                cpu_percent = psutil.cpu_percent(interval=0.5)  # 輕量化的CPU使用率測量
                memory = psutil.virtual_memory()
                memory_percent = memory.percent
                
                return {
                    "cpu_percent": cpu_percent,
                    "memory_percent": memory_percent,
                    "memory_used": memory.used / (1024**3),
                    "memory_total": memory.total / (1024**3),
                    "memory_available": memory.available / (1024**3)
                }
            except ImportError:
                return {"error": "psutil not installed"}
            except Exception as e:
                error_log(f"[SystemMonitorTab] 收集系統資源資訊失敗: {e}")
                return {"error": str(e)}
        
        # 啟動背景工作
        try:
            # 如果已存在任務則先停止
            self.worker_manager.stop_task(self.resource_task_id)
            # 啟動新任務
            self.worker_manager.start_task(self.resource_task_id, collect_resources)
        except Exception as e:
            error_log(f"[SystemMonitorTab] 啟動資源監控任務失敗: {e}")
            
    def _update_resource_ui(self, result):
        """更新資源信息UI（從背景工作線程調用）"""
        if not result or "error" in result:
            error_msg = result.get("error", "未知錯誤") if result else "無結果"
            self.cpu_progress.setValue(0)
            self.cpu_label.setText("N/A")
            self.memory_progress.setValue(0)
            self.memory_label.setText("N/A")
            if hasattr(self, 'resource_details'):
                self.resource_details.setText(f"更新資源資訊失敗: {error_msg}")
            return
            
        # 更新UI元素（在主線程中）
        try:
            cpu_percent = result["cpu_percent"]
            memory_percent = result["memory_percent"]
            
            self.cpu_progress.setValue(int(cpu_percent))
            self.cpu_label.setText(f"{cpu_percent:.1f}%")
            
            self.memory_progress.setValue(int(memory_percent))
            self.memory_label.setText(f"{memory_percent:.1f}%")
            
            if hasattr(self, 'resource_details'):
                details = f"""CPU 使用率: {cpu_percent:.1f}%
記憶體使用: {result["memory_used"]:.1f} GB / {result["memory_total"]:.1f} GB ({memory_percent:.1f}%)
可用記憶體: {result["memory_available"]:.1f} GB"""
                self.resource_details.setText(details)
        except Exception as e:
            error_log(f"[SystemMonitorTab] 更新資源UI失敗: {e}")
            
        except ImportError:
            # psutil 未安裝時的後備方案
            self.cpu_progress.setValue(0)
            self.cpu_label.setText("N/A")
            self.memory_progress.setValue(0)
            self.memory_label.setText("N/A")
            
            if hasattr(self, 'resource_details'):
                self.resource_details.setText("需要安裝 psutil 來顯示系統資源資訊")
        except Exception as e:
            error_log(f"[SystemMonitorTab] 更新系統資源失敗: {e}")
    
    def refresh_module_status(self):
        """刷新模組狀態"""
        debug_log(KEY_LEVEL, "[SystemMonitorTab] refresh_module_status 被調用")
        
        # 只有當分頁可見時才更新，以減少不必要的負載
        if not self.isVisible():
            debug_log(KEY_LEVEL, f"[SystemMonitorTab] 跳過模組狀態更新 - isVisible: {self.isVisible()}")
            return
            
        debug_log(KEY_LEVEL, f"[SystemMonitorTab] 分頁可見，ui_module: {self.ui_module is not None}，開始獲取模組狀態")
            
        # 使用背景工作線程獲取模組狀態
        def get_modules_status():
            try:
                debug_log(ELABORATIVE_LEVEL, "[SystemMonitorTab] 開始獲取模組狀態")
                # 直接使用 ModuleManager 來獲取狀態
                from .module_manager import ModuleManager
                module_manager = ModuleManager()
                
                # 獲取所有模組的狀態
                modules_info = {
                    'stt': 'STT 語音識別',
                    'nlp': 'NLP 自然語言處理',
                    'mem': 'MEM 記憶模組',
                    'llm': 'LLM 大語言模型',
                    'tts': 'TTS 語音合成',
                    'sysmod': 'SYS 系統模組',
                    'ui': 'UI 用戶介面',
                    'ani': 'ANI 動畫模組',
                    'mov': 'MOV 運動模組'
                }
                
                module_status = {}
                for module_id, module_name in modules_info.items():
                    try:
                        status = module_manager.get_module_status(module_id)
                        module_status[module_id] = {
                            'name': module_name,
                            'status': status.get('status', 'unknown'),
                            'loaded': status.get('loaded', False),
                            'enabled': status.get('enabled', False),
                            'message': status.get('message', '無狀態信息')
                        }
                        debug_log(ELABORATIVE_LEVEL, f"[SystemMonitorTab] 模組 {module_id} 狀態: {module_status[module_id]}")
                    except Exception as e:
                        error_log(f"[SystemMonitorTab] 獲取模組 {module_id} 狀態失敗: {e}")
                        module_status[module_id] = {
                            'name': module_name,
                            'status': 'error',
                            'loaded': False,
                            'enabled': False,
                            'message': f'錯誤: {str(e)}'
                        }
                
                debug_log(KEY_LEVEL, f"[SystemMonitorTab] 獲取到 {len(module_status)} 個模組狀態")
                return module_status
                
            except Exception as e:
                error_log(KEY_LEVEL, f"[SystemMonitorTab] 獲取模組狀態失敗: {e}")
                return {"error": str(e)}
                
        # 啟動背景工作
        try:
            debug_log(KEY_LEVEL, f"[SystemMonitorTab] 啟動背景任務: {self.module_task_id}")
            # 如果已存在任務則先停止
            self.worker_manager.stop_task(self.module_task_id)
            # 啟動新任務
            self.worker_manager.start_task(self.module_task_id, get_modules_status)
            debug_log(KEY_LEVEL, f"[SystemMonitorTab] 模組狀態檢查任務已啟動")
        except Exception as e:
            error_log(f"[SystemMonitorTab] 啟動模組狀態檢查失敗: {e}")
            
    def _update_module_ui(self, result):
        """更新模組狀態UI（從背景工作線程調用）"""
        debug_log(KEY_LEVEL, f"[SystemMonitorTab] _update_module_ui 被調用, result 類型: {type(result)}")
        
        if not result:
            debug_log(KEY_LEVEL, "[SystemMonitorTab] result 為空，跳過更新")
            return
            
        if isinstance(result, dict) and "error" in result:
            error_log(f"[SystemMonitorTab] 模組狀態檢查失敗: {result['error']}")
            return
            
        # 更新模組表格
        try:
            if isinstance(result, dict):
                debug_log(KEY_LEVEL, f"[SystemMonitorTab] 準備更新模組表格，模組數量: {len(result)}")
                for module_id, status in result.items():
                    debug_log(KEY_LEVEL, f"[SystemMonitorTab] 模組 {module_id}: 載入={status.get('loaded', False)}, 狀態={status.get('status', 'unknown')}")
                self.update_module_table(result)
            else:
                error_log(f"[SystemMonitorTab] 無法更新模組表格，result 不是字典: {type(result)}")
        except Exception as e:
            error_log(f"[SystemMonitorTab] 更新模組UI失敗: {e}")
    
    def update_module_table(self, modules_status: dict):
        """更新模組狀態表格"""
        if not hasattr(self, 'module_table'):
            return
        
        # 確保已經排除了任何重複的模組項目
        sorted_modules = sorted(modules_status.items(), key=lambda x: x[0].upper())
        
        # 清空現有表格，以避免任何舊數據殘留
        self.module_table.clearContents()
        self.module_table.setRowCount(len(sorted_modules))
        
        # 更新前記錄模組數量，便於調試
        debug_log(ELABORATIVE_LEVEL, f"[SystemMonitorTab] 更新模組表格: 共 {len(sorted_modules)} 個模組")
        
        for row, (module_id, status) in enumerate(sorted_modules):
            try:
                # 模組名稱
                name_item = QTableWidgetItem(module_id.upper())
                name_item.setForeground(QColor(255, 255, 255))  # 白色文字
                self.module_table.setItem(row, 0, name_item)
                
                # 狀態
                state = status.get('status', 'unknown')
                message = status.get('message', '未知狀態')
                status_item = QTableWidgetItem(state)
                status_item.setToolTip(message)  # 添加提示信息
                
                # 始終使用白色文字
                status_item.setForeground(QColor(255, 255, 255))
                
                # 根據載入狀態設置顏色
                if status.get('loaded', False):
                    status_item.setBackground(QColor(40, 167, 69))  # 綠色 - 已載入
                    debug_log(KEY_LEVEL, f"[SystemMonitorTab] 模組 {module_id} 已載入")
                elif state == 'error':
                    status_item.setBackground(QColor(220, 53, 69))  # 紅色 - 錯誤
                elif status.get('enabled', False):
                    status_item.setBackground(QColor(255, 152, 0))  # 黃色 - 已啟用但未載入
                else:
                    status_item.setBackground(QColor(108, 117, 125))  # 灰色 - 禁用
                
                self.module_table.setItem(row, 1, status_item)
                
                # 載入時間
                load_time = status.get('load_time', 'N/A')
                time_item = QTableWidgetItem(str(load_time))
                time_item.setForeground(QColor(255, 255, 255))  # 白色文字
                self.module_table.setItem(row, 2, time_item)
                
                # 記憶體使用（模擬）
                memory_usage = status.get('memory_usage', 'N/A')
                memory_item = QTableWidgetItem(str(memory_usage))
                memory_item.setForeground(QColor(255, 255, 255))  # 白色文字
                self.module_table.setItem(row, 3, memory_item)
                
            except Exception as e:
                error_log(KEY_LEVEL, f"[SystemMonitorTab] 更新模組表格行 {row} (模組 {module_id}) 時出錯: {e}")
        
        # 調整列寬以適應內容
        if hasattr(self.module_table, 'resizeColumnsToContents'):
            self.module_table.resizeColumnsToContents()
    
    def update_network_status(self):
        """更新網路狀態"""
        # 只有當分頁可見時才執行網路檢查，以減少不必要的負載
        if not self.isVisible():
            return
        
        # 使用背景工作線程檢查網路狀態，避免主線程阻塞
        def check_network():
            try:
                import socket
                
                # 減少 timeout 時間以避免長時間阻塞
                timeout = 1
                result = {"internet": False, "dns": False}
                
                # 檢測網際網路連線
                try:
                    socket.create_connection(("8.8.8.8", 53), timeout=timeout)
                    result["internet"] = True
                    
                    # DNS 解析測試 - 只在網路連線時執行
                    try:
                        socket.gethostbyname("google.com")
                        result["dns"] = True
                    except:
                        result["dns"] = False
                        
                except:
                    result["internet"] = False
                
                return result
                
            except Exception as e:
                error_log(f"[SystemMonitorTab] 檢查網路狀態失敗: {e}")
                return {"error": str(e)}
        
        # 啟動背景工作
        try:
            # 如果已存在任務則先停止
            self.worker_manager.stop_task(self.network_task_id)
            # 啟動新任務
            self.worker_manager.start_task(self.network_task_id, check_network)
        except Exception as e:
            error_log(f"[SystemMonitorTab] 啟動網路狀態檢查失敗: {e}")
            
    def _update_network_ui(self, result):
        """更新網路狀態UI（從背景工作線程調用）"""
        if not result or "error" in result:
            # 處理錯誤情況
            self.network_status.setText("🌐 網路狀態: 檢測錯誤")
            if hasattr(self, 'internet_status_label'):
                self.internet_status_label.setText("⚠️ 錯誤")
                self.internet_status_label.setStyleSheet("color: #ff9800;")
            if hasattr(self, 'dns_status_label'):
                self.dns_status_label.setText("⚠️ 錯誤")
                self.dns_status_label.setStyleSheet("color: #ff9800;")
            return
            
        # 更新UI元素
        try:
            internet_status = result.get("internet", False)
            dns_status = result.get("dns", False)
            
            # 更新網路狀態
            if internet_status:
                self.network_status.setText("🌐 網路狀態: 已連線")
                if hasattr(self, 'internet_status_label'):
                    self.internet_status_label.setText("🟢 已連線")
                    self.internet_status_label.setStyleSheet("color: #4caf50;")
            else:
                self.network_status.setText("🌐 網路狀態: 離線")
                if hasattr(self, 'internet_status_label'):
                    self.internet_status_label.setText("🔴 離線")
                    self.internet_status_label.setStyleSheet("color: #f44336;")
            
            # 更新DNS狀態
            if internet_status:
                if dns_status:
                    if hasattr(self, 'dns_status_label'):
                        self.dns_status_label.setText("🟢 正常")
                        self.dns_status_label.setStyleSheet("color: #4caf50;")
                else:
                    if hasattr(self, 'dns_status_label'):
                        self.dns_status_label.setText("🔴 失敗")
                        self.dns_status_label.setStyleSheet("color: #f44336;")
            else:
                # 網路離線時不檢測 DNS
                if hasattr(self, 'dns_status_label'):
                    self.dns_status_label.setText("⚪ 未檢測")
                    self.dns_status_label.setStyleSheet("color: #808080;")
                    
        except Exception as e:
            error_log(f"[SystemMonitorTab] 更新網路UI失敗: {e}")
            
        except Exception as e:
            error_log(f"[SystemMonitorTab] 更新網路狀態失敗: {e}")
    
    def update_performance_metrics(self):
        """更新效能指標"""
        try:
            # 直接使用模組管理器獲取模組實例
            from .module_manager import ModuleManager
            module_manager = ModuleManager()
            
            # 獲取已載入模組的性能指標
            metrics = {}
            debug_log(KEY_LEVEL, "[SystemMonitorTab] 開始收集效能指標")
            
            # 嘗試從各模組獲取效能指標
            try:
                import devtools.debug_api as debug_api
                
                # STT 模組
                stt_module = debug_api.modules.get('stt')
                if stt_module and hasattr(stt_module, 'get_stats'):
                    stt_stats = stt_module.get_stats()
                    metrics['stt_response_time'] = stt_stats.get('avg_response_time', 'N/A')
                    debug_log(KEY_LEVEL, f"[SystemMonitorTab] STT 回應時間: {metrics['stt_response_time']}")
                
                # NLP 模組
                nlp_module = debug_api.modules.get('nlp')
                if nlp_module and hasattr(nlp_module, 'get_stats'):
                    nlp_stats = nlp_module.get_stats()
                    metrics['nlp_response_time'] = nlp_stats.get('avg_processing_time', 'N/A')
                
                # LLM 模組
                llm_module = debug_api.modules.get('llm')
                if llm_module and hasattr(llm_module, 'get_stats'):
                    llm_stats = llm_module.get_stats()
                    metrics['llm_response_time'] = llm_stats.get('avg_response_time', 'N/A')
                
                # TTS 模組
                tts_module = debug_api.modules.get('tts')
                if tts_module and hasattr(tts_module, 'get_stats'):
                    tts_stats = tts_module.get_stats()
                    metrics['tts_response_time'] = tts_stats.get('avg_response_time', 'N/A')
                
                # Animation 模組
                ani_module = debug_api.modules.get('ani')
                if ani_module and hasattr(ani_module, 'get_stats'):
                    ani_stats = ani_module.get_stats()
                    metrics['animation_fps'] = ani_stats.get('fps', 'N/A')
                
                # UI 模組
                ui_module = debug_api.modules.get('ui')
                if ui_module and hasattr(ui_module, 'get_stats'):
                    ui_stats = ui_module.get_stats()
                    metrics['ui_response_time'] = ui_stats.get('avg_response_time', 'N/A')
            except Exception as e:
                error_log(f"[SystemMonitorTab] 獲取模組效能指標失敗: {e}")
                
            # 更新界面
            if hasattr(self, 'stt_response_label'):
                self.stt_response_label.setText(f"{metrics.get('stt_response_time', 'N/A')} ms")
            
            if hasattr(self, 'nlp_response_label'):
                self.nlp_response_label.setText(f"{metrics.get('nlp_response_time', 'N/A')} ms")
            
            if hasattr(self, 'llm_response_label'):
                self.llm_response_label.setText(f"{metrics.get('llm_response_time', 'N/A')} ms")
            
            if hasattr(self, 'tts_response_label'):
                self.tts_response_label.setText(f"{metrics.get('tts_response_time', 'N/A')} ms")
            
            if hasattr(self, 'animation_fps_label'):
                self.animation_fps_label.setText(f"{metrics.get('animation_fps', 'N/A')} FPS")
            
            if hasattr(self, 'ui_response_label'):
                self.ui_response_label.setText(f"{metrics.get('ui_response_time', 'N/A')} ms")
                
        except Exception as e:
            error_log(f"[SystemMonitorTab] 更新效能指標失敗: {e}")
    
    def reload_modules(self):
        """重載模組"""
        debug_log(KEY_LEVEL, "[SystemMonitorTab] 重載所有模組")
        
        try:
            # 直接使用模組管理器重載模組
            from .module_manager import ModuleManager
            module_manager = ModuleManager()
            
            # 獲取所有模組並重載
            modules_info = {
                'stt': 'STT 語音識別',
                'nlp': 'NLP 自然語言處理',
                'mem': 'MEM 記憶模組',
                'llm': 'LLM 大語言模型',
                'tts': 'TTS 語音合成',
                'sysmod': 'SYS 系統模組',
                'ui': 'UI 用戶介面',
                'ani': 'ANI 動畫模組',
                'mov': 'MOV 運動模組'
            }
            
            for module_id in modules_info.keys():
                try:
                    status = module_manager.get_module_status(module_id)
                    if status.get('enabled', False):
                        debug_log(KEY_LEVEL, f"[SystemMonitorTab] 重載模組: {module_id}")
                        module_manager.reload_module(module_id)
                except Exception as e:
                    error_log(f"[SystemMonitorTab] 重載模組 {module_id} 失敗: {e}")
                    
            # 重新整理模組狀態
            self.refresh_module_status()
        except Exception as e:
            error_log(f"[SystemMonitorTab] 重載模組失敗: {e}")
    
    def find_debug_main_window(self):
        """尋找父視窗中的 DebugMainWindow 實例"""
        try:
            # 尋找父視窗
            parent = self.parent()
            while parent:
                if hasattr(parent, 'log_tab') and hasattr(parent, 'update_status'):
                    # 找到 DebugMainWindow 實例
                    self.debug_main_window = parent
                    debug_log(SYSTEM_LEVEL, "[SystemMonitorTab] 成功找到 DebugMainWindow")
                    
                    # 驗證 log_tab 是否有 log_entries 屬性
                    if not hasattr(parent.log_tab, 'log_entries'):
                        debug_log(OPERATION_LEVEL, "[SystemMonitorTab] DebugMainWindow.log_tab 缺少 log_entries 屬性")
                    else:
                        entry_count = len(parent.log_tab.log_entries)
                        debug_log(SYSTEM_LEVEL, f"[SystemMonitorTab] 找到 {entry_count} 個日誌條目")
                    break
                parent = parent.parent()
            
            if not self.debug_main_window:
                debug_log(OPERATION_LEVEL, "[SystemMonitorTab] 無法找到 DebugMainWindow 實例，日誌統計將使用本地數據")
        except Exception as e:
            error_log(KEY_LEVEL, f"[SystemMonitorTab] 尋找 DebugMainWindow 時出錯: {e}")
    
    def update_log_statistics(self):
        """更新日誌統計資訊，從 DebugMainWindow 獲取"""
        if not self.isVisible():
            return
            
        # 每次更新前重新查找 DebugMainWindow，確保連接性
        if not self.debug_main_window:
            self.find_debug_main_window()
            
        try:
            log_stats = {'DEBUG': 0, 'INFO': 0, 'WARNING': 0, 'ERROR': 0}
            
            # 從 DebugMainWindow 獲取日誌數據
            if self.debug_main_window and hasattr(self.debug_main_window, 'log_tab'):
                log_tab = self.debug_main_window.log_tab
                
                if hasattr(log_tab, 'log_entries'):
                    # 遍歷日誌條目獲取統計
                    for entry in log_tab.log_entries:
                        level = entry.get('level', '').upper()
                        if level in ['ELABORATIVE', 'DEBUG', 'ELABORATIVE_LEVEL']:
                            log_stats['DEBUG'] += 1
                        elif level in ['SYSTEM', 'INFO', 'SYSTEM_LEVEL']:
                            log_stats['INFO'] += 1
                        elif level in ['OPERATION', 'WARNING', 'OPERATION_LEVEL']:
                            log_stats['WARNING'] += 1
                        elif level in ['KEY', 'ERROR', 'CRITICAL', 'KEY_LEVEL']:
                            log_stats['ERROR'] += 1
                            
                    debug_log(SYSTEM_LEVEL, f"[SystemMonitorTab] 日誌統計更新: {log_stats}")
            
            # 使用 QTimer.singleShot 確保在主線程中更新 UI
            stats_copy = log_stats.copy()
            
            def update_ui():
                try:
                    # 記錄更新數據，方便調試
                    debug_log(ELABORATIVE_LEVEL, f"[SystemMonitorTab] 更新日誌統計: INFO={stats_copy.get('INFO', 0)}, WARNING={stats_copy.get('WARNING', 0)}, ERROR={stats_copy.get('ERROR', 0)}, DEBUG={stats_copy.get('DEBUG', 0)}")
                    
                    if hasattr(self, 'info_count_label'):
                        self.info_count_label.setText(str(stats_copy.get('INFO', 0)))
                    
                    if hasattr(self, 'warning_count_label'):
                        self.warning_count_label.setText(str(stats_copy.get('WARNING', 0)))
                    
                    if hasattr(self, 'error_count_label'):
                        self.error_count_label.setText(str(stats_copy.get('ERROR', 0)))
                    
                    if hasattr(self, 'debug_count_label'):
                        self.debug_count_label.setText(str(stats_copy.get('DEBUG', 0)))
                    
                    # 更新最近的錯誤列表
                    if hasattr(self, 'recent_errors') and self.debug_main_window and hasattr(self.debug_main_window, 'log_tab'):
                        log_tab = self.debug_main_window.log_tab
                        if hasattr(log_tab, 'log_entries'):
                            # 獲取最近的錯誤和警告
                            recent_errors_text = ""
                            count = 0
                            for entry in reversed(log_tab.log_entries):
                                level = entry.get('level', '').upper()
                                # 使用新的日誌級別名稱
                                if level in ['ERROR', 'CRITICAL', 'KEY', 'KEY_LEVEL'] and count < 5:  # 只顯示最近5個錯誤
                                    message = entry.get('message', '')
                                    timestamp = entry.get('timestamp_str', '') or entry.get('timestamp', '')
                                    recent_errors_text += f"[{timestamp}] {message}\n\n"
                                    count += 1
                            
                            self.recent_errors.setText(recent_errors_text)
                except Exception as e:
                    error_log(f"[SystemMonitorTab] 更新日誌統計UI時出錯: {e}", KEY_LEVEL)
            
            # 在主線程中安全更新UI
            QTimer.singleShot(0, update_ui)
                
        except Exception as e:
            error_log(KEY_LEVEL, f"[SystemMonitorTab] 更新日誌統計時出錯: {e}")
    
    def refresh_status(self):
        """刷新狀態（由外部呼叫）"""
        self.refresh_all_info()
        self.update_log_statistics()
        
    def hideEvent(self, event):
        """當分頁隱藏時停止更新，釋放資源"""
        # 停止所有相關的背景任務
        if hasattr(self, 'worker_manager'):
            # 安全地停止任務 - 不顯示錯誤
            self._safe_stop_task(self.resource_task_id)
            self._safe_stop_task(self.module_task_id)
            self._safe_stop_task(self.network_task_id)
        
        # 繼續原有的隱藏事件處理
        super().hideEvent(event)
        
    def _safe_stop_task(self, task_id):
        """安全地停止背景任務，避免顯示錯誤"""
        try:
            # 檢查任務是否存在
            if task_id in getattr(self.worker_manager, 'workers', {}):
                self.worker_manager.stop_task(task_id)
        except Exception:
            pass  # 忽略錯誤
        
    def closeEvent(self, event):
        """當分頁關閉時清理資源"""
        # 確保停止所有計時器
        if hasattr(self, 'resource_timer') and self.resource_timer:
            self.resource_timer.stop()
            
        if hasattr(self, 'module_timer') and self.module_timer:
            self.module_timer.stop()
            
        if hasattr(self, 'network_timer') and self.network_timer:
            self.network_timer.stop()
            
        if hasattr(self, 'log_stats_timer') and self.log_stats_timer:
            self.log_stats_timer.stop()
            
        # 停止所有背景任務
        if hasattr(self, 'worker_manager'):
            self._safe_stop_task(self.resource_task_id)
            self._safe_stop_task(self.module_task_id)
            self._safe_stop_task(self.network_task_id)
            
        # 繼續原有的關閉事件處理
        super().closeEvent(event)
