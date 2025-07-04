"""
modules/sys_module/workflows.py
Workflow definitions for the SYS module's multi-step command handling
"""
from typing import Dict, Any, List, Optional, Tuple
from enum import Enum
from pathlib import Path
import json
import os

from core.session_manager import WorkflowSession
from utils.debug_helper import info_log, error_log, debug_log

# Import needed SYS actions
from .actions.file_interaction import drop_and_read, intelligent_archive, summarize_tag

# Add test workflow types
class WorkflowType(Enum):
    """Enum for different workflow types"""
    FILE_PROCESSING = "file_processing"
    TASK_AUTOMATION = "task_automation"
    SYSTEM_CONFIG = "system_config"
    MULTI_FILE = "multi_file"
    # Test workflow types
    TEST_ECHO = "echo"
    TEST_COUNTDOWN = "countdown"
    TEST_DATA_COLLECTOR = "data_collector" 
    TEST_RANDOM_FAIL = "random_fail"


class WorkflowStep:
    """Base class for workflow steps"""
    def __init__(self, session: WorkflowSession):
        self.session = session
        
    def execute(self, user_input: Any = None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Execute this step in the workflow
        
        Args:
            user_input: Optional input from the user for this step
            
        Returns:
            (success, message, data)
            - success: Whether the step was successful
            - message: A message to display to the user
            - data: Any data to pass to the next step
        """
        raise NotImplementedError("Workflow steps must implement execute()")
    
    def get_prompt(self) -> str:
        """Get a prompt to show to the user for this step"""
        raise NotImplementedError("Workflow steps must implement get_prompt()")


class FileSelectionStep(WorkflowStep):
    """Step for selecting a file to process"""
    
    def get_prompt(self) -> str:
        return "請選擇一個檔案進行處理。"
    
    def execute(self, user_input: str = None) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Execute the file selection step
        
        Args:
            user_input: The file path selected by the user
            
        Returns:
            Tuple of (success, message, data)
        """
        if not user_input:
            return False, "請提供檔案路徑", None
            
        file_path = user_input
        
        # Check if file exists
        if not os.path.exists(file_path):
            return False, f"檔案 {file_path} 不存在", None
            
        # Store file path in session
        data = {"file_path": file_path}
        
        # Try to read file content
        try:
            content = drop_and_read(file_path)
            data["file_content"] = content[:500] + "..." if len(content) > 500 else content
        except Exception as e:
            info_log(f"[workflow] 無法讀取檔案內容: {e}")
            # Continue anyway, since we at least have the file path
        
        return True, f"已選擇檔案: {os.path.basename(file_path)}", data


class ActionSelectionStep(WorkflowStep):
    """Step for selecting an action to perform on a file"""
    
    def get_prompt(self) -> str:
        file_path = self.session.get_data("file_path", "")
        file_name = os.path.basename(file_path) if file_path else "檔案"
        return f"要對 {file_name} 執行什麼操作？可選: 1.摘要標記, 2.智能歸檔, 3.其他"
    
    def execute(self, user_input: str = None) -> Tuple[bool, str, Dict[str, Any]]:
        """Process the action selection"""
        if not user_input:
            return False, "請選擇要執行的操作", None
            
        action_input = user_input.lower()
        
        # Map input to action
        action_map = {
            "1": "summarize_tag",
            "摘要": "summarize_tag",
            "摘要標記": "summarize_tag",
            "2": "intelligent_archive",
            "歸檔": "intelligent_archive",
            "智能歸檔": "intelligent_archive",
            "3": "other",
            "其他": "other"
        }
        
        action = None
        for key, value in action_map.items():
            if key in action_input:
                action = value
                break
                
        if not action:
            return False, "無法辨認操作選項，請選擇: 1.摘要標記, 2.智能歸檔, 或 3.其他", None
            
        return True, f"已選擇操作: {action}", {"action": action}


class ConfirmationStep(WorkflowStep):
    """Step for confirming the action"""
    
    def get_prompt(self) -> str:
        file_path = self.session.get_data("file_path", "未知檔案")
        file_name = os.path.basename(file_path)
        action = self.session.get_data("action", "未知操作")
        
        action_name_map = {
            "summarize_tag": "摘要標記",
            "intelligent_archive": "智能歸檔",
            "other": "其他操作"
        }
        
        action_name = action_name_map.get(action, action)
        
        return f"確認要對檔案 '{file_name}' 執行 '{action_name}' 操作嗎？(是/否)"
    
    def execute(self, user_input: str = None) -> Tuple[bool, str, Dict[str, Any]]:
        """Process the confirmation"""
        if not user_input:
            return False, "請確認是否要執行操作 (是/否)", None
            
        confirmation = user_input.lower()
        
        # Look for confirmation words
        confirm_words = ["是", "yes", "y", "確認", "sure", "ok", "執行"]
        deny_words = ["否", "no", "n", "取消", "cancel", "不要"]
        
        confirmed = any(word in confirmation for word in confirm_words)
        denied = any(word in confirmation for word in deny_words)
        
        if denied:
            self.session.cancel_session("使用者取消操作")
            return False, "已取消操作", {"cancelled": True}
            
        if not confirmed:
            return False, "請明確回答是否要執行操作 (是/否)", None
            
        return True, "已確認執行操作", {"confirmed": True}


class ExecuteActionStep(WorkflowStep):
    """Step for executing the selected action"""
    
    def get_prompt(self) -> str:
        return "正在執行操作，請稍候..."
    
    def execute(self, user_input: str = None) -> Tuple[bool, str, Dict[str, Any]]:
        """Execute the selected action"""
        file_path = self.session.get_data("file_path")
        action = self.session.get_data("action")
        
        if not file_path or not action:
            error_log("[workflow] 執行動作失敗：缺少檔案路徑或操作類型")
            return False, "執行失敗：缺少必要資訊", {"error": "missing_data"}
        
        result = {}
        try:
            if action == "summarize_tag":
                # Execute summarize_tag action
                summary_result = summarize_tag(file_path)
                result = {
                    "summary_file": summary_result.get("summary_file", ""),
                    "tags": summary_result.get("tags", [])
                }
                message = f"已生成摘要檔案: {os.path.basename(result['summary_file'])}\n標籤: {', '.join(result['tags'])}"
                
            elif action == "intelligent_archive":
                # Check if we have target directory from a previous step
                target_dir = self.session.get_data("target_dir", "")
                # Execute intelligent_archive action
                archive_path = intelligent_archive(file_path, target_dir)
                result = {"archived_path": archive_path}
                message = f"檔案已歸檔至: {archive_path}"
                
            elif action == "other":
                # Handle other actions (placeholder)
                message = "其他操作尚未實作"
                result = {"status": "not_implemented"}
                
            else:
                error_log(f"[workflow] 未知的操作類型: {action}")
                return False, f"未知的操作類型: {action}", {"error": "unknown_action"}
                
            return True, message, result
            
        except Exception as e:
            error_msg = f"執行 {action} 操作時發生錯誤: {e}"
            error_log(f"[workflow] {error_msg}")
            return False, error_msg, {"error": str(e)}


class TargetDirStep(WorkflowStep):
    """Optional step for selecting a target directory for intelligent_archive"""
    
    def get_prompt(self) -> str:
        return "請提供要歸檔到的目標資料夾路徑（留空則自動選擇）:"
    
    def execute(self, user_input: str = None) -> Tuple[bool, str, Dict[str, Any]]:
        """Process the target directory selection"""
        # This is optional, so empty input is fine
        if not user_input:
            return True, "將使用自動選擇的資料夾", {"target_dir": ""}
            
        target_dir = user_input
        
        # Check if directory exists
        if not os.path.isdir(target_dir):
            try:
                # Try to create the directory
                os.makedirs(target_dir, exist_ok=True)
                return True, f"已建立並選擇資料夾: {target_dir}", {"target_dir": target_dir}
            except Exception as e:
                return False, f"無法建立資料夾 {target_dir}: {e}", None
        
        return True, f"已選擇資料夾: {target_dir}", {"target_dir": target_dir}


def create_file_processing_workflow(session: WorkflowSession) -> Dict[int, WorkflowStep]:
    """
    Create a multi-step workflow for file processing
    
    Args:
        session: The workflow session
        
    Returns:
        A dictionary mapping step numbers to workflow steps
    """
    steps = {
        1: FileSelectionStep(session),
        2: ActionSelectionStep(session),
    }
    
    # Steps 3+ are conditionally added based on the chosen action in step 2
    # These will be determined during workflow execution
    
    return steps


def get_next_step(session: WorkflowSession, current_step: int) -> Optional[WorkflowStep]:
    """
    Get the next step in a workflow based on the current session state
    
    Args:
        session: The workflow session
        current_step: The current step number
        
    Returns:
        The next workflow step or None if the workflow is complete
    """
    workflow_type = session.workflow_type
    
    # Base steps for file processing workflow
    if workflow_type == WorkflowType.FILE_PROCESSING.value:
        # Step 1: File selection -> Step 2: Action selection
        if current_step == 1:
            return ActionSelectionStep(session)
        
        # Step 2: Action selection -> Different paths based on action
        elif current_step == 2:
            action = session.get_data("action")
            
            if action == "intelligent_archive":
                # For archive, ask for target directory
                return TargetDirStep(session)
            else:
                # For other actions, go straight to confirmation
                return ConfirmationStep(session)
                
        # Step 3: Target dir (for archive) -> Confirmation
        elif current_step == 3:
            if session.get_data("action") == "intelligent_archive":
                return ConfirmationStep(session)
            else:
                # Already did confirmation in previous step
                return ExecuteActionStep(session)
                
        # Step 4: Confirmation -> Execute
        elif current_step == 4:
            if session.get_data("confirmed", False):
                return ExecuteActionStep(session)
            else:
                # If not confirmed, end workflow
                session.cancel_session("使用者取消操作")
                return None
        
        # Step 5: No more steps after execution
        elif current_step == 5:
            # Complete the session
            session.complete_session()
            return None
    
    # Unknown workflow type or invalid step
    return None
