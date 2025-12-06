# core/framework.py
"""
UEP 核心框架 - 系統骨架和模組註冊管理

這個框架負責：
- 模組註冊與代管
- 系統層模組的 Meta 定義
- 為系統流程提供基礎骨架
- 向 registry 註冊模組

設計原則：
- 專注於骨架功能，不重複實現管理器邏輯
- 為後續系統流程提供基礎架構
- 輕量化設計，避免功能重疊
"""

import time
import threading
from typing import Dict, Any, Optional, List
from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict, deque
from abc import ABC, abstractmethod

from utils.debug_helper import debug_log, info_log, error_log


class ModuleState(Enum):
    """模組狀態"""
    AVAILABLE = "available"      # 可用
    BUSY = "busy"               # 忙碌中
    ERROR = "error"             # 錯誤狀態
    DISABLED = "disabled"       # 已停用
    INITIALIZING = "initializing"  # 初始化中


class ModuleType(Enum):
    """模組類型"""
    INPUT = "input"             # 輸入層模組 (STT)
    PROCESSING = "processing"   # 處理層模組 (NLP, MEM, LLM)
    OUTPUT = "output"          # 輸出層模組 (TTS)
    SYSTEM = "system"          # 系統層模組 (SYS)
    UI = "ui"                  # 用戶介面模組


@dataclass
class ModuleCapabilities:
    """模組能力定義"""
    # STT 模組能力
    STT_CAPABILITIES = [
        "speech_recognition", 
        "speaker_identification", 
        "voice_activity_detection",
        "real_time_transcription"
    ]
    
    # NLP 模組能力  
    NLP_CAPABILITIES = [
        "intent_recognition",
        "sentiment_analysis", 
        "text_classification",
        "language_understanding",
        "entity_extraction"
    ]
    
    # MEM 模組能力
    MEM_CAPABILITIES = [
        "memory_storage",
        "memory_retrieval", 
        "contextual_search",
        "semantic_indexing",
        "memory_analysis"
    ]
    
    # LLM 模組能力
    LLM_CAPABILITIES = [
        "text_generation",
        "conversation",
        "question_answering", 
        "reasoning",
        "summarization"
    ]
    
    # SYS 模組能力
    SYS_CAPABILITIES = [
        "system_command",
        "file_operations",
        "process_management",
        "workflow_execution"
    ]
    
    # TTS 模組能力
    TTS_CAPABILITIES = [
        "text_to_speech",
        "voice_synthesis",
        "audio_output",
        "voice_cloning"
    ]


@dataclass
class PerformanceMetrics:
    """模組效能指標"""
    module_id: str
    timestamp: float = field(default_factory=time.time)
    
    # 處理效能
    processing_time: float = 0.0  # 最近一次處理時間 (秒)
    average_processing_time: float = 0.0  # 平均處理時間
    peak_processing_time: float = 0.0  # 峰值處理時間
    
    # 記憶體使用
    memory_usage: float = 0.0  # 當前記憶體使用 (MB)
    peak_memory_usage: float = 0.0  # 峰值記憶體使用
    
    # 工作負載統計
    total_requests: int = 0  # 總請求數
    successful_requests: int = 0  # 成功請求數
    failed_requests: int = 0  # 失敗請求數
    
    # 模組狀態
    is_active: bool = True
    last_activity: float = field(default_factory=time.time)
    error_count: int = 0
    
    # 自定義指標
    custom_metrics: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def success_rate(self) -> float:
        """成功率"""
        if self.total_requests == 0:
            return 1.0
        return self.successful_requests / self.total_requests
    
    @property
    def error_rate(self) -> float:
        """錯誤率"""
        return 1.0 - self.success_rate

