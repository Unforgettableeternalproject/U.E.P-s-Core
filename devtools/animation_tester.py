"""
ANI 模組動畫測試器
提供圖形界面測試所有註冊的動畫，並可即時調整參數
"""

import sys
import os
from pathlib import Path

# 確保可以導入專案模組
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QSlider, QSpinBox, QDoubleSpinBox,
    QGroupBox, QCheckBox, QTextEdit, QSplitter, QFrame, QListWidget
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPixmap, QPainter, QFont, QColor

from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log
from utils.logger import force_enable_file_logging


class AnimationPreviewWidget(QWidget):
    """動畫預覽窗口"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_pixmap = None
        self.config_zoom = 1.0  # config 的縮放
        self.view_zoom = 0.3     # 視圖縮放（滾輪控制）
        
        # 拖曳相關屬性
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.is_dragging = False
        self.last_mouse_pos = None
        
        # 網格顯示
        self.show_grid = False
        self.grid_size = 50  # 網格大小（像素）
        
        # 原始尺寸邊框顯示
        self.show_original_size = False
        
        self.setMinimumSize(400, 400)
        self.setStyleSheet("""
            background-color: #1e1e1e;
            border: 2px solid #3c3c3c;
            border-radius: 4px;
        """)
        self.setFocusPolicy(Qt.WheelFocus)
        self.setMouseTracking(True)
        
    def set_pixmap(self, pixmap: QPixmap):
        """設置要顯示的圖片"""
        self.current_pixmap = pixmap
        self.update()
        
    def set_config_zoom(self, zoom: float):
        """設置 config 的縮放因子"""
        self.config_zoom = zoom
        self.update()
        
    def set_view_zoom(self, zoom: float):
        """設置視圖縮放因子（滾輪控制）"""
        self.view_zoom = max(0.1, min(5.0, zoom))  # 限制範圍 0.1 - 5.0
        self.update()
        
    def get_total_zoom(self) -> float:
        """獲取總縮放（config zoom * view zoom）"""
        return self.config_zoom * self.view_zoom
    
    def set_show_grid(self, show: bool):
        """設置是否顯示網格"""
        self.show_grid = show
        self.update()
    
    def set_show_original_size(self, show: bool):
        """設置是否顯示原始尺寸邊框"""
        self.show_original_size = show
        self.update()
    
    def wheelEvent(self, event):
        """滾輪事件處理 - 縮放預覽"""
        # 獲取滾輪滾動量
        delta = event.angleDelta().y()
        
        # 計算縮放增量（每次 10%）
        zoom_delta = 0.1 if delta > 0 else -0.1
        
        # 更新視圖縮放
        new_zoom = self.view_zoom + zoom_delta
        self.set_view_zoom(new_zoom)
        
        event.accept()
    
    def mousePressEvent(self, event):
        """滑鼠按下事件 - 開始拖曳"""
        if event.button() == Qt.LeftButton:
            self.is_dragging = True
            self.last_mouse_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
    
    def mouseMoveEvent(self, event):
        """滑鼠移動事件 - 拖曳預覽"""
        if self.is_dragging and self.last_mouse_pos:
            # 計算移動量
            delta = event.pos() - self.last_mouse_pos
            self.drag_offset_x += delta.x()
            self.drag_offset_y += delta.y()
            self.last_mouse_pos = event.pos()
            self.update()
            event.accept()
        elif not self.is_dragging:
            # 顯示可拖曳的游標
            if self.current_pixmap:
                self.setCursor(Qt.OpenHandCursor)
    
    def mouseReleaseEvent(self, event):
        """滑鼠釋放事件 - 停止拖曳"""
        if event.button() == Qt.LeftButton:
            self.is_dragging = False
            self.setCursor(Qt.OpenHandCursor if self.current_pixmap else Qt.ArrowCursor)
            event.accept()
    
    def mouseDoubleClickEvent(self, event):
        """滑鼠雙擊事件 - 重置拖曳偏移"""
        if event.button() == Qt.LeftButton:
            self.drag_offset_x = 0
            self.drag_offset_y = 0
            self.update()
            event.accept()
        
    def paintEvent(self, event):
        """繪製預覽"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # 繪製網格（如果啟用）
        if self.show_grid:
            painter.setPen(QColor(80, 80, 80, 100))
            # 繪製垂直線
            for x in range(0, self.width(), self.grid_size):
                painter.drawLine(x, 0, x, self.height())
            # 繪製水平線
            for y in range(0, self.height(), self.grid_size):
                painter.drawLine(0, y, self.width(), y)
        
        if self.current_pixmap and not self.current_pixmap.isNull():
            # 計算總縮放
            total_zoom = self.get_total_zoom()
            scaled_width = int(self.current_pixmap.width() * total_zoom)
            scaled_height = int(self.current_pixmap.height() * total_zoom)
            
            # 計算中心位置（加上拖曳偏移）
            x = (self.width() - scaled_width) // 2 + self.drag_offset_x
            y = (self.height() - scaled_height) // 2 + self.drag_offset_y
            
            # 縮放並繪製
            scaled_pixmap = self.current_pixmap.scaled(
                scaled_width, scaled_height,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )
            painter.drawPixmap(x, y, scaled_pixmap)
            
            # 繪製原始尺寸邊框（如果啟用）
            if self.show_original_size:
                # 使用淺藍色細線邊框
                painter.setPen(QColor(100, 180, 255, 180))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(x, y, scaled_width, scaled_height)
                
                # 在邊框右上角顯示原始尺寸標註
                painter.setPen(QColor(100, 180, 255, 220))
                size_text = f"{self.current_pixmap.width()} × {self.current_pixmap.height()}"
                text_x = x + scaled_width - 10
                text_y = y + 20
                
                # 繪製半透明背景
                text_rect = painter.fontMetrics().boundingRect(size_text)
                text_rect.adjust(-4, -2, 4, 2)
                text_rect.moveTo(text_x - text_rect.width(), text_y - text_rect.height())
                painter.fillRect(text_rect, QColor(30, 30, 30, 180))
                
                # 繪製文字
                painter.drawText(text_x - painter.fontMetrics().horizontalAdvance(size_text), text_y, size_text)
            
            # 在左上角顯示資訊（使用陰影效果）
            font = painter.font()
            font.setPointSize(9)
            painter.setFont(font)
            
            info_text = (
                f"【原始尺寸】{self.current_pixmap.width()} × {self.current_pixmap.height()} px\n"
                f"【縮放控制】\n"
                f"  視圖: {self.view_zoom:.1f}x (滾輪)\n"
                f"  Config: {self.config_zoom:.1f}x\n"
                f"  總計: {total_zoom:.1f}x\n"
                f"【拖曳偏移】\n"
                f"  X: {self.drag_offset_x:+d}  Y: {self.drag_offset_y:+d}"
            )
            
            # 繪製文字陰影（黑色描邊效果）
            painter.setPen(QColor(0, 0, 0, 200))
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx != 0 or dy != 0:
                        painter.drawText(10 + dx, 20 + dy, info_text)
            
            # 繪製白色文字
            painter.setPen(Qt.white)
            painter.drawText(10, 20, info_text)
        else:
            # 顯示提示文字（帶背景）
            hint_rect = self.rect().adjusted(50, 50, -50, -50)
            painter.fillRect(hint_rect, QColor(40, 40, 40, 200))
            
            painter.setPen(Qt.white)
            font = painter.font()
            font.setPointSize(11)
            painter.setFont(font)
            painter.drawText(
                self.rect(), Qt.AlignCenter,
                "═══ 動畫預覽區域 ═══\n\n"
                "請選擇並播放動畫\n\n"
                "【操作說明】\n"
                "• 滾輪：縮放預覽\n"
                "• 左鍵拖曳：移動預覽\n"
                "• 雙擊：重置位置"
            )


