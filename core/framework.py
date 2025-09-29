# core/framework.py
"""
UEP 核心框架 - 統一的狀態管理、路由決策和上下文處理系統

這個框架整合了：
- Working Context Manager (上下文管理)
- State Manager (狀態管理) 
- Router (路由決策)
- Session Manager (會話管理)

實現從線性通信轉向網狀通信的核心架構
"""

import time
import threading
from typing import Dict, Any, Optional, List, Callable, Protocol, Union
from enum import Enum, auto
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

from utils.debug_helper import debug_log, info_log, error_log
from core.working_context import WorkingContextManager, ContextType, ContextStatus
from core.states.state_manager import UEPState, StateManager
from core.sessions.session_manager import WorkflowSession, SessionStatus


class ModuleState(Enum):
    """模組狀態"""
    AVAILABLE = auto()      # 可用
    BUSY = auto()          # 忙碌中
    ERROR = auto()         # 錯誤狀態
    DISABLED = auto()      # 已停用


class MessageType(Enum):
    """消息類型"""
    REQUEST = auto()        # 請求
    RESPONSE = auto()       # 回應
    EVENT = auto()         # 事件
    NOTIFICATION = auto()   # 通知


class ExecutionMode(Enum):
    """執行模式"""
    SEQUENTIAL = auto()     # 順序執行
    PARALLEL = auto()       # 並行執行
    CONDITIONAL = auto()    # 條件執行
    PRIORITY = auto()       # 優先級執行


@dataclass
class ModuleInfo:
    """模組資訊"""
    module_id: str
    module_instance: Any
    capabilities: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    state: ModuleState = ModuleState.AVAILABLE
    priority: int = 0
    last_active: float = field(default_factory=time.time)


@dataclass 
class FrameworkMessage:
    """框架內部消息"""
    message_id: str
    message_type: MessageType
    source_module: str
    target_module: Optional[str]
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    correlation_id: Optional[str] = None


class RouteStrategy(Protocol):
    """路由策略協議"""
    
    def calculate_route(self, 
                       intent: str, 
                       context: Dict[str, Any], 
                       available_modules: Dict[str, ModuleInfo]) -> List[str]:
        """計算最佳路由路徑"""
        ...


class DecisionEngine(ABC):
    """決策引擎抽象基類"""
    
    @abstractmethod
    def make_decision(self, 
                     current_state: UEPState,
                     context: Dict[str, Any],
                     available_options: List[str]) -> Dict[str, Any]:
        """做出決策"""
        pass


