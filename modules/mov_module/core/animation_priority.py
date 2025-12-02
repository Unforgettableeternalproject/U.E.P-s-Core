"""
動畫優先度管理系統

處理多個來源同時請求動畫播放的衝突
只保留最高優先度的動畫請求，其餘直接忽略
"""

from enum import IntEnum
from typing import Optional, Dict, Any
from dataclasses import dataclass
import time

from utils.debug_helper import debug_log


class AnimationPriority(IntEnum):
    """
    動畫優先度等級（數字越大優先度越高）
    
    優先度規則：
    - 系統循環（SYSTEM_CYCLE）和用戶互動（拖曳、投擲）優先度最高
    - Tease 和特殊動作次之
    - Idle 和滑鼠追蹤優先度最低
    """
    # 最低優先度：背景行為
    CURSOR_TRACKING = 10      # 滑鼠追蹤靜態幀
    EASTER_EGG = 25           # 彩蛋動畫（打哈欠、dance 等）
    
    # 中等優先度：特殊狀態（IDLE 和 MOVEMENT 同級，可互相打斷）
    IDLE_ANIMATION = 30       # 一般 idle 動畫
    MOVEMENT = 30             # 移動動畫（follow, turn）
    SPECIAL_MOVE = 35         # 特殊移動（smile, curious, excited）
    TRANSITION = 40           # 轉場動畫（ground ↔ float）
    TEASE = 45                # Tease 動畫
    
    # 高優先度：系統和用戶互動
    SYSTEM_CYCLE = 50         # 系統循環（input/processing/output）
    USER_INTERACTION = 60     # 用戶互動（拖曳 struggle、投擲 throw）
    
    # 最高優先度：強制動畫
    FORCE_OVERRIDE = 100      # 強制覆蓋（immediate_interrupt=True）


@dataclass
class AnimationRequest:
    """動畫請求"""
    name: str                          # 動畫名稱
    priority: AnimationPriority        # 優先度
    params: Dict[str, Any]             # 參數
    source: str                        # 來源（用於 debug）
    timestamp: float                   # 請求時間
    allow_interrupt: bool = True       # 是否允許被同級/低級動畫打斷（預設允許）
    
    def __repr__(self):
        return f"AnimationRequest({self.name}, priority={self.priority.name}, source={self.source})"


