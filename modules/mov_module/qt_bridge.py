# modules/mov_module/qt_bridge.py
"""
MOV 模組的 Qt 橋接器

用於安全地從 EventBus 線程觸發動畫（跨線程 UI 操作）。
使用 Qt 信號槽機制確保 UI 操作在主線程執行。
"""

try:
    from PyQt5.QtCore import QObject, pyqtSignal
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    QObject = object
    def pyqtSignal(*args, **kwargs):
        return None

from typing import Optional, Dict, Any
from utils.debug_helper import debug_log, info_log, error_log


class MovQtBridge(QObject):
    """
    MOV 模組的 Qt 橋接器
    
    提供線程安全的動畫觸發機制。
    EventBus 線程發射信號 → Qt 主線程處理信號 → 更新 UI
    """
    
    # 定義信號
    animation_requested = pyqtSignal(str, dict)  # (animation_name, params)
    animation_stopped = pyqtSignal()  # 停止當前動畫
    
    def __init__(self, ani_module, parent: Optional[QObject] = None):
        """
        初始化橋接器
        
        Args:
            ani_module: ANI 模組實例
            parent: Qt 父對象
        """
        super().__init__(parent)
        self.ani_module = ani_module
        self._enabled = True
        
        # 連接信號到槽
        self.animation_requested.connect(self._handle_animation_request)
        self.animation_stopped.connect(self._handle_animation_stop)
        
        info_log("[MovQtBridge] Qt 橋接器已創建")
    
    def trigger_animation(self, name: str, params: Optional[Dict[str, Any]] = None):
        """
        線程安全的動畫觸發（從任何線程調用）
        
        Args:
            name: 動畫名稱
            params: 動畫參數（loop, await_finish, 等）
        """
        if not self._enabled:
            debug_log(3, "[MovQtBridge] 橋接器已禁用")
            return
        
        params = params or {}
        
        # 發射信號（線程安全，Qt 會自動路由到主線程）
        self.animation_requested.emit(name, params)
        debug_log(3, f"[MovQtBridge] 發射動畫請求信號: {name}")
    
    def stop_animation(self):
        """線程安全的動畫停止（從任何線程調用）"""
        if not self._enabled:
            return
        
        self.animation_stopped.emit()
        debug_log(3, "[MovQtBridge] 發射停止動畫信號")
    
    def _handle_animation_request(self, name: str, params: Dict[str, Any]):
        """
        處理動畫請求（在 Qt 主線程中執行）
        
        Args:
            name: 動畫名稱
            params: 動畫參數
        """
        try:
            if not self.ani_module:
                debug_log(2, "[MovQtBridge] ANI 模組不可用")
                return
            
            loop = params.get("loop")
            
            # 在主線程中調用 ANI 模組
            result = self.ani_module.play(name, loop=loop)
            debug_log(2, f"[MovQtBridge] 動畫已觸發（主線程）: {name}, result={result}")
            
        except Exception as e:
            error_log(f"[MovQtBridge] 處理動畫請求失敗: {e}")
    
    def _handle_animation_stop(self):
        """處理停止動畫請求（在 Qt 主線程中執行）"""
        try:
            if not self.ani_module or not hasattr(self.ani_module, 'stop'):
                return
            
            self.ani_module.stop()
            debug_log(2, "[MovQtBridge] 動畫已停止（主線程）")
            
        except Exception as e:
            error_log(f"[MovQtBridge] 停止動畫失敗: {e}")
    
    def set_enabled(self, enabled: bool):
        """啟用/禁用橋接器"""
        self._enabled = enabled
        info_log(f"[MovQtBridge] 橋接器 {'啟用' if enabled else '禁用'}")
    
    def shutdown(self):
        """關閉橋接器"""
        info_log("[MovQtBridge] 關閉橋接器...")
        self._enabled = False
        self.ani_module = None
