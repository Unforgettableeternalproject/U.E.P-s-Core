"""
工具相關工作流
包含：clean_trash_bin, translate_document
"""

from typing import Dict, Any

from core.sessions.session_manager import WorkflowSession
from modules.sys_module.workflows import (
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowMode,
    StepResult
)
from modules.sys_module.step_templates import StepTemplate
from utils.debug_helper import info_log, error_log, debug_log


# ==================== Generate Backup Script Workflow ====================

def create_generate_backup_script_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    建立生成備份腳本工作流
    
    這是一個「一次性實用工具」工作流：
    1. 生成備份腳本（.bat / .sh）到桌面
    2. 完成
    
    使用預設路徑：
    - 腳本輸出：桌面/backup_script.bat
    - 目標資料夾：使用者文件夾
    - 備份目標：桌面/Backup
    
    未來擴展：
    - 整合 UEP 對用戶的認知（記憶系統）
    - 生成個性化備份策略
    - 包含用戶習慣分析
    
    註：此工作流將來會重構為更智能的版本
    
    Args:
        session: 工作流程會話
            
    Returns:
        WorkflowEngine 實例
    """
    workflow_def = WorkflowDefinition(
        workflow_type="generate_backup_script",
        name="生成備份腳本",
        description="生成系統備份腳本到桌面（使用預設路徑）",
        workflow_mode=WorkflowMode.DIRECT
    )
    
    # 定義生成備份腳本的處理函數
    def generate_script_processor(session: WorkflowSession) -> StepResult:
        """
        生成備份腳本（使用預設路徑）
        
        未來會擴展為：
        - 根據 UEP 對用戶的認知生成個性化備份腳本
        - 包含用戶習慣、偏好設置、重要檔案路徑等資訊
        - 支援跨平台（Windows/Linux/macOS）
        """
        try:
            from modules.sys_module.actions.automation_helper import generate_backup_script
            from pathlib import Path
            import os
            
            # 使用預設路徑
            home = Path(os.path.expanduser("~"))
            desktop = home / "Desktop"
            
            target_folder = str(home / "Documents")  # 備份使用者文件夾
            dest_folder = str(desktop / "Backup")    # 備份到桌面/Backup
            output_path = str(desktop / "backup_script.bat")  # 腳本放桌面
            
            info_log(f"[BackupScript] 使用預設路徑生成備份腳本")
            info_log(f"[BackupScript] Target: {target_folder}")
            info_log(f"[BackupScript] Dest: {dest_folder}")
            info_log(f"[BackupScript] Output: {output_path}")
            
            # 生成備份腳本
            script_path = generate_backup_script(
                target_folder=target_folder,
                dest_folder=dest_folder,
                output_path=output_path
            )
            
            if script_path:
                info_log(f"[BackupScript] 備份腳本已生成：{script_path}")
                
                # TODO: 未來擴展 - 加入 UEP 個性化資訊
                # - 用戶偏好設置
                # - 重要檔案清單
                # - 排除規則
                # - 備份頻率建議
                
                return StepResult.complete_workflow(
                    f"備份腳本已生成到桌面：{script_path}",
                    data={
                        "script_path": script_path,
                        "target_folder": target_folder,
                        "dest_folder": dest_folder
                    }
                )
            else:
                return StepResult.failure("生成備份腳本失敗")
                
        except Exception as e:
            error_log(f"[BackupScript] 生成失敗：{e}")
            return StepResult.failure(f"生成備份腳本失敗：{str(e)}")
    
    # 使用 StepTemplate 創建處理步驟（無需 required_data）
    generation_step = StepTemplate.create_processing_step(
        session=session,
        step_id="generate_script",
        processor=generate_script_processor,
        required_data=[],  # 不需要任何參數
        description="生成備份腳本到桌面"
    )
    
    workflow_def.add_step(generation_step)
    workflow_def.set_entry_point("generate_script")
    
    return WorkflowEngine(workflow_def, session)


# ==================== Clean Trash Bin Workflow ====================

def create_clean_trash_bin_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    清空資源回收桶工作流
    
    步驟：
    1. 確認是否清空
    2. 執行清空
    """
    workflow_def = WorkflowDefinition(
        workflow_type="clean_trash_bin",
        name="清空資源回收桶",
        description="清空 Windows 資源回收桶",
        workflow_mode=WorkflowMode.DIRECT
    )
    
    # 步驟 1: 確認清空
    confirm_step = StepTemplate.create_confirmation_step(
        session=session,
        step_id="confirm_clean",
        message="Are you sure you want to empty the Recycle Bin? This action cannot be undone.",
        required_data=[]
    )
    
    # 步驟 2: 執行清空
    def execute_clean(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.file_interaction import clean_trash_bin
        
        confirmed = session.get_data("confirm_clean", False)
        
        if not confirmed:
            return StepResult.complete_workflow("Operation cancelled", {"cancelled": True})
        
        info_log("[Workflow] 執行清空資源回收桶")
        
        try:
            result_message = clean_trash_bin()  # 返回字符串
            return StepResult.complete_workflow(
                "Recycle Bin has been emptied successfully",
                {"cleaned": True, "message": result_message}
            )
        except Exception as e:
            return StepResult.failure(f"Failed to empty Recycle Bin: {str(e)}")
    
    clean_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_clean",
        processor=execute_clean,
        required_data=["confirm_clean"],
        description="執行清空"
    )
    
    # 組裝工作流
    workflow_def.add_step(confirm_step)
    workflow_def.add_step(clean_step)
    
    workflow_def.set_entry_point("confirm_clean")
    workflow_def.add_transition("confirm_clean", "execute_clean")
    workflow_def.add_transition("execute_clean", "END")
    
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True  # 啟用自動推進
    return engine

# ==================== Workflow Registry ====================

def get_available_utility_workflows() -> list:
    """獲取可用的工具工作流列表"""
    return [
        "clean_trash_bin",
        "generate_backup_script"
    ]


def create_utility_workflow(workflow_type: str, session: WorkflowSession) -> WorkflowEngine:
    """創建工具工作流"""
    workflows = {
        "clean_trash_bin": create_clean_trash_bin_workflow,
        "generate_backup_script": create_generate_backup_script_workflow
    }
    
    if workflow_type not in workflows:
        raise ValueError(f"未知的工作流類型：{workflow_type}")
    
    return workflows[workflow_type](session)