class AnimationTesterWindow(QMainWindow):
    """動畫測試主窗口"""
    
    def __init__(self):
        super().__init__()
        self.ani_module = None
        self.config = load_config()
        self.current_animation = None
        self.is_playing = False
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_preview)
        
        self.init_ui()
        self.load_ani_module()
        
    def init_ui(self):
        """初始化UI"""
        self.setWindowTitle("U.E.P 動畫測試器")
        self.setGeometry(100, 100, 1200, 800)
        
        # 設置窗口圖標
        icon_path = project_root / "resources" / "assets" / "static" / "Logo.ico"
        if icon_path.exists():
            from PyQt5.QtGui import QIcon
            self.setWindowIcon(QIcon(str(icon_path)))
        
        # 主窗口部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        
        # === 左側：動畫列表與控制 ===
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_panel.setMaximumWidth(350)
        
        # 動畫列表
        list_group = QGroupBox("動畫列表")
        list_layout = QVBoxLayout()
        
        # 重新整理按鈕
        refresh_btn = QPushButton("🔄 重新整理")
        refresh_btn.clicked.connect(self.reload_animations)
        list_layout.addWidget(refresh_btn)
        
        self.animation_list = QListWidget()
        self.animation_list.itemClicked.connect(self.on_animation_selected)
        list_layout.addWidget(self.animation_list)
        list_group.setLayout(list_layout)
        left_layout.addWidget(list_group)
        
        # 播放控制
        control_group = QGroupBox("播放控制")
        control_layout = QVBoxLayout()
        
        # 播放/停止按鈕
        btn_layout = QHBoxLayout()
        self.play_btn = QPushButton("▶ 播放")
        self.play_btn.clicked.connect(self.play_animation)
        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.clicked.connect(self.stop_animation)
        btn_layout.addWidget(self.play_btn)
        btn_layout.addWidget(self.stop_btn)
        control_layout.addLayout(btn_layout)
        
        # 循環播放選項
        self.loop_checkbox = QCheckBox("循環播放")
        self.loop_checkbox.setChecked(True)
        control_layout.addWidget(self.loop_checkbox)
        
        # 網格顯示選項
        self.grid_checkbox = QCheckBox("顯示網格")
        self.grid_checkbox.stateChanged.connect(self.on_grid_toggle)
        control_layout.addWidget(self.grid_checkbox)
        
        # 原始尺寸邊框選項
        self.size_border_checkbox = QCheckBox("顯示原始尺寸邊框")
        self.size_border_checkbox.setChecked(True)  # 預設開啟
        self.size_border_checkbox.stateChanged.connect(self.on_size_border_toggle)
        control_layout.addWidget(self.size_border_checkbox)
        
        # 當前幀信息
        self.frame_label = QLabel("當前幀: 0 / 0")
        control_layout.addWidget(self.frame_label)
        
        control_group.setLayout(control_layout)
        left_layout.addWidget(control_group)
        
        # 參數調整
        params_group = QGroupBox("參數調整")
        params_layout = QVBoxLayout()
        
        # 縮放
        zoom_layout = QHBoxLayout()
        zoom_layout.addWidget(QLabel("縮放:"))
        self.zoom_spinbox = QDoubleSpinBox()
        self.zoom_spinbox.setRange(0.1, 3.0)
        self.zoom_spinbox.setSingleStep(0.1)
        self.zoom_spinbox.setValue(1.0)
        self.zoom_spinbox.valueChanged.connect(self.on_zoom_changed)
        zoom_layout.addWidget(self.zoom_spinbox)
        params_layout.addLayout(zoom_layout)
        
        # 每幀時長
        duration_layout = QHBoxLayout()
        duration_layout.addWidget(QLabel("幀時長(s):"))
        self.duration_spinbox = QDoubleSpinBox()
        self.duration_spinbox.setRange(0.01, 1.0)
        self.duration_spinbox.setSingleStep(0.01)
        self.duration_spinbox.setValue(0.08)
        self.duration_spinbox.valueChanged.connect(self.on_param_changed)
        duration_layout.addWidget(self.duration_spinbox)
        params_layout.addLayout(duration_layout)
        
        # X偏移
        offset_x_layout = QHBoxLayout()
        offset_x_layout.addWidget(QLabel("X偏移:"))
        self.offset_x_spinbox = QSpinBox()
        self.offset_x_spinbox.setRange(-200, 200)
        self.offset_x_spinbox.setValue(0)
        self.offset_x_spinbox.valueChanged.connect(self.on_param_changed)
        offset_x_layout.addWidget(self.offset_x_spinbox)
        params_layout.addLayout(offset_x_layout)
        
        # Y偏移
        offset_y_layout = QHBoxLayout()
        offset_y_layout.addWidget(QLabel("Y偏移:"))
        self.offset_y_spinbox = QSpinBox()
        self.offset_y_spinbox.setRange(-200, 200)
        self.offset_y_spinbox.setValue(0)
        self.offset_y_spinbox.valueChanged.connect(self.on_param_changed)
        offset_y_layout.addWidget(self.offset_y_spinbox)
        params_layout.addLayout(offset_y_layout)
        
        # 應用按鈕
        apply_btn = QPushButton("應用到 config.yaml")
        apply_btn.clicked.connect(self.apply_to_config)
        params_layout.addWidget(apply_btn)
        
        params_group.setLayout(params_layout)
        left_layout.addWidget(params_group)
        
        # 動畫信息
        info_group = QGroupBox("動畫信息")
        info_layout = QVBoxLayout()
        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setMaximumHeight(150)
        info_layout.addWidget(self.info_text)
        info_group.setLayout(info_layout)
        left_layout.addWidget(info_group)
        
        left_layout.addStretch()
        
        # === 右側：預覽區域 ===
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        
        preview_group = QGroupBox("🎬 動畫預覽與測試區域")
        preview_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 13px;
                border: 2px solid #4CAF50;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 15px;
                padding: 0 5px;
                color: #4CAF50;
            }
        """)
        preview_layout = QVBoxLayout()
        preview_layout.setContentsMargins(8, 8, 8, 8)
        self.preview_widget = AnimationPreviewWidget()
        preview_layout.addWidget(self.preview_widget)
        preview_group.setLayout(preview_layout)
        right_layout.addWidget(preview_group)
        
        # 添加到主布局
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        main_layout.addWidget(splitter)
        
        # 狀態欄
        self.statusBar().showMessage("就緒")
        
    def load_ani_module(self):
        """載入 ANI 模組"""
        try:
            from modules.ani_module import register
            self.ani_module = register()
            
            if not self.ani_module:
                error_log("[AnimationTester] ANI 模組載入失敗")
                self.statusBar().showMessage("❌ ANI 模組載入失敗")
                return
                
            # 初始化前端
            if not self.ani_module.initialize_frontend():
                error_log("[AnimationTester] ANI 前端初始化失敗")
                self.statusBar().showMessage("❌ ANI 前端初始化失敗")
                return
                
            # 載入動畫列表
            self.load_animation_list()
            self.statusBar().showMessage(f"✅ 已載入 {len(self.ani_module.manager.clips)} 個動畫")
            info_log(f"[AnimationTester] ANI 模組載入成功")
            
        except Exception as e:
            error_log(f"[AnimationTester] 載入 ANI 模組失敗: {e}")
            self.statusBar().showMessage(f"❌ 錯誤: {e}")
            
    def load_animation_list(self):
        """載入動畫列表到UI"""
        if not self.ani_module:
            return
            
        self.animation_list.clear()
        
        # 獲取所有已註冊的動畫
        clips = self.ani_module.manager.clips
        
        for name in sorted(clips.keys()):
            self.animation_list.addItem(name)
            
        info_log(f"[AnimationTester] 載入了 {len(clips)} 個動畫")
    
    def reload_animations(self):
        """重新載入動畫列表（熱重載）"""
        try:
            # 保存當前用戶修改的參數
            saved_params = None
            if self.current_animation:
                saved_params = {
                    'zoom': self.zoom_spinbox.value(),
                    'duration': self.duration_spinbox.value(),
                    'offset_x': self.offset_x_spinbox.value(),
                    'offset_y': self.offset_y_spinbox.value(),
                    'loop': self.loop_checkbox.isChecked()
                }
            
            # 停止當前播放
            if self.is_playing:
                self.stop_animation()
            
            # 重新載入配置文件並重新註冊動畫
            info_log("[AnimationTester] 開始熱重載...")
            
            # 重新讀取 ANI 模組的配置文件
            from configs.config_loader import load_module_config
            ani_config = load_module_config("ani_module")
            
            # 清空現有動畫並重新註冊
            self.ani_module.manager.clips.clear()
            self.ani_module.config = ani_config
            self.ani_module._apply_config_for_clips(ani_config)
            
            # 重新初始化前端（定時器等）
            self.ani_module.initialize_frontend()
            
            # 重新載入動畫列表
            self.load_animation_list()
            
            # 如果有選中的動畫，重新選擇
            if self.current_animation:
                items = self.animation_list.findItems(self.current_animation, Qt.MatchExactly)
                if items:
                    self.animation_list.setCurrentItem(items[0])
                    # 先讓 on_animation_selected 載入配置
                    self.on_animation_selected(items[0])
                    
                    # 然後恢復用戶修改的參數
                    if saved_params:
                        self.zoom_spinbox.setValue(saved_params['zoom'])
                        self.duration_spinbox.setValue(saved_params['duration'])
                        self.offset_x_spinbox.setValue(saved_params['offset_x'])
                        self.offset_y_spinbox.setValue(saved_params['offset_y'])
                        self.loop_checkbox.setChecked(saved_params['loop'])
                        # 更新預覽縮放
                        self.preview_widget.set_config_zoom(saved_params['zoom'])
            
            self.statusBar().showMessage("✅ 重新整理完成")
            info_log("[AnimationTester] 熱重載完成")
        except Exception as e:
            error_msg = str(e)
            self.statusBar().showMessage(f"❌ 重新整理失敗: {error_msg}")
            error_log(f"[AnimationTester] 熱重載失敗: {error_msg}")
    
    def on_grid_toggle(self, state):
        """網格顯示開關"""
        self.preview_widget.set_show_grid(state == Qt.Checked)
    
    def on_size_border_toggle(self, state):
        """原始尺寸邊框顯示開關"""
        self.preview_widget.set_show_original_size(state == Qt.Checked)
        
    def on_animation_selected(self, item):
        """選擇動畫時的處理"""
        animation_name = item.text()
        self.current_animation = animation_name
        
        # 獲取動畫信息
        clip_info = self.ani_module.get_clip_info(animation_name)
        
        if clip_info:
            # 更新參數UI
            self.zoom_spinbox.setValue(clip_info.get('zoom', 1.0))
            self.duration_spinbox.setValue(1.0 / clip_info['fps'])
            self.offset_x_spinbox.setValue(clip_info.get('offset_x', 0))
            self.offset_y_spinbox.setValue(clip_info.get('offset_y', 0))
            self.loop_checkbox.setChecked(clip_info['loop'])
            
            # 更新信息顯示
            info_text = f"""
