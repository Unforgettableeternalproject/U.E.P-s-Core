# core/status_manager.py
"""
StatusManager - 系統數值管理器

管理 U.E.P 的內部系統數值，包括：
- Mood: 情緒狀態 (-1 到 +1)
- Pride: 自尊心 (-1 到 +1)  
- Helpfulness: 助人意願 (0 到 1)
- Boredom: 無聊程度 (0 到 1)

這些數值會影響 U.E.P 的回應風格、TTS 語氣和行為模式。
"""

import json
import time
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass, asdict
from utils.debug_helper import debug_log, info_log, error_log


@dataclass
class SystemStatus:
    """系統狀態數值"""
    mood: float = 0.0           # 情緒狀態：-1 (負面) 到 +1 (正面)
    pride: float = 0.0          # 自尊心：-1 (自卑) 到 +1 (自信)
    helpfulness: float = 0.8    # 助人意願：0 (不願意) 到 1 (非常願意)
    boredom: float = 0.0        # 無聊程度：0 (不無聊) 到 1 (非常無聊)
    
    # 統計數據
    total_interactions: int = 0
    successful_tasks: int = 0
    failed_tasks: int = 0
    last_interaction_time: float = 0.0  # 將在 __post_init__ 中初始化
    last_update_reason: str = ""
    
    def __post_init__(self):
        """初始化後處理：設定 last_interaction_time 預設值"""
        import time
        # 只有當 last_interaction_time 為 0.0 時才設定為當前時間
        if self.last_interaction_time == 0.0:
            self.last_interaction_time = time.time()
    
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SystemStatus':
        """從字典創建實例"""
        return cls(**data)
    
    def validate_ranges(self):
        """驗證數值範圍並修正"""
        self.mood = max(-1.0, min(1.0, self.mood))
        self.pride = max(-1.0, min(1.0, self.pride))
        self.helpfulness = max(0.0, min(1.0, self.helpfulness))
        self.boredom = max(0.0, min(1.0, self.boredom))
    
    def get(self, key: str, default=None):
        """獲取狀態屬性值（類似字典的 get 方法）"""
        return getattr(self, key, default)


