"""
modules/sys_module/workflows.py
Workflow definitions for the SYS module's multi-step command handling

包含工作流程定義、步驟基礎設施和執行系統，支援：
- 靈活定義步驟依賴和條件轉換
- 區分必要與可選步驟
- 數據驗證和傳遞機制
- 動態生成提示和指令
- 步驟模板和重用機制
"""
from typing import Dict, Any, List, Optional, Tuple, Callable, Union, Set, ForwardRef
from enum import Enum
from pathlib import Path
import json
import os
import inspect
import datetime
from abc import ABC, abstractmethod

from core.session_manager import WorkflowSession
from utils.debug_helper import info_log, error_log, debug_log

# 前向引用，解決循環引用問題
WorkflowEngine = ForwardRef('WorkflowEngine')

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
    TEST_TTS = "tts_test"


class StepResult:
    """結果資料類，包含工作流程步驟執行結果"""
    
    def __init__(
        self, 
        success: bool, 
        message: str, 
        data: Optional[Dict[str, Any]] = None, 
        next_step: Optional[str] = None,
        skip_to: Optional[str] = None,
        cancel: bool = False,
        complete: bool = False
    ):
        """
        初始化步驟結果
        
        Args:
            success: 步驟是否成功執行
            message: 顯示給用戶的訊息
            data: 傳遞給下一步驟的數據
            next_step: 指定的下一個步驟 ID (如果不是默認流程)
            skip_to: 跳過中間步驟，直接到指定 ID 的步驟
            cancel: 是否取消整個工作流程
            complete: 是否已完成工作流程
        """
        self.success = success
        self.message = message
        self.data = data or {}
        self.next_step = next_step
        self.skip_to = skip_to
        self.cancel = cancel
        self.complete = complete
        
    @classmethod
    def success(cls, message: str, data: Optional[Dict[str, Any]] = None, next_step: Optional[str] = None):
        """成功結果的工廠方法"""
        return cls(True, message, data, next_step)
        
    @classmethod
    def failure(cls, message: str, data: Optional[Dict[str, Any]] = None):
        """失敗結果的工廠方法"""
        return cls(False, message, data)
    
    @classmethod
    def cancel_workflow(cls, message: str, data: Optional[Dict[str, Any]] = None):
        """取消工作流程的工廠方法"""
        return cls(False, message, data, cancel=True)
        
    @classmethod
    def complete_workflow(cls, message: str, data: Optional[Dict[str, Any]] = None):
        """完成工作流程的工廠方法"""
        return cls(True, message, data, complete=True)
        
    @classmethod
    def skip_to(cls, step_id: str, message: str, data: Optional[Dict[str, Any]] = None):
        """跳至特定步驟的工廠方法"""
        return cls(True, message, data, skip_to=step_id)
    
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典，用於 API 回應"""
        return {
            "success": self.success,
            "message": self.message,
            "data": self.data,
            "next_step": self.next_step,
            "cancel": self.cancel,
            "complete": self.complete
        }
        
class StepRequirement:
    """步驟要求類，描述步驟執行所需的數據"""
    
    def __init__(self, key: str, required: bool = True, validator: Optional[Callable[[Any], bool]] = None, 
                 error_message: Optional[str] = None):
        """
        初始化步驟要求
        
        Args:
            key: 數據鍵名
            required: 是否必要 (True) 或可選 (False)
            validator: 可選的驗證函數
            error_message: 驗證失敗時的錯誤訊息
        """
        self.key = key
        self.required = required
        self.validator = validator
        self.error_message = error_message or f"缺少必要數據: {key}"

class WorkflowStep(ABC):
    """工作流程步驟基類"""
    
    # 步驟類型：UI交互、處理數據、系統操作等
    STEP_TYPE_INTERACTIVE = "interactive"  # 需要用戶輸入
    STEP_TYPE_PROCESSING = "processing"    # 處理數據，不需用戶輸入
    STEP_TYPE_SYSTEM = "system"            # 系統操作，如檔案IO、API調用等
    
    # 步驟優先級：必要、可選、條件式
    PRIORITY_REQUIRED = "required"      # 必須執行的步驟
    PRIORITY_OPTIONAL = "optional"      # 可選步驟，可以跳過
    PRIORITY_CONDITIONAL = "conditional"  # 條件式步驟，取決於前面步驟的結果
    
    def __init__(self, session: WorkflowSession):
        self.session = session
        self._id = self._get_step_id()
        self._requirements: List[StepRequirement] = []
        self._data_validators: Dict[str, Tuple[Callable, str]] = {}
        self._auto_advance_condition: Optional[Callable[[], bool]] = None
        self._step_type = self.STEP_TYPE_INTERACTIVE  # 默認為交互式
        self._priority = self.PRIORITY_REQUIRED  # 默認為必要步驟
        
    def _get_step_id(self) -> str:
        """獲取步驟 ID，默認使用類名"""
        return self.__class__.__name__
        
    @property
    def id(self) -> str:
        """步驟唯一識別碼"""
        return self._id
        
    @property
    def step_type(self) -> str:
        """步驟類型"""
        return self._step_type
        
    @property
    def priority(self) -> str:
        """步驟優先級"""
        return self._priority
        
    def set_step_type(self, step_type: str) -> 'WorkflowStep':
        """設置步驟類型"""
        self._step_type = step_type
        return self
        
    def set_priority(self, priority: str) -> 'WorkflowStep':
        """設置步驟優先級"""
        self._priority = priority
        return self
        
    def set_id(self, step_id: str) -> 'WorkflowStep':
        """設置步驟 ID"""
        self._id = step_id
        return self
        
    def add_requirement(self, key: str, required: bool = True, 
                        validator: Optional[Callable[[Any], bool]] = None, 
                        error_message: Optional[str] = None) -> 'WorkflowStep':
        """添加步驟要求"""
        self._requirements.append(StepRequirement(key, required, validator, error_message))
        return self
        
    def add_data_validator(self, key: str, validator: Callable[[Any], bool], 
                          error_message: str) -> 'WorkflowStep':
        """添加數據驗證器"""
        self._data_validators[key] = (validator, error_message)
        return self
        
    def set_auto_advance_condition(self, condition: Callable[[], bool]) -> 'WorkflowStep':
        """設置自動前進條件"""
        self._auto_advance_condition = condition
        return self
    
    def should_auto_advance(self) -> bool:
        """是否應該自動前進，不等待用戶輸入"""
        return (self._auto_advance_condition is not None and self._auto_advance_condition()) or \
               self._step_type != self.STEP_TYPE_INTERACTIVE
        
    def validate_requirements(self) -> Tuple[bool, Optional[str]]:
        """驗證步驟要求是否滿足"""
        for req in self._requirements:
            value = self.session.get_data(req.key, None)
            
            # 檢查必要項是否存在
            if req.required and value is None:
                return False, req.error_message
                
            # 如果有值且有驗證器，執行驗證
            if value is not None and req.validator is not None:
                try:
                    if not req.validator(value):
                        return False, req.error_message
                except Exception as e:
                    return False, f"數據驗證失敗: {req.key} - {str(e)}"
                    
        return True, None
        
    def validate_data(self, data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """驗證數據有效性"""
        for key, (validator, error_msg) in self._data_validators.items():
            if key in data:
                try:
                    if not validator(data[key]):
                        return False, error_msg
                except Exception as e:
                    return False, f"數據驗證失敗: {key} - {str(e)}"
        return True, None
    
    @abstractmethod
    def execute(self, user_input: Any = None) -> StepResult:
        """
        執行此步驟
        
        Args:
            user_input: 用戶輸入 (可選)
            
        Returns:
            StepResult: 步驟執行結果
        """
        pass
    
    @abstractmethod
    def get_prompt(self) -> str:
        """獲取顯示給用戶的提示"""
        pass
        
    def get_description(self) -> str:
        """獲取步驟描述，用於顯示在UI或日誌中"""
        return f"步驟 {self.id}"
        
    def on_enter(self) -> None:
        """當進入此步驟時調用"""
        debug_log(2, f"[Workflow] 進入步驟: {self.id}")
        
    def on_exit(self, result: StepResult) -> None:
        """當離開此步驟時調用"""
        debug_log(2, f"[Workflow] 離開步驟: {self.id}, 結果: {'成功' if result.success else '失敗'}")
        
    def can_skip(self) -> bool:
        """是否可以跳過此步驟"""
        return self._priority != self.PRIORITY_REQUIRED
        
    def should_execute(self) -> bool:
        """是否應該執行此步驟 (可用於條件步驟)"""
        return True


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
            "intelligent_archive": "智能歯檔",
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


# 舊版工作流程轉換為新架構兼容函數
# 這些函數用於兼容性，讓舊版代碼可以使用新架構
def create_file_processing_workflow(session: WorkflowSession) -> Dict[int, WorkflowStep]:
    """
    Create a multi-step workflow for file processing (舊版兼容函數)
    
    Args:
        session: The workflow session
        
    Returns:
        A dictionary mapping step numbers to workflow steps
    """
    # 調用下面定義的新架構創建函數
    engine = create_file_processing_workflow_v2(session)
    
    # 將新架構轉換為舊架構格式
    # 這是臨時兼容方案，最終應該完全遷移到新架構
    steps = {}
    workflow_def = engine.workflow_def
    
    # 映射步驟 ID 到索引
    step_ids = list(workflow_def._steps.keys())
    for i, step_id in enumerate(step_ids, 1):
        steps[i] = workflow_def.get_step(step_id)
    
    return steps


def get_next_step(session: WorkflowSession, current_step: int) -> Optional[WorkflowStep]:
    """
    Get the next step in a workflow based on the current session state (舊版兼容函數)
    
    Args:
        session: The workflow session
        current_step: The current step number
        
    Returns:
        The next workflow step or None if the workflow is complete
    """
    # 創建引擎實例
    engine = create_file_processing_workflow_v2(session)
    
    # 映射步驟索引到 ID
    workflow_def = engine.workflow_def
    step_ids = list(workflow_def._steps.keys())
    
    # 獲取當前步驟 ID
    if 0 < current_step <= len(step_ids):
        current_id = step_ids[current_step - 1]
    else:
        return None
    
    # 模擬引擎中的下一步邏輯
    # 這裡假設用一個空的成功結果來獲取下一步
    # 更複雜的邏輯需要考慮會話中的數據和條件轉換
    result = StepResult.success("模擬步驟執行")
    next_id = workflow_def.get_next_step_id(current_id, result)
    
    if not next_id or next_id == "END" or next_id == "CANCEL":
        return None
    
    return workflow_def.get_step(next_id)


# 新架構的檔案處理工作流程定義
def create_file_processing_workflow_v2(session: WorkflowSession) -> WorkflowEngine:
    """
    使用新架構創建檔案處理工作流程
    
    Args:
        session: 工作流程會話
        
    Returns:
        工作流程引擎實例
    """
    # 定義工作流程
    workflow_def = WorkflowDefinition(
        workflow_type=WorkflowType.FILE_PROCESSING.value,
        name="檔案處理工作流程",
        description="處理檔案的多步驟工作流程，包含檔案選擇、操作選擇和確認等步驟"
    )
    
    # 定義檔案選擇步驟
    file_select_step = StepTemplate.create_file_selection_step(
        session, 
        "file_selection", 
        "請選擇要處理的檔案:"
    )
    
    # 定義操作選擇步驟
    operation_options = [
        ("summarize_tag", "摘要標記 - 生成檔案摘要和標籤"),
        ("intelligent_archive", "智能歸檔 - 將檔案歸檔到適當位置"),
        ("other", "其他操作")
    ]
    operation_select_step = StepTemplate.create_selection_step(
        session,
        "operation_selection",
        "請選擇要對檔案執行的操作:",
        operation_options,
        ["file_selection"]  # 需要先選擇檔案
    )
    
    # 定義目標目錄步驟（僅用於智能歸檔）
    target_dir_step = StepTemplate.create_input_step(
        session,
        "target_dir",
        "請輸入目標資料夾路徑 (留空則自動選擇):",
        required_data=["file_selection", "operation_selection"]
    )
    
    # 定義確認步驟
    def get_confirmation_prompt(session):
        file_path = session.get_data("file_selection", "未知檔案")
        operation = session.get_data("operation_selection", "未知操作")
        file_name = os.path.basename(file_path)
        
        op_name = {
            "summarize_tag": "摘要標記",
            "intelligent_archive": "智能歸檔",
            "other": "其他操作"
        }.get(operation, operation)
        
        return f"確認要對檔案 '{file_name}' 執行 '{op_name}' 操作嗎？(是/否)"
    
    confirmation_step = StepTemplate.create_confirmation_step(
        session,
        "confirmation",
        get_confirmation_prompt(session),
        "已確認，正在執行操作...",
        "已取消操作",
        ["file_selection", "operation_selection"]
    )
    
    # 定義執行步驟
    def execute_file_operation(session):
        file_path = session.get_data("file_selection")
        operation = session.get_data("operation_selection")
        
        try:
            if operation == "summarize_tag":
                result = summarize_tag(file_path)
                return StepResult.success(
                    f"已生成摘要檔案：{os.path.basename(result.get('summary_file', ''))}",
                    result
                )
                
            elif operation == "intelligent_archive":
                target_dir = session.get_data("target_dir", "")
                new_path = intelligent_archive(file_path, target_dir)
                return StepResult.success(
                    f"檔案已歸檔至: {new_path}",
                    {"archived_path": new_path}
                )
                
            elif operation == "other":
                return StepResult.success("其他操作尚未實作")
                
            else:
                return StepResult.failure(f"未知的操作類型: {operation}")
                
        except Exception as e:
            error_log(f"[Workflow] 執行檔案操作失敗: {e}")
            return StepResult.failure(f"操作失敗: {e}")
    
    execute_step = StepTemplate.create_processing_step(
        session,
        "execute_operation",
        execute_file_operation,
        ["file_selection", "operation_selection", "confirmation"]
    )
    
    # 將步驟添加到工作流程
    workflow_def.add_step(file_select_step)
    workflow_def.add_step(operation_select_step)
    workflow_def.add_step(target_dir_step)
    workflow_def.add_step(confirmation_step)
    workflow_def.add_step(execute_step)
    
    # 設置入口點
    workflow_def.set_entry_point("file_selection")
    
    # 定義步驟轉換
    # 第1步 -> 第2步：檔案選擇 -> 操作選擇
    workflow_def.add_transition("file_selection", "operation_selection")
    
    # 第2步 -> 第3步或第4步：操作選擇 -> 目標目錄或確認
    workflow_def.add_transition(
        "operation_selection", 
        "target_dir", 
        lambda r: session.get_data("operation_selection") == "intelligent_archive"
    )
    workflow_def.add_transition(
        "operation_selection", 
        "confirmation", 
        lambda r: session.get_data("operation_selection") != "intelligent_archive"
    )
    
    # 第3步 -> 第4步：目標目錄 -> 確認
    workflow_def.add_transition("target_dir", "confirmation")
    
    # 第4步 -> 第5步：確認 -> 執行
    workflow_def.add_transition("confirmation", "execute_operation")
    
    # 第5步 -> 結束：執行 -> 結束
    workflow_def.add_transition("execute_operation", "END")
    
    # 創建工作流程引擎
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True  # 啟用自動前進
    
    return engine


# 示範：如何使用新架構定義更複雜的檔案處理工作流程
def create_advanced_file_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    創建高級檔案處理工作流程，包含更多步驟和條件分支
    
    這是一個示範，展示如何使用新架構定義更複雜的工作流程
    """
    # 工作流程配置
    steps_config = [
        {
            "id": "file_selection",
            "type": "file_selection",
            "prompt": "請選擇要處理的檔案:"
        },
        {
            "id": "file_type_detection",
            "type": "processing",
            "processor": lambda s: StepResult.success(
                "已識別檔案類型",
                {"file_type": os.path.splitext(s.get_data("file_selection", ""))[1].lower()}
            ),
            "required_data": ["file_selection"]
        },
        {
            "id": "operation_selection",
            "type": "selection",
            "prompt": "請選擇要執行的操作:",
            "options": [
                ("summarize", "生成摘要與標籤"),
                ("archive", "智能歸檔"),
                ("transform", "格式轉換"),
                ("extract", "內容提取"),
                ("analyze", "內容分析")
            ],
            "required_data": ["file_selection", "file_type"],
            "transitions": [
                {
                    "target_id": "summary_options",
                    "condition": lambda r: r.data.get("operation_selection") == "summarize"
                },
                {
                    "target_id": "archive_options",
                    "condition": lambda r: r.data.get("operation_selection") == "archive"
                },
                {
                    "target_id": "transform_options",
                    "condition": lambda r: r.data.get("operation_selection") == "transform"
                },
                {
                    "target_id": "extract_options",
                    "condition": lambda r: r.data.get("operation_selection") == "extract"
                },
                {
                    "target_id": "analyze_options",
                    "condition": lambda r: r.data.get("operation_selection") == "analyze"
                }
            ]
        },
        # 摘要選項分支
        {
            "id": "summary_options",
            "type": "selection",
            "prompt": "選擇摘要模式:",
            "options": [
                ("brief", "簡要摘要"),
                ("detailed", "詳細摘要"),
                ("keywords", "僅關鍵字")
            ],
            "required_data": ["file_selection", "operation_selection"]
        },
        # 歸檔選項分支
        {
            "id": "archive_options",
            "type": "input",
            "prompt": "請輸入目標資料夾 (留空則自動選擇):",
            "required_data": ["file_selection", "operation_selection"]
        },
        # 確認步驟
        {
            "id": "confirmation",
            "type": "confirmation",
            "prompt": "確認執行操作?",
            "confirm_message": "已確認，正在執行...",
            "cancel_message": "操作已取消",
            "required_data": ["file_selection", "operation_selection"]
        },
        # 執行步驟
        {
            "id": "execute",
            "type": "processing",
            "processor": lambda s: StepResult.success("操作執行完成"),
            "required_data": ["file_selection", "operation_selection", "confirmation"]
        }
    ]
    
    # 使用通用工作流程創建函數
    return create_standard_workflow(
        workflow_type=WorkflowType.FILE_PROCESSING.value,
        name="高級檔案處理工作流程",
        steps_config=steps_config,
        session=session
    )