class CoreFramework:
    """UEP 核心框架 - 統一管理所有核心組件"""
    
    def __init__(self, use_schema_adapter=True):
        """初始化核心框架"""
        # 核心組件初始化
        self.working_context = WorkingContextManager()
        self.state_manager = StateManager()
        
        # Schema 適配器支持
        self.use_schema_adapter = use_schema_adapter
        if self.use_schema_adapter:
            try:
                from core.schema_adapter import schema_handler
                self.schema_handler = schema_handler
                info_log("[CoreFramework] Schema 適配器已啟用")
            except ImportError:
                info_log("[CoreFramework] Schema 適配器不可用，使用傳統數據處理")
                self.use_schema_adapter = False
                self.schema_handler = None
        else:
            self.schema_handler = None
        
        # 模組註冊表
        self.modules: Dict[str, ModuleInfo] = {}
        
        # 消息佇列和事件系統
        self.message_queue: List[FrameworkMessage] = []
        self.event_handlers: Dict[str, List[Callable]] = {}
        
        # 路由和決策引擎
        self.route_strategies: Dict[str, RouteStrategy] = {}
        self.decision_engines: Dict[str, DecisionEngine] = {}
        
        # 活躍會話管理
        self.active_sessions: Dict[str, WorkflowSession] = {}
        
        # 執行上下文
        self.execution_context: Dict[str, Any] = {
            'current_pipeline': [],
            'pending_tasks': [],
            'global_context': {}
        }
        
        # 執行緒安全
        self._lock = threading.RLock()
        
        info_log("[CoreFramework] 核心框架初始化完成")
    
    # ========== 模組管理 ==========
    
    def register_module(self, 
                       module_id: str, 
                       module_instance: Any,
                       capabilities: List[str] = None,
                       dependencies: List[str] = None,
                       priority: int = 0) -> bool:
        """
        註冊模組到框架
        
        Args:
            module_id: 模組唯一標識
            module_instance: 模組實例
            capabilities: 模組能力列表
            dependencies: 依賴的其他模組
            priority: 優先級 (數字越大優先級越高)
            
        Returns:
            註冊是否成功
        """
        try:
            with self._lock:
                if module_id in self.modules:
                    error_log(f"[CoreFramework] 模組 {module_id} 已存在")
                    return False
                
                module_info = ModuleInfo(
                    module_id=module_id,
                    module_instance=module_instance,
                    capabilities=capabilities or [],
                    dependencies=dependencies or [],
                    priority=priority
                )
                
                self.modules[module_id] = module_info
                info_log(f"[CoreFramework] 註冊模組: {module_id}")
                
                # 觸發模組註冊事件
                self._emit_event('module_registered', {
                    'module_id': module_id,
                    'capabilities': capabilities or []
                })
                
                return True
                
        except Exception as e:
            error_log(f"[CoreFramework] 註冊模組失敗 {module_id}: {e}")
            return False
    
    def unregister_module(self, module_id: str) -> bool:
        """註銷模組"""
        try:
            with self._lock:
                if module_id not in self.modules:
                    error_log(f"[CoreFramework] 模組 {module_id} 不存在")
                    return False
                
                del self.modules[module_id]
                info_log(f"[CoreFramework] 註銷模組: {module_id}")
                
                # 觸發模組註銷事件
                self._emit_event('module_unregistered', {'module_id': module_id})
                
                return True
                
        except Exception as e:
            error_log(f"[CoreFramework] 註銷模組失敗 {module_id}: {e}")
            return False
    
    def get_module(self, module_id: str) -> Optional[Any]:
        """獲取模組實例"""
        module_info = self.modules.get(module_id)
        return module_info.module_instance if module_info else None
    
    def get_available_modules(self) -> Dict[str, ModuleInfo]:
        """獲取所有可用模組"""
        return {
            module_id: info for module_id, info in self.modules.items()
            if info.state == ModuleState.AVAILABLE
        }
    
    def get_modules_by_capability(self, capability: str) -> List[str]:
        """根據能力獲取模組列表"""
        return [
            module_id for module_id, info in self.modules.items()
            if capability in info.capabilities and info.state == ModuleState.AVAILABLE
        ]
    
    # ========== 狀態管理整合 ==========
    
    def get_current_state(self) -> UEPState:
        """獲取當前系統狀態"""
        return self.state_manager.get_state()
    
    def set_state(self, new_state: UEPState, context: Dict[str, Any] = None):
        """設置系統狀態"""
        old_state = self.state_manager.get_state()
        self.state_manager.set_state(new_state)
        
        # 觸發狀態變更事件
        self._emit_event('state_changed', {
            'old_state': old_state,
            'new_state': new_state,
            'context': context or {}
        })
        
        info_log(f"[CoreFramework] 狀態變更: {old_state.name} → {new_state.name}")
    
    def handle_state_event(self, intent: str, result: Dict[str, Any]):
        """處理狀態事件 (整合原有的 state_manager.on_event)"""
        self.state_manager.on_event(intent, result)
        
        # 更新執行上下文
        self.execution_context['global_context'].update({
            'last_intent': intent,
            'last_result': result,
            'timestamp': time.time()
        })
    
    # ========== 智能路由系統 ==========
    
    def register_route_strategy(self, strategy_name: str, strategy: RouteStrategy):
        """註冊路由策略"""
        self.route_strategies[strategy_name] = strategy
        info_log(f"[CoreFramework] 註冊路由策略: {strategy_name}")
    
    def calculate_optimal_route(self, 
                              intent: str, 
                              context: Dict[str, Any],
                              strategy_name: str = 'default') -> List[str]:
        """
        計算最佳執行路由
        
        Args:
            intent: 意圖
            context: 上下文資訊
            strategy_name: 使用的路由策略
            
        Returns:
            模組執行順序列表
        """
        strategy = self.route_strategies.get(strategy_name)
        if not strategy:
            # 使用預設路由策略
            return self._default_route(intent, context)
        
        available_modules = self.get_available_modules()
        route = strategy.calculate_route(intent, context, available_modules)
        
        debug_log(2, f"[CoreFramework] 計算路由 {intent}: {' → '.join(route)}")
        return route
    
    def _default_route(self, intent: str, context: Dict[str, Any]) -> List[str]:
        """預設路由策略 (與原有 router 兼容)"""
        # 基於意圖的基本路由
        if intent == "chat":
            return ["nlp", "mem", "llm", "tts"]
        elif intent == "command":
            return ["nlp", "llm", "sys"]
        else:
            return ["llm"]
    
    # ========== 執行引擎 ==========
    
    def execute_pipeline(self, 
                        intent: str,
                        data: Dict[str, Any],
                        execution_mode: ExecutionMode = ExecutionMode.SEQUENTIAL) -> Dict[str, Any]:
        """
        執行處理管線
        
        Args:
            intent: 處理意圖
            data: 輸入資料
            execution_mode: 執行模式
            
        Returns:
            處理結果
        """
        try:
            # 計算執行路由
            route = self.calculate_optimal_route(intent, data)
            
            if not route:
                return {"status": "error", "message": "無法計算有效路由"}
            
            # 記錄執行管線
            self.execution_context['current_pipeline'] = route
            
            # 根據執行模式處理
            if execution_mode == ExecutionMode.SEQUENTIAL:
                return self._execute_sequential(route, intent, data)
            elif execution_mode == ExecutionMode.PARALLEL:
                return self._execute_parallel(route, intent, data)
            elif execution_mode == ExecutionMode.CONDITIONAL:
                return self._execute_conditional(route, intent, data)
            else:
                return self._execute_sequential(route, intent, data)
                
        except Exception as e:
            error_log(f"[CoreFramework] 執行管線失敗: {e}")
            return {"status": "error", "message": str(e)}
    
    def _execute_sequential(self, route: List[str], intent: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """順序執行管線"""
        from core.data_transformer import smart_transform
        
        current_data = data.copy()
        current_data['intent'] = intent
        previous_module = None
        
        for i, module_id in enumerate(route):
            module = self.get_module(module_id)
            if not module:
                error_log(f"[CoreFramework] 模組不存在: {module_id}")
                continue
            
            try:
                # 更新模組狀態
                if module_id in self.modules:
                    self.modules[module_id].state = ModuleState.BUSY
                    self.modules[module_id].last_active = time.time()
                
                # 如果不是第一個模組，進行數據轉換
                if previous_module and i > 0:
                    debug_log(3, f"[CoreFramework] 數據轉換: {previous_module} -> {module_id}")
                    current_data = smart_transform(previous_module, module_id, current_data)
                
                # 執行模組處理
                debug_log(2, f"[CoreFramework] 執行模組: {module_id}")
                
                # 檢查模組是否為非同步
                import asyncio
                import inspect
                
                if inspect.iscoroutinefunction(module.handle):
                    # 非同步模組處理
                    try:
                        # 嘗試獲取事件循環
                        try:
                            loop = asyncio.get_running_loop()
                            # 如果已經在事件循環中，使用 run_coroutine_threadsafe
                            import concurrent.futures
                            import threading
                            
                            def run_async():
                                return asyncio.run(module.handle(current_data))
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(run_async)
                                result = future.result()
                        except RuntimeError:
                            # 沒有運行中的事件循環，直接運行
                            result = asyncio.run(module.handle(current_data))
                    except Exception as e:
                        debug_log(1, f"[CoreFramework] 非同步模組執行失敗: {e}")
                        # 如果非同步執行失敗，嘗試直接呼叫 (但這會得到協程對象)
                        try:
                            # 最後嘗試，直接運行協程
                            result = asyncio.run(module.handle(current_data))
                        except Exception as e2:
                            error_log(f"[CoreFramework] 模組執行異常 {module_id}: {e2}")
                            return {"status": "error", "message": str(e2)}
                else:
                    # 同步模組處理
                    result = module.handle(current_data)
                
                if result.get("status") == "error":
                    error_log(f"[CoreFramework] 模組執行失敗 {module_id}: {result.get('message')}")
                    return result
                
                # 更新資料流 - 保留重要字段，用新結果更新
                if isinstance(result, dict):
                    # 保留原始輸入和意圖
                    result['original_input'] = data.get('text', data.get('original_input', ''))
                    result['intent'] = intent
                    current_data = result
                else:
                    # 如果結果不是字典，包裝它
                    current_data = {
                        'data': result,
                        'original_input': data.get('text', ''),
                        'intent': intent
                    }
                
                # 觸發模組執行事件
                self._emit_event('module_executed', {
                    'module_id': module_id,
                    'intent': intent,
                    'result': result
                })
                
                # 記錄當前模組為下次轉換做準備
                previous_module = module_id
                
            except Exception as e:
                error_log(f"[CoreFramework] 模組執行異常 {module_id}: {e}")
                return {"status": "error", "message": f"模組 {module_id} 執行失敗: {str(e)}"}
            
            finally:
                # 恢復模組狀態
                if module_id in self.modules:
                    self.modules[module_id].state = ModuleState.AVAILABLE
        
        return current_data
    
    def _execute_parallel(self, route: List[str], intent: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """並行執行管線 (待實現)"""
        # TODO: 實現並行執行邏輯
        return self._execute_sequential(route, intent, data)
    
    def _execute_conditional(self, route: List[str], intent: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """條件執行管線 (待實現)"""
        # TODO: 實現條件執行邏輯
        return self._execute_sequential(route, intent, data)
    
    # ========== 事件系統 ==========
    
    def register_event_handler(self, event_name: str, handler: Callable):
        """註冊事件處理器"""
        if event_name not in self.event_handlers:
            self.event_handlers[event_name] = []
        self.event_handlers[event_name].append(handler)
        debug_log(3, f"[CoreFramework] 註冊事件處理器: {event_name}")
    
    def _emit_event(self, event_name: str, event_data: Dict[str, Any]):
        """觸發事件"""
        handlers = self.event_handlers.get(event_name, [])
        for handler in handlers:
            try:
                handler(event_data)
            except Exception as e:
                error_log(f"[CoreFramework] 事件處理器異常 {event_name}: {e}")
    
    # ========== Working Context 整合 ==========
    
    def add_context_data(self, 
                        context_type: ContextType, 
                        data: Any, 
                        metadata: Dict[str, Any] = None) -> Optional[str]:
        """添加資料到工作上下文"""
        return self.working_context.add_data_to_context(context_type, data, metadata)
    
    def get_context_status(self, context_id: str) -> Optional[Dict[str, Any]]:
        """獲取上下文狀態"""
        return self.working_context.get_context_status(context_id)
    
    def register_decision_handler(self, context_type: ContextType, handler):
        """註冊決策處理器"""
        self.working_context.register_decision_handler(context_type, handler)
    
    # ========== 會話管理 ==========
    
    def create_workflow_session(self, 
                               workflow_type: str,
                               command: str,
                               initial_data: Dict[str, Any] = None) -> str:
        """創建工作流會話"""
        from core.sessions.session_manager import session_manager
        
        # 使用統一的 session_manager 創建會話
        session = session_manager.create_session(
            workflow_type=workflow_type,
            command=command,
            initial_data=initial_data
        )
        
        # 設置系統狀態為 WORK
        self.state_manager.set_state(UEPState.WORK)
        
        info_log(f"[CoreFramework] 創建工作流會話: {session.session_id}")
        return session.session_id
    
    def get_session(self, session_id: str) -> Optional[WorkflowSession]:
        """獲取工作流會話"""
        return self.active_sessions.get(session_id)
    
    def complete_session(self, session_id: str, result: Dict[str, Any] = None) -> bool:
        """完成工作流會話"""
        session = self.get_session(session_id)
        if session:
            session.complete_session(result)
            # 清理已完成的會話
            if session.status in [SessionStatus.COMPLETED, SessionStatus.FAILED]:
                del self.active_sessions[session_id]
            return True
        return False
    
    # ========== 框架管理 ==========
    
    def get_framework_status(self) -> Dict[str, Any]:
        """獲取框架狀態"""
        return {
            'current_state': self.get_current_state().name,
            'registered_modules': list(self.modules.keys()),
            'available_modules': list(self.get_available_modules().keys()),
            'active_sessions': len(self.active_sessions),
            'working_contexts': len(self.working_context.contexts),
            'execution_context': self.execution_context.copy(),
            'timestamp': time.time()
        }
    
    def cleanup(self):
        """清理框架資源"""
        # 清理過期上下文
        self.working_context.cleanup_expired_contexts()
        
        # 清理完成的會話
        completed_sessions = [
            session_id for session_id, session in self.active_sessions.items()
            if session.status in [SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.EXPIRED]
        ]
        
        for session_id in completed_sessions:
            del self.active_sessions[session_id]
        
        debug_log(3, f"[CoreFramework] 清理完成，移除 {len(completed_sessions)} 個會話")


# 全局框架實例
core_framework = CoreFramework()
