# configs/user_settings_manager.py
"""
使用者設定管理器
負責載入、保存和熱重載使用者設定
"""

import os
import yaml
from typing import Dict, Any, Optional, Set, Callable
from datetime import datetime
from pathlib import Path

from utils.debug_helper import debug_log, info_log, error_log, OPERATION_LEVEL


class UserSettingsManager:
    """使用者設定管理器 - 單例模式"""
    
    _instance = None
    
    # 定義需要重載的設定項目 (設定路徑 -> 需要重載的模組)
    # 注意：只有會影響模組運行時行為的設定才需要 reload
    RELOAD_REQUIRED = {
        # 身分設定 - NLP (身分管理)
        "general.identity.user_name": ["nlp_module"],
        "general.identity.uep_nickname": ["nlp_module", "llm_module"],
        
        # 互動設定 - 輸入模式 (SystemLoop 運行時切換)
        "interaction.speech_input.enabled": ["system_loop"],
        
        # 互動設定 - STT (運行時行為)
        "interaction.speech_input.microphone_device_index": ["stt_module"],
        "interaction.speech_input.vad_sensitivity": ["stt_module"],
        
        # 互動設定 - TTS (運行時行為)
        "interaction.speech_output.volume": ["tts_module"],
        "interaction.speech_output.speed": ["tts_module"],
        "interaction.speech_output.default_emotion": ["tts_module"],
        "interaction.speech_output.emotion_intensity": ["tts_module"],
        
        # 互動設定 - 對話與記憶
        "interaction.conversation.temperature": ["llm_module"],
        "interaction.conversation.enable_learning": ["llm_module"],
        "interaction.conversation.user_additional_prompt": ["llm_module"],
        "interaction.memory.enabled": ["mem_module"],
        
        # 行為設定 - MOV (運行時物理行為)
        "behavior.movement.boundary_mode": ["mov_module"],
        "behavior.movement.enable_throw_behavior": ["mov_module"],
        "behavior.movement.max_throw_speed": ["mov_module"],
        "behavior.movement.enable_cursor_tracking": ["mov_module"],
        "behavior.movement.movement_smoothing": ["mov_module"],
        "behavior.movement.ground_friction": ["mov_module"],
        
        # 行為設定 - 權限 (SYS 模組)
        "behavior.permissions.allow_system_commands": ["sys_module"],
        "behavior.permissions.allow_file_creation": ["sys_module"],
        "behavior.permissions.allow_file_modification": ["sys_module"],
        "behavior.permissions.allow_file_deletion": ["sys_module"],
        "behavior.permissions.allow_app_launch": ["sys_module"],
        "behavior.permissions.require_confirmation": ["sys_module"],
        
        # 監控設定 - 網路 (LLM 模組)
        "monitoring.network.allow_internet_access": ["llm_module"],
        "monitoring.network.allow_api_calls": ["llm_module"],
        "monitoring.network.timeout": ["llm_module"],
        
        # 進階設定 - 效能 (運行時效能調整)
        # 注意：UI 效能設定需要重啟應用程式才能生效，因此不列入 RELOAD_REQUIRED
        # "advanced.performance.max_fps": ["ui_module"],  # 需要重啟
        # "advanced.performance.enable_hardware_acceleration": ["ui_module"],  # 需要重啟
        
        # 實驗性功能 (運行時功能切換)
        "advanced.experimental.enable_emotion_analysis": ["nlp_module"],
    }
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(UserSettingsManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self.settings_file = Path(__file__).parent / "user_settings.yaml"
        self.settings: Dict[str, Any] = {}
        self.pending_changes: Dict[str, Any] = {}  # 等待重載的變更
        self.reload_callbacks: Dict[str, Callable] = {}  # 模組重載回調
        self.gs_active = False  # GS (Generative Session) 是否活躍
        
        self._initialized = True
        self.load_settings()
        
        info_log("[UserSettingsManager] 使用者設定管理器已初始化")
    
    def load_settings(self) -> bool:
        """載入使用者設定"""
        try:
            if not self.settings_file.exists():
                error_log(f"[UserSettingsManager] 設定檔案不存在: {self.settings_file}")
                return False
            
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                self.settings = yaml.safe_load(f)
            
            info_log("[UserSettingsManager] 使用者設定已載入")
            debug_log(OPERATION_LEVEL, f"[UserSettingsManager] 設定內容: {len(self.settings)} 個主要類別")
            return True
            
        except Exception as e:
            error_log(f"[UserSettingsManager] 載入設定失敗: {e}")
            return False
    
    def save_settings(self) -> bool:
        """保存使用者設定"""
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                yaml.dump(self.settings, f, allow_unicode=True, default_flow_style=False)
            
            info_log("[UserSettingsManager] 使用者設定已保存")
            return True
            
        except Exception as e:
            error_log(f"[UserSettingsManager] 保存設定失敗: {e}")
            return False
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """
        獲取設定值
        
        Args:
            key_path: 設定路徑，如 "interaction.speech_input.enabled"
            default: 預設值
            
        Returns:
            設定值
        """
        keys = key_path.split('.')
        value = self.settings
        
        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            return default
    
    def set(self, key_path: str, value: Any, apply_immediately: bool = False) -> bool:
        """
        設定值
        
        Args:
            key_path: 設定路徑，如 "interaction.speech_input.enabled"
            value: 新值
            apply_immediately: 是否立即套用 (忽略 GS 狀態)
            
        Returns:
            是否成功
        """
        keys = key_path.split('.')
        
        try:
            # 更新設定
            target = self.settings
            for key in keys[:-1]:
                if key not in target:
                    target[key] = {}
                target = target[key]
            
            old_value = target.get(keys[-1])
            target[keys[-1]] = value
            
            # 檢查是否需要重載
            needs_reload = key_path in self.RELOAD_REQUIRED
            
            if needs_reload:
                modules_to_reload = self.RELOAD_REQUIRED[key_path]
                info_log(f"[UserSettingsManager] 設定 '{key_path}' 需要重載模組: {modules_to_reload}")
                
                if self.gs_active and not apply_immediately:
                    # GS 活躍中，標記待處理
                    self.pending_changes[key_path] = {
                        'value': value,
                        'old_value': old_value,
                        'modules': modules_to_reload
                    }
                    info_log(f"[UserSettingsManager] GS 活躍中，設定變更已標記為待處理")
                    return True
                else:
                    # 立即重載
                    return self._apply_reload(key_path, modules_to_reload)
            else:
                # 不需要重載，即時生效
                debug_log(OPERATION_LEVEL, f"[UserSettingsManager] 設定 '{key_path}' 已即時生效")
                return True
                
        except Exception as e:
            error_log(f"[UserSettingsManager] 設定值失敗: {e}")
            return False
    
    def _apply_reload(self, key_path: str, modules: list) -> bool:
        """
        套用重載
        
        Args:
            key_path: 設定路徑
            modules: 需要重載的模組列表
            
        Returns:
            是否成功
        """
        try:
            info_log(f"[UserSettingsManager] 開始重載模組: {modules}")
            
            for module_name in modules:
                if module_name in self.reload_callbacks:
                    callback = self.reload_callbacks[module_name]
                    success = callback(key_path, self.get(key_path))
                    
                    if not success:
                        error_log(f"[UserSettingsManager] 模組 {module_name} 重載失敗")
                        return False
                else:
                    debug_log(OPERATION_LEVEL, 
                             f"[UserSettingsManager] 模組 {module_name} 沒有註冊重載回調")
            
            info_log(f"[UserSettingsManager] 模組重載完成")
            return True
            
        except Exception as e:
            error_log(f"[UserSettingsManager] 重載失敗: {e}")
            return False
    
    def register_reload_callback(self, module_name: str, callback: Callable) -> None:
        """
        註冊模組重載回調
        
        Args:
            module_name: 模組名稱 (如 "stt_module")
            callback: 回調函數，接收 (key_path: str, value: Any) -> bool
        """
        self.reload_callbacks[module_name] = callback
        info_log(f"[UserSettingsManager] 已註冊 {module_name} 重載回調")
    
    def set_gs_active(self, active: bool) -> None:
        """
        設定 GS (Generative Session) 狀態
        
        Args:
            active: 是否活躍
        """
        old_state = self.gs_active
        self.gs_active = active
        
        if old_state and not active:
            # GS 結束，套用待處理的變更
            info_log("[UserSettingsManager] GS 結束，開始套用待處理的設定變更")
            self.apply_pending_changes()
    
    def apply_pending_changes(self) -> Dict[str, bool]:
        """
        套用所有待處理的變更
        
        Returns:
            Dict[設定路徑, 是否成功]
        """
        if not self.pending_changes:
            debug_log(OPERATION_LEVEL, "[UserSettingsManager] 沒有待處理的設定變更")
            return {}
        
        results = {}
        
        for key_path, change_info in self.pending_changes.items():
            modules = change_info['modules']
            success = self._apply_reload(key_path, modules)
            results[key_path] = success
            
            if success:
                info_log(f"[UserSettingsManager] 設定 '{key_path}' 已成功套用")
            else:
                error_log(f"[UserSettingsManager] 設定 '{key_path}' 套用失敗")
        
        # 清空待處理變更
        self.pending_changes.clear()
        
        return results
    
    def get_pending_changes(self) -> Dict[str, Any]:
        """獲取待處理的變更列表"""
        return self.pending_changes.copy()
    
    def has_pending_changes(self) -> bool:
        """是否有待處理的變更"""
        return len(self.pending_changes) > 0
    
    def check_reload_required(self, key_path: str) -> tuple[bool, Optional[list]]:
        """
        檢查設定是否需要重載
        
        Args:
            key_path: 設定路徑
            
        Returns:
            (是否需要重載, 需要重載的模組列表)
        """
        if key_path in self.RELOAD_REQUIRED:
            return True, self.RELOAD_REQUIRED[key_path]
        return False, None
    
    def get_all_settings(self) -> Dict[str, Any]:
        """獲取所有設定"""
        return self.settings.copy()
    
    def reset_to_defaults(self) -> bool:
        """重置為預設設定"""
        try:
            # 重新載入預設設定檔案
            self.load_settings()
            info_log("[UserSettingsManager] 設定已重置為預設值")
            return True
        except Exception as e:
            error_log(f"[UserSettingsManager] 重置設定失敗: {e}")
            return False


# 全域實例
user_settings_manager = UserSettingsManager()


def load_user_settings() -> Dict[str, Any]:
    """載入使用者設定 - 便利函數"""
    user_settings_manager.load_settings()
    return user_settings_manager.get_all_settings()


def save_user_settings() -> bool:
    """保存使用者設定 - 便利函數"""
    return user_settings_manager.save_settings()


def get_user_setting(key_path: str, default: Any = None) -> Any:
    """獲取使用者設定值 - 便利函數"""
    return user_settings_manager.get(key_path, default)


def set_user_setting(key_path: str, value: Any, apply_immediately: bool = False) -> bool:
    """設定使用者設定值 - 便利函數"""
    return user_settings_manager.set(key_path, value, apply_immediately)
