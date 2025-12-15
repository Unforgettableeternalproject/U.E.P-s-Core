# on_call_input_dialog.py
"""
ON_CALL 文字輸入對話框 - 底部條狀輸入框
用於文字輸入模式下的使用者交互
"""

import sys
from PyQt5.QtWidgets import (
    QWidget, QHBoxLayout, QLineEdit, QPushButton, QApplication
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QFont
from utils.debug_helper import debug_log, info_log


class OnCallInputDialog(QWidget):
    """ON_CALL 底部條狀輸入框"""
    
    # 信號
    input_submitted = pyqtSignal(str)  # 輸入提交時發出信號，攜帶輸入文字
    dialog_closed = pyqtSignal()       # 對話框關閉時發出信號
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.submitted_text = ""
        self.setup_ui()
        
    def setup_ui(self):
        """設置 UI 元件"""
        # 無邊框視窗
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        
        # 固定大小和位置（螢幕底部中央）
        screen = QApplication.desktop().screenGeometry()
        width = 800
        height = 80
        x = (screen.width() - width) // 2
        y = screen.height() - height - 50  # 距離底部 50px
        
        self.setGeometry(x, y, width, height)
        self.setFixedSize(width, height)
        
        # 設置視窗風格（預留 10px 邊框用於未來材質）
        self.setStyleSheet("""
            QWidget {
                background-color: rgba(240, 200, 150, 230);
                border-radius: 12px;
            }
            QLineEdit {
                background-color: rgba(255, 255, 255, 245);
                border: 2px solid rgba(100, 100, 100, 100);
                border-radius: 8px;
                padding: 12px 15px;
                font-size: 16px;
                font-family: 'Microsoft JhengHei', 'Segoe UI';
                color: rgba(50, 50, 50, 255);
            }
            QLineEdit:focus {
                border: 2px solid rgba(100, 180, 255, 200);
            }
            QPushButton {
                background-color: rgba(100, 180, 255, 220);
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px 24px;
                font-size: 15px;
                font-weight: bold;
                font-family: 'Microsoft JhengHei';
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: rgba(80, 160, 255, 240);
            }
            QPushButton:pressed {
                background-color: rgba(60, 140, 235, 200);
            }
            QPushButton#cancelBtn {
                background-color: rgba(180, 180, 180, 200);
            }
            QPushButton#cancelBtn:hover {
                background-color: rgba(200, 200, 200, 220);
            }
        """)
        
        # 主佈局（水平排列）
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)  # 預留邊框空間
        main_layout.setSpacing(12)
        
        # 文字輸入框（單行）
        self.input_line_edit = QLineEdit()
        self.input_line_edit.setPlaceholderText("在此輸入你想對 UEP 說的話...")
        self.input_line_edit.returnPressed.connect(self.submit_input)  # Enter 鍵提交
        main_layout.addWidget(self.input_line_edit, stretch=1)
        
        # 取消按鈕
        cancel_button = QPushButton("取消")
        cancel_button.setObjectName("cancelBtn")
        cancel_button.clicked.connect(self.close_dialog)
        main_layout.addWidget(cancel_button)
        
        # 提交按鈕
        submit_button = QPushButton("送出")
        submit_button.clicked.connect(self.submit_input)
        submit_button.setDefault(True)  # 預設按鈕
        main_layout.addWidget(submit_button)
        
        # 延遲設置焦點（確保視窗完全顯示後）
        QTimer.singleShot(100, lambda: self.input_line_edit.setFocus())
        
        info_log("[OnCallInputDialog] 底部條狀輸入框已初始化")
    
    def submit_input(self):
        """提交輸入文字"""
        self.submitted_text = self.input_line_edit.text().strip()
        
        if not self.submitted_text:
            debug_log(2, "[OnCallInputDialog] 輸入為空，請輸入內容")
            # 輸入框震動效果（可選）
            self.input_line_edit.setStyleSheet(self.input_line_edit.styleSheet() + 
                "QLineEdit { border: 2px solid rgba(255, 100, 100, 200); }")
            QTimer.singleShot(300, lambda: self.input_line_edit.setStyleSheet(""))
            return
        
        info_log(f"[OnCallInputDialog] 輸入提交: {self.submitted_text}")
        self.input_submitted.emit(self.submitted_text)
        self.close()
    
    def close_dialog(self):
        """關閉對話框（取消）"""
        debug_log(2, "[OnCallInputDialog] 使用者取消輸入")
        self.submitted_text = ""
        self.dialog_closed.emit()
        self.close()
    
    def get_input(self) -> str:
        """獲取輸入的文字"""
        return self.submitted_text
    
    def closeEvent(self, a0):
        """對話框關閉事件"""
        global _current_dialog
        
        if not self.submitted_text:
            # 如果沒有提交文字就關閉，視為取消
            self.dialog_closed.emit()
        debug_log(2, "[OnCallInputDialog] 對話框已關閉")
        
        # 清除全局引用
        if _current_dialog is self:
            _current_dialog = None
        
        super().closeEvent(a0)
    
    def keyPressEvent(self, a0):
        """處理按鍵事件"""
        if a0.key() == Qt.Key_Escape:
            # ESC 鍵取消
            self.close_dialog()
        else:
            super().keyPressEvent(a0)


# 全域變數：持有當前對話框實例
_current_dialog = None


def show_on_call_input_dialog(parent=None):
    """
    顯示 ON_CALL 底部條狀輸入框（非阻擋模式）
    必須在主執行緒中呼叫
    
    Returns:
        OnCallInputDialog 實例，可用於連接 signal
    """
    global _current_dialog
    
    # 如果已經有對話框在顯示，先關閉
    if _current_dialog is not None:
        try:
            _current_dialog.close()
        except:
            pass
    
    # 創建並顯示新對話框
    _current_dialog = OnCallInputDialog(parent)
    _current_dialog.show()
    _current_dialog.raise_()
    _current_dialog.activateWindow()
    
    info_log("[OnCallInputDialog] 底部輸入框已顯示")
    return _current_dialog  # 返回對話框實例供外部連接 signal


if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # 測試對話框
    text = show_on_call_input_dialog()
    print(f"輸入結果: {text}")
