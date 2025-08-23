# core/frontend_base.py
"""
前端模組基類 - 專為 UI、ANI、MOV 模組設計的特殊基類

前端模組與內部處理模組 (STT, NLP, MEM, LLM, TTS, SYS) 不同：
- 主要處理使用者介面和視覺表現
- 需要特殊的事件循環和即時響應
- 直接與系統狀態和工作上下文整合
- 支援多執行緒和非同步操作
"""

import time
import threading
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Callable, Union
from enum import Enum, auto
from dataclasses import dataclass

# 安全導入 PyQt5
try:
    from PyQt5.QtCore import QObject, QTimer, pyqtSignal, QMetaObject
    from PyQt5.QtWidgets import QApplication
    PYQT5_AVAILABLE = True
    
    # 自定義元類來解決衝突
    class FrontendMetaClass(type(QObject), type):
        """解決 BaseModule 和 QObject 元類衝突的混合元類"""
        pass
        
except ImportError:
    PYQT5_AVAILABLE = False
    # 如果 PyQt5 不可用，提供替代實作
    class QObject:
        def __init__(self): pass
    class QTimer: pass
    def pyqtSignal(*args): return None
    QMetaObject = None
    class FrontendMetaClass(type): pass

from core.module_base import BaseModule
from core.working_context import WorkingContextManager, ContextType
from core.state_manager import UEPState, StateManager
from utils.debug_helper import debug_log, info_log, error_log


class FrontendModuleType(Enum):
    """前端模組類型"""
    UI = "UI"           # 使用者介面模組
    ANI = "ANI"         # 動畫模組
    MOV = "MOV"        # 行為/移動模組


class UIEventType(Enum):
    """UI 事件類型"""
    MOUSE_CLICK = auto()
    MOUSE_HOVER = auto()
    DRAG_START = auto()
    DRAG_END = auto()
    FILE_DROP = auto()
    KEYBOARD_INPUT = auto()
    WINDOW_MOVE = auto()
    ANIMATION_COMPLETE = auto()
    STATE_CHANGE = auto()


@dataclass
class UIEvent:
    """UI 事件資料結構"""
    event_type: UIEventType
    timestamp: float
    data: Dict[str, Any]
    source_module: str
    target_module: Optional[str] = None


# Qt 信號類 (如果 PyQt5 可用)
if PYQT5_AVAILABLE:
    class FrontendSignals(QObject):
        """前端模組的 Qt 信號容器"""
        state_changed = pyqtSignal(str, dict)  # 狀態變更信號
        event_occurred = pyqtSignal(object)    # 事件發生信號
        
        def __init__(self):
            super().__init__()
            # 為計時器功能提供槽方法
            self._timer_callbacks = {}
        
        def add_timer_callback(self, timer_id: str, callback):
            """添加計時器回調"""
            self._timer_callbacks[timer_id] = callback
        
        def timer_timeout(self, timer_id: str = "default"):
            """計時器超時槽方法"""
            if timer_id in self._timer_callbacks:
                try:
                    self._timer_callbacks[timer_id]()
                except Exception as e:
                    print(f"Timer callback error: {e}")
                    
else:
    class FrontendSignals:
        """前端模組的空信號容器 (PyQt5 不可用時)"""
        def __init__(self):
            self.state_changed = None
            self.event_occurred = None
            self._timer_callbacks = {}
            
        def add_timer_callback(self, timer_id: str, callback):
            """添加計時器回調 (空實作)"""
            pass
            
        def timer_timeout(self, timer_id: str = "default"):
            """計時器超時槽方法 (空實作)"""
            pass