class AnimationPriorityManager:
    """
    動畫優先度管理器
    
    職責：
    1. 接收動畫請求並檢查優先度
    2. 只允許高於或等於當前優先度的請求通過
    3. 追蹤當前正在播放的動畫及其優先度
    4. 提供優先度查詢和重置機制
    5. 支援從配置文件讀取來源預設優先度映射
    """
    
    def __init__(self, module_id: str = "MOV", config: Optional[Dict[str, Any]] = None):
        self.module_id = module_id
        self.current_request: Optional[AnimationRequest] = None
        self._lock_until: float = 0.0  # 優先度鎖定時間
        
        # 從配置讀取設定
        self.config = config or {}
        priority_config = self.config.get("animation_priority", {})
        self.enabled = priority_config.get("enabled", True)
        
        # 讀取來源優先度映射
        self.source_defaults: Dict[str, AnimationPriority] = {}
        source_defaults_config = priority_config.get("source_defaults", {})
        for source, priority_name in source_defaults_config.items():
            try:
                # 將字串轉換為 AnimationPriority 枚舉
                priority = AnimationPriority[priority_name]
                self.source_defaults[source] = priority
            except (KeyError, ValueError):
                debug_log(1, f"[{module_id}] 無效的優先度名稱: {priority_name} for source {source}")
        
        # 鎖定設定
        locking_config = priority_config.get("locking", {})
        self.lock_during_playback = locking_config.get("lock_during_playback", True)
        self.default_lock_duration = float(locking_config.get("default_lock_duration", 2.0))
        
        debug_log(2, f"[{module_id}] AnimationPriorityManager 初始化: enabled={self.enabled}, "
                     f"source_defaults={len(self.source_defaults)} 項")
    
    def get_default_priority(self, source: str) -> Optional[AnimationPriority]:
        """
        根據來源取得預設優先度
        
        Args:
            source: 來源名稱（例如 "idle_behavior", "system_cycle_behavior"）
        
        Returns:
            對應的優先度，如果沒有映射則返回 None
        """
        return self.source_defaults.get(source)
        
    def request_animation(
        self,
        name: str,
        priority: Optional[AnimationPriority],
        source: str,
        params: Optional[Dict[str, Any]] = None,
        lock_duration: float = 0.0,
    ) -> bool:
        """
        請求播放動畫
        
        Args:
            name: 動畫名稱
            priority: 優先度等級（如果為 None，會嘗試從 source_defaults 查找）
            source: 請求來源（用於 debug）
            params: 動畫參數
            lock_duration: 鎖定時間（秒），在此期間拒絕低優先度請求
        
        Returns:
            True 如果請求被接受，False 如果被拒絕
        """
        # 如果優先度管理系統未啟用，直接接受所有請求
        if not self.enabled:
            return True
        
        params = params or {}
        now = time.time()
        
        # 如果沒有指定優先度，嘗試從來源預設映射中查找
        if priority is None:
            priority = self.get_default_priority(source)
            if priority is None:
                # 沒有找到預設優先度，使用 IDLE_ANIMATION 作為回退
                priority = AnimationPriority.IDLE_ANIMATION
                debug_log(3, f"[{self.module_id}] 來源 '{source}' 沒有預設優先度，使用 IDLE_ANIMATION")
        
        # 檢查是否有 force_override 或 immediate_interrupt
        force_override = params.get("immediate_interrupt", False)
        if force_override:
            priority = AnimationPriority.FORCE_OVERRIDE
        
        # 建立請求
        allow_interrupt = params.get("allow_interrupt", True)  # 預設允許打斷
        request = AnimationRequest(
            name=name,
            priority=priority,
            params=params,
            source=source,
            timestamp=now,
            allow_interrupt=allow_interrupt,
        )
        
        # 檢查是否處於優先度鎖定期間
        if now < self._lock_until and self.current_request:
            if priority < self.current_request.priority:
                debug_log(3, 
                    f"[{self.module_id}] 動畫請求被拒絕（優先度鎖定）: "
                    f"{request} < {self.current_request}"
                )
                return False
        
        # 檢查優先度
        if self.current_request is not None:
            if priority < self.current_request.priority:
                debug_log(3, 
                    f"[{self.module_id}] 動畫請求被拒絕（優先度不足）: "
                    f"{request} < {self.current_request}"
                )
                return False
            elif priority == self.current_request.priority:
                # 相同優先度：檢查是否允許打斷
                if not self.current_request.allow_interrupt:
                    debug_log(3, 
                        f"[{self.module_id}] 動畫請求被拒絕（當前動畫不允許打斷）: "
                        f"{request} (current: {self.current_request})"
                    )
                    return False
                # 允許打斷，但如果是相同動畫，還是要檢查 force_restart
                if name == self.current_request.name:
                    force_restart = params.get("force_restart", False)
                    if not force_restart:
                        debug_log(3, 
                            f"[{self.module_id}] 動畫請求被拒絕（重複動畫）: {name}"
                        )
                        return False
        
        # 接受請求
        debug_log(2, 
            f"[{self.module_id}] 動畫請求被接受: {request} "
            f"(替換: {self.current_request.name if self.current_request else 'None'})"
        )
        
        self.current_request = request
        
        # 設定優先度鎖定
        if lock_duration > 0:
            self._lock_until = now + lock_duration
            debug_log(3, f"[{self.module_id}] 優先度鎖定 {lock_duration}s")
        
        return True
    
    def on_animation_finished(self, name: str):
        """
        通知動畫播放完成
        
        清除當前請求，允許低優先度動畫播放
        """
        if self.current_request and self.current_request.name == name:
            debug_log(3, f"[{self.module_id}] 動畫完成，清除優先度: {name}")
            self.current_request = None
            self._lock_until = 0.0
    
    def reset(self):
        """重置優先度管理器"""
        debug_log(2, f"[{self.module_id}] 重置動畫優先度管理器")
        self.current_request = None
        self._lock_until = 0.0
    
    def get_current_priority(self) -> Optional[AnimationPriority]:
        """取得當前動畫優先度"""
        return self.current_request.priority if self.current_request else None
    
    def is_locked(self) -> bool:
        """檢查是否處於優先度鎖定狀態"""
        return time.time() < self._lock_until
    
    def force_set_priority(
        self,
        name: str,
        priority: AnimationPriority,
        source: str,
        lock_duration: float = 0.0,
    ):
        """
        強制設定當前動畫優先度（不檢查）
        
        用於外部強制設定動畫狀態（例如系統循環開始）
        """
        now = time.time()
        self.current_request = AnimationRequest(
            name=name,
            priority=priority,
            params={},
            source=source,
            timestamp=now,
        )
        if lock_duration > 0:
            self._lock_until = now + lock_duration
        
        debug_log(2, 
            f"[{self.module_id}] 強制設定動畫優先度: {self.current_request} "
            f"(鎖定: {lock_duration}s)"
        )