class WorkflowDefinition:
    """工作流程定義類，包含步驟和轉換規則"""
    
    def __init__(self, workflow_type: str, name: str, description: str = ""):
        self.workflow_type = workflow_type
        self.name = name
        self.description = description
        self._steps: Dict[str, WorkflowStep] = {}
        self._transitions: Dict[str, List[Tuple[Callable[[StepResult], bool], str]]] = {}
        self._entry_point: Optional[str] = None
        self._data_schema: Dict[str, Dict[str, Any]] = {}  # 定義工作流數據欄位的預期類型和描述
        
    def add_step(self, step: WorkflowStep) -> 'WorkflowDefinition':
        """添加步驟到工作流程"""
        self._steps[step.id] = step
        return self
        
    def set_entry_point(self, step_id: str) -> 'WorkflowDefinition':
        """設置工作流程入口點"""
        if step_id not in self._steps:
            raise ValueError(f"找不到步驟 ID: {step_id}")
        self._entry_point = step_id
        return self
        
    def add_transition(self, from_step_id: str, to_step_id: str, 
                      condition: Optional[Callable[[StepResult], bool]] = None) -> 'WorkflowDefinition':
        """
        添加步驟轉換規則
        
        Args:
            from_step_id: 來源步驟 ID
            to_step_id: 目標步驟 ID
            condition: 可選的條件函數，若返回 True 則使用此轉換規則
        """
        if from_step_id not in self._steps:
            raise ValueError(f"找不到來源步驟 ID: {from_step_id}")
        if to_step_id not in self._steps and to_step_id != "END":
            raise ValueError(f"找不到目標步驟 ID: {to_step_id}")
            
        if from_step_id not in self._transitions:
            self._transitions[from_step_id] = []
            
        # 如果沒有提供條件，使用默認條件（總是 True）
        if condition is None:
            condition = lambda _: True
            
        self._transitions[from_step_id].append((condition, to_step_id))
        return self
        
    def add_data_field(self, key: str, field_type: type, description: str, 
                      required: bool = False) -> 'WorkflowDefinition':
        """添加數據欄位定義"""
        self._data_schema[key] = {
            "type": field_type,
            "description": description,
            "required": required
        }
        return self
        
    def get_step(self, step_id: str) -> Optional[WorkflowStep]:
        """獲取指定 ID 的步驟"""
        return self._steps.get(step_id)
        
    def get_next_step_id(self, current_step_id: str, result: StepResult) -> Optional[str]:
        """根據當前步驟和結果確定下一步驟"""
        # 如果結果中明確指定了下一步驟或跳轉目標
        if result.skip_to:
            return result.skip_to
        if result.next_step:
            return result.next_step
        if result.complete:
            return "END"
        if result.cancel:
            return "CANCEL"
            
        # 如果步驟失敗，留在當前步驟
        if not result.success:
            return current_step_id
            
        # 檢查轉換規則
        transitions = self._transitions.get(current_step_id, [])
        for condition, next_id in transitions:
            try:
                if condition(result):
                    return next_id
            except Exception as e:
                error_log(f"[Workflow] 轉換條件執行錯誤: {e}")
                
        # 找不到有效轉換，返回 None
        return None
        
    def get_entry_point(self) -> str:
        """獲取工作流程入口點"""
        if not self._entry_point:
            if not self._steps:
                raise ValueError("工作流程沒有任何步驟")
            # 默認使用第一個添加的步驟
            return next(iter(self._steps.keys()))
        return self._entry_point
        
    def validate(self) -> Tuple[bool, str]:
        """驗證工作流程定義是否有效"""
        if not self._steps:
            return False, "工作流程沒有任何步驟"
            
        # 確保有入口點
        try:
            self.get_entry_point()
        except Exception as e:
            return False, f"無效的入口點: {e}"
            
        # 確保所有步驟都可以到達
        reachable_steps = set()
        to_visit = [self.get_entry_point()]
        
        while to_visit:
            current = to_visit.pop()
            if current in reachable_steps:
                continue
                
            if current != "END" and current != "CANCEL":
                reachable_steps.add(current)
                
            # 檢查此步驟的所有可能轉換
            transitions = self._transitions.get(current, [])
            for _, next_id in transitions:
                if next_id not in reachable_steps and next_id != "END" and next_id != "CANCEL":
                    to_visit.append(next_id)
        
        # 檢查是否有無法到達的步驟
        unreachable = set(self._steps.keys()) - reachable_steps
        if unreachable:
            return False, f"這些步驟無法到達: {', '.join(unreachable)}"
            
        return True, "工作流程定義有效"
        
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式，用於序列化和存儲"""
        return {
            "workflow_type": self.workflow_type,
            "name": self.name,
            "description": self.description,
            "steps": list(self._steps.keys()),
            "entry_point": self.get_entry_point(),
            "data_schema": self._data_schema
        }
        
    @classmethod
    def from_dict(cls, data: Dict[str, Any], step_factory: Callable[[str, WorkflowSession], WorkflowStep]) -> 'WorkflowDefinition':
        """從字典創建工作流程定義"""
        workflow = cls(data["workflow_type"], data["name"], data.get("description", ""))
        
        # 添加數據欄位定義
        for key, schema in data.get("data_schema", {}).items():
            workflow.add_data_field(
                key, schema["type"], schema["description"], schema.get("required", False)
            )
            
        # 創建步驟實例（需要提供 step_factory 函數）
        # 此處無法直接反序列化步驟，因為它們需要 session 對象
        
        return workflow


class WorkflowEngine:
    """工作流程執行引擎，負責執行工作流程和管理步驟轉換"""
    
    def __init__(self, workflow_def: WorkflowDefinition, session: WorkflowSession):
        self.workflow_def = workflow_def
        self.session = session
        self.current_step_id = workflow_def.get_entry_point()
        self.history: List[Tuple[str, StepResult]] = []  # 步驟執行歷史
        self.auto_advance = False  # 是否自動執行無需用戶輸入的步驟
        
    def start(self) -> StepResult:
        """開始執行工作流程"""
        info_log(f"[Workflow] 開始執行 {self.workflow_def.name} 工作流程")
        
        # 如果設置了自動前進，嘗試執行第一個步驟
        if self.auto_advance:
            current_step = self.workflow_def.get_step(self.current_step_id)
            if current_step and current_step.should_auto_advance():
                return self.process_step(None)
                
        # 返回初始步驟的提示
        return StepResult(
            success=True,
            message=f"已啟動工作流程: {self.workflow_def.name}",
            data={"workflow_type": self.workflow_def.workflow_type, "step": self.current_step_id}
        )
        
    def process_step(self, user_input: Any = None) -> StepResult:
        """處理當前步驟並前進到下一個"""
        current_step = self.workflow_def.get_step(self.current_step_id)
        if not current_step:
            return StepResult.failure(f"找不到步驟: {self.current_step_id}")
            
        # 調用步驟的 on_enter 方法
        current_step.on_enter()
            
        # 驗證步驟要求
        valid, error_msg = current_step.validate_requirements()
        if not valid:
            return StepResult.failure(error_msg or "步驟要求未滿足")
            
        # 執行步驟
        try:
            result = current_step.execute(user_input)
        except Exception as e:
            error_log(f"[Workflow] 步驟執行錯誤: {e}")
            return StepResult.failure(f"步驟執行錯誤: {e}")
            
        # 調用步驟的 on_exit 方法
        current_step.on_exit(result)
            
        # 記錄執行歷史
        self.history.append((self.current_step_id, result))
        
        # 如果步驟失敗，不前進
        if not result.success:
            return result
            
        # 更新會話數據
        if result.data:
            for key, value in result.data.items():
                self.session.set_data(key, value)
                
        # 確定下一個步驟
        next_step_id = self.workflow_def.get_next_step_id(self.current_step_id, result)
        
        # 處理特殊情況
        if next_step_id == "END":
            self.session.complete_session()
            return StepResult.complete_workflow("工作流程完成", result.data)
            
        if next_step_id == "CANCEL":
            self.session.cancel_session("工作流程取消")
            return StepResult.cancel_workflow("工作流程取消", result.data)
            
        if not next_step_id:
            error_log(f"[Workflow] 找不到從 {self.current_step_id} 的有效轉換")
            return StepResult.failure(f"工作流程錯誤: 找不到下一步驟")
            
        # 更新當前步驟
        self.current_step_id = next_step_id
        next_step = self.workflow_def.get_step(next_step_id)
        
        # 如果下一步驟可以自動執行，並且啟用了自動前進
        if self.auto_advance and next_step and next_step.should_auto_advance():
            return self.process_step(None)
            
        # 否則返回下一步驟的提示
        return StepResult(
            success=True,
            message=result.message,
            data=result.data,
            next_step=next_step_id
        )
        
    def get_current_prompt(self) -> str:
        """獲取當前步驟的提示"""
        current_step = self.workflow_def.get_step(self.current_step_id)
        if not current_step:
            return f"錯誤: 找不到步驟 {self.current_step_id}"
        return current_step.get_prompt()
        
    def get_current_step_type(self) -> str:
        """獲取當前步驟的類型"""
        current_step = self.workflow_def.get_step(self.current_step_id)
        if not current_step:
            return "unknown"
        return current_step.step_type
    
    def can_current_step_skip(self) -> bool:
        """當前步驟是否可以跳過"""
        current_step = self.workflow_def.get_step(self.current_step_id)
        if not current_step:
            return False
        return current_step.can_skip()
        
    def get_workflow_status(self) -> Dict[str, Any]:
        """獲取工作流程執行狀態"""
        return {
            "workflow_type": self.workflow_def.workflow_type,
            "name": self.workflow_def.name,
            "current_step": self.current_step_id,
            "history_length": len(self.history),
            "session_id": self.session.session_id,
            "session_status": self.session.status.value
        }


class StepTemplate:
    """步驟模板工廠類，用於快速創建常見的步驟類型"""
    
    @staticmethod
    def create_input_step(session: WorkflowSession, step_id: str, prompt: str, 
                        validator: Optional[Callable[[str], Tuple[bool, str]]] = None,
                        required_data: List[str] = None) -> WorkflowStep:
        """
        創建輸入步驟
        
        Args:
            session: 工作流程會話
            step_id: 步驟ID
            prompt: 提示訊息
            validator: 輸入驗證函數，返回 (is_valid, error_message)
            required_data: 此步驟需要的數據鍵列表
            
        Returns:
            輸入步驟實例
        """
        class InputStep(WorkflowStep):
            def __init__(self, session, step_id, prompt, validator):
                super().__init__(session)
                self.set_id(step_id)
                self._prompt = prompt
                self._validator = validator
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                
            def get_prompt(self) -> str:
                return self._prompt
                
            def execute(self, user_input: Any = None) -> StepResult:
                if not user_input:
                    return StepResult.failure("請提供輸入")
                    
                # 驗證輸入
                if self._validator:
                    is_valid, error_msg = self._validator(user_input)
                    if not is_valid:
                        return StepResult.failure(error_msg)
                        
                # 保存輸入到會話
                data_key = step_id.lower().replace(" ", "_")
                data = {data_key: user_input}
                
                return StepResult.success(f"已接收輸入: {user_input}", data)
                
        step = InputStep(session, step_id, prompt, validator)
        
        # 添加必要數據要求
        if required_data:
            for key in required_data:
                step.add_requirement(key, True, error_message=f"缺少必要數據: {key}")
                
        return step
    
    @staticmethod
    def create_confirmation_step(session: WorkflowSession, step_id: str, 
                               prompt: str, confirm_message: str, cancel_message: str,
                               required_data: List[str] = None) -> WorkflowStep:
        """創建確認步驟（是/否選擇）"""
        class ConfirmationStep(WorkflowStep):
            def __init__(self, session, step_id, prompt, confirm_msg, cancel_msg):
                super().__init__(session)
                self.set_id(step_id)
                self._prompt = prompt
                self._confirm_msg = confirm_msg
                self._cancel_msg = cancel_msg
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                
            def get_prompt(self) -> str:
                return self._prompt
                
            def execute(self, user_input: Any = None) -> StepResult:
                if not user_input:
                    return StepResult.failure("請明確回答是或否")
                    
                # 轉換為小寫並檢查確認詞
                input_lower = str(user_input).lower()
                confirm_words = ["是", "yes", "y", "確認", "確定", "同意", "ok", "好", "執行"]
                cancel_words = ["否", "no", "n", "取消", "不", "不要", "拒絕", "不同意"]
                
                if any(word in input_lower for word in confirm_words):
                    return StepResult.success(self._confirm_msg, {"confirmed": True})
                elif any(word in input_lower for word in cancel_words):
                    return StepResult.cancel_workflow(self._cancel_msg, {"confirmed": False})
                else:
                    return StepResult.failure("無法理解您的回答，請明確回答是或否")
                
        step = ConfirmationStep(session, step_id, prompt, confirm_message, cancel_message)
        
        # 添加必要數據要求
        if required_data:
            for key in required_data:
                step.add_requirement(key, True, error_message=f"缺少必要數據: {key}")
                
        return step
    
    @staticmethod
    def create_processing_step(session: WorkflowSession, step_id: str, 
                             processor: Callable[[WorkflowSession], StepResult],
                             required_data: List[str] = None) -> WorkflowStep:
        """創建處理步驟（不需要用戶輸入）"""
        class ProcessingStep(WorkflowStep):
            def __init__(self, session, step_id, processor):
                super().__init__(session)
                self.set_id(step_id)
                self._processor = processor
                self.set_step_type(self.STEP_TYPE_PROCESSING)
                
            def get_prompt(self) -> str:
                return "正在處理中，請稍候..."
                
            def execute(self, user_input: Any = None) -> StepResult:
                try:
                    return self._processor(self.session)
                except Exception as e:
                    error_log(f"[Workflow] 處理步驟錯誤: {e}")
                    return StepResult.failure(f"處理錯誤: {e}")
                
        step = ProcessingStep(session, step_id, processor)
        
        # 添加必要數據要求
        if required_data:
            for key in required_data:
                step.add_requirement(key, True, error_message=f"缺少必要數據: {key}")
                
        return step
    
    @staticmethod
    def create_selection_step(session: WorkflowSession, step_id: str, prompt: str,
                           options: List[Tuple[str, str]], required_data: List[str] = None) -> WorkflowStep:
        """創建選擇步驟（從多個選項中選擇）"""
        class SelectionStep(WorkflowStep):
            def __init__(self, session, step_id, prompt, options):
                super().__init__(session)
                self.set_id(step_id)
                self._prompt = prompt
                self._options = options  # [(value, display_text), ...]
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                
            def get_prompt(self) -> str:
                # 構建帶有選項的提示
                options_text = "\n".join([f"{i+1}. {text}" for i, (_, text) in enumerate(self._options)])
                return f"{self._prompt}\n\n{options_text}"
                
            def execute(self, user_input: Any = None) -> StepResult:
                if not user_input:
                    return StepResult.failure("請選擇一個選項")
                    
                # 處理不同輸入方式（數字或文字）
                input_str = str(user_input).strip().lower()
                
                # 嘗試作為數字索引處理
                try:
                    if input_str.isdigit():
                        idx = int(input_str) - 1
                        if 0 <= idx < len(self._options):
                            value, text = self._options[idx]
                            return StepResult.success(
                                f"已選擇: {text}", 
                                {step_id.lower().replace(" ", "_"): value}
                            )
                except Exception:
                    pass
                    
                # 嘗試匹配選項文字
                for value, text in self._options:
                    if input_str in text.lower() or input_str in value.lower():
                        return StepResult.success(
                            f"已選擇: {text}", 
                            {step_id.lower().replace(" ", "_"): value}
                        )
                        
                # 無匹配
                return StepResult.failure("無效的選擇，請重新輸入")
                
        step = SelectionStep(session, step_id, prompt, options)
        
        # 添加必要數據要求
        if required_data:
            for key in required_data:
                step.add_requirement(key, True, error_message=f"缺少必要數據: {key}")
                
        return step
    
    @staticmethod
    def create_file_selection_step(session: WorkflowSession, step_id: str, 
                                 prompt: str, file_filter: Optional[Callable[[str], bool]] = None) -> WorkflowStep:
        """創建檔案選擇步驟"""
        class FileSelectionStep(WorkflowStep):
            def __init__(self, session, step_id, prompt, file_filter):
                super().__init__(session)
                self.set_id(step_id)
                self._prompt = prompt
                self._file_filter = file_filter
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                
            def get_prompt(self) -> str:
                return self._prompt
                
            def execute(self, user_input: Any = None) -> StepResult:
                if not user_input:
                    return StepResult.failure("請提供檔案路徑")
                    
                # 檢查檔案是否存在
                file_path = str(user_input)
                if not os.path.exists(file_path):
                    return StepResult.failure(f"檔案 '{file_path}' 不存在")
                    
                # 如果提供了篩選器，檢查檔案是否符合條件
                if self._file_filter and not self._file_filter(file_path):
                    return StepResult.failure("檔案類型不支援，請選擇其他檔案")
                    
                # 成功，返回檔案路徑
                return StepResult.success(
                    f"已選擇檔案: {os.path.basename(file_path)}", 
                    {step_id.lower().replace(" ", "_"): file_path}
                )
                
        return FileSelectionStep(session, step_id, prompt, file_filter)


def create_standard_workflow(workflow_type: str, name: str, 
                          steps_config: List[Dict[str, Any]],
                          session: WorkflowSession) -> WorkflowEngine:
    """
    根據配置創建標準工作流程
    
    Args:
        workflow_type: 工作流程類型
        name: 工作流程名稱
        steps_config: 步驟配置列表，每個步驟是一個字典
            - id: 步驟ID
            - type: 步驟類型 (input, confirmation, processing, selection, file_selection)
            - prompt: 提示訊息
            - required_data: 必要數據列表
            - [其他特定步驟類型參數]
        session: 工作流程會話
        
    Returns:
        配置好的工作流程引擎
    """
    workflow_def = WorkflowDefinition(workflow_type, name)
    
    # 創建所有步驟
    for i, config in enumerate(steps_config):
        step_id = config.get("id", f"Step{i+1}")
        step_type = config.get("type", "input")
        required_data = config.get("required_data", [])
        
        # 根據類型創建不同步驟
        if step_type == "input":
            step = StepTemplate.create_input_step(
                session, step_id, 
                config.get("prompt", "請輸入:"),
                config.get("validator"),
                required_data
            )
        elif step_type == "confirmation":
            step = StepTemplate.create_confirmation_step(
                session, step_id,
                config.get("prompt", "確認?"),
                config.get("confirm_message", "已確認"),
                config.get("cancel_message", "已取消"),
                required_data
            )
        elif step_type == "processing":
            step = StepTemplate.create_processing_step(
                session, step_id,
                config.get("processor"),
                required_data
            )
        elif step_type == "selection":
            step = StepTemplate.create_selection_step(
                session, step_id,
                config.get("prompt", "請選擇:"),
                config.get("options", []),
                required_data
            )
        elif step_type == "file_selection":
            step = StepTemplate.create_file_selection_step(
                session, step_id,
                config.get("prompt", "請選擇檔案:"),
                config.get("file_filter")
            )
        else:
            raise ValueError(f"未知的步驟類型: {step_type}")
            
        # 設置步驟優先級
        priority = config.get("priority", WorkflowStep.PRIORITY_REQUIRED)
        step.set_priority(priority)
        
        # 添加步驟到工作流程
        workflow_def.add_step(step)
        
    # 設置入口點
    workflow_def.set_entry_point(steps_config[0].get("id", "Step1"))
    
    # 設置默認轉換規則（按順序依次執行）
    for i in range(len(steps_config) - 1):
        from_id = steps_config[i].get("id", f"Step{i+1}")
        to_id = steps_config[i+1].get("id", f"Step{i+2}")
        
        # 添加配置中指定的條件轉換
        transitions = steps_config[i].get("transitions", [])
        if transitions:
            for transition in transitions:
                target_id = transition.get("target_id")
                condition = transition.get("condition")
                if target_id and condition:
                    workflow_def.add_transition(from_id, target_id, condition)
        else:
            # 如果沒有特定轉換，添加默認下一步
            workflow_def.add_transition(from_id, to_id)
    
    # 最後一個步驟完成後結束工作流程
    last_id = steps_config[-1].get("id", f"Step{len(steps_config)}")
    workflow_def.add_transition(last_id, "END")
    
    # 創建並返回工作流程引擎
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True  # 啟用自動前進
    
    return engine


# 測試工作流程橋接层
# 這個橋接層使得舊的測試工作流程函數可以無痛轉移到新架構

class LegacyTestWorkflowAdapter:
    """
    舊測試工作流程適配器，將舊的函數式工作流程轉換為新架構
    """
    
    def __init__(self, workflow_func: Callable, workflow_type: str, 
                 llm_module=None, tts_module=None):
        """
        初始化適配器
        
        Args:
            workflow_func: 舊的工作流程函數
            workflow_type: 工作流程類型
            llm_module: LLM模組實例
            tts_module: TTS模組實例
        """
        self.workflow_func = workflow_func
        self.workflow_type = workflow_type
        self.llm_module = llm_module
        self.tts_module = tts_module
        
    def create_workflow_engine(self, session: WorkflowSession) -> WorkflowEngine:
        """
        創建基於舊工作流程函數的新架構引擎
        
        Args:
            session: 工作流程會話
            
        Returns:
            新架構的工作流程引擎
        """
        # 創建工作流程定義
        workflow_def = WorkflowDefinition(
            workflow_type=self.workflow_type,
            name=f"測試工作流程 - {self.workflow_type}",
            description=f"從舊架構轉換的 {self.workflow_type} 測試工作流程"
        )
        
        # 創建適配器步驟
        adapter_step = self._create_adapter_step(session)
        workflow_def.add_step(adapter_step)
        workflow_def.set_entry_point(adapter_step.id)
        
        # 設置轉換（適配器步驟根據結果自動決定是否結束）
        workflow_def.add_transition(adapter_step.id, "END")
        
        # 創建引擎
        engine = WorkflowEngine(workflow_def, session)
        engine.auto_advance = True
        
        return engine
        
    def _create_adapter_step(self, session: WorkflowSession) -> WorkflowStep:
        """創建適配器步驟，包裝舊的工作流程函數"""
        
        class LegacyWorkflowStep(WorkflowStep):
            def __init__(self, session, adapter):
                super().__init__(session)
                self.adapter = adapter
                self.set_id(f"legacy_{adapter.workflow_type}_step")
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                
            def get_prompt(self) -> str:
                # 從會話數據中獲取當前提示
                return self.session.get_data("current_prompt", "請輸入:")
                
            def execute(self, user_input: Any = None) -> StepResult:
                # 從會話中獲取當前的 session_data
                session_data = self.session.get_data("legacy_session_data", {})
                
                # 如果有用戶輸入，更新到 session_data
                if user_input is not None:
                    # 確定要更新的字段
                    current_step = session_data.get("step", 1)
                    
                    # 根據工作流程類型和當前步驟決定如何處理用戶輸入
                    if self.adapter.workflow_type == "echo":
                        session_data["message"] = user_input
                    elif self.adapter.workflow_type == "countdown":
                        if current_step == 1:
                            session_data["count"] = user_input
                        else:
                            session_data["user_input"] = user_input
                    elif self.adapter.workflow_type == "data_collector":
                        if current_step == 1:
                            session_data["name"] = user_input
                        elif current_step == 2:
                            session_data["age"] = user_input
                        elif current_step == 3:
                            session_data["interests"] = user_input
                        elif current_step == 4:
                            session_data["feedback"] = user_input
                    elif self.adapter.workflow_type == "random_fail":
                        if current_step == 1:
                            if "max_retries_stage" in session_data:
                                session_data["max_retries_input"] = user_input
                            else:
                                session_data["fail_chance"] = user_input
                        else:
                            session_data["user_input"] = user_input
                    elif self.adapter.workflow_type == "tts_test":
                        if current_step == 1:
                            session_data["text"] = user_input
                        elif current_step == 2:
                            session_data["user_input"] = user_input  # 用於情緒選擇
                        elif current_step == 3:
                            session_data["user_input"] = user_input  # 用於保存選擇
                    else:
                        # 通用處理
                        session_data["user_input"] = user_input
                
                # 調用舊的工作流程函數
                try:
                    result = self.adapter.workflow_func(
                        session_data, 
                        self.adapter.llm_module, 
                        self.adapter.tts_module
                    )
                    
                    # 更新會話數據
                    self.session.set_data("legacy_session_data", result.get("session_data", session_data))
                    
                    # 處理結果狀態
                    status = result.get("status", "processing")
                    message = result.get("message", "")
                    requires_input = result.get("requires_input", False)
                    
                    # 如果需要更多輸入，保存提示
                    if requires_input:
                        prompt = result.get("prompt", "請輸入:")
                        self.session.set_data("current_prompt", prompt)
                        
                        # 返回成功結果，但不完成工作流程
                        return StepResult.success(message, result.get("data", {}))
                    else:
                        # 工作流程完成
                        if status == "completed":
                            return StepResult.complete_workflow(message, result.get("result", {}))
                        elif status == "error":
                            return StepResult.failure(message)
                        else:
                            return StepResult.success(message, result.get("data", {}))
                            
                except Exception as e:
                    error_log(f"[Workflow Adapter] 執行舊工作流程時發生錯誤: {e}")
                    return StepResult.failure(f"工作流程執行錯誤: {e}")
                    
            def should_auto_advance(self) -> bool:
                # 檢查是否需要用戶輸入
                session_data = self.session.get_data("legacy_session_data", {})
                
                # 如果沒有初始化會話數據，需要先執行一次
                if not session_data:
                    return True
                    
                # 檢查當前狀態是否需要輸入
                return not self.session.get_data("requires_input", False)
                
        return LegacyWorkflowStep(session, self)


def create_test_workflow_engine(workflow_type: str, session: WorkflowSession,
                               llm_module=None, tts_module=None) -> WorkflowEngine:
    """
    為測試工作流程創建新架構的引擎
    
    Args:
        workflow_type: 工作流程類型
        session: 工作流程會話
        llm_module: LLM模組實例
        tts_module: TTS模組實例
        
    Returns:
        新架構的工作流程引擎
    """
    # 導入測試工作流程函數
    from .actions.test_workflows import get_test_workflow
    
    # 獲取舊的工作流程函數
    workflow_func = get_test_workflow(workflow_type)
    if not workflow_func:
        raise ValueError(f"未找到測試工作流程: {workflow_type}")
        
    # 創建適配器
    adapter = LegacyTestWorkflowAdapter(workflow_func, workflow_type, llm_module, tts_module)
    
    # 創建引擎
    return adapter.create_workflow_engine(session)


def create_native_test_workflow_engine(workflow_type: str, session: WorkflowSession,
                                      llm_module=None, tts_module=None) -> WorkflowEngine:
    """
    使用新架構原生實現的測試工作流程引擎
    
    這個函數展示如何使用新架構重寫測試工作流程
    """
    if workflow_type == "echo":
        return _create_echo_workflow_engine(session)
    elif workflow_type == "countdown":
        return _create_countdown_workflow_engine(session)
    elif workflow_type == "data_collector":
        # 暫時使用適配器模式，之後可以實現原生版本
        return create_test_workflow_engine(workflow_type, session, llm_module, tts_module)
    elif workflow_type == "random_fail":
        # 暫時使用適配器模式，之後可以實現原生版本
        return create_test_workflow_engine(workflow_type, session, llm_module, tts_module)
    elif workflow_type == "tts_test":
        # 暫時使用適配器模式，之後可以實現原生版本
        return create_test_workflow_engine(workflow_type, session, llm_module, tts_module)
    else:
        raise ValueError(f"未支援的原生測試工作流程類型: {workflow_type}")


def _create_echo_workflow_engine(session: WorkflowSession) -> WorkflowEngine:
    """創建原生 echo 工作流程引擎"""
    workflow_def = WorkflowDefinition(
        workflow_type="echo",
        name="回顯測試工作流程",
        description="簡單的回顯測試，用於驗證工作流程基本功能"
    )
    
    # 創建輸入步驟
    input_step = StepTemplate.create_input_step(
        session, 
        "echo_input", 
        "請輸入要回顯的訊息:"
    )
    
    # 創建處理步驟
    def echo_processor(session):
        message = session.get_data("echo_input", "")
        return StepResult.complete_workflow(
            f"已完成訊息回顯: {message}",
            {
                "echo_message": message,
                "timestamp": datetime.datetime.now().isoformat()
            }
        )
    
    process_step = StepTemplate.create_processing_step(
        session,
        "echo_process",
        echo_processor,
        ["echo_input"]
    )
    
    # 添加步驟和轉換
    workflow_def.add_step(input_step)
    workflow_def.add_step(process_step)
    workflow_def.set_entry_point("echo_input")
    workflow_def.add_transition("echo_input", "echo_process")
    workflow_def.add_transition("echo_process", "END")
    
    # 創建引擎
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


def _create_countdown_workflow_engine(session: WorkflowSession) -> WorkflowEngine:
    """創建原生 countdown 工作流程引擎"""
    workflow_def = WorkflowDefinition(
        workflow_type="countdown",
        name="倒數測試工作流程",
        description="倒數測試，用於驗證多步驟工作流程"
    )
    
    # 步驟 1: 輸入起始數字
    def validate_count(value):
        try:
            count = int(value)
            return count > 0
        except ValueError:
            return False
            
    input_step = StepTemplate.create_input_step(
        session,
        "countdown_input",
        "請輸入一個正整數作為倒數起始值:",
        lambda x: (validate_count(x), "請輸入一個大於零的整數")
    )
    
    # 步驟 2: 倒數過程（可重複）
    class CountdownStep(WorkflowStep):
        def __init__(self, session):
            super().__init__(session)
            self.set_id("countdown_step")
            self.set_step_type(self.STEP_TYPE_INTERACTIVE)
            
        def get_prompt(self) -> str:
            count = self.session.get_data("current_count", 0)
            if count > 0:
                return f"當前值: {count}，按 Enter 繼續倒數或輸入 '跳過' 直接結束:"
            else:
                return "倒數完成！"
            
        def execute(self, user_input: Any = None) -> StepResult:
            # 初始化倒數
            if not self.session.get_data("initialized", False):
                start_count = int(self.session.get_data("countdown_input", "0"))
                self.session.set_data("current_count", start_count)
                self.session.set_data("original_count", start_count)
                self.session.set_data("initialized", True)
                
            current_count = self.session.get_data("current_count", 0)
            
            # 檢查是否完成
            if current_count <= 0:
                original_count = self.session.get_data("original_count", 0)
                return StepResult.complete_workflow(
                    f"倒數完成！從 {original_count} 到 0",
                    {
                        "original_count": original_count,
                        "countdown_completed": True,
                        "completion_time": datetime.datetime.now().isoformat()
                    }
                )
            
            # 檢查是否跳過
            if user_input and "跳過" in str(user_input):
                return StepResult.complete_workflow("倒數已跳過")
            
            # 繼續倒數
            new_count = current_count - 1
            self.session.set_data("current_count", new_count)
            
            if new_count > 0:
                return StepResult.success(f"倒數: {current_count} -> {new_count}")
            else:
                return StepResult.success("倒數完成！")
                
    countdown_step = CountdownStep(session)
    
    # 添加步驟和轉換
    workflow_def.add_step(input_step)
    workflow_def.add_step(countdown_step)
    workflow_def.set_entry_point("countdown_input")
    workflow_def.add_transition("countdown_input", "countdown_step")
    workflow_def.add_transition("countdown_step", "countdown_step")  # 自迴圈
    workflow_def.add_transition("countdown_step", "END")
    
    # 創建引擎
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine
