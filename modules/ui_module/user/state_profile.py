# state_profile.py
import os
import sys
from typing import Dict, Any, Optional

try:
    from PyQt5.QtWidgets import (
        QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QTabWidget, QLabel, QGroupBox, QScrollArea, QAbstractScrollArea,
        QFrame, QPushButton, QCheckBox, QSpinBox, QSizePolicy,
        QSlider, QComboBox, QLineEdit, QTextEdit,
        QFormLayout, QGridLayout, QApplication, QMessageBox, QListWidget, QStatusBar
    )
    from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, QSettings)
    from PyQt5.QtGui import QFont, QIcon
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    print("[StateProfile] PyQt5 不可用")


class UEPStateProfileWidget(QWidget):
    settings_changed = pyqtSignal(str, object)
    apply_requested = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        if not PYQT5_AVAILABLE:
            return
        self.settings = QSettings("UEP", "StateProfile")
        self.dark_mode = False
        self.init_ui()
        self.load_settings()

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(15)

        scroll_area = QScrollArea()
        self._tall_scroll(scroll_area)

        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(30, 30, 30, 30)
        scroll_layout.setSpacing(20)

        status_group = self.create_status_control_group()
        scroll_layout.addWidget(status_group)

        mood_group = self.create_mood_control_group()
        scroll_layout.addWidget(mood_group)

        pride_group = self.create_pride_control_group()
        scroll_layout.addWidget(pride_group)

        example3_group = self.create_example3_control_group()
        scroll_layout.addWidget(example3_group)

        example4_group = self.create_example4_control_group()
        scroll_layout.addWidget(example4_group)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        main_layout.addWidget(scroll_area, 1)

        self.apply_theme()

    def create_status_control_group(self):
        group = QGroupBox("系統狀態 (Status)")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.status_combo = QComboBox()
        self.status_combo.addItems([
            "Active - 活躍模式",
            "Idle - 待機模式",
            "Sleeping - 休眠模式",
            "Processing - 處理中",
            "Learning - 學習模式"
        ])
        self.status_combo.currentTextChanged.connect(
            lambda v: self.settings_changed.emit("status_mode", v.split(" - ")[0])
        )
        layout.addRow("當前狀態:", self.status_combo)

        activity_layout = QHBoxLayout()
        self.activity_slider = QSlider(Qt.Horizontal)
        self.activity_slider.setRange(0, 100)
        self.activity_slider.setValue(75)
        self.activity_label = QLabel("75%")
        activity_layout.addWidget(self.activity_slider)
        activity_layout.addWidget(self.activity_label)
        self.activity_slider.valueChanged.connect(self._update_activity)
        layout.addRow("活躍度:", activity_layout)

        response_layout = QHBoxLayout()
        self.response_slider = QSlider(Qt.Horizontal)
        self.response_slider.setRange(1, 10)
        self.response_slider.setValue(7)
        self.response_speed_label = QLabel("7")
        response_layout.addWidget(self.response_slider)
        response_layout.addWidget(self.response_speed_label)
        self.response_slider.valueChanged.connect(
            lambda v: self.response_speed_label.setText(str(v))
        )
        layout.addRow("響應速度:", response_layout)

        self.auto_adjust_checkbox = QCheckBox("啟用自動狀態調整")
        self.auto_adjust_checkbox.setChecked(True)
        self.auto_adjust_checkbox.stateChanged.connect(
            lambda s: self.settings_changed.emit("auto_adjust", s == Qt.Checked)
        )
        layout.addRow(self.auto_adjust_checkbox)

        return self._loose_group(group)

    def create_mood_control_group(self):
        group = QGroupBox("情緒狀態 (Mood)")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        info_label = QLabel("調整 U.E.P 的情緒表達與互動風格")
        info_label.setObjectName("infoText")
        layout.addWidget(info_label)

        mood_select_layout = QHBoxLayout()
        mood_label = QLabel("情緒模式:")
        self.mood_combo = QComboBox()
        self.mood_combo.addItems([
            "😊 友善 (Friendly)",
            "🤔 專業 (Professional)",
            "😌 平靜 (Calm)",
            "😄 活潑 (Energetic)",
            "🧐 嚴肅 (Serious)"
        ])
        self.mood_combo.currentTextChanged.connect(
            lambda v: self.settings_changed.emit("mood_mode", v)
        )
        mood_select_layout.addWidget(mood_label)
        mood_select_layout.addWidget(self.mood_combo, 1)
        layout.addLayout(mood_select_layout)

        form_layout = QFormLayout()
        form_layout.setSpacing(12)

        friendliness_layout = QHBoxLayout()
        self.friendliness_slider = QSlider(Qt.Horizontal)
        self.friendliness_slider.setRange(0, 100)
        self.friendliness_slider.setValue(80)
        self.friendliness_label = QLabel("80%")
        friendliness_layout.addWidget(self.friendliness_slider)
        friendliness_layout.addWidget(self.friendliness_label)
        self.friendliness_slider.valueChanged.connect(
            lambda v: self.friendliness_label.setText(f"{v}%")
        )
        form_layout.addRow("友善度:", friendliness_layout)

        empathy_layout = QHBoxLayout()
        self.empathy_slider = QSlider(Qt.Horizontal)
        self.empathy_slider.setRange(0, 100)
        self.empathy_slider.setValue(70)
        self.empathy_label = QLabel("70%")
        empathy_layout.addWidget(self.empathy_slider)
        empathy_layout.addWidget(self.empathy_label)
        self.empathy_slider.valueChanged.connect(
            lambda v: self.empathy_label.setText(f"{v}%")
        )
        form_layout.addRow("同理心:", empathy_layout)

        humor_layout = QHBoxLayout()
        self.humor_slider = QSlider(Qt.Horizontal)
        self.humor_slider.setRange(0, 100)
        self.humor_slider.setValue(50)
        self.humor_label = QLabel("50%")
        humor_layout.addWidget(self.humor_slider)
        humor_layout.addWidget(self.humor_label)
        self.humor_slider.valueChanged.connect(
            lambda v: self.humor_label.setText(f"{v}%")
        )
        form_layout.addRow("幽默感:", humor_layout)

        layout.addLayout(form_layout)

        self.mood_memory_checkbox = QCheckBox("記住上次的情緒狀態")
        self.mood_memory_checkbox.setChecked(True)
        layout.addWidget(self.mood_memory_checkbox)

        return self._loose_group(group)

    def create_pride_control_group(self):
        group = QGroupBox("自信度 (Pride)")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        info_label = QLabel("調整 U.E.P 的自信程度與表達方式")
        info_label.setObjectName("infoText")
        layout.addWidget(info_label)

        form_layout = QFormLayout()
        form_layout.setSpacing(12)

        pride_layout = QHBoxLayout()
        self.pride_slider = QSlider(Qt.Horizontal)
        self.pride_slider.setRange(0, 100)
        self.pride_slider.setValue(65)
        self.pride_label = QLabel("65%")
        pride_layout.addWidget(self.pride_slider)
        pride_layout.addWidget(self.pride_label)
        self.pride_slider.valueChanged.connect(self._update_pride)
        form_layout.addRow("自信水平:", pride_layout)

        certainty_layout = QHBoxLayout()
        self.certainty_slider = QSlider(Qt.Horizontal)
        self.certainty_slider.setRange(0, 100)
        self.certainty_slider.setValue(75)
        self.certainty_label = QLabel("75%")
        certainty_layout.addWidget(self.certainty_slider)
        certainty_layout.addWidget(self.certainty_label)
        self.certainty_slider.valueChanged.connect(
            lambda v: self.certainty_label.setText(f"{v}%")
        )
        form_layout.addRow("表達確定性:", certainty_layout)

        initiative_layout = QHBoxLayout()
        self.initiative_slider = QSlider(Qt.Horizontal)
        self.initiative_slider.setRange(0, 100)
        self.initiative_slider.setValue(60)
        self.initiative_label = QLabel("60%")
        initiative_layout.addWidget(self.initiative_slider)
        initiative_layout.addWidget(self.initiative_label)
        self.initiative_slider.valueChanged.connect(
            lambda v: self.initiative_label.setText(f"{v}%")
        )
        form_layout.addRow("主動性:", initiative_layout)

        layout.addLayout(form_layout)

        self.pride_preset_combo = QComboBox()
        self.pride_preset_combo.addItems([
            "謙遜 (Humble)",
            "適中 (Balanced)",
            "自信 (Confident)",
            "堅定 (Assertive)"
        ])
        self.pride_preset_combo.setCurrentIndex(1)
        self.pride_preset_combo.currentTextChanged.connect(self._apply_pride_preset)

        preset_layout = QHBoxLayout()
        preset_layout.addWidget(QLabel("快速預設:"))
        preset_layout.addWidget(self.pride_preset_combo, 1)
        layout.addLayout(preset_layout)

        return self._loose_group(group)

    def create_example3_control_group(self):
        group = QGroupBox("語言風格 (Language Style)")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        info_label = QLabel("自訂 U.E.P 的語言表達風格與用詞習慣")
        info_label.setObjectName("infoText")
        layout.addWidget(info_label)

        form_layout = QFormLayout()
        form_layout.setSpacing(12)

        formality_layout = QHBoxLayout()
        self.formality_slider = QSlider(Qt.Horizontal)
        self.formality_slider.setRange(0, 100)
        self.formality_slider.setValue(50)
        self.formality_label = QLabel("50% (適中)")
        formality_layout.addWidget(self.formality_slider)
        formality_layout.addWidget(self.formality_label)
        self.formality_slider.valueChanged.connect(self._update_formality)
        form_layout.addRow("正式程度:", formality_layout)

        verbosity_layout = QHBoxLayout()
        self.verbosity_slider = QSlider(Qt.Horizontal)
        self.verbosity_slider.setRange(0, 100)
        self.verbosity_slider.setValue(60)
        self.verbosity_label = QLabel("60%")
        verbosity_layout.addWidget(self.verbosity_slider)
        verbosity_layout.addWidget(self.verbosity_label)
        self.verbosity_slider.valueChanged.connect(
            lambda v: self.verbosity_label.setText(f"{v}%")
        )
        form_layout.addRow("詳細程度:", verbosity_layout)

        technical_layout = QHBoxLayout()
        self.technical_slider = QSlider(Qt.Horizontal)
        self.technical_slider.setRange(0, 100)
        self.technical_slider.setValue(40)
        self.technical_label = QLabel("40%")
        technical_layout.addWidget(self.technical_slider)
        technical_layout.addWidget(self.technical_label)
        self.technical_slider.valueChanged.connect(
            lambda v: self.technical_label.setText(f"{v}%")
        )
        form_layout.addRow("技術性用詞:", technical_layout)

        layout.addLayout(form_layout)

        self.use_emoji_checkbox = QCheckBox("使用表情符號 (Emoji)")
        self.use_emoji_checkbox.setChecked(True)
        layout.addWidget(self.use_emoji_checkbox)

        self.use_metaphor_checkbox = QCheckBox("使用比喻與類比")
        self.use_metaphor_checkbox.setChecked(True)
        layout.addWidget(self.use_metaphor_checkbox)

        return self._loose_group(group)

    def create_example4_control_group(self):
        group = QGroupBox("學習與適應 (Learning & Adaptation)")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        info_label = QLabel("控制 U.E.P 如何學習與適應使用者習慣")
        info_label.setObjectName("infoText")
        layout.addWidget(info_label)

        form_layout = QFormLayout()
        form_layout.setSpacing(12)

        learning_layout = QHBoxLayout()
        self.learning_slider = QSlider(Qt.Horizontal)
        self.learning_slider.setRange(0, 100)
        self.learning_slider.setValue(70)
        self.learning_label = QLabel("70%")
        learning_layout.addWidget(self.learning_slider)
        learning_layout.addWidget(self.learning_label)
        self.learning_slider.valueChanged.connect(
            lambda v: self.learning_label.setText(f"{v}%")
        )
        form_layout.addRow("學習速度:", learning_layout)

        memory_layout = QHBoxLayout()
        self.memory_slider = QSlider(Qt.Horizontal)
        self.memory_slider.setRange(0, 100)
        self.memory_slider.setValue(85)
        self.memory_label = QLabel("85%")
        memory_layout.addWidget(self.memory_slider)
        memory_layout.addWidget(self.memory_label)
        self.memory_slider.valueChanged.connect(
            lambda v: self.memory_label.setText(f"{v}%")
        )
        form_layout.addRow("記憶保留:", memory_layout)

        adaptation_layout = QHBoxLayout()
        self.adaptation_slider = QSlider(Qt.Horizontal)
        self.adaptation_slider.setRange(0, 100)
        self.adaptation_slider.setValue(75)
        self.adaptation_label = QLabel("75%")
        adaptation_layout.addWidget(self.adaptation_slider)
        adaptation_layout.addWidget(self.adaptation_label)
        self.adaptation_slider.valueChanged.connect(
            lambda v: self.adaptation_label.setText(f"{v}%")
        )
        form_layout.addRow("適應性:", adaptation_layout)

        layout.addLayout(form_layout)

        self.learn_preferences_checkbox = QCheckBox("學習使用者偏好")
        self.learn_preferences_checkbox.setChecked(True)
        layout.addWidget(self.learn_preferences_checkbox)

        self.learn_context_checkbox = QCheckBox("學習對話情境")
        self.learn_context_checkbox.setChecked(True)
        layout.addWidget(self.learn_context_checkbox)

        reset_btn = QPushButton("🔄 重置學習資料")
        reset_btn.clicked.connect(self.reset_learning_data)
        layout.addWidget(reset_btn)

        return self._loose_group(group)

    def _tall_scroll(self, scroll_area: QScrollArea):
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_area.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)
        scroll_area.setMinimumHeight(620)

    def _loose_group(self, group: QGroupBox):
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        return group

    def _update_activity(self, value):
        self.activity_label.setText(f"{value}%")
        self.settings_changed.emit("activity_level", value)

    def _update_pride(self, value):
        self.pride_label.setText(f"{value}%")
        self.settings_changed.emit("pride_level", value)

    def _update_formality(self, value):
        if value < 30:
            desc = "非常隨意"
        elif value < 50:
            desc = "隨意"
        elif value < 70:
            desc = "適中"
        elif value < 85:
            desc = "正式"
        else:
            desc = "非常正式"
        self.formality_label.setText(f"{value}% ({desc})")

    def _apply_pride_preset(self, preset):
        presets = {
            "謙遜 (Humble)": (40, 50, 30),
            "適中 (Balanced)": (65, 75, 60),
            "自信 (Confident)": (80, 85, 75),
            "堅定 (Assertive)": (90, 95, 85)
        }
        if preset in presets:
            pride, certainty, initiative = presets[preset]
            self.pride_slider.setValue(pride)
            self.certainty_slider.setValue(certainty)
            self.initiative_slider.setValue(initiative)

    def reset_learning_data(self):
        reply = QMessageBox.question(
            self, "確認重置",
            "確定要重置所有學習資料嗎？此操作無法復原。",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            QMessageBox.information(self, "完成", "學習資料已重置")

    def apply_theme(self):
        if self.dark_mode:
            self.setStyleSheet("""
                QWidget { background: transparent; }
                QGroupBox#settingsGroup { background:#1f2023; border:1px solid #2f3136; border-radius:10px; margin-top:12px; padding-top:18px; color:#e6e6e6; font-weight:600; }
                QGroupBox#settingsGroup::title { subcontrol-origin: margin; left:15px; padding:0 8px; color:#e6e6e6; }
                QLabel { color:#e6e6e6; }
                QLabel#infoText { color:#b5b8bf; font-style:italic; }
                QComboBox, QLineEdit, QSpinBox, QTextEdit { background:#26272b; color:#e6e6e6; border:1px solid #2f3136; border-radius:8px; padding:8px 12px; }
                QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QTextEdit:focus { border:1px solid #d5b618; }
                QPushButton { background:#d5b618; color:#000000; border:none; border-radius:10px; padding:10px 18px; font-weight:700; }
                QPushButton:hover { background:#e6c51c; }
                QPushButton:pressed { background:#b89f14; }
                QCheckBox { color:#e6e6e6; spacing:8px; }
                QCheckBox::indicator { width:18px; height:18px; border-radius:4px; border:2px solid #2f3136; background:#26272b; }
                QCheckBox::indicator:checked { background:#d5b618; border-color:#d5b618; }
                QSlider::groove:horizontal { background:#1f2023; height:8px; border-radius:4px; }
                QSlider::handle:horizontal { background:#d5b618; width:18px; height:18px; border-radius:9px; margin:-6px 0; }
                QListWidget { background:#1f2023; color:#e6e6e6; border:1px solid #2f3136; border-radius:8px; }
                QListWidget::item:selected { background:#d5b618; color:#000000; }
                QScrollBar:vertical { background:#1f2023; width:12px; border:none; }
                QScrollBar::handle:vertical { background:#3a3b42; border-radius:6px; min-height:40px; }
            """)
        else:
            self.setStyleSheet("""
                QWidget { background: transparent; }
                QGroupBox#settingsGroup { background:#ffffff; border:1px solid #bccfef; border-radius:10px; margin-top:12px; padding-top:20px; font-weight:600; color:#2d3142; }
                QGroupBox#settingsGroup::title { subcontrol-origin:margin; left:15px; padding:0 8px; }
                QPushButton { background:#739ef0; color:#ffffff; border:none; padding:10px 20px; border-radius:8px; font-weight:600; }
                QPushButton:hover { background:#4a7cdb; }
                QPushButton:pressed { background:#2558b5; }
                QCheckBox { color:#2d3142; spacing:8px; }
                QCheckBox::indicator { width:20px; height:20px; border-radius:4px; background:#ffffff; border:2px solid #bccfef; }
                QCheckBox::indicator:checked { background:#346ddb; border:2px solid #346ddb; }
                QSlider::groove:horizontal { background:#d7deec; height:8px; border-radius:4px; }
                QSlider::handle:horizontal { background:#739ef0; width:20px; height:20px; border-radius:10px; margin:-6px 0; }
                QComboBox, QLineEdit, QSpinBox, QTextEdit { background:#ffffff; color:#2d3142; border:1px solid #bccfef; border-radius:6px; padding:8px 12px; }
                QLabel { color:#2d3142; }
                QLabel#infoText { color:#739ef0; font-style:italic; }
                QListWidget { background:#ffffff; border:1px solid #bccfef; border-radius:6px; color:#2d3142; }
                QListWidget::item:selected { background:#739ef0; color:#ffffff; }
                QScrollBar:vertical { background:#f5f5f9; width:12px; border-radius:6px; }
                QScrollBar::handle:vertical { background:#a2bef2; border-radius:6px; min-height:40px; }
            """)

    def get_settings_dict(self) -> Dict[str, Any]:
        return {
            "status": {
                "mode": self.status_combo.currentText().split(" - ")[0],
                "activity": self.activity_slider.value(),
                "response_speed": self.response_slider.value(),
                "auto_adjust": self.auto_adjust_checkbox.isChecked()
            },
            "mood": {
                "mode": self.mood_combo.currentText(),
                "friendliness": self.friendliness_slider.value(),
                "empathy": self.empathy_slider.value(),
                "humor": self.humor_slider.value(),
                "memory": self.mood_memory_checkbox.isChecked()
            },
            "pride": {
                "level": self.pride_slider.value(),
                "certainty": self.certainty_slider.value(),
                "initiative": self.initiative_slider.value(),
                "preset": self.pride_preset_combo.currentText()
            },
            "language_style": {
                "formality": self.formality_slider.value(),
                "verbosity": self.verbosity_slider.value(),
                "technical": self.technical_slider.value(),
                "use_emoji": self.use_emoji_checkbox.isChecked(),
                "use_metaphor": self.use_metaphor_checkbox.isChecked()
            },
            "learning": {
                "speed": self.learning_slider.value(),
                "memory": self.memory_slider.value(),
                "adaptation": self.adaptation_slider.value(),
                "learn_preferences": self.learn_preferences_checkbox.isChecked(),
                "learn_context": self.learn_context_checkbox.isChecked()
            }
        }

    def load_settings(self):
        try:
            self.activity_slider.setValue(self.settings.value("status/activity", 75, type=int))
            self.response_slider.setValue(self.settings.value("status/response_speed", 7, type=int))
            self.friendliness_slider.setValue(self.settings.value("mood/friendliness", 80, type=int))
            self.empathy_slider.setValue(self.settings.value("mood/empathy", 70, type=int))
            self.humor_slider.setValue(self.settings.value("mood/humor", 50, type=int))
            self.pride_slider.setValue(self.settings.value("pride/level", 65, type=int))
            self.certainty_slider.setValue(self.settings.value("pride/certainty", 75, type=int))
            self.initiative_slider.setValue(self.settings.value("pride/initiative", 60, type=int))
        except Exception:
            pass

    def save_to_qsettings(self):
        try:
            self.settings.setValue("status/mode", self.status_combo.currentText())
            self.settings.setValue("status/activity", self.activity_slider.value())
            self.settings.setValue("status/response_speed", self.response_slider.value())
            self.settings.setValue("status/auto_adjust", self.auto_adjust_checkbox.isChecked())
            self.settings.setValue("mood/mode", self.mood_combo.currentText())
            self.settings.setValue("mood/friendliness", self.friendliness_slider.value())
            self.settings.setValue("mood/empathy", self.empathy_slider.value())
            self.settings.setValue("mood/humor", self.humor_slider.value())
            self.settings.setValue("mood/memory", self.mood_memory_checkbox.isChecked())
            self.settings.setValue("pride/level", self.pride_slider.value())
            self.settings.setValue("pride/certainty", self.certainty_slider.value())
            self.settings.setValue("pride/initiative", self.initiative_slider.value())
            self.settings.setValue("pride/preset", self.pride_preset_combo.currentText())
            self.settings.setValue("language/formality", self.formality_slider.value())
            self.settings.setValue("language/verbosity", self.verbosity_slider.value())
            self.settings.setValue("language/technical", self.technical_slider.value())
            self.settings.setValue("language/use_emoji", self.use_emoji_checkbox.isChecked())
            self.settings.setValue("language/use_metaphor", self.use_metaphor_checkbox.isChecked())
            self.settings.setValue("learning/speed", self.learning_slider.value())
            self.settings.setValue("learning/memory", self.memory_slider.value())
            self.settings.setValue("learning/adaptation", self.adaptation_slider.value())
            self.settings.setValue("learning/learn_preferences", self.learn_preferences_checkbox.isChecked())
            self.settings.setValue("learning/learn_context", self.learn_context_checkbox.isChecked())
            self.settings.sync()
        except Exception:
            pass



class StateProfileWindow(QMainWindow):
    settings_changed = pyqtSignal(str, object)
    action_triggered = pyqtSignal(str, dict)
    window_closed = pyqtSignal()

    def __init__(self, ui_module=None):
        super().__init__()
        if not PYQT5_AVAILABLE:
            return
        self.ui_module = ui_module
        self.settings = QSettings("UEP", "StateProfile")
        self.dark_mode = False
        self.init_ui()
        self.load_settings()

    def init_ui(self):
        self.setWindowTitle("UEP 狀態檔案")
        self.setMinimumSize(900, 950)
        self.resize(1200, 950)

        try:
            icon_path = os.path.join(os.path.dirname(__file__), "../../../arts/U.E.P.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except Exception:
            pass

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.create_header(main_layout)
        self.create_tab_widget(main_layout)
        self.create_bottom_buttons(main_layout)
        self.create_status_bar()

        self.apply_theme()

    def _tall_scroll(self, scroll_area: QScrollArea):
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_area.setMinimumHeight(620)
        scroll_area.setAlignment(Qt.AlignTop)

    def _loose_group(self, group: QGroupBox):
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        return group

    def create_header(self, parent_layout):
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(92)

        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(30, 16, 30, 16)
        header_layout.setSpacing(16)

        title_container = QVBoxLayout()
        title_label = QLabel("狀態檔案")
        title_label.setObjectName("mainTitle")
        title_label.setMinimumHeight(34)

        subtitle_label = QLabel("調整 U.E.P 的行為、情緒與學習模式")
        subtitle_label.setObjectName("subtitle")
        subtitle_label.setWordWrap(True)

        title_container.addWidget(title_label)
        title_container.addWidget(subtitle_label)
        title_container.addStretch()

        header_layout.addLayout(title_container)
        header_layout.addStretch()

        self.theme_toggle = QPushButton("🌙")
        self.theme_toggle.setObjectName("themeToggle")
        self.theme_toggle.setFixedSize(56, 56)
        self.theme_toggle.setCursor(Qt.PointingHandCursor)

        btn_font = QFont("Segoe UI Emoji, Apple Color Emoji, Noto Color Emoji")
        btn_font.setPointSize(20)
        self.theme_toggle.setFont(btn_font)

        self.theme_toggle.clicked.connect(self.toggle_theme)
        header_layout.addWidget(self.theme_toggle)

        parent_layout.addWidget(header)

    def create_tab_widget(self, parent_layout: QVBoxLayout):
        self.tab_widget = QTabWidget()
        self.tab_widget.setObjectName("mainTabs")
        self.tab_widget.setTabPosition(QTabWidget.North)

        tb = self.tab_widget.tabBar()
        tb.setElideMode(Qt.ElideNone)
        tb.setUsesScrollButtons(True)
        tb.setExpanding(False)
        tb.setStyleSheet("QTabBar::tab { min-height:42px; padding:12px 28px; }")

        self.profile_widget = UEPStateProfileWidget(parent=self.tab_widget)
        self.profile_widget.settings_changed.connect(
            lambda k, v: self.settings_changed.emit(k, v)
        )
        self.profile_widget.apply_requested.connect(self._handle_apply_request)

        self.tab_widget.addTab(self.profile_widget, "📊 狀態設定")

        parent_layout.addWidget(self.tab_widget, 1)

    def create_bottom_buttons(self, parent_layout):
        button_frame = QFrame()
        button_frame.setObjectName("bottomBar")
        button_frame.setFixedHeight(70)
        button_layout = QHBoxLayout(button_frame)
        button_layout.setContentsMargins(30, 15, 30, 15)

        self.apply_button = QPushButton("✓ 套用設定")
        self.apply_button.clicked.connect(self.apply_settings)

        self.reset_button = QPushButton("🔄 重置為預設值")
        self.reset_button.clicked.connect(self.reset_to_defaults)

        self.close_button = QPushButton("關閉")
        self.close_button.clicked.connect(self.close)

        button_layout.addWidget(self.apply_button)
        button_layout.addWidget(self.reset_button)
        button_layout.addStretch()
        button_layout.addWidget(self.close_button)

        parent_layout.addWidget(button_frame)

    def create_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("狀態檔案已就緒")

    def _handle_apply_request(self, data: dict):
        self.action_triggered.emit("apply_settings", data)

    def apply_settings(self):
        settings_dict = self.profile_widget.get_settings_dict()
        self.profile_widget.save_to_qsettings()
        self._handle_apply_request(settings_dict)
        if hasattr(self, "status_bar"):
            self.status_bar.showMessage("設定已套用！", 2000)
        QMessageBox.information(self, "成功", "設定已套用！")

    def reset_to_defaults(self):
        reply = QMessageBox.question(
            self, "確認重置",
            "確定要將所有設定重置為預設值嗎？",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.settings.clear()
            self.profile_widget.load_settings()
            if hasattr(self, "status_bar"):
                self.status_bar.showMessage("已重置為預設值", 2000)
            QMessageBox.information(self, "完成", "已重置為預設值")

  
    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.theme_toggle.setText("☀️" if self.dark_mode else "🌙")
        self.apply_theme()
        self.profile_widget.dark_mode = self.dark_mode
        self.profile_widget.apply_theme()

    def apply_theme(self):
        if self.dark_mode:
            self.setStyleSheet("""
                QMainWindow { background:#000000; }
                QFrame#bottomBar, QStatusBar { background:#000000; color:#b5b8bf; border-top:1px solid #2f3136; }
                QWidget#header { background:#1f2023; border-bottom:1px solid #2f3136; }
                QLabel#mainTitle { color:#ffffff; font-size:28px; font-weight:800; }
                QLabel#subtitle { color:#b5b8bf; font-size:13px; }
                QTabWidget#mainTabs::pane { background:#26272b; border:1px solid #2f3136; border-radius:8px; }
                QTabBar::tab { background:#1f2023; color:#b5b8bf; padding:10px 18px; margin-right:4px; border-top-left-radius:8px; border-top-right-radius:8px; border:1px solid #2f3136; font-weight:600; }
                QTabBar::tab:selected { background:#26272b; color:#ffffff; border:1px solid #d5b618; }
                QTabBar::tab:hover:!selected { background:#232427; }
                QPushButton { background:#d5b618; color:#000000; border:none; border-radius:10px; padding:10px 18px; font-weight:700; }
                QPushButton:hover { background:#e6c51c; }
                QPushButton:pressed { background:#b89f14; }
                QPushButton#themeToggle { background:#000; color:#000; border:none; min-width:56px; min-height:56px; border-radius:28px; font-size:20px; padding:0; }
                QCheckBox { color:#e6e6e6; spacing:8px; }
                QCheckBox::indicator { width:18px; height:18px; border-radius:4px; border:2px solid #2f3136; background:#26272b; }
                QCheckBox::indicator:checked { background:#d5b618; border-color:#d5b618; }
                QSlider::groove:horizontal { background:#1f2023; height:8px; border-radius:4px; }
                QSlider::handle:horizontal { background:#d5b618; width:18px; height:18px; border-radius:9px; margin:-6px 0; }
                QTreeWidget { background:#1f2023; color:#e6e6e6; border:1px solid #2f3136; border-radius:8px; }
                QTreeWidget::item:selected { background:#d5b618; color:#000000; }
                QListWidget { background:#1f2023; color:#e6e6e6; border:1px solid #2f3136; border-radius:8px; }
                QListWidget::item:selected { background:#d5b618; color:#000000; }
                QScrollBar:vertical { background:#1f2023; width:12px; border:none; }
                QScrollBar::handle:vertical { background:#3a3b42; border-radius:6px; min-height:40px; }
            """)
        else:
            self.setStyleSheet("""
                QMainWindow { background:#f5f5f9; }
                QStatusBar { background:#ffffff; color:#2d3142; border-top:1px solid #bccfef; }
                QWidget#header { background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #d7deec, stop:1 #bccfef); border-bottom:1px solid #a2bef2; }
                QLabel#mainTitle { color:#2d3142; font-size:28px; font-weight:700; }
                QLabel#subtitle { color:#4a5568; font-size:13px; }
                QPushButton#themeToggle { background:#346ddb; color:#ffffff; border:none; min-width:56px; min-height:56px; border-radius:28px; font-size:22px; padding:0; }
                QTabWidget#mainTabs::pane { border:1px solid #e0e0e8; background:#ffffff; border-radius:8px; }
                QTabBar::tab { background:#d7deec; color:#5a5a66; padding:10px 18px; margin-right:4px; border-top-left-radius:8px; border-top-right-radius:8px; font-weight:600; }
                QTabBar::tab:selected { background:#ffffff; color:#2d3142; }
                QTabBar::tab:hover:!selected { background:#a2bef2; }
                QPushButton { background:#739ef0; color:#ffffff; border:none; padding:10px 20px; border-radius:8px; font-weight:600; }
                QPushButton:hover { background:#4a7cdb; }
                QPushButton:pressed { background:#2558b5; }
                QFrame#bottomBar { background:#ffffff; border-top:1px solid #bccfef; }
                QGroupBox#settingsGroup { background:#ffffff; border:1px solid #bccfef; border-radius:10px; margin-top:12px; padding-top:20px; font-weight:600; color:#2d3142; }
                QGroupBox#settingsGroup::title { subcontrol-origin:margin; left:15px; padding:0 8px; }
                QCheckBox { color:#2d3142; spacing:8px; }
                QCheckBox::indicator { width:20px; height:20px; border-radius:4px; background:#ffffff; border:2px solid #bccfef; }
                QCheckBox::indicator:checked { background:#346ddb; border:2px solid #346ddb; }
                QSlider::groove:horizontal { background:#d7deec; height:8px; border-radius:4px; }
                QSlider::handle:horizontal { background:#739ef0; width:20px; height:20px; border-radius:10px; margin:-6px 0; }
                QComboBox, QLineEdit, QSpinBox, QTextEdit { background:#ffffff; color:#2d3142; border:1px solid #bccfef; border-radius:6px; padding:8px 12px; }
                QLabel { color:#2d3142; }
                QLabel#infoText { color:#739ef0; font-style:italic; }
                QTreeWidget { background:#ffffff; border:1px solid #bccfef; border-radius:6px; color:#2d3142; }
                QTreeWidget::item:selected { background:#739ef0; color:#ffffff; }
                QListWidget { background:#ffffff; border:1px solid #bccfef; border-radius:6px; color:#2d3142; }
                QListWidget::item:selected { background:#739ef0; color:#ffffff; }
                QScrollBar:vertical { background:#f5f5f9; width:12px; border-radius:6px; }
                QScrollBar::handle:vertical { background:#a2bef2; border-radius:6px; min-height:40px; }
            """)

    def load_settings(self):
        try:
            self.dark_mode = self.settings.value("theme/dark_mode", False, type=bool)
            self.theme_toggle.setText("☀️" if self.dark_mode else "🌙")
        except Exception:
            pass

    def save_settings(self):
        try:
            self.settings.setValue("theme/dark_mode", self.dark_mode)
            self.settings.sync()
        except Exception:
            pass

    def closeEvent(self, event):
        self.save_settings()
        self.window_closed.emit()
        event.accept()


# ==================== 測試入口 ====================
def create_test_window():
    if not PYQT5_AVAILABLE:
        return None
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    window = StateProfileWindow()
    window.show()
    return app, window


if __name__ == "__main__":
    app, window = create_test_window()
    if app and window:
        sys.exit(app.exec_())
