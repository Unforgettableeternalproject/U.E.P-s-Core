# core/status_manager.py
"""
StatusManager - 系統數值管理器

管理 U.E.P 的內部系統數值，包括：
- Mood: 情緒狀態 (-1 到 +1)
- Pride: 自尊心 (0 到 100)  
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
    pride: float = 50.0         # 自尊心：0 (沒有自尊) 到 100 (非常自尊)
    helpfulness: float = 0.8    # 助人意願：0 (不願意) 到 1 (非常願意)
    boredom: float = 0.0        # 無聊程度：0 (不無聊) 到 1 (非常無聊)
    
    # 統計數據
    total_interactions: int = 0
    successful_tasks: int = 0
    failed_tasks: int = 0
    last_interaction_time: float = 0.0
    
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
        self.pride = max(0.0, min(100.0, self.pride))
        self.helpfulness = max(0.0, min(1.0, self.helpfulness))
        self.boredom = max(0.0, min(1.0, self.boredom))


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
        self.status = SystemStatus()
        self.storage_path = Path("memory/system_status.json")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 更新回調
        self.update_callbacks: Dict[str, Callable] = {}
        
        # 自動保存設定
        self.auto_save = True
        self.save_interval = 60.0  # 60秒自動保存一次
        self._last_save_time = 0.0
        
        # 載入現有狀態
        self._load_status()
        
        info_log("[StatusManager] 系統狀態管理器初始化完成")
    
    def register_update_callback(self, name: str, callback: Callable):
        """註冊狀態更新回調"""
        self.update_callbacks[name] = callback
        debug_log(2, f"[StatusManager] 註冊更新回調: {name}")
    
    def unregister_update_callback(self, name: str):
        """取消註冊狀態更新回調"""
        if name in self.update_callbacks:
            del self.update_callbacks[name]
            debug_log(2, f"[StatusManager] 取消註冊回調: {name}")
    
    def get_status(self) -> SystemStatus:
        """獲取當前系統狀態"""
        return self.status
    
    def get_status_dict(self) -> Dict[str, Any]:
        """獲取當前系統狀態的字典格式"""
        return self.status.to_dict()
    
    def update_mood(self, delta: float, reason: str = ""):
        """更新情緒狀態"""
        old_mood = self.status.mood
        self.status.mood += delta
        self.status.validate_ranges()
        
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
            mood_boost = min(0.1, delta * 0.01)  # 最多提升 0.1 mood
            self.status.mood += mood_boost
        elif delta < 0 and self.status.pride < 30:  # 自尊降低且過低時
            mood_penalty = max(-0.05, delta * 0.005)
            helpfulness_penalty = max(-0.05, delta * 0.002)
            self.status.mood += mood_penalty
            self.status.helpfulness += helpfulness_penalty
        
        self.status.validate_ranges()
        
        debug_log(2, f"[StatusManager] 自尊更新: {old_pride:.1f} -> {self.status.pride:.1f} "
                    f"(變化: {delta:+.1f}) 原因: {reason}")
        
        self._trigger_callbacks("pride", old_pride, self.status.pride, reason)
        self._auto_save()
    
    def update_helpfulness(self, delta: float, reason: str = ""):
        """更新助人意願"""
        old_helpfulness = self.status.helpfulness
        self.status.helpfulness += delta
        self.status.validate_ranges()
        
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
            pride_penalty = -0.5
            self.status.mood += mood_penalty
            self.status.pride += pride_penalty
            self.status.validate_ranges()
        
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
    
    def record_interaction(self, successful: bool = True, task_type: str = "general"):
        """記錄互動"""
        self.status.total_interactions += 1
        self.status.last_interaction_time = time.time()
        
        if successful:
            self.status.successful_tasks += 1
            # 成功的互動提升各項數值
            self.update_pride(1.0, f"成功完成 {task_type}")
            self.update_helpfulness(0.01, f"成功幫助用戶 - {task_type}")
            self.update_mood(0.05, f"成功互動 - {task_type}")
        else:
            self.status.failed_tasks += 1
            # 失敗的互動降低數值
            self.update_pride(-2.0, f"任務失敗 - {task_type}")
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
        if self.status.pride >= 80:
            return "非常自信"
        elif self.status.pride >= 60:
            return "自信"
        elif self.status.pride >= 40:
            return "普通"
        elif self.status.pride >= 20:
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
        """手動保存狀態"""
        try:
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump(self.status.to_dict(), f, ensure_ascii=False, indent=2)
            debug_log(3, f"[StatusManager] 狀態已保存到 {self.storage_path}")
        except Exception as e:
            error_log(f"[StatusManager] 保存狀態失敗: {e}")
    
    def _load_status(self):
        """載入狀態"""
        try:
            if self.storage_path.exists():
                with open(self.storage_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.status = SystemStatus.from_dict(data)
                self.status.validate_ranges()
                info_log(f"[StatusManager] 狀態已從 {self.storage_path} 載入")
            else:
                info_log("[StatusManager] 使用預設狀態")
        except Exception as e:
            error_log(f"[StatusManager] 載入狀態失敗: {e}，使用預設狀態")
            self.status = SystemStatus()
    
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
            f"自尊: {modifiers['pride_level']} ({self.status.pride:.1f}), "
            f"助人意願: {modifiers['helpfulness_level']} ({self.status.helpfulness:.2f}), "
            f"無聊程度: {modifiers['boredom_level']} ({self.status.boredom:.2f})"
        )


# 全局實例
status_manager = StatusManager()