@dataclass
class SystemPerformanceSnapshot:
    """系統效能快照"""
    timestamp: float = field(default_factory=time.time)
    
    # 系統整體狀態
    total_modules: int = 0
    active_modules: int = 0
    failed_modules: int = 0
    
    # 系統整體效能
    system_cpu_usage: float = 0.0
    system_memory_usage: float = 0.0
    system_uptime: float = 0.0
    
    # 模組效能指標
    module_metrics: Dict[str, PerformanceMetrics] = field(default_factory=dict)
    
    # 系統級統計
    total_system_requests: int = 0
    system_success_rate: float = 1.0
    system_average_response_time: float = 0.0


@dataclass
class ModuleInfo:
    """模組資訊"""
    module_id: str
    module_name: str
    module_instance: Any
    module_type: ModuleType
    capabilities: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    state: ModuleState = ModuleState.AVAILABLE
    priority: int = 0
    version: str = "1.0.0"
    last_active: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemFlow:
    """系統流程骨架定義"""
    flow_id: str
    flow_name: str
    required_modules: List[str]
    optional_modules: List[str] = field(default_factory=list)
    execution_order: List[str] = field(default_factory=list)
    flow_metadata: Dict[str, Any] = field(default_factory=dict)


class ModuleRegistry(ABC):
    """模組註冊表抽象介面"""
    
    @abstractmethod
    def register_module(self, module_info: ModuleInfo) -> bool:
        """註冊模組"""
        pass
    
    @abstractmethod
    def get_module(self, module_id: str) -> Optional[ModuleInfo]:
        """獲取模組資訊"""
        pass
    
    @abstractmethod
    def list_modules(self) -> List[ModuleInfo]:
        """列出所有模組"""
        pass


