# system_status.py
"""
系統狀態視窗 - 顯示 U.E.P 的心情與系統指標

主要功能：
- 自然語言顯示系統狀態（mood, pride, helpfulness, boredom）
- 簡化的性能監控（CPU、記憶體、模組狀態）
- 調試日誌分頁（僅在調試模式下顯示）
"""

import os
import sys
import psutil
import logging
from typing import Dict, Any, Optional
from datetime import datetime

from utils.debug_helper import debug_log, info_log, error_log, OPERATION_LEVEL
from configs.config_loader import load_config

try:
    from PyQt5.QtWidgets import (
        QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QScrollArea,
        QFrame, QPushButton, QSizePolicy, QLabel, QTabWidget,
        QApplication, QMessageBox, QStatusBar, QTextEdit, QProgressBar
    )
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QSettings, QSize
    from PyQt5.QtGui import QFont, QIcon
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    print("[SystemStatus] PyQt5 不可用")

try:
    from .theme_manager import theme_manager, Theme, install_theme_hook
except Exception:
    theme_manager = None
    Theme = None
    def install_theme_hook(_): pass


class SystemStatusWidget(QWidget):
    """系統狀態顯示組件"""
    
    def __init__(self, status_manager=None, parent=None):
        super().__init__(parent)
        if not PYQT5_AVAILABLE:
            return
            
        install_theme_hook(self)
        self.status_manager = status_manager
        self.config = load_config()
        self.startup_quote = self._generate_startup_quote()  # 生成啟動時的一句話
        
        self._build_ui()
        self._start_update_timer()
        
    def _generate_startup_quote(self) -> str:
        """生成啟動時的一句話"""
        import random
        quotes = [
            "今天也要努力呢～",
            "有什麼我可以幫忙的嗎？",
            "準備好和我一起工作了嗎？",
            "讓我們開始今天的旅程吧！",
            "我會一直在這裡的。",
            "希望今天一切順利～",
            "隨時可以找我幫忙喔！",
            "新的一天，新的開始！",
            "我準備好了，你呢？",
            "讓我們一起創造美好的一天吧！"
        ]
        return random.choice(quotes)
    
    def _build_ui(self):
        """構建UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)
        
        # 啟動語錄卡片（較小）
        self.quote_card = self._make_card("💬 U.E.P 說", min_height=100)
        self.quote_text = self._make_text_label()
        self.quote_text.setText(self.startup_quote)
        self._put_content(self.quote_card, self.quote_text)
        layout.addWidget(self.quote_card)
        
        # 狀態顯示（簡潔版）
        self.status_label = QLabel()
        self.status_label.setObjectName("statusSummary")
        self.status_label.setWordWrap(True)
        f = QFont()
        f.setPointSize(10)
        self.status_label.setFont(f)
        install_theme_hook(self.status_label)
        layout.addWidget(self.status_label)
        
        # 性能卡片
        self.performance_card = self._make_card("⚙️ 系統性能")
        self.performance_layout = QVBoxLayout()
        
        # CPU 使用率
        self.cpu_label = QLabel("CPU 使用率:")
        self.cpu_bar = QProgressBar()
        self.cpu_bar.setTextVisible(True)
        self.cpu_bar.setFormat("%p%")
        
        # 記憶體使用率
        self.mem_label = QLabel("記憶體使用率:")
        self.mem_bar = QProgressBar()
        self.mem_bar.setTextVisible(True)
        self.mem_bar.setFormat("%p%")
        
        # 系統運行時間
        self.uptime_label = QLabel("系統運行時間: 計算中...")
        
        # 模組狀態
        self.module_label = QLabel("模組狀態: 正在載入...")
        
        self.performance_layout.addWidget(self.cpu_label)
        self.performance_layout.addWidget(self.cpu_bar)
        self.performance_layout.addSpacing(10)
        self.performance_layout.addWidget(self.mem_label)
        self.performance_layout.addWidget(self.mem_bar)
        self.performance_layout.addSpacing(10)
        self.performance_layout.addWidget(self.uptime_label)
        self.performance_layout.addWidget(self.module_label)
        
        perf_widget = QWidget()
        install_theme_hook(perf_widget)
        perf_widget.setLayout(self.performance_layout)
        self._put_content(self.performance_card, perf_widget)
        layout.addWidget(self.performance_card)
        
        layout.addStretch()
        
    def _make_card(self, title: str, min_height: int = 200) -> QGroupBox:
        """創建卡片容器"""
        box = QGroupBox(title)
        box.setObjectName("settingsGroup")
        install_theme_hook(box)
        box.setMinimumHeight(min_height)
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        
        lay = QVBoxLayout(box)
        lay.setContentsMargins(18, 16, 18, 18)
        lay.setSpacing(10)
        return box
        
    def _make_text_label(self) -> QLabel:
        """創建文字標籤"""
        lb = QLabel()
        lb.setObjectName("statusText")
        lb.setWordWrap(True)
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = QFont()
        f.setPointSize(11)
        lb.setFont(f)
        install_theme_hook(lb)
        return lb
        
    def _put_content(self, card: QGroupBox, widget: QWidget):
        """將內容放入卡片"""
        lay: QVBoxLayout = card.layout()
        lay.addWidget(widget)
        
    def _start_update_timer(self):
        """啟動更新定時器"""
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self._update_status)
        self.update_timer.start(2000)  # 每2秒更新一次
        self._update_status()  # 立即更新一次
        
    def _update_status(self):
        """更新狀態顯示"""
        # 更新狀態文字
        status_text = self._get_status_text()
        self.status_label.setText(f"💡 {status_text}")
        
        # 更新性能指標
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            self.cpu_bar.setValue(int(cpu_percent))
            
            mem = psutil.virtual_memory()
            self.mem_bar.setValue(int(mem.percent))
            
            # 獲取 UEP 系統運行時間
            try:
                from core.framework import core_framework
                if hasattr(core_framework, 'start_time') and core_framework.start_time:
                    uptime = datetime.now().timestamp() - core_framework.start_time
                    hours = int(uptime // 3600)
                    minutes = int((uptime % 3600) // 60)
                    seconds = int(uptime % 60)
                    if hours > 0:
                        self.uptime_label.setText(f"系統運行時間: {hours} 小時 {minutes} 分鐘")
                    elif minutes > 0:
                        self.uptime_label.setText(f"系統運行時間: {minutes} 分鐘 {seconds} 秒")
                    else:
                        self.uptime_label.setText(f"系統運行時間: {seconds} 秒")
                else:
                    self.uptime_label.setText("系統運行時間: 未啟動")
            except Exception as e:
                self.uptime_label.setText("系統運行時間: 未啟動")
            
            # 獲取模組狀態
            try:
                from core.registry import module_manager
                loaded_modules = [name for name, mod in module_manager.modules.items() if mod is not None]
                total_modules = len(module_manager.available_modules)
                loaded_count = len(loaded_modules)
                if loaded_count > 0:
                    self.module_label.setText(f"模組狀態: {loaded_count}/{total_modules} 已載入")
                else:
                    self.module_label.setText("模組狀態: 延遲載入模式")
            except Exception:
                self.module_label.setText("模組狀態: 獲取失敗")
                
        except Exception as e:
            error_log(f"[SystemStatus] 更新性能指標失敗: {e}")
            
    def _get_status_text(self) -> str:
        """根據 status_manager 生成自然的狀態描述（多句話，模糊表達）"""
        if not self.status_manager:
            return "系統尚未完全就緒..."
            
        try:
            status = self.status_manager.get_status()
            if not status:
                return "正在感知周圍環境..."
            
            # 獲取各項狀態值
            mood = getattr(status, 'mood', 0.0)
            pride = getattr(status, 'pride', 0.0)
            helpfulness = getattr(status, 'helpfulness', 0.5)
            boredom = getattr(status, 'boredom', 0.0)
            
            # 每個維度獨立成句，不明確標註是哪個狀態
            sentences = []
            
            # 整體氛圍（mood）- 細分區間
            if mood >= 0.8:
                sentences.append("今天感覺特別好。")
            elif mood >= 0.6:
                sentences.append("心情不錯。")
            elif mood >= 0.4:
                sentences.append("還算平穩。")
            elif mood >= 0.2:
                sentences.append("稍微有點起伏。")
            elif mood >= 0:
                sentences.append("有些疲憊感。")
            elif mood >= -0.2:
                sentences.append("需要調整一下。")
            elif mood >= -0.4:
                sentences.append("狀態不太理想。")
            elif mood >= -0.6:
                sentences.append("現在有點吃力。")
            else:
                sentences.append("需要好好休息了。")
            
            # 自信與能力感（pride）
            if pride >= 0.8:
                sentences.append("對自己的表現很滿意。")
            elif pride >= 0.6:
                sentences.append("覺得可以應付大部分事情。")
            elif pride >= 0.4:
                sentences.append("應該能處理好。")
            elif pride >= 0.2:
                sentences.append("不確定能不能做得很好。")
            elif pride >= 0:
                sentences.append("有點擔心會出錯。")
            elif pride >= -0.3:
                sentences.append("信心不太足。")
            elif pride >= -0.6:
                sentences.append("怕搞砸。")
            else:
                sentences.append("覺得自己做不好。")
            
            # 協助意願（helpfulness）
            if helpfulness >= 0.9:
                sentences.append("超想幫忙的！")
            elif helpfulness >= 0.7:
                sentences.append("很樂意協助。")
            elif helpfulness >= 0.5:
                sentences.append("可以幫忙。")
            elif helpfulness >= 0.3:
                sentences.append("如果需要的話會幫忙。")
            elif helpfulness >= 0.1:
                sentences.append("可能會比較慢一些。")
            elif helpfulness >= -0.2:
                sentences.append("現在不太想動。")
            else:
                sentences.append("想要安靜一下。")
            
            # 活力與興趣（boredom）
            if boredom >= 0.8:
                sentences.append("好想做點新鮮的事情！")
            elif boredom >= 0.6:
                sentences.append("希望有些變化。")
            elif boredom >= 0.4:
                sentences.append("可以找點事做。")
            elif boredom >= 0.2:
                sentences.append("維持現狀就好。")
            # boredom 低不需要特別提示
            
            return " ".join(sentences)
            
        except Exception as e:
            error_log(f"[SystemStatus] 獲取狀態失敗: {e}")
            return f"無法獲取系統狀態: {str(e)}"
            
    def _mood_to_text(self, mood: float) -> str:
        """將 mood 值轉換為自然語言"""
        if mood >= 0.7:
            return "😊 心情非常好！感覺充滿活力和正能量。"
        elif mood >= 0.3:
            return "🙂 心情不錯，準備好協助你了。"
        elif mood >= -0.3:
            return "😐 心情還好，有點平淡。"
        elif mood >= -0.7:
            return "😟 心情有點低落，可能需要休息一下。"
        else:
            return "😔 心情不太好，希望能儘快恢復。"
            
    def _pride_to_text(self, pride: float) -> str:
        """將 pride 值轉換為自然語言"""
        if pride >= 0.7:
            return "非常自信，相信能處理各種挑戰"
        elif pride >= 0.3:
            return "有一定自信，能勝任大部分任務"
        elif pride >= -0.3:
            return "自信程度一般"
        elif pride >= -0.7:
            return "有點缺乏自信"
        else:
            return "自信心較低，需要鼓勵"
            
    def _helpfulness_to_text(self, helpfulness: float) -> str:
        """將 helpfulness 值轉換為自然語言"""
        if helpfulness >= 0.8:
            return "非常願意提供幫助"
        elif helpfulness >= 0.5:
            return "願意協助你"
        elif helpfulness >= 0.3:
            return "可以提供基本幫助"
        else:
            return "助人意願較低"
            
    def _boredom_to_text(self, boredom: float) -> str:
        """將 boredom 值轉換為自然語言"""
        if boredom >= 0.7:
            return "非常無聊，希望有點新鮮事"
        elif boredom >= 0.4:
            return "有點無聊"
        elif boredom >= 0.2:
            return "不太無聊"
        else:
            return "一點也不無聊，正忙著呢"
            
    def apply_theme(self):
        """應用主題"""
        if theme_manager:
            theme_manager.apply_app()
        try:
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()
        except Exception:
            pass


class DebugLogWidget(QWidget):
    """調試日誌顯示組件"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        if not PYQT5_AVAILABLE:
            return
            
        install_theme_hook(self)
        self.log_handler = None
        self._build_ui()
        self._setup_log_handler()
        
    def _build_ui(self):
        """構建UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setObjectName("debugLogText")
        # QTextEdit 使用 document().setMaximumBlockCount() 來限制行數
        self.log_text.document().setMaximumBlockCount(1000)
        install_theme_hook(self.log_text)
        
        layout.addWidget(self.log_text)
        
        # 清除按鈕
        clear_btn = QPushButton("清除日誌")
        clear_btn.clicked.connect(self.clear_logs)
        layout.addWidget(clear_btn)
        
    def _setup_log_handler(self):
        """設置日誌處理器"""
        import logging
        
        class QtLogHandler(logging.Handler):
            def __init__(self, text_widget):
                super().__init__()
                self.text_widget = text_widget
                self.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s - %(message)s', 
                                                   datefmt='%H:%M:%S'))
                # 定義不同等級的顏色
                self.colors = {
                    'DEBUG': '#888888',    # 灰色
                    'INFO': '#2196F3',     # 藍色
                    'WARNING': '#FF9800',  # 橙色
                    'ERROR': '#F44336',    # 紅色
                    'CRITICAL': '#D32F2F'  # 深紅色
                }
                
            def emit(self, record):
                try:
                    msg = self.format(record)
                    level = record.levelname
                    color = self.colors.get(level, '#FFFFFF')
                    
                    # 使用 HTML 格式添加顏色
                    colored_msg = f'<span style="color: {color};">{msg}</span>'
                    
                    # 直接在主線程添加（如果在其他線程會自動排隊）
                    try:
                        self.text_widget.append(colored_msg)
                    except RuntimeError:
                        # 如果 widget 已被刪除，忽略錯誤
                        pass
                except Exception as e:
                    # 靜默處理錯誤，避免日誌循環
                    pass
        
        # 創建並添加 handler 到 UEP logger
        self.log_handler = QtLogHandler(self.log_text)
        self.log_handler.setLevel(logging.DEBUG)
        
        logger = logging.getLogger("UEP")
        logger.addHandler(self.log_handler)
        
        debug_log(OPERATION_LEVEL, "[SystemStatus] 日誌處理器已安裝")
        
    def append_log(self, message: str):
        """添加日誌訊息（保留用於手動添加）"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")
        
    def clear_logs(self):
        """清除日誌"""
        self.log_text.clear()
        
    def cleanup(self):
        """清理資源"""
        if self.log_handler:
            import logging
            logger = logging.getLogger("UEP")
            logger.removeHandler(self.log_handler)
            debug_log(OPERATION_LEVEL, "[SystemStatus] 日誌處理器已移除")
        
    def apply_theme(self):
        """應用主題"""
        if theme_manager:
            theme_manager.apply_app()
        try:
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()
        except Exception:
            pass