class BaseFrontendModule(BaseModule):
    """
    前端模組基類
    
    整合了 BaseModule 接口，並使用組合模式包含 Qt 功能：
    - 標準模組介面
    - Qt 信號/槽機制 (通過組合)
    - 狀態同步
    - 事件處理
    
    避免多重繼承的元類衝突問題
    """
    
    def __init__(self, module_type: FrontendModuleType):
        BaseModule.__init__(self)
        
        self.module_type = module_type
        self.module_id = module_type.value
        
        # Qt 信號對象
        self.signals = FrontendSignals()
        
        # 框架引用 (將在註冊時設置)
        self.framework = None
        self.context_manager = None
        self.state_manager = None
        
        # 內部狀態
        self.local_state = {}
        self.event_handlers = {}
        self.is_active = False
        
        # 執行緒安全
        self._lock = threading.RLock()
        
        # 設置信號連接 (只在 PyQt5 可用時)
        if PYQT5_AVAILABLE and self.signals.state_changed:
            self.signals.state_changed.connect(self._on_state_changed)
            self.signals.event_occurred.connect(self._on_event_occurred)
        
        debug_log(1, f"[{self.module_id}] 前端模組基類初始化完成")
    
    def set_framework_references(self, 
                               framework,
                               context_manager: WorkingContextManager,
                               state_manager: StateManager):
        """設置框架引用"""
        self.framework = framework
        self.context_manager = context_manager
        self.state_manager = state_manager
        
        # 訂閱狀態變更
        if hasattr(self.state_manager, 'add_state_listener'):
            self.state_manager.add_state_listener(self._on_system_state_changed)
        
        info_log(f"[{self.module_id}] 框架引用設置完成")
    
    @abstractmethod
    def initialize_frontend(self) -> bool:
        """
        初始化前端特定功能
        
        子類別需要實作：
        - UI 組件初始化
        - 事件處理器註冊
        - 資源載入
        """
        pass
    
    def initialize(self):
        """標準模組初始化"""
        try:
            if self.initialize_frontend():
                self.is_initialized = True
                self.is_active = True
                info_log(f"[{self.module_id}] 模組初始化成功")
                return True
            else:
                error_log(f"[{self.module_id}] 前端初始化失敗")
                return False
        except Exception as e:
            error_log(f"[{self.module_id}] 初始化異常: {e}")
            return False
    
    def handle(self, data: dict) -> dict:
        """處理資料 - 前端模組的特殊實作"""
        try:
            if not self.is_initialized or not self.is_active:
                return {"error": "模組未初始化或未啟用"}
            
            # 委託給子類別的前端處理方法
            return self.handle_frontend_request(data)
            
        except Exception as e:
            error_log(f"[{self.module_id}] 處理請求異常: {e}")
            return {"error": str(e)}
    
    @abstractmethod
    def handle_frontend_request(self, data: dict) -> dict:
        """處理前端特定請求"""
        pass
    
    def emit_event(self, event_type: UIEventType, event_data: Dict[str, Any]):
        """發送 UI 事件"""
        event = UIEvent(
            event_type=event_type,
            timestamp=time.time(),
            data=event_data,
            source_module=self.module_id
        )
        
        # 只在 PyQt5 可用且有信號時發射
        if PYQT5_AVAILABLE and self.signals.event_occurred:
            self.signals.event_occurred.emit(event)
        
        debug_log(1, f"[{self.module_id}] 發送事件: {event_type.name}")
    
    def register_event_handler(self, 
                             event_type: UIEventType, 
                             handler: Callable[[UIEvent], None]):
        """註冊事件處理器"""
        if event_type not in self.event_handlers:
            self.event_handlers[event_type] = []
        
        self.event_handlers[event_type].append(handler)
        debug_log(1, f"[{self.module_id}] 註冊事件處理器: {event_type.name}")
    
    def update_local_state(self, key: str, value: Any):
        """更新本地狀態"""
        with self._lock:
            self.local_state[key] = value
            self.state_changed.emit(key, {key: value})
    
    def get_local_state(self, key: str = None) -> Any:
        """獲取本地狀態"""
        with self._lock:
            if key is None:
                return self.local_state.copy()
            return self.local_state.get(key)
    
    def get_system_state(self) -> Optional[UEPState]:
        """獲取系統狀態"""
        if self.state_manager:
            return self.state_manager.get_current_state()
        return None
    
    def update_context(self, context_type: ContextType, data: Dict[str, Any]):
        """更新工作上下文"""
        if self.context_manager:
            context_id = f"{self.module_id}_{context_type.value}_{int(time.time())}"
            self.context_manager.create_context(
                context_id=context_id,
                context_type=context_type,
                initial_data=data
            )
    
    def _on_state_changed(self, key: str, data: dict):
        """內部狀態變更處理"""
        debug_log(1, f"[{self.module_id}] 本地狀態變更: {key}")
        # 子類別可以覆寫此方法以處理狀態變更
        self.on_local_state_changed(key, data)
    
    def _on_event_occurred(self, event: UIEvent):
        """內部事件處理"""
        handlers = self.event_handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                error_log(f"[{self.module_id}] 事件處理器異常: {e}")
    
    def _on_system_state_changed(self, old_state: UEPState, new_state: UEPState):
        """系統狀態變更回調"""
        debug_log(1, f"[{self.module_id}] 系統狀態變更: {old_state} -> {new_state}")
        self.on_system_state_changed(old_state, new_state)
    
    # 子類別可覆寫的回調方法
    def on_local_state_changed(self, key: str, data: dict):
        """本地狀態變更回調 - 子類別可覆寫"""
        pass
    
    def on_system_state_changed(self, old_state: UEPState, new_state: UEPState):
        """系統狀態變更回調 - 子類別可覆寫"""
        pass
    
    def shutdown(self):
        """關閉模組"""
        self.is_active = False
        self.is_initialized = False
        
        # 清理 Qt 連接 (只在 PyQt5 可用時)
        if PYQT5_AVAILABLE and self.signals.state_changed:
            try:
                self.signals.state_changed.disconnect()
                self.signals.event_occurred.disconnect()
            except Exception as e:
                debug_log(1, f"[{self.module_id}] 清理信號連接時發生錯誤: {e}")
        
        info_log(f"[{self.module_id}] 模組已關閉")