class StatusManager:
    """系統狀態管理器 - 全局單例"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
            
        self._initialized = True
        
        # 🆕 Identity-aware 狀態管理
        self.status_by_identity: Dict[str, SystemStatus] = {}  # identity_id -> SystemStatus
        self.current_identity_id: Optional[str] = None
        self.status = SystemStatus()  # 向後兼容的 fallback（無 Identity 時使用）
        
        # 存儲路徑
        self.storage_path = Path("memory/system_status.json")  # 舊格式，向後兼容
        self.identity_storage_dir = Path("memory/identities")  # 🆕 每個 Identity 獨立文件
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.identity_storage_dir.mkdir(parents=True, exist_ok=True)
        
        # 特殊狀態覆寫（現在是 Identity-aware）
        self._helpfulness_override: Dict[str, Optional[float]] = {}  # identity_id -> override_value
        
        # 更新回調
        self.update_callbacks: Dict[str, Callable] = {}
        
        # 自動保存設定
        self.auto_save = True
        self.save_interval = 60.0  # 60秒自動保存一次
        self._last_save_time = 0.0
        
        # 載入現有狀態
        self._load_status()
        
        info_log("[StatusManager] 系統狀態管理器初始化完成（Identity-aware）")
    
    def switch_identity(self, identity_id: str):
        """切換到指定 Identity 的系統狀態
        
        Args:
            identity_id: 要切換到的 Identity ID
        """
        # 保存當前 Identity 的狀態
        if self.current_identity_id:
            self.status_by_identity[self.current_identity_id] = self.status
            self._save_identity_status(self.current_identity_id)
        
        # 切換到新 Identity
        self.current_identity_id = identity_id
        
        # 載入新 Identity 的狀態（如果不存在則創建）
        if identity_id not in self.status_by_identity:
            self.status_by_identity[identity_id] = SystemStatus()
            info_log(f"[StatusManager] 為 Identity {identity_id} 創建新的系統狀態")
        
        self.status = self.status_by_identity[identity_id]
        
        # 確保 last_interaction_time 已初始化（避免計算出從 1970 年至今的時間）
        if self.status.last_interaction_time == 0.0:
            import time
            self.status.last_interaction_time = time.time()
            debug_log(2, f"[StatusManager] 初始化 Identity {identity_id} 的 last_interaction_time")
        
        info_log(f"[StatusManager] 切換到 Identity: {identity_id}")
        debug_log(2, f"[StatusManager] 當前狀態: {self.get_summary()}")
    
    def get_current_identity(self) -> Optional[str]:
        """獲取當前 Identity ID"""
        return self.current_identity_id
    
    def clear_identity(self):
        """清除當前 Identity（回到 fallback 狀態）"""
        if self.current_identity_id:
            # 保存當前狀態
            self.status_by_identity[self.current_identity_id] = self.status
            self._save_identity_status(self.current_identity_id)
            
            info_log(f"[StatusManager] 清除 Identity: {self.current_identity_id}")
            self.current_identity_id = None
            self.status = SystemStatus()  # 回到默認狀態
    
    def register_update_callback(self, name: str, callback: Callable):
        """註冊狀態更新回調"""
        self.update_callbacks[name] = callback
        debug_log(2, f"[StatusManager] 註冊更新回調: {name}")
    
    def unregister_update_callback(self, name: str):
        """取消註冊狀態更新回調"""
        if name in self.update_callbacks:
            del self.update_callbacks[name]
            debug_log(2, f"[StatusManager] 取消註冊回調: {name}")
    
    def get_status(self, identity_id: Optional[str] = None) -> SystemStatus:
        """獲取系統狀態
        
        Args:
            identity_id: 指定 Identity ID，如果為 None 則返回當前 Identity 的狀態
        
        Returns:
            SystemStatus: 對應 Identity 的系統狀態
        """
        if identity_id:
            return self.status_by_identity.get(identity_id, SystemStatus())
        return self.status
    
    def get_status_dict(self) -> Dict[str, Any]:
        """獲取當前系統狀態的字典格式"""
        d = {
            "mood": self.status.mood,
            "pride": self.status.pride,
            "helpfulness": self.status.helpfulness,  # 自然值（0~1）
            "boredom": self.status.boredom,
            "last_update_reason": getattr(self.status, "last_update_reason", None),
        }
        # 新增有效值與當前覆蓋狀態
        d["helpfulness_effective"] = self.get_effective_helpfulness()
        d["helpfulness_overridden"] = (self._helpfulness_override is not None)
        return d
    
    def update_mood(self, delta: float, reason: str = ""):
        """更新情緒狀態"""
        old_mood = self.status.mood
        self.status.mood += delta
        self.status.validate_ranges()
        self.status.last_update_reason = reason
        
        debug_log(2, f"[StatusManager] 情緒更新: {old_mood:.2f} -> {self.status.mood:.2f} "
                    f"(變化: {delta:+.2f}) 原因: {reason}")
        
        self._trigger_callbacks("mood", old_mood, self.status.mood, reason)
        self._auto_save()
    
    def update_pride(self, delta: float, reason: str = ""):
        """更新自尊心"""
        old_pride = self.status.pride
        self.status.pride += delta
        self.status.validate_ranges()
        
        # Pride 會影響 Mood 和 Helpfulness
        if delta > 0:  # 自尊提升時
            mood_boost = min(0.1, delta * 0.1)  # 調整係數適應新範圍
            self.status.mood += mood_boost
        elif delta < 0 and self.status.pride < -0.5:  # 自尊降低且過低時 (改為 -0.5)
            mood_penalty = max(-0.05, delta * 0.5)
            helpfulness_penalty = max(-0.05, delta * 0.2)
            self.status.mood += mood_penalty
            self.status.helpfulness += helpfulness_penalty
        
        self.status.validate_ranges()
        self.status.last_update_reason = reason
        
        debug_log(2, f"[StatusManager] 自尊更新: {old_pride:.2f} -> {self.status.pride:.2f} "
                    f"(變化: {delta:+.2f}) 原因: {reason}")
        
        self._trigger_callbacks("pride", old_pride, self.status.pride, reason)
        self._auto_save()
    
    def update_helpfulness(self, delta: float, reason: str = ""):
        """更新助人意願"""
        old_helpfulness = self.status.helpfulness
        self.status.helpfulness = max(0.0, min(1.0, self.status.helpfulness + float(delta)))
        self.status.validate_ranges()
        self.status.last_update_reason = reason
        
        debug_log(2, f"[StatusManager] 助人意願更新: {old_helpfulness:.2f} -> {self.status.helpfulness:.2f} "
                    f"(變化: {delta:+.2f}) 原因: {reason}")
        
        self._trigger_callbacks("helpfulness", old_helpfulness, self.status.helpfulness, reason)
        self._auto_save()
    
    def update_boredom(self, delta: float, reason: str = ""):
        """更新無聊程度"""
        old_boredom = self.status.boredom
        self.status.boredom += delta
        self.status.validate_ranges()
        
        # Boredom 會輕微影響 Mood 和 Pride
        if delta > 0.5:  # 非常無聊時
            mood_penalty = -0.02
            pride_penalty = -0.05  # 調整為適合 -1 到 +1 範圍
            self.status.mood += mood_penalty
            self.status.pride += pride_penalty
            self.status.validate_ranges()
            
        self.status.last_update_reason = reason
        
        debug_log(2, f"[StatusManager] 無聊程度更新: {old_boredom:.2f} -> {self.status.boredom:.2f} "
                    f"(變化: {delta:+.2f}) 原因: {reason}")
        
        self._trigger_callbacks("boredom", old_boredom, self.status.boredom, reason)
        self._auto_save()
    
    def reset_boredom(self, reason: str = "用戶互動"):
        """重置無聊程度（有用戶互動時）"""
        if self.status.boredom > 0:
            self.status.boredom = 0.0
            debug_log(3, f"[StatusManager] 無聊程度重置，原因: {reason}")
            self._trigger_callbacks("boredom", None, 0.0, reason)
            self._auto_save()

    def apply_session_penalties(self, session_type: str = "general") -> Dict[str, float]:
        """
        系統自動調整 - 每次創建 General Session 時的微調
        這不是給 LLM 處理的，而是 system loop 每次創建 GS 時的自動微調
        
        Args:
            session_type: 會話類型，影響 penalty 的計算方式
            
        Returns:
            Dict[str, float]: 各項數值的變化量
        """
        penalties = {}
        current_time = time.time()
        
        # 計算距離上次互動的時間（小時）
        if self.status.last_interaction_time > 0:
            hours_since_last = (current_time - self.status.last_interaction_time) / 3600
        else:
            hours_since_last = 0
        
        # 時間相關的 Boredom 增長
        if hours_since_last > 0.5:  # 超過30分鐘沒有互動
            boredom_increase = min(0.1, hours_since_last * 0.02)  # 每小時增加 0.02，最多 0.1
            self.update_boredom(boredom_increase, f"時間流逝 ({hours_since_last:.1f}小時)")
            penalties['boredom'] = boredom_increase
        
        # Boredom 對其他數值的影響
        if self.status.boredom > 0.7:  # 非常無聊時
            mood_penalty = -0.01
            pride_penalty = -0.005
            self.update_mood(mood_penalty, "長時間無互動導致情緒低落")
            self.update_pride(pride_penalty, "缺乏成就感")
            penalties['mood'] = mood_penalty
            penalties['pride'] = pride_penalty
        
        # 數值自然回歸 - 極端數值會緩慢回歸中性
        if abs(self.status.mood) > 0.8:  # 情緒過於極端
            regression = -0.005 if self.status.mood > 0 else 0.005
            self.update_mood(regression, "情緒自然回歸")
            penalties['mood'] = penalties.get('mood', 0) + regression
        
        if abs(self.status.pride) > 0.8:  # 自尊過於極端
            regression = -0.003 if self.status.pride > 0 else 0.003
            self.update_pride(regression, "自尊自然回歸")
            penalties['pride'] = penalties.get('pride', 0) + regression
        
        if self.status.helpfulness > 0.95:  # 助人意願過高時稍微降低
            regression = -0.005
            self.update_helpfulness(regression, "助人意願自然調整")
            penalties['helpfulness'] = regression
        
        if penalties:
            debug_log(2, f"[StatusManager] 會話 penalty 已應用: {penalties}")
            
        return penalties

    def record_interaction(self, successful: bool = True, task_type: str = "general"):
        """記錄互動"""
        self.status.total_interactions += 1
        self.status.last_interaction_time = time.time()
        
        if successful:
            self.status.successful_tasks += 1
            # 成功的互動提升各項數值
            self.update_pride(0.1, f"成功完成 {task_type}")  # 調整為適合 -1 到 +1 範圍
            self.update_helpfulness(0.01, f"成功幫助用戶 - {task_type}")
            self.update_mood(0.05, f"成功互動 - {task_type}")
        else:
            self.status.failed_tasks += 1
            # 失敗的互動降低數值
            self.update_pride(-0.2, f"任務失敗 - {task_type}")  # 調整為適合 -1 到 +1 範圍
            self.update_mood(-0.02, f"任務失敗 - {task_type}")
        
        # 重置無聊程度
        self.reset_boredom("用戶互動")
        
        debug_log(2, f"[StatusManager] 記錄互動: {'成功' if successful else '失敗'} - {task_type}")
    
    def get_personality_modifiers(self) -> Dict[str, Any]:
        """獲取個性修飾符供 LLM 使用"""
        return {
            "mood_level": self._get_mood_level(),
            "pride_level": self._get_pride_level(), 
            "helpfulness_level": self._get_helpfulness_level(),
            "boredom_level": self._get_boredom_level(),
            "mood_numeric": self.status.mood,
            "pride_numeric": self.status.pride,
            "helpfulness_numeric": self.status.helpfulness,
            "boredom_numeric": self.status.boredom,
            "interaction_stats": {
                "total": self.status.total_interactions,
                "success_rate": self._get_success_rate(),
                "last_interaction": self._get_time_since_last_interaction()
            }
        }
    
    def _get_mood_level(self) -> str:
        """獲取情緒等級描述"""
        if self.status.mood >= 0.6:
            return "非常積極"
        elif self.status.mood >= 0.2:
            return "積極"
        elif self.status.mood >= -0.2:
            return "中性"
        elif self.status.mood >= -0.6:
            return "消極"
        else:
            return "非常消極"
    
    def _get_pride_level(self) -> str:
        """獲取自尊心等級描述"""
        if self.status.pride >= 0.6:
            return "非常自信"
        elif self.status.pride >= 0.2:
            return "自信"
        elif self.status.pride >= -0.2:
            return "普通"
        elif self.status.pride >= -0.6:
            return "缺乏自信"
        else:
            return "非常沒自信"
    
    def _get_helpfulness_level(self) -> str:
        """獲取助人意願等級描述"""
        if self.status.helpfulness >= 0.8:
            return "非常願意幫助"
        elif self.status.helpfulness >= 0.6:
            return "樂於幫助"
        elif self.status.helpfulness >= 0.4:
            return "普通意願"
        elif self.status.helpfulness >= 0.2:
            return "不太願意"
        else:
            return "不願意幫助"
    
    def _get_boredom_level(self) -> str:
        """獲取無聊等級描述"""
        if self.status.boredom >= 0.8:
            return "非常無聊"
        elif self.status.boredom >= 0.6:
            return "有些無聊"
        elif self.status.boredom >= 0.4:
            return "輕微無聊"
        elif self.status.boredom >= 0.2:
            return "稍微無聊"
        else:
            return "不無聊"
    
    def _get_success_rate(self) -> float:
        """獲取成功率"""
        if self.status.total_interactions == 0:
            return 0.0
        return self.status.successful_tasks / self.status.total_interactions
    
    def _get_time_since_last_interaction(self) -> str:
        """獲取距離上次互動的時間"""
        if self.status.last_interaction_time == 0:
            return "從未互動"
        
        elapsed = time.time() - self.status.last_interaction_time
        if elapsed < 60:
            return f"{int(elapsed)} 秒前"
        elif elapsed < 3600:
            return f"{int(elapsed / 60)} 分鐘前"
        else:
            return f"{int(elapsed / 3600)} 小時前"
    
    def _trigger_callbacks(self, field: str, old_value: Any, new_value: Any, reason: str):
        """觸發更新回調"""
        for name, callback in self.update_callbacks.items():
            try:
                callback(field, old_value, new_value, reason)
            except Exception as e:
                error_log(f"[StatusManager] 回調 {name} 執行失敗: {e}")
    
    def _auto_save(self):
        """自動保存狀態"""
        if self.auto_save:
            current_time = time.time()
            if current_time - self._last_save_time > self.save_interval:
                self.save_status()
                self._last_save_time = current_time
    
    def save_status(self):
        """手動保存狀態（向後兼容 + Identity-aware）"""
        try:
            # 保存當前 Identity 的狀態
            if self.current_identity_id:
                self._save_identity_status(self.current_identity_id)
            
            # 向後兼容：保存 fallback 狀態到舊路徑
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump(self.status.to_dict(), f, ensure_ascii=False, indent=2)
            debug_log(3, f"[StatusManager] 狀態已保存到 {self.storage_path}")
        except Exception as e:
            error_log(f"[StatusManager] 保存狀態失敗: {e}")
    
    def _save_identity_status(self, identity_id: str):
        """保存指定 Identity 的狀態到獨立文件"""
        try:
            status = self.status_by_identity.get(identity_id)
            if not status:
                return
            
            identity_file = self.identity_storage_dir / f"{identity_id}_status.json"
            with open(identity_file, 'w', encoding='utf-8') as f:
                json.dump(status.to_dict(), f, ensure_ascii=False, indent=2)
            debug_log(3, f"[StatusManager] Identity {identity_id} 狀態已保存")
        except Exception as e:
            error_log(f"[StatusManager] 保存 Identity {identity_id} 狀態失敗: {e}")
    
    def _load_status(self):
        """載入狀態（向後兼容 + Identity-aware）"""
        try:
            # 載入舊格式的 fallback 狀態（向後兼容）
            if self.storage_path.exists():
                with open(self.storage_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 檢查並遷移舊的 Pride 範圍 (0-100 -> -1 到 +1)
                if 'pride' in data and data['pride'] > 1.0:
                    old_pride = data['pride']
                    data['pride'] = (old_pride - 50.0) / 50.0
                    info_log(f"[StatusManager] Pride 範圍遷移: {old_pride} -> {data['pride']:.2f}")
                
                self.status = SystemStatus.from_dict(data)
                self.status.validate_ranges()
                info_log(f"[StatusManager] Fallback 狀態已從 {self.storage_path} 載入")
            else:
                info_log("[StatusManager] 使用預設 fallback 狀態")
            
            # 🆕 載入所有 Identity 的狀態
            self._load_all_identity_statuses()
            
        except Exception as e:
            error_log(f"[StatusManager] 載入狀態失敗: {e}，使用預設狀態")
            self.status = SystemStatus()
    
    def _load_all_identity_statuses(self):
        """載入所有 Identity 的狀態文件"""
        try:
            if not self.identity_storage_dir.exists():
                return
            
            loaded_count = 0
            for status_file in self.identity_storage_dir.glob("*_status.json"):
                try:
                    identity_id = status_file.stem.replace("_status", "")
                    
                    with open(status_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    status = SystemStatus.from_dict(data)
                    status.validate_ranges()
                    self.status_by_identity[identity_id] = status
                    loaded_count += 1
                    debug_log(3, f"[StatusManager] 載入 Identity {identity_id} 的狀態")
                    
                except Exception as e:
                    error_log(f"[StatusManager] 載入 {status_file} 失敗: {e}")
            
            if loaded_count > 0:
                info_log(f"[StatusManager] 已載入 {loaded_count} 個 Identity 的狀態")
                
        except Exception as e:
            error_log(f"[StatusManager] 載入 Identity 狀態失敗: {e}")
    
    def reset_status(self):
        """重置狀態到預設值"""
        self.status = SystemStatus()
        self.save_status()
        info_log("[StatusManager] 系統狀態已重置")
    
    def get_summary(self) -> str:
        """獲取狀態摘要"""
        modifiers = self.get_personality_modifiers()
        return (
            f"情緒: {modifiers['mood_level']} ({self.status.mood:+.2f}), "
            f"自尊: {modifiers['pride_level']} ({self.status.pride:+.2f}), "
            f"助人意願: {modifiers['helpfulness_level']} ({self.status.helpfulness:.2f}), "
            f"無聊程度: {modifiers['boredom_level']} ({self.status.boredom:.2f})"
        )
        
    def get_effective_helpfulness(self) -> float:
        """回傳『有效的』助人意願。若有覆蓋值（例如 Mischief），回傳覆蓋值；否則回自然值。"""
        if self.current_identity_id:
            override = self._helpfulness_override.get(self.current_identity_id)
            if override is not None:
                return float(override)
        return float(self.status.helpfulness)
    
    def suppress_helpfulness(self, reason: str = "system_override"):
        """將助人意願以覆蓋值 -1 強制關閉（不影響自然值），適用於 Mischief 等態。"""
        if self.current_identity_id:
            self._helpfulness_override[self.current_identity_id] = -1.0
        self.status.last_update_reason = reason

    def clear_helpfulness_override(self, reason: str = "system_restore"):
        """解除覆蓋，恢復使用自然值（0~1）。"""
        if self.current_identity_id and self.current_identity_id in self._helpfulness_override:
            del self._helpfulness_override[self.current_identity_id]
        self.status.last_update_reason = reason


# 全局實例
status_manager = StatusManager()