動畫名稱: {animation_name}
總幀數: {clip_info['frames']}
幀率: {clip_info['fps']:.2f} fps
時長: {clip_info['frames'] / clip_info['fps']:.2f} 秒
循環: {'是' if clip_info['loop'] else '否'}
縮放: {clip_info.get('zoom', 1.0)}
偏移: ({clip_info.get('offset_x', 0)}, {clip_info.get('offset_y', 0)})
            """
            self.info_text.setText(info_text.strip())
            
            # 更新幀標籤
            self.frame_label.setText(f"當前幀: 0 / {clip_info['frames']}")
            
            # 顯示第一幀
            if not self.is_playing:
                self.show_first_frame(animation_name)
            
            info_log(f"[AnimationTester] 選擇動畫: {animation_name}")
    
    def show_first_frame(self, animation_name: str):
        """顯示動畫的第一幀"""
        try:
            # 播放動畫（不循環）
            result = self.ani_module.play(animation_name, loop=False)
            
            if result.get('success') or result.get('status') == 'coalesced':
                # 立即停止並獲取第一幀
                first_frame = self.ani_module.get_current_frame()
                self.ani_module.stop()
                
                if first_frame and not first_frame.isNull():
                    self.preview_widget.set_pixmap(first_frame)
                    self.preview_widget.set_config_zoom(self.zoom_spinbox.value())
        except Exception as e:
            error_log(f"[AnimationTester] 顯示第一幀失敗: {e}")
        
    def play_animation(self):
        """播放動畫"""
        if not self.current_animation or not self.ani_module:
            self.statusBar().showMessage("⚠️ 請先選擇動畫")
            return
            
        # 停止現有播放
        if self.is_playing:
            self.stop_animation()
            
        # 播放動畫
        loop = self.loop_checkbox.isChecked()
        result = self.ani_module.play(self.current_animation, loop=loop)
        
        if result.get('success') or result.get('status') == 'coalesced':
            self.is_playing = True
            self.play_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.animation_list.setEnabled(False)  # 播放時鎖定列表
            
            # 啟動預覽更新
            fps = 1.0 / self.duration_spinbox.value()
            interval = int(1000 / fps)
            self.update_timer.start(interval)
            
            self.statusBar().showMessage(f"▶ 播放中: {self.current_animation}")
            info_log(f"[AnimationTester] 開始播放: {self.current_animation}")
        else:
            error_msg = result.get('error', '未知錯誤')
            self.statusBar().showMessage(f"❌ 播放失敗: {error_msg}")
            error_log(f"[AnimationTester] 播放失敗: {error_msg}")
            
    def stop_animation(self):
        """停止動畫"""
        if self.ani_module:
            self.ani_module.stop()
            
        self.is_playing = False
        self.play_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.animation_list.setEnabled(True)  # 停止時解鎖列表
        self.update_timer.stop()
        
        self.statusBar().showMessage(f"■ 已停止")
        info_log(f"[AnimationTester] 停止播放")
        
    def update_preview(self):
        """更新預覽畫面"""
        if not self.ani_module or not self.is_playing:
            return
            
        try:
            # 獲取當前幀
            pixmap = self.ani_module.get_current_frame()
            
            if pixmap and not pixmap.isNull():
                self.preview_widget.set_pixmap(pixmap)
                self.preview_widget.set_config_zoom(self.zoom_spinbox.value())
                
                # 更新幀信息
                status = self.ani_module.get_current_animation_status()
                if status and status.get('is_playing'):
                    frame = status.get('frame', 0)
                    clip_info = self.ani_module.get_clip_info(self.current_animation)
                    if clip_info:
                        total = clip_info['frames']
                        self.frame_label.setText(f"當前幀: {frame} / {total}")
            else:
                # 動畫可能已結束
                status = self.ani_module.get_current_animation_status()
                if not status or not status.get('is_playing'):
                    self.stop_animation()
                    self.statusBar().showMessage(f"✓ 播放完成")
                    
        except Exception as e:
            error_log(f"[AnimationTester] 更新預覽失敗: {e}")
            
    def on_zoom_changed(self):
        """縮放參數改變時的處理"""
        if not self.current_animation or not self.ani_module:
            return
            
        # 更新預覽的 config 縮放
        self.preview_widget.set_config_zoom(self.zoom_spinbox.value())
        
    def on_param_changed(self):
        """其他參數改變時的處理"""
        if not self.current_animation or not self.ani_module:
            return
            
        # 如果正在播放，更新幀率
        if self.is_playing:
            fps = 1.0 / self.duration_spinbox.value()
            interval = int(1000 / fps)
            self.update_timer.setInterval(interval)
        
    def apply_to_config(self):
        """將當前參數應用到 config.yaml"""
        if not self.current_animation:
            self.statusBar().showMessage("⚠️ 請先選擇動畫")
            return
            
        try:
            # 準備更新的值
            new_values = {
                'zoom': self.zoom_spinbox.value(),
                'frame_duration': self.duration_spinbox.value(),
                'offsetX': self.offset_x_spinbox.value(),
                'offsetY': self.offset_y_spinbox.value(),
                'loop': self.loop_checkbox.isChecked()
            }
            
            # 讀取 config 文件
            config_path = project_root / "modules" / "ani_module" / "config.yaml"
            
            import yaml
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f)
                
            # 更新對應的動畫配置
            clips = config_data.get('resources', {}).get('clips', {})
            
            if self.current_animation in clips:
                clips[self.current_animation].update(new_values)
                
                # 寫回文件
                with open(config_path, 'w', encoding='utf-8') as f:
                    yaml.dump(config_data, f, allow_unicode=True, sort_keys=False)
                    
                self.statusBar().showMessage(f"✅ 已更新 {self.current_animation} 的配置，正在重新載入...")
                info_log(f"[AnimationTester] 已更新 {self.current_animation} 配置: {new_values}")
                
                # 自動熱重載
                self.reload_animations()
            else:
                self.statusBar().showMessage(f"⚠️ 在配置中找不到 {self.current_animation}")
                
        except Exception as e:
            error_log(f"[AnimationTester] 更新配置失敗: {e}")
            self.statusBar().showMessage(f"❌ 更新失敗: {e}")
            
    def closeEvent(self, event):
        """關閉窗口時清理"""
        self.stop_animation()
        if self.ani_module:
            try:
                self.ani_module.shutdown()
            except:
                pass
        event.accept()


def main():
    """主程序入口"""
    app = QApplication(sys.argv)
    
    # 設置應用程式樣式
    app.setStyle('Fusion')
    
    # 深色主題
    from PyQt5.QtGui import QPalette, QColor
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.WindowText, Qt.white)
    palette.setColor(QPalette.Base, QColor(35, 35, 35))
    palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ToolTipBase, QColor(25, 25, 25))
    palette.setColor(QPalette.ToolTipText, Qt.white)
    palette.setColor(QPalette.Text, Qt.white)
    palette.setColor(QPalette.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ButtonText, Qt.white)
    palette.setColor(QPalette.BrightText, Qt.red)
    palette.setColor(QPalette.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.HighlightedText, QColor(35, 35, 35))
    app.setPalette(palette)
    
    # force_enable_file_logging()
    # 創建主窗口
    window = AnimationTesterWindow()
    window.show()
    
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