class SystemStatusWindow(QMainWindow):
    """系統狀態視窗"""
    
    settings_changed = pyqtSignal(str, object)
    window_closed = pyqtSignal()
    
    def __init__(self, status_manager=None, parent=None):
        super().__init__(parent)
        if not PYQT5_AVAILABLE:
            return
            
        self.status_manager = status_manager
        self.settings = QSettings("UEP", "SystemStatus")
        self.config = load_config()
        
        self.setWindowTitle("U.E.P 系統狀態")
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowTitleHint |
            Qt.WindowMinMaxButtonsHint |
            Qt.WindowCloseButtonHint
        )
        
        self.setMinimumSize(900, 700)
        self.resize(1000, 750)
        
        # 設定圖示
        try:
            icon_path = os.path.join(
                os.path.dirname(__file__), 
                "../../../resources/assets/static/Logo.ico"
            )
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except Exception:
            pass
            
        install_theme_hook(self)
        self._build_ui()
        self._wire_theme_manager()
        self.load_settings()
        
    def _build_ui(self):
        """構建UI"""
        # 中央容器
        central = QWidget()
        install_theme_hook(central)
        self.setCentralWidget(central)
        
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # 頂部標題欄
        header = self._create_header()
        layout.addWidget(header)
        
        # 主要內容區域
        self.tab_widget = QTabWidget()
        install_theme_hook(self.tab_widget)
        
        # 狀態分頁
        self.status_widget = SystemStatusWidget(self.status_manager)
        self.tab_widget.addTab(self.status_widget, "系統狀態")
        
        # 調試日誌分頁（根據 user_settings.yaml 的 monitoring.logs.show_logs 設定）
        self.debug_widget = DebugLogWidget()
        self.debug_tab_index = -1  # 記錄日誌分頁的索引
        self._update_log_tab_visibility()
            
        layout.addWidget(self.tab_widget)
        
        # 底部狀態欄
        status_bar = QStatusBar()
        install_theme_hook(status_bar)
        status_bar.showMessage("系統狀態監控中")
        self.setStatusBar(status_bar)
        
    def _create_header(self) -> QFrame:
        """創建頂部標題欄"""
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(110)
        install_theme_hook(header)
        
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(30, 16, 30, 16)
        header_layout.setSpacing(16)
        
        # 標題容器
        title_container = QVBoxLayout()
        title_label = QLabel("系統狀態")
        title_label.setObjectName("mainTitle")
        subtitle = QLabel("查看 U.E.P 的心情與系統運行狀況")
        subtitle.setObjectName("subtitle")
        
        title_container.addWidget(title_label)
        title_container.addWidget(subtitle)
        title_container.addStretch()
        
        header_layout.addLayout(title_container)
        header_layout.addStretch()
        
        # 主題切換按鈕
        self.theme_toggle = QPushButton("🌙")
        self.theme_toggle.setObjectName("themeToggle")
        self.theme_toggle.setFixedSize(48, 48)
        self.theme_toggle.setCursor(Qt.PointingHandCursor)
        btn_font = QFont("Segoe UI Emoji", 18)
        self.theme_toggle.setFont(btn_font)
        self.theme_toggle.clicked.connect(self.toggle_theme)
        install_theme_hook(self.theme_toggle)
        
        header_layout.addWidget(self.theme_toggle)
        
        return header
        
    def _wire_theme_manager(self):
        """連接主題管理器"""
        if theme_manager:
            # 初始化主題按鈕圖示
            temp_is_dark = True
            self.theme_toggle.setText("☀️" if temp_is_dark else "🌙")
            
            # 訂閱主題變化事件
            theme_manager.theme_changed.connect(self._on_theme_changed)
            
            # 應用當前主題
            theme_manager.apply_app()
            self._on_theme_changed(theme_manager.theme.value)
            
    def _on_theme_changed(self, theme_name: str):
        """主題變化回調"""
        is_dark = self._tm_is_dark()
        self.theme_toggle.setText("☀️" if is_dark else "🌙")
        
    def _tm_is_dark(self) -> bool:
        """判斷當前主題是否為暗色"""
        if not theme_manager:
            return False
        return theme_manager.theme == Theme.DARK
        
    def toggle_theme(self):
        """切換主題"""
        if theme_manager:
            theme_manager.toggle()
            if self.status_widget:
                self.status_widget.apply_theme()
            if self.debug_widget:
                self.debug_widget.apply_theme()
                
    def _update_log_tab_visibility(self):
        """根據設定更新日誌分頁可見性"""
        from configs.user_settings_manager import get_user_setting
        show_logs = get_user_setting("monitoring.logs.show_logs", False)
        
        # 檢查日誌分頁是否已存在
        current_index = -1
        for i in range(self.tab_widget.count()):
            if self.tab_widget.widget(i) == self.debug_widget:
                current_index = i
                break
        
        if show_logs:
            # 需要顯示日誌分頁
            if current_index == -1:
                # 分頁不存在，添加
                self.debug_tab_index = self.tab_widget.addTab(self.debug_widget, "調試日誌")
                debug_log(OPERATION_LEVEL, "[SystemStatus] 日誌分頁已顯示")
        else:
            # 需要隱藏日誌分頁
            if current_index >= 0:
                # 分頁存在，移除
                self.tab_widget.removeTab(current_index)
                self.debug_tab_index = -1
                debug_log(OPERATION_LEVEL, "[SystemStatus] 日誌分頁已隱藏")
                
    def on_settings_changed(self, key: str, value):
        """設定變更回調"""
        if key == "monitoring.logs.show_logs":
            debug_log(OPERATION_LEVEL, f"[SystemStatus] 檢測到設定變更: {key} = {value}")
            self._update_log_tab_visibility()
                
    def load_settings(self):
        """載入設定"""
        try:
            # 載入視窗位置和大小
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
                
            # 載入當前分頁
            current_tab = self.settings.value("current_tab", 0, type=int)
            if 0 <= current_tab < self.tab_widget.count():
                self.tab_widget.setCurrentIndex(current_tab)
                
        except Exception as e:
            error_log(f"[SystemStatus] 載入設定失敗: {e}")
            
    def save_settings(self):
        """保存設定"""
        try:
            self.settings.setValue("geometry", self.saveGeometry())
            self.settings.setValue("current_tab", self.tab_widget.currentIndex())
            self.settings.sync()
        except Exception as e:
            error_log(f"[SystemStatus] 保存設定失敗: {e}")
            
    def closeEvent(self, event):
        """視窗關閉事件"""
        self.save_settings()
        
        # 清理日誌處理器
        if self.debug_widget and hasattr(self.debug_widget, 'cleanup'):
            self.debug_widget.cleanup()
            
        self.window_closed.emit()
        super().closeEvent(event)


def create_test_window(status_manager=None):
    """創建測試視窗"""
    if not PYQT5_AVAILABLE:
        return None, None
        
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
        
    if theme_manager:
        theme_manager.apply_app()
        
    window = SystemStatusWindow(status_manager)
    window.show()
    
    return app, window


if __name__ == "__main__":
    app, window = create_test_window()
    if app and window:
        sys.exit(app.exec_())
