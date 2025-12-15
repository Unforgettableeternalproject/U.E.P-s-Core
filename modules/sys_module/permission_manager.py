"""
modules/sys_module/permission_manager.py
Permission manager for SYS module operations

管理系統操作權限，包括：
- 檔案創建/修改/刪除權限
- 應用程式啟動權限
- 系統命令執行權限
- 操作確認機制
"""

from typing import Optional, Callable, Any
from enum import Enum
from configs.user_settings_manager import get_user_setting
from utils.debug_helper import debug_log, info_log, error_log


class PermissionType(Enum):
    """權限類型"""
    FILE_CREATE = "file_creation"
    FILE_MODIFY = "file_modification"
    FILE_DELETE = "file_deletion"
    APP_LAUNCH = "app_launch"
    SYSTEM_COMMAND = "system_command"


class PermissionManager:
    """
    權限管理器 - 檢查並執行需要權限的操作
    
    與 user_settings 整合，從 behavior.permissions 讀取權限設定
    """
    
    def __init__(self):
        """初始化權限管理器"""
        self._confirmation_callback: Optional[Callable[[str, str], bool]] = None
        info_log("[Permission] 權限管理器初始化完成")
    
    def set_confirmation_callback(self, callback: Callable[[str, str], bool]):
        """
        設置確認回調函數
        
        Args:
            callback: 確認函數，接收 (operation: str, details: str) -> bool
        """
        self._confirmation_callback = callback
        debug_log(2, "[Permission] 已設置確認回調函數")
    
    def check_permission(self, permission_type: PermissionType, 
                        operation_details: str = "") -> tuple[bool, str]:
        """
        檢查是否有執行指定操作的權限
        
        Args:
            permission_type: 權限類型
            operation_details: 操作詳情（用於確認對話框）
            
        Returns:
            (是否允許, 原因說明)
        """
        # 根據權限類型獲取對應的設定路徑
        setting_path_map = {
            PermissionType.FILE_CREATE: "behavior.permissions.allow_file_creation",
            PermissionType.FILE_MODIFY: "behavior.permissions.allow_file_modification",
            PermissionType.FILE_DELETE: "behavior.permissions.allow_file_deletion",
            PermissionType.APP_LAUNCH: "behavior.permissions.allow_app_launch",
            PermissionType.SYSTEM_COMMAND: "behavior.permissions.allow_system_commands",
        }
        
        setting_path = setting_path_map.get(permission_type)
        if not setting_path:
            error_log(f"[Permission] 未知的權限類型: {permission_type}")
            return False, f"未知的權限類型: {permission_type.value}"
        
        # 讀取權限設定
        is_allowed = get_user_setting(setting_path, True)  # 預設允許
        
        if not is_allowed:
            reason = f"權限被拒絕: {permission_type.value}"
            debug_log(1, f"[Permission] {reason}")
            return False, reason
        
        # 檢查是否需要確認
        require_confirmation = get_user_setting("behavior.permissions.require_confirmation", True)
        
        if require_confirmation and self._confirmation_callback:
            operation_name = self._get_permission_name(permission_type)
            confirmed = self._confirmation_callback(operation_name, operation_details)
            
            if not confirmed:
                reason = f"用戶未確認操作: {operation_name}"
                info_log(f"[Permission] {reason}")
                return False, reason
        
        debug_log(3, f"[Permission] 允許操作: {permission_type.value}")
        return True, "權限允許"
    
    def execute_with_permission(self, permission_type: PermissionType,
                               operation: Callable[[], Any],
                               operation_details: str = "") -> tuple[bool, Any, str]:
        """
        檢查權限並執行操作
        
        Args:
            permission_type: 權限類型
            operation: 要執行的操作函數
            operation_details: 操作詳情
            
        Returns:
            (是否成功, 操作結果, 訊息)
        """
        # 檢查權限
        allowed, reason = self.check_permission(permission_type, operation_details)
        
        if not allowed:
            return False, None, reason
        
        # 執行操作
        try:
            result = operation()
            info_log(f"[Permission] 操作成功: {permission_type.value}")
            return True, result, "操作成功"
        except Exception as e:
            error_msg = f"操作失敗: {str(e)}"
            error_log(f"[Permission] {error_msg}")
            return False, None, error_msg
    
    def _get_permission_name(self, permission_type: PermissionType) -> str:
        """獲取權限的中文名稱"""
        name_map = {
            PermissionType.FILE_CREATE: "創建檔案",
            PermissionType.FILE_MODIFY: "修改檔案",
            PermissionType.FILE_DELETE: "刪除檔案",
            PermissionType.APP_LAUNCH: "啟動應用程式",
            PermissionType.SYSTEM_COMMAND: "執行系統命令",
        }
        return name_map.get(permission_type, permission_type.value)


# 全局權限管理器實例
_permission_manager: Optional[PermissionManager] = None


def get_permission_manager() -> PermissionManager:
    """獲取全局權限管理器實例"""
    global _permission_manager
    if _permission_manager is None:
        _permission_manager = PermissionManager()
    return _permission_manager
