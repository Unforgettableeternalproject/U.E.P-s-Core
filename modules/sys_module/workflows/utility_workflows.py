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
    StepResult,
    StepTemplate
)
from utils.debug_helper import info_log, error_log, debug_log


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
        "clean_trash_bin"
        # translate_document 已移至 file_workflows.py
    ]


def create_utility_workflow(workflow_type: str, session: WorkflowSession) -> WorkflowEngine:
    """創建工具工作流"""
    workflows = {
        "clean_trash_bin": create_clean_trash_bin_workflow
        # translate_document 已移至 file_workflows.py
    }
    
    if workflow_type not in workflows:
        raise ValueError(f"未知的工作流類型：{workflow_type}")
    
    return workflows[workflow_type](session)
