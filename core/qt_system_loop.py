# core/qt_system_loop.py
"""
Qt 整合的系統循環 - 將 SystemLoop 包裝在 QThread 中

讓 SystemLoop 在 QThread 背景執行，主線程留給 PyQt 事件循環。
這樣可以：
1. UI 在主線程運行，響應流暢
2. Core 邏輯在 QThread 運行，不阻塞 UI
3. 使用 Qt Signal/Slot 安全通訊
"""

try:
    from PyQt5.QtCore import QThread, pyqtSignal, QObject
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    # 提供 fallback，避免 import 失敗
    QThread = object
    QObject = object
    def pyqtSignal(*args, **kwargs):
        return None

from typing import Dict, Any, Optional
from utils.debug_helper import debug_log, info_log, error_log


class CoreLoopThread(QThread):
    """
    SystemLoop 的 QThread 包裝
    
    在 Qt 的線程系統中運行 SystemLoop，使其不阻塞主線程。
    """
    
    # 定義信號
    loop_started = pyqtSignal()  # 循環已啟動
    loop_stopped = pyqtSignal()  # 循環已停止
    error_occurred = pyqtSignal(str)  # 發生錯誤
    status_changed = pyqtSignal(str)  # 狀態變更
    
    def __init__(self, system_loop, parent: Optional[QObject] = None):
        """
        初始化 Core Loop Thread
        
        Args:
            system_loop: SystemLoop 實例
            parent: Qt 父物件
        """
        super().__init__(parent)
        self.system_loop = system_loop
        self._is_running = False
        
        info_log("[CoreLoopThread] Qt 系統循環線程已創建")
    
    def run(self):
        """Qt 線程的主執行方法"""
        try:
            info_log("[CoreLoopThread] 🚀 開始在 QThread 中運行系統循環...")
            self._is_running = True
            self.loop_started.emit()
            
            # 啟動 SystemLoop（會創建其內部線程）
            success = self.system_loop.start()
            
            if not success:
                error_log("[CoreLoopThread] ❌ SystemLoop 啟動失敗")
                self.error_occurred.emit("SystemLoop 啟動失敗")
                return
            
            info_log("[CoreLoopThread] ✅ SystemLoop 已在 QThread 中啟動")
            
            # 保持線程活躍，等待停止信號
            # SystemLoop 內部有自己的 loop_thread，我們只需要等待
            while self._is_running and not self.isInterruptionRequested():
                self.msleep(100)  # 100ms 檢查一次
            
            info_log("[CoreLoopThread] 🛑 收到停止信號，準備停止...")
            
        except Exception as e:
            error_log(f"[CoreLoopThread] ❌ 運行時錯誤: {e}")
            self.error_occurred.emit(str(e))
        finally:
            self._cleanup()
    
    def stop_loop(self):
        """停止系統循環"""
        try:
            info_log("[CoreLoopThread] 🛑 停止系統循環...")
            self._is_running = False
            
            # 停止 SystemLoop
            if self.system_loop:
                self.system_loop.stop()
            
            # 請求線程中斷
            self.requestInterruption()
            
            # 等待線程結束（最多 5 秒）
            if not self.wait(5000):
                error_log("[CoreLoopThread] ⚠️ 線程未在 5 秒內結束")
            
            info_log("[CoreLoopThread] ✅ 系統循環已停止")
            
        except Exception as e:
            error_log(f"[CoreLoopThread] ❌ 停止時錯誤: {e}")
    
    def _cleanup(self):
        """清理資源"""
        try:
            info_log("[CoreLoopThread] 🧹 清理資源...")
            self.loop_stopped.emit()
            info_log("[CoreLoopThread] ✅ 清理完成")
        except Exception as e:
            error_log(f"[CoreLoopThread] ❌ 清理時錯誤: {e}")


class QtSystemLoopManager(QObject):
    """
    Qt 系統循環管理器
    
    協調 SystemLoop 和 UI 之間的通訊。
    提供信號槽機制供其他模組連接。
    """
    
    # 對外信號
    loop_status_changed = pyqtSignal(str)  # 循環狀態變更
    loop_error = pyqtSignal(str)  # 循環錯誤
    
    def __init__(self, parent: Optional[QObject] = None):
        """初始化管理器"""
        super().__init__(parent)
        self.core_loop_thread: Optional[CoreLoopThread] = None
        self.system_loop = None
        
        info_log("[QtSystemLoopManager] Qt 系統循環管理器已創建")
    
    def start_system_loop(self, system_loop) -> bool:
        """
        啟動系統循環（在 QThread 中）
        
        Args:
            system_loop: SystemLoop 實例
            
        Returns:
            bool: 是否成功啟動
        """
        try:
            if self.core_loop_thread and self.core_loop_thread.isRunning():
                info_log("[QtSystemLoopManager] 系統循環已在運行")
                return True
            
            info_log("[QtSystemLoopManager] 🚀 啟動系統循環...")
            self.system_loop = system_loop
            
            # 創建 QThread
            self.core_loop_thread = CoreLoopThread(system_loop, parent=self)
            
            # 連接信號
            self.core_loop_thread.loop_started.connect(self._on_loop_started)
            self.core_loop_thread.loop_stopped.connect(self._on_loop_stopped)
            self.core_loop_thread.error_occurred.connect(self._on_loop_error)
            self.core_loop_thread.status_changed.connect(self._on_status_changed)
            
            # 啟動線程
            self.core_loop_thread.start()
            
            info_log("[QtSystemLoopManager] ✅ 系統循環線程已啟動")
            return True
            
        except Exception as e:
            error_log(f"[QtSystemLoopManager] ❌ 啟動系統循環失敗: {e}")
            return False
    
    def stop_system_loop(self):
        """停止系統循環"""
        try:
            if not self.core_loop_thread:
                info_log("[QtSystemLoopManager] 系統循環未運行")
                return
            
            info_log("[QtSystemLoopManager] 🛑 停止系統循環...")
            self.core_loop_thread.stop_loop()
            self.core_loop_thread = None
            info_log("[QtSystemLoopManager] ✅ 系統循環已停止")
            
        except Exception as e:
            error_log(f"[QtSystemLoopManager] ❌ 停止系統循環失敗: {e}")
    
    # 信號處理方法
    def _on_loop_started(self):
        """循環啟動回調"""
        info_log("[QtSystemLoopManager] 📡 收到循環啟動信號")
        self.loop_status_changed.emit("running")
    
    def _on_loop_stopped(self):
        """循環停止回調"""
        info_log("[QtSystemLoopManager] 📡 收到循環停止信號")
        self.loop_status_changed.emit("stopped")
    
    def _on_loop_error(self, error_msg: str):
        """循環錯誤回調"""
        error_log(f"[QtSystemLoopManager] 📡 收到循環錯誤: {error_msg}")
        self.loop_error.emit(error_msg)
    
    def _on_status_changed(self, status: str):
        """狀態變更回調"""
        debug_log(1, f"[QtSystemLoopManager] 📡 狀態變更: {status}")
        self.loop_status_changed.emit(status)