class FrontendAdapter:
    """
    前端適配器 - 處理前端模組與核心框架的整合
    
    功能：
    - 管理前端模組註冊
    - 協調前端模組間通信
    - 處理前端特有的事件循環
    - 與核心框架同步
    """
    
    def __init__(self, framework):
        self.framework = framework
        self.frontend_modules = {}
        self.event_queue = []
        self.is_running = False
        
        # 事件處理執行緒
        self.event_thread = None
        self._stop_event = threading.Event()
        
        info_log("[FrontendAdapter] 前端適配器初始化完成")
    
    def register_frontend_module(self, module: BaseFrontendModule) -> bool:
        """註冊前端模組"""
        try:
            # 設置框架引用
            module.set_framework_references(
                self.framework,
                self.framework.context_manager,
                self.framework.state_manager
            )
            
            # 註冊到核心框架
            success = self.framework.register_module(
                module_id=module.module_id,
                module_instance=module,
                capabilities=[f"frontend_{module.module_type.value}"],
                priority=100  # 前端模組高優先級
            )
            
            if success:
                self.frontend_modules[module.module_id] = module
                
                # 連接事件信號
                module.event_occurred.connect(self._handle_frontend_event)
                
                info_log(f"[FrontendAdapter] 註冊前端模組: {module.module_id}")
                return True
            
            return False
            
        except Exception as e:
            error_log(f"[FrontendAdapter] 註冊前端模組失敗: {e}")
            return False
    
    def start_event_loop(self):
        """啟動前端事件循環"""
        if self.is_running:
            return
        
        self.is_running = True
        self._stop_event.clear()
        
        self.event_thread = threading.Thread(
            target=self._event_loop,
            name="FrontendEventLoop"
        )
        self.event_thread.start()
        
        info_log("[FrontendAdapter] 前端事件循環已啟動")
    
    def stop_event_loop(self):
        """停止前端事件循環"""
        if not self.is_running:
            return
        
        self.is_running = False
        self._stop_event.set()
        
        if self.event_thread:
            self.event_thread.join(timeout=5.0)
        
        info_log("[FrontendAdapter] 前端事件循環已停止")
    
    def _event_loop(self):
        """前端事件循環"""
        while self.is_running and not self._stop_event.is_set():
            try:
                # 處理事件佇列
                if self.event_queue:
                    event = self.event_queue.pop(0)
                    self._process_event(event)
                
                # 短暫休眠避免過度佔用 CPU
                time.sleep(0.01)  # 100 FPS
                
            except Exception as e:
                error_log(f"[FrontendAdapter] 事件循環異常: {e}")
    
    def _handle_frontend_event(self, event: UIEvent):
        """處理前端事件"""
        self.event_queue.append(event)
    
    def _process_event(self, event: UIEvent):
        """處理單個事件"""
        try:
            # 事件分發邏輯
            if event.target_module:
                # 定向事件
                target_module = self.frontend_modules.get(event.target_module)
                if target_module:
                    target_module._on_event_occurred(event)
            else:
                # 廣播事件
                for module in self.frontend_modules.values():
                    if module.module_id != event.source_module:
                        module._on_event_occurred(event)
            
            debug_log(1, f"[FrontendAdapter] 處理事件: {event.event_type.name}")
            
        except Exception as e:
            error_log(f"[FrontendAdapter] 事件處理異常: {e}")
    
    def get_frontend_module(self, module_type: FrontendModuleType) -> Optional[BaseFrontendModule]:
        """獲取前端模組"""
        module_id = f"frontend_{module_type.value}"
        return self.frontend_modules.get(module_id)
    
    def shutdown(self):
        """關閉適配器"""
        self.stop_event_loop()
        
        for module in self.frontend_modules.values():
            module.shutdown()
        
        self.frontend_modules.clear()
        info_log("[FrontendAdapter] 前端適配器已關閉")
