# imports.py - 集中管理 PyQt5 相關的導入和類型註冊

import sys
import threading
import traceback

# 嘗試導入 PyQt5
try:
    from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                QPushButton, QTextEdit, QLabel, QComboBox,
                                QLineEdit, QCheckBox, QSplitter, QFrame,
                                QTableWidget, QTableWidgetItem, QHeaderView,
                                QFileDialog, QMessageBox, QSpinBox, QTreeView,
                                QListWidget, QListWidgetItem, QDialog,
                                QApplication, QTabWidget, QMainWindow, QMenuBar,
                                QStatusBar, QToolBar, QAction, QMenu)
    from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, QThread, QMetaType,
                             QObject, QCoreApplication, QSize, QPoint, QRect,
                             QMargins, QEvent, QMutex, QMutexLocker)
    from PyQt5.QtGui import (QFont, QColor, QTextCharFormat, QTextCursor, QIcon,
                           QPalette, QPixmap, QPainter, QPen, QBrush)

    # 註冊所有可能需要在跨線程信號中使用的類型
    # 為避免"Cannot queue arguments of type 'QTextCharFormat'"和"Cannot queue arguments of type 'QTextCursor'"錯誤
    def register_qt_types():
        """註冊所有 Qt 類型以便在跨線程信號中使用"""
        
        types_to_register = [
            "QTextCharFormat",
            "QTextCursor",
            "QColor",
            "QFont",
            "QPixmap"
        ]
        
        # 使用 QMetaType.type 方式註冊
        for qt_type in types_to_register:
            try:
                QMetaType.type(qt_type)
            except Exception as e:
                print(f"無法使用 QMetaType.type 註冊 {qt_type}: {e}")
        
        # 使用 qRegisterMetaType 方式註冊
        try:
            from PyQt5.QtCore import qRegisterMetaType
            for qt_type in types_to_register:
                try:
                    qRegisterMetaType(qt_type)
                except Exception as e:
                    print(f"無法使用 qRegisterMetaType 註冊 {qt_type}: {e}")
        except ImportError:
            pass  # 如果 qRegisterMetaType 不可用，繼續使用 QMetaType.type 方式
    
    # 註冊所有類型
    register_qt_types()
    
    # 線程安全的 UI 操作輔助函數
    def ensure_main_thread(func):
        """裝飾器：確保函數在主線程中執行"""
        def wrapper(*args, **kwargs):
            if QThread.currentThread() != QApplication.instance().thread():
                # 如果不在主線程，使用 QTimer.singleShot 在主線程中執行
                result = [None]  # 使用列表存儲結果，因為在外部函數中無法修改非列表變量
                completed = threading.Event()
                
                def main_thread_call():
                    try:
                        result[0] = func(*args, **kwargs)
                    except Exception as e:
                        print(f"主線程函數調用出錯: {e}", file=sys.stderr)
                        traceback.print_exc()
                    finally:
                        completed.set()
                
                QTimer.singleShot(0, main_thread_call)
                completed.wait()  # 等待主線程執行完成
                return result[0]
            else:
                # 已經在主線程，直接執行
                return func(*args, **kwargs)
        return wrapper
    
    # 標記 PyQt5 可用
    PYQT5_AVAILABLE = True
    
except ImportError as e:
    print(f"無法導入 PyQt5: {e}")
    PYQT5_AVAILABLE = False
    
    # 提供一個空的裝飾器實現
    def ensure_main_thread(func):
        return func
    
    # 定義基本類以便在非 PyQt5 環境中不會出錯
    class DummyClass:
        pass
        
    QWidget = QVBoxLayout = QHBoxLayout = QGroupBox = QPushButton = QTextEdit = QLabel = DummyClass
    QComboBox = QLineEdit = QCheckBox = QSplitter = QFrame = QTableWidget = DummyClass
    QTableWidgetItem = QHeaderView = QFileDialog = QMessageBox = QSpinBox = QTreeView = DummyClass
    QListWidget = QListWidgetItem = QDialog = QApplication = QTabWidget = QMainWindow = DummyClass
    QMenuBar = QStatusBar = QToolBar = QAction = QMenu = DummyClass
    
    Qt = QTimer = pyqtSignal = QThread = QMetaType = QObject = QCoreApplication = DummyClass
    QSize = QPoint = QRect = QMargins = QEvent = DummyClass
    
    QFont = QColor = QTextCharFormat = QTextCursor = QIcon = QPalette = QPixmap = DummyClass
    QPainter = QPen = QBrush = DummyClass
    
    def register_qt_types():
        """在非 PyQt5 環境中，這是一個空函數"""
        pass

# 導出所有符號
__all__ = [
    # 可用性標記和工具函數
    'PYQT5_AVAILABLE', 'register_qt_types', 'ensure_main_thread',
    
    # QtWidgets
    'QWidget', 'QVBoxLayout', 'QHBoxLayout', 'QGroupBox', 'QPushButton', 'QTextEdit',
    'QLabel', 'QComboBox', 'QLineEdit', 'QCheckBox', 'QSplitter', 'QFrame',
    'QTableWidget', 'QTableWidgetItem', 'QHeaderView', 'QFileDialog', 'QMessageBox',
    'QSpinBox', 'QTreeView', 'QListWidget', 'QListWidgetItem', 'QDialog',
    'QApplication', 'QTabWidget', 'QMainWindow', 'QMenuBar', 'QStatusBar', 'QToolBar',
    'QAction', 'QMenu',
    
    # QtCore
    'Qt', 'QTimer', 'pyqtSignal', 'QThread', 'QMetaType', 'QObject', 'QCoreApplication',
    'QSize', 'QPoint', 'QRect', 'QMargins', 'QEvent',
    
    # QtGui
    'QFont', 'QColor', 'QTextCharFormat', 'QTextCursor', 'QIcon', 'QPalette',
    'QPixmap', 'QPainter', 'QPen', 'QBrush'
]