class CoreFramework:
    """UEP 核心框架 - 系統骨架和模組管理"""
    
    def __init__(self):
        """初始化核心框架"""
        # 載入配置
        from configs.config_loader import load_config
        self.config = load_config()
        
        # 框架狀態
        self.is_initialized = False
        self.initialization_time = None
        
        # 模組註冊表
        self.modules: Dict[str, ModuleInfo] = {}
        
        # 系統流程定義
        self.system_flows: Dict[str, SystemFlow] = {}
        
        # Schema 適配器已移除 - 模組使用自己的 Input/Output Schema
        
        # 前端橋接器（可選，在 debug GUI 或生產模式中初始化）
        self.frontend_bridge: Optional['FrontendBridge'] = None  # type: ignore
        
        # 框架狀態
        self.is_initialized = False
        self.initialization_time = None
        
        # ========== 效能監控系統 ==========
        self.performance_monitoring_enabled = True
        self.performance_metrics: Dict[str, PerformanceMetrics] = {}
        self.performance_history: deque = deque(maxlen=100)  # 保留最近100個快照
        self.metrics_lock = threading.Lock()
        self.modules_lock = threading.Lock()  # 保護 self.modules 字典的執行緒鎖
        self.system_start_time = time.time()
        
        # 監控統計
        self.monitoring_stats: Dict[str, int | float] = {
            "total_snapshots": 0,
            "last_snapshot_time": 0.0,
            "monitoring_errors": 0
        }
        
        # 初始化預定義流程
        self._initialize_system_flows()
        
        # 🌙 訂閱 SLEEP 相關事件
        self._subscribe_sleep_events()
        
        info_log("[CoreFramework] 核心框架初始化完成")
        info_log("[CoreFramework] 效能監控系統已啟用")
    
    # ========== 初始化方法 ==========
    
    def initialize(self) -> bool:
        """初始化框架 - 自動發現和註冊模組"""
        try:
            if self.is_initialized:
                info_log("[CoreFramework] 框架已初始化")
                return True
            
            info_log("[CoreFramework] 開始自動模組發現和註冊...")
            
            # 自動發現和註冊模組
            self._auto_discover_modules()
            
            self.is_initialized = True
            self.initialization_time = time.time()
            
            info_log(f"[CoreFramework] 框架初始化完成，已註冊 {len(self.modules)} 個模組")
            return True
            
        except Exception as e:
            error_log(f"[CoreFramework] 框架初始化失敗: {e}")
            return False
    
    def _auto_discover_modules(self):
        """自動發現和註冊模組"""
        try:
            # 嘗試載入各個模組
            module_configs = [
                {
                    "module_id": "stt",
                    "module_name": "stt_module", 
                    "module_type": ModuleType.INPUT,
                    "capabilities": ModuleCapabilities.STT_CAPABILITIES,
                    "priority": 10
                },
                {
                    "module_id": "nlp",
                    "module_name": "nlp_module",
                    "module_type": ModuleType.INPUT, 
                    "capabilities": ModuleCapabilities.NLP_CAPABILITIES,
                    "priority": 20
                },
                {
                    "module_id": "mem",
                    "module_name": "mem_module",
                    "module_type": ModuleType.PROCESSING,
                    "capabilities": ModuleCapabilities.MEM_CAPABILITIES,
                    "priority": 15
                },
                {
                    "module_id": "llm", 
                    "module_name": "llm_module",
                    "module_type": ModuleType.PROCESSING,
                    "capabilities": ModuleCapabilities.LLM_CAPABILITIES,
                    "priority": 25
                },
                {
                    "module_id": "tts",
                    "module_name": "tts_module",
                    "module_type": ModuleType.OUTPUT,
                    "capabilities": ModuleCapabilities.TTS_CAPABILITIES,
                    "priority": 5
                },
                {
                    "module_id": "sys",
                    "module_name": "sys_module", 
                    "module_type": ModuleType.PROCESSING,
                    "capabilities": ModuleCapabilities.SYS_CAPABILITIES,
                    "priority": 30
                },
                # 前端模組 (UI 由 Framework 管理，ANI/MOV 由 UI 內部透過 debug_api 載入)
                {
                    "module_id": "ui",
                    "module_name": "ui_module",
                    "module_type": ModuleType.OUTPUT,
                    "capabilities": [],  # UI 模組不需要 capabilities
                    "priority": 1
                }
            ]
            
            for config in module_configs:
                self._try_register_module(config)
                
        except Exception as e:
            error_log(f"[CoreFramework] 自動模組發現失敗: {e}")
    
    def _try_register_module(self, config: Dict[str, Any]):
        """嘗試註冊單個模組 - 參考 debug_api 的錯誤處理方式"""
        try:
            module_name = config["module_name"]
            
            # 檢查配置是否啟用此模組
            modules_enabled = self.config.get("modules_enabled", {})
            if not modules_enabled.get(module_name, False):
                debug_log(2, f"[CoreFramework] 模組 {module_name} 在配置中被停用，跳過註冊")
                return False
            
            info_log(f"[CoreFramework] 嘗試載入模組 '{module_name}'")
            
            # 嘗試載入模組實例 - 參考 debug_api 的錯誤處理
            from core.registry import get_module
            try:
                module_instance = get_module(module_name)
                if module_instance is None:
                    raise ImportError(f"{module_name} register() 回傳為 None")
                info_log(f"[CoreFramework] 載入模組成功：{module_name}")
            except NotImplementedError:
                debug_log(1, f"[CoreFramework] 模組 '{module_name}' 尚未被實作")
                return False
            except ImportError as e:
                error_log(f"[CoreFramework] 無法導入模組 '{module_name}': {e}")
                return False
            except Exception as e:
                error_log(f"[CoreFramework] 載入模組 '{module_name}' 時發生錯誤: {e}")
                return False
            
            # 創建模組資訊
            module_info = ModuleInfo(
                module_id=config["module_id"],
                module_name=module_name,
                module_instance=module_instance,
                module_type=config["module_type"],
                capabilities=config["capabilities"],
                priority=config["priority"],
                metadata={
                    "auto_discovered": True,
                    "registration_time": time.time(),
                    "enabled_in_config": True
                }
            )
            
            # 註冊模組
            success = self.register_module(module_info)
            if success:
                info_log(f"[CoreFramework] 已註冊模組: {config['module_id']}")
            
            return success
            
        except Exception as e:
            debug_log(1, f"[CoreFramework] 註冊模組失敗 {config.get('module_id', 'unknown')}: {e}")
            return False
    
    # ========== 模組註冊管理 ==========
    
    def register_module(self, module_info: ModuleInfo) -> bool:
        """
        註冊模組到框架
        
        Args:
            module_info: 模組資訊
            
        Returns:
            註冊是否成功
        """
        try:
            with self.modules_lock:
                if module_info.module_id in self.modules:
                    debug_log(1, f"[CoreFramework] 模組 {module_info.module_id} 已存在，跳過註冊")
                    return False
                
                # 註冊到本地註冊表
                self.modules[module_info.module_id] = module_info
                
                # 註意: registry.py 只提供 get_module() 函數用於載入模組
                # 它會自動調用模組的 register() 並緩存實例
                # 不需要手動註冊到 registry,因為模組已經通過 get_module() 載入
                
                debug_log(2, f"[CoreFramework] 已註冊模組: {module_info.module_id}")
                return True
            
        except Exception as e:
            error_log(f"[CoreFramework] 註冊模組失敗 {module_info.module_id}: {e}")
            return False
    
    def unregister_module(self, module_id: str) -> bool:
        """註銷模組"""
        try:
            with self.modules_lock:
                if module_id not in self.modules:
                    debug_log(1, f"[CoreFramework] 模組 {module_id} 不存在")
                    return False
                
                module_info = self.modules[module_id]
                
                # 註意: registry.py 的 _loaded_modules 是模組級私有變數
                # 不提供 unregister 方法,也不應該直接操作
                # 模組註銷只影響 framework 本地註冊表
                
                # 從本地註冊表移除
                del self.modules[module_id]
                
                info_log(f"[CoreFramework] 已註銷模組: {module_id}")
                return True
            
        except Exception as e:
            error_log(f"[CoreFramework] 註銷模組失敗 {module_id}: {e}")
            return False
    
    def get_module(self, module_id: str) -> Optional[Any]:
        """獲取模組實例"""
        with self.modules_lock:
            module_info = self.modules.get(module_id)
            return module_info.module_instance if module_info else None
    
    def get_module_info(self, module_id: str) -> Optional[ModuleInfo]:
        """獲取模組資訊"""
        with self.modules_lock:
            return self.modules.get(module_id)
    
    def list_modules(self, module_type: Optional[ModuleType] = None) -> List[ModuleInfo]:
        """列出模組"""
        with self.modules_lock:
            if module_type is None:
                return list(self.modules.values())
            else:
                return [info for info in self.modules.values() if info.module_type == module_type]
    
    def get_modules_by_capability(self, capability: str) -> List[ModuleInfo]:
        """根據能力獲取模組"""
        with self.modules_lock:
            return [
                info for info in self.modules.values()
                if capability in info.capabilities and info.state == ModuleState.AVAILABLE
            ]
    
    # ========== 系統流程骨架 ==========
    
    def _initialize_system_flows(self):
        """初始化預定義的系統流程"""
        # 對話流程
        chat_flow = SystemFlow(
            flow_id="chat_flow",
            flow_name="對話處理流程",
            required_modules=["nlp", "llm"],
            optional_modules=["mem", "tts"],
            execution_order=["nlp", "mem", "llm", "tts"]
        )
        
        # 指令流程
        command_flow = SystemFlow(
            flow_id="command_flow", 
            flow_name="指令處理流程",
            required_modules=["nlp", "sys"],
            optional_modules=["mem", "llm"],
            execution_order=["nlp", "mem", "llm", "sys"]
        )
        
        # 語音輸入流程
        voice_flow = SystemFlow(
            flow_id="voice_flow",
            flow_name="語音輸入流程",
            required_modules=["stt", "nlp"],
            optional_modules=["mem", "llm", "tts"],
            execution_order=["stt", "nlp", "mem", "llm", "tts"]
        )
        
        self.system_flows = {
            "chat": chat_flow,
            "command": command_flow,
            "voice": voice_flow
        }
    
    def get_system_flow(self, flow_id: str) -> Optional[SystemFlow]:
        """獲取系統流程定義"""
        return self.system_flows.get(flow_id)
    
    def register_system_flow(self, flow: SystemFlow):
        """註冊自定義系統流程"""
        self.system_flows[flow.flow_id] = flow
        info_log(f"[CoreFramework] 已註冊系統流程: {flow.flow_id}")
    
    # ========== 框架狀態和統計 ==========
    
    def get_framework_status(self) -> Dict[str, Any]:
        """獲取框架狀態"""
        uptime = time.time() - self.initialization_time if self.initialization_time else 0
        
        module_states = {}
        with self.modules_lock:
            for module_id, info in self.modules.items():
                module_states[module_id] = {
                    "state": info.state.value,
                    "type": info.module_type.value,
                    "capabilities": info.capabilities,
                    "last_active": info.last_active
                }
            
            total_modules = len(self.modules)
            available_modules = len([m for m in self.modules.values() if m.state == ModuleState.AVAILABLE])
        
        return {
            "is_initialized": self.is_initialized,
            "uptime_seconds": uptime,
            "total_modules": total_modules,
            "available_modules": available_modules,
            "system_flows": list(self.system_flows.keys()),
            "module_states": module_states
        }
    
    def update_module_state(self, module_id: str, new_state: ModuleState):
        """更新模組狀態"""
        with self.modules_lock:
            if module_id in self.modules:
                old_state = self.modules[module_id].state
                self.modules[module_id].state = new_state
                self.modules[module_id].last_active = time.time()
                
                debug_log(3, f"[CoreFramework] 模組狀態更新 {module_id}: {old_state.value} → {new_state.value}")
    
    # ========== 系統骨架支援方法 ==========
    
    def validate_flow_dependencies(self, flow_id: str) -> Dict[str, Any]:
        """驗證系統流程的依賴關係"""
        flow = self.get_system_flow(flow_id)
        if not flow:
            return {"valid": False, "error": f"流程 {flow_id} 不存在"}
        
        missing_modules = []
        available_modules = []
        
        with self.modules_lock:
            for module_id in flow.required_modules:
                if module_id in self.modules and self.modules[module_id].state == ModuleState.AVAILABLE:
                    available_modules.append(module_id)
                else:
                    missing_modules.append(module_id)
        
        return {
            "valid": len(missing_modules) == 0,
            "missing_modules": missing_modules,
            "available_modules": available_modules,
            "flow": flow
        }
    
    def get_execution_skeleton(self, flow_id: str) -> Optional[List[str]]:
        """獲取執行骨架（模組執行順序）"""
        validation = self.validate_flow_dependencies(flow_id)
        if validation["valid"]:
            flow = validation["flow"]
            return flow.execution_order
        else:
            error_log(f"[CoreFramework] 流程 {flow_id} 依賴不滿足: {validation['missing_modules']}")
            return None

    # ========== 效能監控方法 ==========
    
    # NOTE: 模組效能監控整合功能
    # 各模組尚未完成重構以支援自動效能指標報告
    # 當模組重構完成後，模組應調用 update_module_metrics() 提供效能資料
    # System Loop 會定期調用 collect_system_performance_snapshot() 進行監控
    
    def enable_performance_monitoring(self, enabled: bool = True):
        """啟用/停用效能監控"""
        self.performance_monitoring_enabled = enabled
        status = "啟用" if enabled else "停用"
        info_log(f"[CoreFramework] 效能監控已{status}")
    
    def update_module_metrics(self, module_id: str, metrics_data: Dict[str, Any]):
        """更新模組效能指標 - 供模組調用"""
        if not self.performance_monitoring_enabled:
            return
            
        try:
            with self.metrics_lock:
                if module_id not in self.performance_metrics:
                    self.performance_metrics[module_id] = PerformanceMetrics(module_id=module_id)
                
                metrics = self.performance_metrics[module_id]
                current_time = time.time()
                
                # 更新基本指標
                if 'processing_time' in metrics_data:
                    processing_time = metrics_data['processing_time']
                    metrics.processing_time = processing_time
                    
                    # 更新平均處理時間
                    if metrics.total_requests > 0:
                        total_time = metrics.average_processing_time * metrics.total_requests
                        metrics.average_processing_time = (total_time + processing_time) / (metrics.total_requests + 1)
                    else:
                        metrics.average_processing_time = processing_time
                    
                    # 更新峰值處理時間
                    if processing_time > metrics.peak_processing_time:
                        metrics.peak_processing_time = processing_time
                
                # 更新記憶體使用
                if 'memory_usage' in metrics_data:
                    memory_usage = metrics_data['memory_usage']
                    metrics.memory_usage = memory_usage
                    if memory_usage > metrics.peak_memory_usage:
                        metrics.peak_memory_usage = memory_usage
                
                # 更新請求統計
                if 'request_result' in metrics_data:
                    metrics.total_requests += 1
                    if metrics_data['request_result'] == 'success':
                        metrics.successful_requests += 1
                    else:
                        metrics.failed_requests += 1
                        metrics.error_count += 1
                
                # 更新活動狀態
                metrics.last_activity = current_time
                metrics.is_active = True
                
                # 更新自定義指標
                if 'custom_metrics' in metrics_data:
                    metrics.custom_metrics.update(metrics_data['custom_metrics'])
                
                # 更新時間戳
                metrics.timestamp = current_time
                
                debug_log(3, f"[CoreFramework] 已更新 {module_id} 效能指標")
                
        except Exception as e:
            self.monitoring_stats["monitoring_errors"] += 1
            error_log(f"[CoreFramework] 更新 {module_id} 效能指標失敗: {e}")
    
    def get_module_metrics(self, module_id: str) -> Optional[PerformanceMetrics]:
        """獲取模組效能指標"""
        with self.metrics_lock:
            return self.performance_metrics.get(module_id)
    
    def get_all_module_metrics(self) -> Dict[str, PerformanceMetrics]:
        """獲取所有模組效能指標"""
        with self.metrics_lock:
            return self.performance_metrics.copy()
    
    def collect_system_performance_snapshot(self) -> SystemPerformanceSnapshot:
        """蒐集系統效能快照 - 供 system loop 調用"""
        try:
            current_time = time.time()
            
            with self.metrics_lock:
                # 統計模組狀態
                total_modules = len(self.modules)
                active_modules = sum(1 for metrics in self.performance_metrics.values() 
                                   if metrics.is_active and (current_time - metrics.last_activity) < 300)  # 5分鐘內有活動
                failed_modules = sum(1 for metrics in self.performance_metrics.values() 
                                   if metrics.error_count > 0)
                
                # 計算系統整體統計
                total_system_requests = sum(metrics.total_requests for metrics in self.performance_metrics.values())
                total_successful = sum(metrics.successful_requests for metrics in self.performance_metrics.values())
                system_success_rate = total_successful / total_system_requests if total_system_requests > 0 else 1.0
                
                # 計算平均響應時間
                avg_times = [metrics.average_processing_time for metrics in self.performance_metrics.values() 
                           if metrics.average_processing_time > 0]
                system_average_response_time = sum(avg_times) / len(avg_times) if avg_times else 0.0
                
                # 獲取系統資源使用（簡化實現）
                system_uptime = current_time - self.system_start_time
                
                # 創建快照
                snapshot = SystemPerformanceSnapshot(
                    timestamp=current_time,
                    total_modules=total_modules,
                    active_modules=active_modules,
                    failed_modules=failed_modules,
                    system_uptime=system_uptime,
                    module_metrics=self.performance_metrics.copy(),
                    total_system_requests=total_system_requests,
                    system_success_rate=system_success_rate,
                    system_average_response_time=system_average_response_time
                )
                
                # 添加到歷史記錄
                self.performance_history.append(snapshot)
                
                # 更新監控統計
                self.monitoring_stats["total_snapshots"] += 1
                self.monitoring_stats["last_snapshot_time"] = current_time
                
                debug_log(2, f"[CoreFramework] 效能快照已生成: {total_modules} 模組, {active_modules} 活躍")
                
                return snapshot
                
        except Exception as e:
            self.monitoring_stats["monitoring_errors"] += 1
            error_log(f"[CoreFramework] 生成效能快照失敗: {e}")
            # 返回空快照
            return SystemPerformanceSnapshot()
    
    def get_performance_history(self, count: int = 10) -> List[SystemPerformanceSnapshot]:
        """獲取效能歷史記錄"""
        with self.metrics_lock:
            return list(self.performance_history)[-count:]
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """獲取效能摘要 - 供調試使用"""
        with self.metrics_lock:
            current_time = time.time()
            
            summary = {
                "framework_status": {
                    "initialized": self.is_initialized,
                    "monitoring_enabled": self.performance_monitoring_enabled,
                    "uptime": current_time - self.system_start_time,
                    "total_modules": len(self.modules)
                },
                "monitoring_stats": self.monitoring_stats.copy(),
                "module_summary": {}
            }
            
            # 模組摘要
            for module_id, metrics in self.performance_metrics.items():
                summary["module_summary"][module_id] = {
                    "is_active": metrics.is_active,
                    "total_requests": metrics.total_requests,
                    "success_rate": metrics.success_rate,
                    "average_processing_time": metrics.average_processing_time,
                    "last_activity": current_time - metrics.last_activity
                }
            
            return summary
    
    def reset_performance_metrics(self):
        """重置所有效能指標"""
        with self.metrics_lock:
            self.performance_metrics.clear()
            self.performance_history.clear()
            self.monitoring_stats = {
                "total_snapshots": 0,
                "last_snapshot_time": 0,
                "monitoring_errors": 0
            }
            info_log("[CoreFramework] 效能指標已重置")
    
    # ========== SLEEP 狀態支援 ==========
    
    def _subscribe_sleep_events(self):
        """訂閱 SLEEP 相關事件"""
        try:
            from core.event_bus import event_bus, SystemEvent
            
            # 訂閱 SLEEP_ENTERED 事件（卸載模組）
            event_bus.subscribe(
                SystemEvent.SLEEP_ENTERED,
                self._on_sleep_entered,
                handler_name="framework_sleep_entry"
            )
            
            # 訂閱 SLEEP_EXITED 事件（開始重載協調）
            event_bus.subscribe(
                SystemEvent.SLEEP_EXITED,
                self._on_sleep_exited,
                handler_name="framework_wake"
            )
            
            debug_log(2, "[CoreFramework] 已訂閱 SLEEP_ENTERED/SLEEP_EXITED 事件")
            
        except Exception as e:
            error_log(f"[CoreFramework] 訂閱 SLEEP 事件失敗: {e}")
    
    def _on_sleep_entered(self, event):
        """處理 SLEEP_ENTERED 事件 - 卸載非關鍵模組"""
        try:
            sleep_reason = event.data.get('reason', 'unknown')
            debug_log(2, f"[CoreFramework] 收到 SLEEP_ENTERED 事件 (原因: {sleep_reason})")
            self._handle_sleep_entry()
        except Exception as e:
            error_log(f"[CoreFramework] 處理 SLEEP_ENTERED 事件失敗: {e}")
    
    def _handle_sleep_entry(self):
        """處理進入 SLEEP 狀態 - 卸載非關鍵模組"""
        try:
            info_log("[CoreFramework] 🌙 系統進入 SLEEP 狀態，開始卸載非關鍵模組...")
            
            # 定義非關鍵模組（可以卸載的模組）- 使用 module_id
            # UI 模組通常不卸載，因為前端需要顯示睡覺動畫和喚醒按鈕
            non_critical_module_ids = ["stt", "nlp", "llm", "mem", "tts", "sys"]
            
            # 建立 module_id 到 module_name 的映射
            id_to_name_map = {
                "stt": "stt_module",
                "nlp": "nlp_module",
                "llm": "llm_module",
                "mem": "mem_module",
                "tts": "tts_module",
                "sys": "sys_module"
            }
            
            # 使用 Framework 自己的模組註冊表檢查
            from core import registry
            
            unloaded_count = 0
            for module_id in non_critical_module_ids:
                module_name = id_to_name_map[module_id]  # 獲取完整模組名稱（帶 _module 後綴）
                
                # 檢查模組是否已載入（Framework 使用 module_id，Registry 使用 module_name）
                is_in_framework = module_id in self.modules
                is_in_registry = registry.is_loaded(module_name)
                debug_log(2, f"[CoreFramework] 檢查模組 {module_id} ({module_name}): framework={is_in_framework}, registry={is_in_registry}")
                
                if is_in_framework or is_in_registry:
                    # 特別處理 sys_module：先調用 shutdown 暫停監控任務
                    if module_id == "sys":
                        sys_module = self.get_module(module_id)
                        if sys_module and hasattr(sys_module, 'shutdown'):
                            try:
                                sys_module.shutdown()
                                info_log("[CoreFramework] ✅ sys_module 監控任務已暫停")
                            except Exception as e:
                                error_log(f"[CoreFramework] sys_module shutdown 失敗: {e}")
                    
                    debug_log(2, f"[CoreFramework] 開始卸載模組: {module_name}")
                    success = registry.unload_module(module_name)  # 使用完整模組名稱卸載
                    if success:
                        unloaded_count += 1
                        info_log(f"[CoreFramework] ✅ 已卸載模組: {module_name}")
                        # 同時從 Framework 註冊表移除（使用 module_id）
                        with self.modules_lock:
                            if module_id in self.modules:
                                del self.modules[module_id]
                    else:
                        error_log(f"[CoreFramework] ❌ 卸載模組失敗: {module_name}")
                else:
                    debug_log(2, f"[CoreFramework] 模組 {module_name} 未載入，跳過")
            
            info_log(f"[CoreFramework] ✅ 已卸載 {unloaded_count} 個非關鍵模組")
            
            # 強制垃圾回收，釋放模組佔用的記憶體（特別是 GPU 記憶體）
            import gc
            gc.collect()
            info_log("[CoreFramework] 🗑️ 垃圾回收完成")
            
        except Exception as e:
            error_log(f"[CoreFramework] 處理 SLEEP 進入失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
    
    def _on_sleep_exited(self, event):
        """處理 SLEEP_EXITED 事件 - 開始重載協調（但不恢復操作）"""
        try:
            wake_reason = event.data.get('wake_reason', 'unknown')
            info_log(f"[CoreFramework] 🔄 SLEEP 已退出（原因: {wake_reason}），開始準備模組重載...")
            
            # 標記系統正在重載中，前端此時應保持睡眠 UI
            # 實際重載由 wake_api 的 _reload_modules() 處理
            # Framework 只需準備好接收模組請求
            
            debug_log(2, "[CoreFramework] 模組將在需要時通過 registry 自動重載")
            debug_log(2, "[CoreFramework] 等待 WAKE_READY 事件後系統才會完全恢復")
            
        except Exception as e:
            error_log(f"[CoreFramework] 處理 SLEEP_EXITED 失敗: {e}")
    
    def _handle_sleep_exit(self):
        """處理退出 SLEEP 狀態（從 STATE_CHANGED 觸發）"""
        try:
            info_log("[CoreFramework] ⏰ 系統狀態從 SLEEP 變更")
            # STATE_CHANGED 的 sleep 退出主要用於狀態追蹤
            # 實際重載由 SLEEP_EXITED 事件處理
            
        except Exception as e:
            error_log(f"[CoreFramework] 處理 SLEEP 狀態退出失敗: {e}")


# 全局框架實例
core_framework = CoreFramework()