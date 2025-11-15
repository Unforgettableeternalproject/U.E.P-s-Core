"""
modules/sys_module/workflows.py
Core workflow engine and infrastructure for the SYS module

包含工作流程引擎、步驟基礎設施和執行系統，支援：
- 靈活定義步驟依賴和條件轉換
- 區分必要與可選步驟
- 數據驗證和傳遞機制
- 動態生成提示和指令
- 步驟模板和重用機制

所有實際的工作流程定義（包括測試和真實功能）都應在外部定義並註冊。
"""

from typing import Dict, Any, List, Optional, Tuple, Callable, Union, Set, ForwardRef
from enum import Enum
from pathlib import Path
import json
import os
import inspect
import datetime
import time
from abc import ABC, abstractmethod

from core.sessions.session_manager import WorkflowSession
from utils.debug_helper import info_log, error_log, debug_log

# 前向引用，解決循環引用問題
WorkflowEngine = ForwardRef('WorkflowEngine')


class WorkflowType(Enum):
    """工作流程類型枚舉"""
    FILE_PROCESSING = "file_processing"
    TASK_AUTOMATION = "task_automation"
    SYSTEM_CONFIG = "system_config"
    MULTI_FILE = "multi_file"
    OTHER = "other"


class WorkflowMode(str, Enum):
    """工作流程執行模式枚舉"""
    DIRECT = "direct"           # 直接工作 (阻塞主循環，同步執行)
    BACKGROUND = "background"   # 背景工作 (獨立執行緒，非阻塞)


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
        complete: bool = False,
        continue_current_step: bool = False,
        llm_review_data: Optional[Dict[str, Any]] = None,
        requires_user_confirmation: bool = False
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
            continue_current_step: 是否繼續在當前步驟（用於循環步驟）
            llm_review_data: 供 LLM 審核的結構化資料
            requires_user_confirmation: 是否需要使用者確認
        """
        self.success = success
        self.message = message
        self.data = data or {}
        self.next_step = next_step
        self.skip_to = skip_to
        self.cancel = cancel
        self.complete = complete
        self.continue_current_step = continue_current_step
        self.llm_review_data = llm_review_data
        self.requires_user_confirmation = requires_user_confirmation
        
    @classmethod
    def success(cls, message: str, data: Optional[Dict[str, Any]] = None, 
                next_step: Optional[str] = None, skip_to: Optional[str] = None, 
                continue_current_step: bool = False):
        """成功結果的工廠方法"""
        return cls(True, message, data, next_step, skip_to, False, False, continue_current_step)
        
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
            "complete": self.complete,
            "continue_current_step": self.continue_current_step,
            "llm_review_data": self.llm_review_data,
            "requires_user_confirmation": self.requires_user_confirmation
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
    STEP_TYPE_LLM_PROCESSING = "llm_processing"  # 需要LLM處理的步驟
    
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
        self._description = ""  # 步驟描述，用於 LLM 上下文
        
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
        
    def set_description(self, description: str) -> 'WorkflowStep':
        """設置步驟描述（用於 LLM 上下文）"""
        self._description = description
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
        """設置自動推進條件"""
        self._auto_advance_condition = condition
        return self
        
    def validate_requirements(self) -> Tuple[bool, str]:
        """驗證步驟要求是否滿足"""
        for req in self._requirements:
            value = self.session.get_data(req.key)
            
            if req.required and value is None:
                return False, req.error_message
            
            if value is not None and req.validator and not req.validator(value):
                return False, req.error_message
        
        return True, ""
        
    def validate_data(self, data: Dict[str, Any]) -> Tuple[bool, str]:
        """驗證數據是否有效"""
        for key, (validator, error_message) in self._data_validators.items():
            if key in data and not validator(data[key]):
                return False, error_message
        
        return True, ""
        
    def should_auto_advance(self) -> bool:
        """判斷是否應該自動推進到下一步"""
        if self._auto_advance_condition:
            result = self._auto_advance_condition()
            debug_log(3, f"[WorkflowStep] {self.id} should_auto_advance (custom): {result}")
            return result
        # 支援 PROCESSING 和 LLM_PROCESSING 兩種自動推進類型
        result = self.step_type in (self.STEP_TYPE_PROCESSING, self.STEP_TYPE_LLM_PROCESSING)
        debug_log(3, f"[WorkflowStep] {self.id} should_auto_advance (type={self.step_type}): {result}")
        return result
        
    @abstractmethod
    def get_prompt(self) -> str:
        """獲取步驟提示訊息"""
        pass
        
    @abstractmethod
    def execute(self, user_input: Any = None) -> StepResult:
        """執行步驟邏輯"""
        pass
        
    def get_status(self) -> Dict[str, Any]:
        """獲取步驟狀態信息"""
        return {
            "id": self.id,
            "type": self.step_type,
            "priority": self.priority,
            "description": self._description,
            "requirements": [(req.key, req.required) for req in self._requirements],
            "can_auto_advance": self.should_auto_advance()
        }


class FileSelectionStep(WorkflowStep):
    """文件選擇步驟，支援多種文件選擇模式"""
    
    def __init__(self, session: WorkflowSession, prompt: str = "請選擇文件:",
                 file_types: Optional[List[str]] = None, multiple: bool = False):
        """
        初始化文件選擇步驟
        
        Args:
            session: 工作流程會話
            prompt: 提示訊息
            file_types: 支援的文件類型列表，如 ['.txt', '.docx']
            multiple: 是否允許多選
        """
        super().__init__(session)
        self.prompt = prompt
        self.file_types = file_types or []
        self.multiple = multiple
        self.set_step_type(self.STEP_TYPE_INTERACTIVE)
        
    def get_prompt(self) -> str:
        """獲取文件選擇提示"""
        prompt = self.prompt
        
        if self.file_types:
            prompt += f"\n支援的文件類型: {', '.join(self.file_types)}"
        
        if self.multiple:
            prompt += "\n可選擇多個文件，以逗號分隔"
        
        return prompt
        
    def execute(self, user_input: Any = None) -> StepResult:
        """執行文件選擇邏輯"""
        if not user_input:
            return StepResult.failure("請選擇文件")
        
        # 解析文件路徑
        file_paths = []
        if isinstance(user_input, str):
            if self.multiple:
                file_paths = [f.strip() for f in user_input.split(',') if f.strip()]
            else:
                file_paths = [user_input.strip()]
        elif isinstance(user_input, list):
            file_paths = user_input
        else:
            return StepResult.failure("無效的文件選擇格式")
        
        # 驗證文件
        valid_files = []
        for file_path in file_paths:
            if not os.path.exists(file_path):
                return StepResult.failure(f"文件不存在: {file_path}")
            
            if self.file_types:
                _, ext = os.path.splitext(file_path)
                if ext.lower() not in [ft.lower() for ft in self.file_types]:
                    return StepResult.failure(f"不支援的文件類型: {ext}")
            
            valid_files.append(file_path)
        
        result_data = {
            "selected_files": valid_files,
            "file_count": len(valid_files)
        }
        
        if len(valid_files) == 1:
            result_data["selected_file"] = valid_files[0]
        
        return StepResult.success(
            f"已選擇 {len(valid_files)} 個文件",
            result_data
        )


class ActionSelectionStep(WorkflowStep):
    """動作選擇步驟，支援從預定義動作列表中選擇"""
    
    def __init__(self, session: WorkflowSession, prompt: str = "請選擇動作:",
                 actions: Optional[List[str]] = None, action_labels: Optional[List[str]] = None):
        """
        初始化動作選擇步驟
        
        Args:
            session: 工作流程會話
            prompt: 提示訊息
            actions: 動作列表
            action_labels: 動作標籤列表，用於顯示
        """
        super().__init__(session)
        self.prompt = prompt
        self.actions = actions or []
        self.action_labels = action_labels or self.actions
        self.set_step_type(self.STEP_TYPE_INTERACTIVE)
        
    def get_prompt(self) -> str:
        """獲取動作選擇提示"""
        prompt = self.prompt
        
        if self.actions:
            prompt += "\n可選動作:"
            for i, (action, label) in enumerate(zip(self.actions, self.action_labels)):
                prompt += f"\n{i + 1}. {label}"
        
        return prompt
        
    def execute(self, user_input: Any = None) -> StepResult:
        """執行動作選擇邏輯"""
        if not user_input:
            return StepResult.failure("請選擇動作")
        
        # 解析選擇
        selected_action = None
        user_str = str(user_input).strip()
        
        # 嘗試按索引選擇
        try:
            index = int(user_str) - 1
            if 0 <= index < len(self.actions):
                selected_action = self.actions[index]
        except ValueError:
            pass
        
        # 嘗試按動作名稱選擇
        if not selected_action:
            for action in self.actions:
                if action.lower() == user_str.lower():
                    selected_action = action
                    break
        
        if not selected_action:
            return StepResult.failure("無效的動作選擇")
        
        return StepResult.success(
            f"已選擇動作: {selected_action}",
            {"selected_action": selected_action}
        )


class ConfirmationStep(WorkflowStep):
    """確認步驟，要求用戶確認操作"""
    
    def __init__(self, session: WorkflowSession, message: str = "確認執行操作?",
                 confirm_text: str = "確認", cancel_text: str = "取消"):
        """
        初始化確認步驟
        
        Args:
            session: 工作流程會話
            message: 確認訊息
            confirm_text: 確認文字
            cancel_text: 取消文字
        """
        super().__init__(session)
        self.message = message
        self.confirm_text = confirm_text
        self.cancel_text = cancel_text
        self.set_step_type(self.STEP_TYPE_INTERACTIVE)
        
    def get_prompt(self) -> str:
        """獲取確認提示"""
        return f"{self.message}\n輸入 '{self.confirm_text}' 確認，或輸入 '{self.cancel_text}' 取消"
        
    def execute(self, user_input: Any = None) -> StepResult:
        """執行確認邏輯"""
        if not user_input:
            return StepResult.failure("請輸入確認或取消")
        
        user_str = str(user_input).strip().lower()
        
        if user_str == self.confirm_text.lower() or user_str == "y" or user_str == "yes":
            return StepResult.success("操作已確認")
        elif user_str == self.cancel_text.lower() or user_str == "n" or user_str == "no":
            return StepResult.cancel_workflow("操作已取消")
        else:
            return StepResult.failure("請輸入有效的確認或取消指令")


class WorkflowDefinition:
    """工作流程定義類，包含步驟、轉換規則和元數據"""
    
    def __init__(self, workflow_type: str, name: str, description: str = "",
                 workflow_mode: WorkflowMode = WorkflowMode.DIRECT,
                 requires_llm_review: bool = False,
                 auto_advance_on_approval: bool = True):
        """
        初始化工作流程定義
        
        Args:
            workflow_type: 工作流程類型
            name: 工作流程名稱
            description: 工作流程描述
            workflow_mode: 工作流程執行模式 (DIRECT/BACKGROUND)
            requires_llm_review: 是否需要 LLM 審核每步驟
            auto_advance_on_approval: LLM 批准後自動推進
        """
        self.workflow_type = workflow_type
        self.name = name
        self.description = description
        self.workflow_mode = workflow_mode
        self.requires_llm_review = requires_llm_review
        self.auto_advance_on_approval = auto_advance_on_approval
        self.steps: Dict[str, WorkflowStep] = {}
        self.transitions: Dict[str, List[Tuple[str, Optional[Callable]]]] = {}
        self.entry_point: Optional[str] = None
        self.metadata: Dict[str, Any] = {}
        
    def add_step(self, step: WorkflowStep) -> 'WorkflowDefinition':
        """添加步驟"""
        self.steps[step.id] = step
        return self
        
    def add_transition(self, from_step: str, to_step: str, 
                      condition: Optional[Callable[[StepResult], bool]] = None) -> 'WorkflowDefinition':
        """
        添加步驟轉換
        
        Args:
            from_step: 源步驟 ID
            to_step: 目標步驟 ID  
            condition: 轉換條件，接受 StepResult 並返回 bool
        """
        if from_step not in self.transitions:
            self.transitions[from_step] = []
        self.transitions[from_step].append((to_step, condition))
        return self
        
    def set_entry_point(self, step_id: str) -> 'WorkflowDefinition':
        """設置入口點"""
        self.entry_point = step_id
        return self
        
    def set_metadata(self, key: str, value: Any) -> 'WorkflowDefinition':
        """設置元數據"""
        self.metadata[key] = value
        return self
        
    def get_next_step(self, current_step: str, result: StepResult) -> Optional[str]:
        """根據當前步驟和結果確定下一步驟"""
        # 優先檢查結果中的指定步驟
        if result.skip_to:
            return result.skip_to
        if result.next_step:
            return result.next_step
        if result.cancel or result.complete:
            return None
            
        # 檢查轉換規則
        if current_step in self.transitions:
            for to_step, condition in self.transitions[current_step]:
                if to_step == "END":
                    return None
                if condition is None or condition(result):
                    return to_step
        
        return None
        
    def validate(self) -> Tuple[bool, str]:
        """驗證工作流程定義"""
        if not self.entry_point:
            return False, "未設置入口點"
        
        if self.entry_point not in self.steps:
            return False, f"入口點步驟不存在: {self.entry_point}"
        
        # 檢查所有轉換目標是否存在
        for from_step, transitions in self.transitions.items():
            for to_step, _ in transitions:
                if to_step != "END" and to_step not in self.steps:
                    return False, f"轉換目標步驟不存在: {to_step} (從 {from_step})"
        
        return True, ""
        
    def get_info(self) -> Dict[str, Any]:
        """獲取工作流程信息"""
        return {
            "workflow_type": self.workflow_type,
            "name": self.name,
            "description": self.description,
            "workflow_mode": self.workflow_mode.value if isinstance(self.workflow_mode, WorkflowMode) else self.workflow_mode,
            "requires_llm_review": self.requires_llm_review,
            "auto_advance_on_approval": self.auto_advance_on_approval,
            "steps": list(self.steps.keys()),
            "entry_point": self.entry_point,
            "metadata": self.metadata
        }


class WorkflowEngine:
    """工作流程引擎，管理工作流程執行"""
    
    def __init__(self, definition: WorkflowDefinition, session: WorkflowSession):
        """
        初始化工作流程引擎
        
        Args:
            definition: 工作流程定義
            session: 工作流程會話
        """
        self.definition = definition
        self.session = session
        self.auto_advance = True  # 🔧 修復：默認啟用自動推進，讓 PROCESSING 步驟自動執行
        self.max_auto_steps = 50  # 防止無限循環，但允許更多步驟
        self.llm_review_timeout = 60  # LLM 審核超時時間（秒）
        self.awaiting_llm_review = False  # 是否正在等待 LLM 審核
        self.pending_review_result: Optional[StepResult] = None  # 待審核的步驟結果
        self.waiting_for_input = False  # 是否正在等待用戶輸入（防止重複請求）
        self.finding_effective_first_step = False  # 🔧 是否正在查找等效第一步（禁用事件發布）
        
        # 🔧 步驟執行狀態追蹤（防止重複觸發長時間運行的步驟）
        self.step_executing = False
        self.executing_step_id = None
        self.step_execution_start_time = None
        
        # 驗證工作流程定義
        is_valid, error = self.definition.validate()
        if not is_valid:
            raise ValueError(f"工作流程定義無效: {error}")
            
        # 初始化會話狀態
        if not self.session.get_data("current_step"):
            self.session.add_data("current_step", self.definition.entry_point)
            self.session.add_data("step_history", [])
            
    def get_current_step(self) -> Optional[WorkflowStep]:
        """獲取當前步驟"""
        current_step_id = self.session.get_data("current_step")
        if current_step_id and current_step_id in self.definition.steps:
            return self.definition.steps[current_step_id]
        return None
    
    def peek_next_step(self) -> Optional[Dict[str, Any]]:
        """預覽下一步資訊（不執行）
        
        Returns:
            Dict with step info or None if workflow complete:
            {
                "step_id": str,
                "step_type": "interactive" | "processing" | "system",
                "requires_input": bool,
                "prompt": str (if interactive)
            }
        """
        current_step_id = self.session.get_data("current_step")
        if not current_step_id:
            return None
        
        # 使用 StepResult.success() 作為 dummy 結果來取得下一步
        dummy_result = StepResult.success("preview")
        next_step_id = self.definition.get_next_step(current_step_id, dummy_result)
        
        if not next_step_id or next_step_id not in self.definition.steps:
            return None  # 工作流即將完成
        
        next_step = self.definition.steps[next_step_id]
        
        return {
            "step_id": next_step_id,
            "step_type": next_step.step_type,
            "requires_input": next_step.step_type == "interactive",
            "prompt": next_step.get_prompt() if next_step.step_type == "interactive" else None
        }
        
    def get_prompt(self) -> str:
        """獲取當前步驟的提示"""
        current_step = self.get_current_step()
        if current_step:
            return current_step.get_prompt()
        return "工作流程已完成"
    
    def is_awaiting_llm_review(self) -> bool:
        """檢查是否正在等待 LLM 審核"""
        return self.awaiting_llm_review
    
    def handle_llm_review_response(self, action: str, modified_params: Optional[Dict[str, Any]] = None) -> StepResult:
        """
        處理 LLM 審核響應
        
        Args:
            action: LLM 決策 ('approve', 'modify', 'cancel')
            modified_params: 修改的參數（當 action='modify' 時）
            
        Returns:
            StepResult: 處理結果
        """
        if not self.awaiting_llm_review or not self.pending_review_result:
            return StepResult.failure("當前沒有待審核的步驟")
        
        debug_log(2, f"[WorkflowEngine] 處理 LLM 審核響應: action={action}")
        
        # 重置審核狀態
        self.awaiting_llm_review = False
        result = self.pending_review_result
        self.pending_review_result = None
        
        if action == 'approve':
            # 批准：繼續工作流程
            info_log("[WorkflowEngine] LLM 已批准步驟，繼續執行")
            
            # 🔧 如果設置了自動推進，則移動到下一步並執行
            if self.definition.auto_advance_on_approval:
                current_step_id = self.session.get_data("current_step")
                
                # ✅ 直接查詢轉換表，不使用 get_next_step（它會被 complete=True 阻擋）
                next_step_id = None
                if current_step_id in self.definition.transitions:
                    transitions = self.definition.transitions[current_step_id]
                    if transitions:
                        # 取第一個轉換（不檢查條件，因為我們已經批准了）
                        next_step_id = transitions[0][0] if transitions[0][0] != "END" else None
                
                debug_log(2, f"[WorkflowEngine] 當前步驟: {current_step_id}, 下一步驟: {next_step_id}")
                
                if next_step_id:
                    # ⚠️ 重要：先執行下一步，再移動 current_step
                    # 這樣如果執行失敗，current_step 仍然指向當前步驟
                    next_step = self.definition.steps.get(next_step_id)
                    
                    # ✅ 執行下一步（如果是自動推進步驟）
                    # 注意：不在這裡檢查 should_skip()，讓步驟的 execute() 方法自行決定
                    # 這樣可以保證互動步驟正確顯示提示
                    if next_step and next_step.should_auto_advance():
                        debug_log(2, f"[WorkflowEngine] 批准後自動執行下一步: {next_step_id}")
                        try:
                            # 🔧 移動到下一步
                            self.session.add_data("current_step", next_step_id)
                            
                            # 執行下一步
                            next_result = next_step.execute()
                            debug_log(2, f"[WorkflowEngine] 下一步執行結果: success={next_result.success}, complete={next_result.complete}")
                            
                            # 🔧 手動記錄步驟歷史（因為直接調用 execute() 不會經過 process_input）
                            step_history = self.session.get_data("step_history", [])
                            step_history.append({
                                "step_id": next_step.id,
                                "timestamp": datetime.datetime.now().isoformat(),
                                "success": next_result.success,
                                "message": next_result.message
                            })
                            self.session.add_data("step_history", step_history)
                            debug_log(3, f"[WorkflowEngine] 已記錄步驟歷史: {next_step.id}")
                            
                            # ⚠️ 重要：返回完整的結果，包括 complete 標誌
                            # 這樣 SYS 模組才能正確判斷工作流是否完成並發布事件
                            # 🔧 修正：工作流完成時（complete=True），不再請求 LLM 審核
                            # 最後一步的結果會由之前的 WORKFLOW_STEP_COMPLETED 事件觸發 LLM 生成最終回應
                            if next_result.complete:
                                debug_log(2, f"[WorkflowEngine] 工作流完成，不請求 LLM 審核")
                                return next_result
                            
                            # 如果需要審核且未完成，包裝成審核請求
                            # 🚫 但不對 wrapper 步驟（如 ConditionalStep）請求審核
                            # ConditionalStep 的類名包含 'Conditional'
                            is_conditional_step = 'Conditional' in next_step.__class__.__name__
                            if self.definition.requires_llm_review and next_result.success and not is_conditional_step:
                                return self._request_llm_review(next_result, next_step)
                            
                            return next_result
                        except Exception as e:
                            error_log(f"[WorkflowEngine] 執行下一步失敗: {e}")
                            import traceback
                            error_log(f"[WorkflowEngine] 堆疊追蹤:\n{traceback.format_exc()}")
                            return StepResult.failure(f"執行下一步失敗: {e}")
                    else:
                        # 下一步不是自動推進，移動到下一步並返回等待用戶輸入
                        self.session.add_data("current_step", next_step_id)
                        
                        # 🔧 如果下一步是互動步驟，設置等待輸入標記
                        from core.working_context import working_context_manager
                        if next_step.step_type == "interactive":  # ✅ 修正：使用字符串而非枚舉
                            debug_log(2, f"[WorkflowEngine] 設置工作流等待輸入標記: {next_step_id}")
                            working_context_manager.set_workflow_waiting_input(True)
                            working_context_manager.set_context_data('workflow_input_context', {
                                'workflow_session_id': self.session.session_id,
                                'workflow_type': self.definition.workflow_type,
                                'step_id': next_step.id,
                                'step_type': next_step.step_type,
                                'optional': getattr(next_step, 'optional', False),
                                'prompt': next_step.get_prompt()
                            })
                        
                        return StepResult.success(
                            "步驟已批准，等待用戶輸入",
                            {"approved": True, "next_step": next_step_id}
                        )
                else:
                    # ✅ 沒有下一步：工作流完成
                    # 但當前步驟（current_step_id）可能是最後一個自動步驟，需要先執行它
                    current_step = self.definition.steps.get(current_step_id)
                    if current_step and current_step.should_auto_advance():
                        debug_log(2, f"[WorkflowEngine] 執行最後的自動步驟: {current_step_id}")
                        try:
                            final_result = current_step.execute()
                            debug_log(2, f"[WorkflowEngine] 最後步驟執行結果: success={final_result.success}, complete={final_result.complete}")
                            # 標記 current_step 為 None（工作流完成）
                            self.session.add_data("current_step", None)
                            # 返回最後步驟的結果（包含所有數據）
                            return final_result
                        except Exception as e:
                            error_log(f"[WorkflowEngine] 執行最後步驟失敗: {e}")
                            self.session.add_data("current_step", None)
                            return StepResult.failure(f"執行最後步驟失敗: {e}")
                    else:
                        # 當前步驟不是自動步驟，直接完成
                        self.session.add_data("current_step", None)
                        return StepResult.complete_workflow("工作流程已完成")
            
            return result
            
        elif action == 'modify':
            # 修改：使用新參數重新執行當前步驟
            if not modified_params:
                return StepResult.failure("修改操作需要提供參數")
            
            info_log(f"[WorkflowEngine] LLM 要求修改參數並重新執行: {modified_params}")
            
            # 更新會話數據
            for key, value in modified_params.items():
                self.session.add_data(key, value)
            
            # 重新執行當前步驟
            current_step = self.get_current_step()
            if not current_step:
                return StepResult.failure("無法重新執行：找不到當前步驟")
            
            try:
                new_result = current_step.execute()
                
                # 如果需要 LLM 審核，再次進入審核流程
                if self.definition.requires_llm_review and new_result.success:
                    return self._request_llm_review(new_result, current_step)
                
                return new_result
                
            except Exception as e:
                error_log(f"[WorkflowEngine] 重新執行步驟錯誤: {e}")
                return StepResult.failure(f"重新執行失敗: {e}")
            
        elif action == 'cancel':
            # 取消：終止工作流程
            info_log("[WorkflowEngine] LLM 取消工作流程")
            self.session.add_data("current_step", None)
            return StepResult.cancel_workflow("LLM 已取消工作流程")
        
        else:
            return StepResult.failure(f"未知的 LLM 審核操作: {action}")
    
    def _should_request_review(self, result: StepResult, current_step: WorkflowStep) -> bool:
        """
        判斷是否應該請求 LLM 審核
        
        Args:
            result: 步驟執行結果
            current_step: 當前步驟
            
        Returns:
            bool: 是否需要審核
        """
        # 工作流不需要 LLM 審核
        if not self.definition.requires_llm_review:
            return False
        
        # Interactive 步驟不需要審核（只是收集輸入）
        if current_step.step_type == current_step.STEP_TYPE_INTERACTIVE:
            return False
        
        # 🚫 Conditional 步驟不需要審核（wrapper 步驟，只負責路由）
        if 'Conditional' in current_step.__class__.__name__:
            return False
        
        # LLM_PROCESSING 步驟：只有第一次執行時需要審核（result.llm_review_data 有值）
        # 第二次執行時（已有結果）不需要審核，直接自動推進
        if current_step.step_type == current_step.STEP_TYPE_LLM_PROCESSING:
            return result.llm_review_data is not None
        
        # 🔧 工作流完成步驟（complete=True）不需要審核
        # 這是最終結果，不應該再讓 LLM 生成回應
        if result.complete:
            return False
        
        # 其他情況需要審核
        return True
    
    def _request_llm_review(self, result: StepResult, current_step: WorkflowStep) -> StepResult:
        """
        請求 LLM 審核步驟結果
        
        Args:
            result: 步驟執行結果
            current_step: 當前步驟
            
        Returns:
            StepResult: 審核請求結果
        """
        debug_log(2, f"[WorkflowEngine] 請求 LLM 審核步驟: {current_step.id}")
        
        # 設置審核狀態
        self.awaiting_llm_review = True
        self.pending_review_result = result
        
        # 🔧 準備審核數據：只有當步驟明確提供 llm_review_data 時才創建
        # 如果步驟返回 llm_review_data=None，表示不需要 LLM 生成回應（例如系統操作步驟）
        review_data = None
        if result.llm_review_data is not None:
            review_data = result.llm_review_data.copy()
            review_data.update({
                "step_id": current_step.id,
                "step_type": current_step.step_type,
                "message": result.message,
                "data": result.data,
                "workflow_type": self.definition.workflow_type,
                "workflow_name": self.definition.name
            })
        
        # 🔧 返回特殊結果，指示需要 LLM 審核
        # ✅ 保留原始的 complete 標誌，讓 SYS 模組能正確判斷工作流是否完成
        return StepResult(
            success=True,
            message="步驟執行完成，等待 LLM 審核",
            data=result.data,
            llm_review_data=review_data,
            requires_user_confirmation=False,
            complete=result.complete  # 保留原始 complete 標誌
        )
        
    def process_input(self, user_input: Any = None) -> StepResult:
        """處理用戶輸入並執行步驟"""
        try:
            return self._process_input_internal(user_input)
        except Exception as e:
            error_log(f"[WorkflowEngine] 工作流執行錯誤: {e}")
            
            # 發布 WORKFLOW_FAILED 事件
            if hasattr(self, '_event_bus') and self._event_bus:
                from core.event_bus import SystemEvent
                self._event_bus.publish(
                    event_type=SystemEvent.WORKFLOW_FAILED,
                    data={
                        "session_id": self.session.session_id,
                        "workflow_type": self.definition.workflow_type,
                        "error_message": str(e),
                        "current_step": self.session.get_data("current_step")
                    },
                    source="sys"
                )
            
            return StepResult.failure(f"工作流執行失敗: {e}")
    
    def _process_input_internal(self, user_input: Any = None) -> StepResult:
        """內部處理用戶輸入並執行步驟"""
        # 檢查是否正在等待 LLM 審核
        if self.awaiting_llm_review:
            return StepResult.failure("工作流程正在等待 LLM 審核，請稍候")
        
        current_step = self.get_current_step()
        if not current_step:
            return StepResult.complete_workflow("工作流程已完成")
        
        # 階段三：如果是 Interactive 步驟且沒有提供輸入，檢查是否可以跳過
        # 🔧 修正：先檢查 should_skip()，如果可以跳過則繼續執行，不要請求輸入
        # 注意：空字符串也視為無效輸入
        debug_log(2, f"[WorkflowEngine] 檢查 Interactive 步驟: {current_step.id if current_step else 'None'}, user_input={user_input is not None}")
        if current_step.step_type == current_step.STEP_TYPE_INTERACTIVE and not user_input:
            # 🆕 檢查步驟是否可以跳過（數據已存在）
            can_skip = hasattr(current_step, 'should_skip') and current_step.should_skip()
            debug_log(2, f"[WorkflowEngine] can_skip 檢查結果: {can_skip}")
            if can_skip:
                debug_log(2, f"[WorkflowEngine] Interactive 步驟可以跳過（數據已存在），繼續執行: {current_step.id}")
                # 繼續執行步驟，不要請求輸入
            else:
                # 如果已經在等待輸入，不要重複請求，直接返回當前提示
                if self.waiting_for_input:
                    return StepResult(
                        success=False,
                        message=current_step.get_prompt(),
                        data={"requires_input": True, "step_id": current_step.id, "already_waiting": True}
                    )
                
                try:
                    from core.event_bus import event_bus, SystemEvent
                    from core.working_context import working_context_manager
                    
                    # 設置等待輸入標記
                    self.waiting_for_input = True
                    
                    # ✅ 設置 working_context，標記工作流正在等待輸入
                    working_context_manager.set_workflow_waiting_input(True)
                    working_context_manager.set_context_data('workflow_input_context', {
                        'workflow_session_id': self.session.session_id,
                        'workflow_type': self.definition.workflow_type,
                        'step_id': current_step.id,
                        'step_type': current_step.step_type,
                        'optional': getattr(current_step, 'optional', False),
                        'prompt': current_step.get_prompt()
                    })
                    
                    # 檢查是否已經為此步驟發布過事件（防止重複）
                    last_input_request = self.session.get_data("_last_input_request_step")
                    if last_input_request != current_step.id:
                        self.session.add_data("_last_input_request_step", current_step.id)
                        
                        # 發布工作流需要輸入事件
                        event_bus.publish(
                            SystemEvent.WORKFLOW_REQUIRES_INPUT,
                        {
                            "workflow_type": self.definition.workflow_type,
                            "session_id": self.session.session_id,
                            "step_id": current_step.id,
                            "step_type": current_step.step_type,
                            "optional": getattr(current_step, 'optional', False),
                            "prompt": current_step.get_prompt(),
                            "timestamp": time.time()
                        },
                        source="WorkflowEngine"
                    )
                    
                    debug_log(2, f"[WorkflowEngine] Interactive 步驟需要輸入: {current_step.id}")
                    
                    # 返回需要輸入的結果
                    return StepResult(
                        success=False,
                        message=current_step.get_prompt(),
                        data={"requires_input": True, "step_id": current_step.id}
                    )
                    
                except Exception as e:
                    error_log(f"[WorkflowEngine] 發布輸入請求事件失敗: {e}")
                    # 繼續執行，使用傳統流程
            
        # 驗證步驟要求
        is_valid, error = current_step.validate_requirements()
        if not is_valid:
            return StepResult.failure(error)
        
        # 🔧 特殊處理：LLM_PROCESSING 步驟
        debug_log(3, f"[WorkflowEngine] 檢查步驟類型: {current_step.step_type}, 是否為LLM_PROCESSING: {current_step.step_type == current_step.STEP_TYPE_LLM_PROCESSING}")
        if current_step.step_type == current_step.STEP_TYPE_LLM_PROCESSING:
            debug_log(2, f"[WorkflowEngine] 檢測到 LLM_PROCESSING 步驟: {current_step.id}")
            
            # 檢查是否已有LLM處理結果
            output_key = getattr(current_step, '_output_data_key', None)
            if output_key and self.session.get_data(output_key) is not None:
                debug_log(2, f"[WorkflowEngine] LLM處理結果已存在，繼續執行步驟")
                # 已有結果，正常執行步驟（會直接返回成功）
                try:
                    result = current_step.execute(user_input)
                except Exception as e:
                    error_log(f"[WorkflowEngine] LLM處理步驟執行失敗: {e}")
                    return StepResult.failure(f"LLM處理步驟執行失敗: {e}")
            else:
                debug_log(2, f"[WorkflowEngine] 首次執行LLM處理步驟，發布事件給LLM模組")
                # 第一次執行，請求LLM處理
                try:
                    result = current_step.execute(user_input)
                    
                    # 檢查是否包含LLM處理請求
                    if result.llm_review_data and result.llm_review_data.get("requires_llm_processing"):
                        debug_log(2, f"[WorkflowEngine] 發布 LLM 處理請求事件")
                        
                        from core.event_bus import event_bus, SystemEvent
                        
                        # 發布事件給LLM模組
                        event_bus.publish(
                            SystemEvent.WORKFLOW_STEP_COMPLETED,
                            {
                                "session_id": self.session.session_id,
                                "workflow_type": self.definition.workflow_type,
                                "step_result": result.to_dict(),
                                "requires_llm_processing": True,
                                "llm_request_data": result.llm_review_data.get("request_data"),
                                "timestamp": time.time()
                            },
                            source="sys"
                        )
                        
                        # 返回等待LLM處理的結果
                        return StepResult(
                            success=False,
                            message=f"等待LLM處理: {result.llm_review_data.get('task', '未知任務')}",
                            data={"requires_llm_processing": True, "step_id": current_step.id}
                        )
                    
                except Exception as e:
                    error_log(f"[WorkflowEngine] LLM處理步驟執行失敗: {e}")
                    return StepResult.failure(f"LLM處理步驟執行失敗: {e}")
        
        # 執行步驟（有實際輸入時，重置等待標記）
        # 注意：空字符串不視為有效輸入
        if user_input:
            self.waiting_for_input = False
            # ✅ 清除 working_context 中的等待標記和去重標記
            from core.working_context import working_context_manager
            working_context_manager.set_workflow_waiting_input(False)
            working_context_manager.set_context_data('workflow_input_context', None)
            # 清除去重標記，允許下一個 Interactive 步驟發布事件
            self.session.add_data("_last_input_request_step", None)
            
        try:
            result = current_step.execute(user_input)
            
            # 階段三：如果是 Interactive 步驟且執行成功，發布輸入完成事件
            if current_step.step_type == current_step.STEP_TYPE_INTERACTIVE and result.success:
                try:
                    from core.event_bus import event_bus, SystemEvent
                    
                    event_bus.publish(
                        SystemEvent.WORKFLOW_INPUT_COMPLETED,
                        {
                            "workflow_type": self.definition.workflow_type,
                            "session_id": self.session.session_id,
                            "step_id": current_step.id,
                            "timestamp": time.time()
                        },
                        source="WorkflowEngine"
                    )
                    
                    debug_log(2, f"[WorkflowEngine] Interactive 步驟輸入完成: {current_step.id}")
                    
                except Exception as e:
                    error_log(f"[WorkflowEngine] 發布輸入完成事件失敗: {e}")
            
            # 記錄步驟歷史
            step_history = self.session.get_data("step_history", [])
            step_history.append({
                "step_id": current_step.id,
                "timestamp": datetime.datetime.now().isoformat(),
                "success": result.success,
                "message": result.message
            })
            self.session.add_data("step_history", step_history)
            
            # 處理結果
            if result.cancel:
                self.session.add_data("current_step", None)
                return result
            elif result.complete:
                # 🔧 工作流完成：直接返回結果，不需要 LLM 審核
                # 最後一個步驟已經是最終結果，不應該再讓 LLM 生成回應
                self.session.add_data("current_step", None)
                debug_log(2, f"[WorkflowEngine] 工作流完成，不請求 LLM 審核")
                return result
            elif result.success:
                # 更新會話數據
                if result.data:
                    for key, value in result.data.items():
                        self.session.add_data(key, value)
                
                # **檢查是否需要 LLM 審核**
                # 🔧 Interactive 步驟不需要審核，因為它們只是收集輸入參數
                # 審核應該在下一個實際執行步驟完成後進行
                # 
                # ⚠️ Interactive → Interactive 轉換的特殊處理：
                # 不在這裡立即審核（會導致嵌套 LLM.handle() 調用），
                # 而是發布特殊事件讓 LLM 在下一個循環生成提示
                debug_log(2, f"[Workflow] 查找下一步：current={current_step.id}, result.complete={result.complete}, result.cancel={result.cancel}, result.next_step={result.next_step}")
                next_step_id = self.definition.get_next_step(current_step.id, result)
                debug_log(2, f"[Workflow] get_next_step 返回: next_step_id={next_step_id}")
                next_step = self.definition.steps.get(next_step_id) if next_step_id else None
                
                # 使用統一的審核判斷方法
                if self._should_request_review(result, current_step):
                    # 🔧 在請求審核之前更新 current_step，避免 SystemLoop 重複執行
                    if next_step_id:
                        self.session.add_data("current_step", next_step_id)
                    return self._request_llm_review(result, current_step)
                
                # 檢查是否需要繼續在當前步驟
                if result.continue_current_step:
                    # 不改變當前步驟，但如果是自動推進模式且當前步驟支持自動推進，則繼續執行
                    if self.auto_advance and current_step.should_auto_advance():
                        return self._auto_advance_current_step(result)
                    else:
                        # 返回結果，等待下次調用
                        return result
                
                # 自動推進或等待下一次調用
                if next_step_id:
                    self.session.add_data("current_step", next_step_id)
                    debug_log(2, f"[WorkflowEngine] 已更新 current_step -> {next_step_id}")
                    
                    # 🔧 如果下一步是 Interactive 步驟，發布需要輸入事件
                    if next_step and next_step.step_type == next_step.STEP_TYPE_INTERACTIVE:
                        # 🔧 檢查步驟是否應該被跳過（數據已存在）
                        should_skip_next = hasattr(next_step, 'should_skip') and next_step.should_skip()
                        
                        if not should_skip_next:
                            try:
                                from core.event_bus import event_bus, SystemEvent
                                
                                # 🆕 Interactive → Interactive 轉換：需要 LLM 生成下一步提示
                                # 🔧 但如果正在查找等效第一步，不要發布事件
                                if current_step.step_type == current_step.STEP_TYPE_INTERACTIVE and self.definition.requires_llm_review and not self.finding_effective_first_step:
                                    # 發布步驟完成事件，讓 LLM 生成提示
                                    event_bus.publish(
                                        SystemEvent.WORKFLOW_STEP_COMPLETED,
                                        {
                                            "session_id": self.session.session_id,
                                            "workflow_type": self.definition.workflow_type,
                                            "step_result": result.to_dict(),
                                            "requires_llm_review": True,
                                            "llm_review_data": {
                                                "requires_user_response": True,
                                                "should_end_session": False,
                                            },
                                            "next_step_info": {
                                                "step_id": next_step.id,
                                                "step_type": next_step.step_type,
                                                "requires_input": True,
                                                "prompt": next_step.get_prompt()
                                            }
                                        },
                                        source="sys"
                                    )
                                    debug_log(2, f"[WorkflowEngine] Interactive → Interactive: 已發布步驟完成事件供 LLM 生成提示")
                                
                                # 檢查是否已經為此步驟發布過事件（防止重複）
                                last_input_request = self.session.get_data("_last_input_request_step")
                                if last_input_request != next_step.id:
                                    self.session.add_data("_last_input_request_step", next_step.id)
                                    
                                    # 發布工作流需要輸入事件
                                    event_bus.publish(
                                        SystemEvent.WORKFLOW_REQUIRES_INPUT,
                                    {
                                        "workflow_type": self.definition.workflow_type,
                                        "session_id": self.session.session_id,
                                        "step_id": next_step.id,
                                        "step_type": next_step.step_type,
                                        "optional": getattr(next_step, 'optional', False),
                                        "prompt": next_step.get_prompt(),
                                        "timestamp": time.time()
                                    },
                                    source="WorkflowEngine"
                                )
                                
                                debug_log(2, f"[WorkflowEngine] 推進到下一個 Interactive 步驟: {next_step.id}")
                            except Exception as e:
                                error_log(f"[WorkflowEngine] 發布下一步輸入請求事件失敗: {e}")
                        else:
                            debug_log(2, f"[WorkflowEngine] Interactive 步驟 {next_step.id} 將被跳過（數據已存在），不發布輸入請求")
                            # 🔧 步驟會被跳過，需要調用 _auto_advance 來執行步驟並繼續推進
                            if self.auto_advance:
                                debug_log(2, f"[WorkflowEngine] 調用 _auto_advance 來執行跳過的步驟並繼續推進")
                                return self._auto_advance(result)
                    
                    # 檢查下一步是否可以自動推進
                    debug_log(2, f"[WorkflowEngine] 檢查自動推進: auto_advance={self.auto_advance}, next_step={next_step.id if next_step else None}, step_type={next_step.step_type if next_step else None}")
                    if next_step:
                        should_advance = next_step.should_auto_advance()
                        debug_log(2, f"[WorkflowEngine] should_auto_advance() = {should_advance}")
                    
                    if self.auto_advance and next_step and next_step.should_auto_advance():
                        debug_log(2, f"[WorkflowEngine] 開始自動推進到 {next_step.id}")
                        return self._auto_advance(result)
                else:
                    self.session.add_data("current_step", None)
                    return StepResult.complete_workflow("工作流程已完成")
                    
            return result
            
        except Exception as e:
            error_log(f"[WorkflowEngine] 步驟執行錯誤: {e}")
            return StepResult.failure(f"步驟執行錯誤: {e}")
            
    def _auto_advance_current_step(self, last_result: StepResult) -> StepResult:
        """自動推進當前步驟（用於循環步驟）"""
        auto_steps = 0
        current_result = last_result
        max_loop_steps = 100  # 循環步驟允許更多執行次數
        
        while auto_steps < max_loop_steps:
            current_step_id = self.session.get_data("current_step")
            if not current_step_id:
                break
                
            current_step = self.definition.steps.get(current_step_id)
            if not current_step:
                break
                
            # 對於循環步驟，不需要檢查 should_auto_advance，直接執行
            # 支援 PROCESSING 和 LLM_PROCESSING 兩種類型
            if current_step.step_type not in (current_step.STEP_TYPE_PROCESSING, current_step.STEP_TYPE_LLM_PROCESSING):
                # 非處理步驟不應該進入這個方法
                return current_result
                
            # 🔧 特殊處理：LLM_PROCESSING 步驟
            if current_step.step_type == current_step.STEP_TYPE_LLM_PROCESSING:
                debug_log(2, f"[WorkflowEngine] [Auto-Advance] 檢測到 LLM_PROCESSING 步驟: {current_step.id}")
                
                # 檢查是否已有LLM處理結果
                output_key = getattr(current_step, '_output_data_key', None)
                if output_key and self.session.get_data(output_key) is not None:
                    debug_log(2, f"[WorkflowEngine] [Auto-Advance] LLM處理結果已存在，繼續執行步驟")
                    # 已有結果，正常執行步驟（會直接返回成功）
                    step_result = current_step.execute()
                else:
                    debug_log(2, f"[WorkflowEngine] [Auto-Advance] 首次執行LLM處理步驟，發布事件給LLM模組")
                    # 第一次執行，請求LLM處理
                    step_result = current_step.execute()
                    
                    # 檢查是否包含LLM處理請求
                    if step_result.llm_review_data and step_result.llm_review_data.get("requires_llm_processing"):
                        debug_log(2, f"[WorkflowEngine] [Auto-Advance] 發布 LLM 處理請求事件")
                        
                        from core.event_bus import event_bus, SystemEvent
                        
                        # 發布事件給LLM模組
                        event_bus.publish(
                            SystemEvent.WORKFLOW_STEP_COMPLETED,
                            {
                                "session_id": self.session.session_id,
                                "workflow_type": self.definition.workflow_type,
                                "step_result": step_result.to_dict(),
                                "requires_llm_processing": True,
                                "llm_request_data": step_result.llm_review_data.get("request_data"),
                                "timestamp": time.time()
                            },
                            source="sys"
                        )
                        
                        # 返回等待LLM處理的結果（保留 complete 標記以便退出循環）
                        return StepResult(
                            success=False,
                            message=f"等待LLM處理: {step_result.llm_review_data.get('task', '未知任務')}",
                            data={"requires_llm_processing": True, "step_id": current_step.id},
                            complete=True  # 標記為完成，避免被視為錯誤
                        )
            else:
                # 顯示當前步驟的提示（如果有且不為空）
                prompt = current_step.get_prompt()
                if prompt and prompt.strip() and prompt != "處理中...":
                    print(f"🔄 {prompt}")
                    
                # 執行當前步驟
                step_result = current_step.execute()
            auto_steps += 1
            
            # 更新會話數據
            if step_result.data:
                for key, value in step_result.data.items():
                    self.session.add_data(key, value)
            
            # 記錄步驟歷史
            step_history = self.session.get_data("step_history", [])
            step_history.append({
                "step_id": current_step.id,
                "timestamp": datetime.datetime.now().isoformat(),
                "success": step_result.success,
                "message": step_result.message
            })
            self.session.add_data("step_history", step_history)
            
            # 檢查結果類型
            if step_result.cancel or step_result.complete:
                self.session.add_data("current_step", None)
                return step_result
            elif step_result.continue_current_step:
                # 繼續在當前步驟，但更新結果
                current_result = step_result
                continue
            elif not step_result.success:
                return step_result
            else:
                # 步驟成功完成，更新結果並繼續循環（可能有自動步驟需要執行）
                current_result = step_result
                # 不要 break，繼續循環檢查是否有下一個自動步驟
                
        # 如果達到最大循環次數，返回警告
        if auto_steps >= max_loop_steps:
            return StepResult.failure(f"循環步驟執行次數超過限制 ({max_loop_steps})")
            
        return current_result
    
    def _auto_advance(self, last_result: StepResult) -> StepResult:
        """自動推進工作流程"""
        auto_steps = 0
        current_result = last_result
        
        debug_log(2, f"[WorkflowEngine] [_auto_advance] 開始自動推進，最大步驟數: {self.max_auto_steps}")
        
        while auto_steps < self.max_auto_steps:
            current_step_id = self.session.get_data("current_step")
            debug_log(2, f"[WorkflowEngine] [_auto_advance] 循環 {auto_steps}: 當前步驟ID = {current_step_id}")
            
            if not current_step_id:
                debug_log(2, f"[WorkflowEngine] [_auto_advance] 無當前步驟，退出循環")
                break
                
            current_step = self.definition.steps.get(current_step_id)
            if not current_step:
                debug_log(2, f"[WorkflowEngine] [_auto_advance] 找不到步驟定義: {current_step_id}")
                return current_result
            
            debug_log(2, f"[WorkflowEngine] [_auto_advance] 檢查步驟 {current_step_id} (類型: {current_step.step_type})")
            
            # ✅ 注意：不在這裡檢查 should_skip()，讓步驟的 execute() 方法自行決定
            # 這樣可以保證跳過邏輯與步驟執行邏輯一致
            
            should_advance = current_step.should_auto_advance()
            debug_log(2, f"[WorkflowEngine] [_auto_advance] should_auto_advance() = {should_advance}")
            
            if not should_advance:
                # 如果當前步驟不能自動推進，檢查是否為 INTERACTIVE 步驟需要輸入
                if current_step.step_type == current_step.STEP_TYPE_INTERACTIVE:
                    # 🔧 先檢查是否可以跳過（數據已存在）
                    can_skip = hasattr(current_step, 'should_skip') and current_step.should_skip()
                    if can_skip:
                        debug_log(2, f"[WorkflowEngine] [_auto_advance] Interactive 步驟可以跳過（數據已存在），繼續執行: {current_step.id}")
                        # 不發布輸入請求，讓後續邏輯執行步驟並自動推進
                    else:
                        debug_log(2, f"[WorkflowEngine] [_auto_advance] Interactive 步驟需要輸入，發布事件")
                        
                        try:
                            from core.event_bus import event_bus, SystemEvent
                            
                            # 檢查是否已經為此步驟發布過事件（防止重複）
                            last_input_request = self.session.get_data("_last_input_request_step")
                            if last_input_request != current_step.id:
                                self.session.add_data("_last_input_request_step", current_step.id)
                                
                                # 🔧 發布 WORKFLOW_STEP_COMPLETED 事件讓 LLM 生成提示
                                # 構建步驟結果（表示成功推進到 Interactive 步驟）
                                # 但在查找等效第一步時不發布（避免重複提示）
                                if self.definition.requires_llm_review and not getattr(self, 'finding_effective_first_step', False):
                                    event_bus.publish(
                                        SystemEvent.WORKFLOW_STEP_COMPLETED,
                                        {
                                            "session_id": self.session.session_id,
                                            "workflow_type": self.definition.workflow_type,
                                            "step_result": current_result.to_dict() if current_result else {},
                                            "requires_llm_review": True,
                                            "llm_review_data": {
                                                "requires_user_response": True,
                                                "should_end_session": False,
                                            },
                                            "next_step_info": {
                                                "step_id": current_step.id,
                                                "step_type": current_step.step_type,
                                                "requires_input": True,
                                                "prompt": current_step.get_prompt()
                                            }
                                        },
                                        source="sys"
                                    )
                                    debug_log(2, f"[WorkflowEngine] [_auto_advance] 已發布 WORKFLOW_STEP_COMPLETED 事件供 LLM 生成提示")
                                elif getattr(self, 'finding_effective_first_step', False):
                                    debug_log(2, f"[WorkflowEngine] [_auto_advance] 跳過發布 WORKFLOW_STEP_COMPLETED（正在查找等效第一步）")
                                
                                # 發布 WORKFLOW_REQUIRES_INPUT 事件
                                event_bus.publish(
                                    SystemEvent.WORKFLOW_REQUIRES_INPUT,
                                {
                                    "workflow_type": self.definition.workflow_type,
                                    "session_id": self.session.session_id,
                                    "step_id": current_step.id,
                                    "step_type": current_step.step_type,
                                    "optional": getattr(current_step, 'optional', False),
                                    "prompt": current_step.get_prompt(),
                                    "timestamp": time.time()
                                },
                                source="WorkflowEngine"
                            )
                        except Exception as e:
                            error_log(f"[WorkflowEngine] 發布 WORKFLOW_REQUIRES_INPUT 事件失敗: {e}")
                        
                        # 如果發布了輸入請求，返回之前的結果
                        debug_log(2, f"[WorkflowEngine] [_auto_advance] 步驟不能自動推進，退出")
                        return current_result
                else:
                    # 非 Interactive 步驟且不能自動推進，返回之前的結果
                    debug_log(2, f"[WorkflowEngine] [_auto_advance] 步驟不能自動推進，退出")
                    return current_result
                
            # 🔧 特殊處理：LLM_PROCESSING 步驟
            if current_step.step_type == current_step.STEP_TYPE_LLM_PROCESSING:
                debug_log(2, f"[WorkflowEngine] [_auto_advance] 檢測到 LLM_PROCESSING 步驟: {current_step.id}")
                
                # 檢查是否已有LLM處理結果
                output_key = getattr(current_step, '_output_data_key', None)
                if output_key and self.session.get_data(output_key) is not None:
                    debug_log(2, f"[WorkflowEngine] [_auto_advance] LLM處理結果已存在，繼續執行步驟")
                    # 已有結果，正常執行步驟（會直接返回成功）
                    step_result = current_step.execute()
                else:
                    debug_log(2, f"[WorkflowEngine] [_auto_advance] 首次執行LLM處理步驟，發布事件給LLM模組")
                    # 第一次執行，請求LLM處理
                    step_result = current_step.execute()
                    
                    # 檢查是否包含LLM處理請求
                    if step_result.llm_review_data and step_result.llm_review_data.get("requires_llm_processing"):
                        debug_log(2, f"[WorkflowEngine] [_auto_advance] 發布 LLM 處理請求事件")
                        
                        from core.event_bus import event_bus, SystemEvent
                        
                        # 發布事件給LLM模組
                        event_bus.publish(
                            SystemEvent.WORKFLOW_STEP_COMPLETED,
                            {
                                "session_id": self.session.session_id,
                                "workflow_type": self.definition.workflow_type,
                                "step_result": step_result.to_dict(),
                                "requires_llm_processing": True,
                                "llm_request_data": step_result.llm_review_data.get("request_data"),
                                "timestamp": time.time()
                            },
                            source="sys"
                        )
                        
                        # 返回等待LLM處理的結果
                        return StepResult(
                            success=False,
                            message=f"等待LLM處理: {step_result.llm_review_data.get('task', '未知任務')}",
                            data={"requires_llm_processing": True, "step_id": current_step.id}
                        )
            else:
                # 顯示當前步驟的提示（如果有且不為空）
                prompt = current_step.get_prompt()
                if prompt and prompt.strip() and prompt != "處理中...":
                    print(f"🔄 {prompt}")
                    
                # 執行當前步驟
                step_result = current_step.execute()
            
            auto_steps += 1
            
            # 更新會話數據
            if step_result.data:
                for key, value in step_result.data.items():
                    self.session.add_data(key, value)
            
            # 記錄步驟歷史
            step_history = self.session.get_data("step_history", [])
            step_history.append({
                "step_id": current_step.id,
                "timestamp": datetime.datetime.now().isoformat(),
                "success": step_result.success,
                "message": step_result.message
            })
            self.session.add_data("step_history", step_history)
            
            # 檢查結果類型
            if step_result.cancel:
                self.session.add_data("current_step", None)
                return step_result
            elif step_result.complete:
                # 🔧 工作流完成：直接返回結果，不需要 LLM 審核
                # 最後一個步驟已經是最終結果，不應該再讓 LLM 生成回應
                self.session.add_data("current_step", None)
                debug_log(2, f"[WorkflowEngine] [_auto_advance] 工作流完成，不請求 LLM 審核")
                return step_result
            elif step_result.continue_current_step:
                # 繼續在當前步驟，但更新結果
                current_result = step_result
                continue
            elif not step_result.success:
                return step_result
            
            # 移動到下一步
            next_step_id = self.definition.get_next_step(current_step_id, step_result)
            if next_step_id:
                # 清除當前步驟的循環標記
                loop_continue_key = f"loop_continue_{current_step_id}"
                self.session.add_data(loop_continue_key, False)
                
                self.session.add_data("current_step", next_step_id)
                current_result = step_result
            else:
                # 清除當前步驟的循環標記
                loop_continue_key = f"loop_continue_{current_step_id}"
                self.session.add_data(loop_continue_key, False)
                
                self.session.add_data("current_step", None)
                return step_result
                
        return current_result
        
    def reset(self) -> None:
        """重置工作流程到初始狀態"""
        self.session.add_data("current_step", self.definition.entry_point)
        self.session.add_data("step_history", [])
        
    def get_status(self) -> Dict[str, Any]:
        """獲取工作流程狀態"""
        current_step = self.get_current_step()
        return {
            "workflow_type": self.definition.workflow_type,
            "workflow_name": self.definition.name,
            "workflow_mode": self.definition.workflow_mode.value if isinstance(self.definition.workflow_mode, WorkflowMode) else self.definition.workflow_mode,
            "requires_llm_review": self.definition.requires_llm_review,
            "current_step": current_step.id if current_step else None,
            "is_complete": current_step is None,
            "awaiting_llm_review": self.awaiting_llm_review,
            "step_history": self.session.get_data("step_history", []),
            "auto_advance": self.auto_advance
        }


class StepTemplate:
    """步驟模板類，提供常用步驟的快速創建方法"""
    
    @staticmethod
    def create_input_step(session: WorkflowSession, step_id: str, prompt: str,
                         validator: Optional[Callable[[str], Tuple[bool, str]]] = None,
                         required_data: Optional[List[str]] = None,
                         optional: bool = False,
                         skip_if_data_exists: bool = False,
                         description: str = "") -> WorkflowStep:
        """
        創建輸入步驟
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            prompt: 提示訊息
            validator: 驗證函數，返回 (是否有效, 錯誤訊息)
            required_data: 必要數據列表
            optional: 是否為可選輸入，可選輸入允許空值
            skip_if_data_exists: 是否在數據已存在時跳過步驟（連提示都不需要）
                - True: 接受初始數據模式（數據存在就跳過）
                - False: 接受沒有輸入模式（仍然詢問用戶）
                - optional=True + skip_if_data_exists=True: 兩者皆有模式
            description: 步驟描述，用於 LLM 上下文
        """
        class InputStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                if description:
                    self.set_description(description)
                
                if required_data:
                    for req in required_data:
                        self.add_requirement(req)
            
            def should_skip(self) -> bool:
                """檢查是否應該跳過此步驟（因為數據已存在）
                
                跳過條件：
                1. skip_if_data_exists=True
                2. session 中已有該步驟的數據
                3. 數據不是 None 且不是空字符串
                
                注意：空字符串不算有效數據，不會觸發跳過
                """
                if not skip_if_data_exists:
                    return False
                
                # 檢查 session 中是否已有此步驟的**有效**數據
                existing_data = self.session.get_data(step_id, None)
                
                # None 或空字符串都不算有效數據
                if existing_data is None:
                    return False
                    
                # 轉換為字符串並去除空白
                data_str = str(existing_data).strip()
                if not data_str:
                    return False
                
                # 有有效數據，跳過此步驟
                debug_log(2, f"[Workflow] 步驟 {step_id} 跳過：數據已存在 ({existing_data})")
                return True
                        
            def get_prompt(self) -> str:
                if optional:
                    return f"{prompt} (留空跳過)"
                return prompt
                
            def execute(self, user_input: Any = None) -> StepResult:
                # ✅ 檢查是否應該跳過（數據已存在且 skip_if_data_exists=True）
                if self.should_skip():
                    existing_data = self.session.get_data(step_id, "")
                    return StepResult.success(
                        f"使用現有數據: {existing_data}",
                        {step_id: existing_data}
                    )
                
                if not user_input:
                    if optional:
                        return StepResult.success(
                            "跳過輸入",
                            {step_id: ""}
                        )
                    return StepResult.failure("請輸入內容")
                
                input_str = str(user_input).strip()
                if not input_str:
                    if optional:
                        return StepResult.success(
                            "跳過輸入",
                            {step_id: ""}
                        )
                    return StepResult.failure("輸入內容不能為空")
                
                # 驗證輸入
                if validator:
                    is_valid, error_msg = validator(input_str)
                    if not is_valid:
                        return StepResult.failure(error_msg)
                
                return StepResult.success(
                    f"已輸入: {input_str}",
                    {step_id: input_str}
                )
                
        return InputStep(session)
        
    @staticmethod
    def create_confirmation_step(session: WorkflowSession, step_id: str, 
                                message: Union[str, Callable[[], str]],
                                confirm_message: str = "操作已確認",
                                cancel_message: str = "操作已取消",
                                required_data: Optional[List[str]] = None,
                                description: str = "") -> WorkflowStep:
        """
        創建確認步驟
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            message: 確認訊息或生成訊息的函數
            confirm_message: 確認時的回應訊息
            cancel_message: 取消時的回應訊息
            required_data: 必要數據列表
            description: 步驟描述，用於 LLM 上下文
        """
        class ConfirmationStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                if description:
                    self.set_description(description)
                
                if required_data:
                    for req in required_data:
                        self.add_requirement(req)
                        
            def get_prompt(self) -> str:
                msg = message() if callable(message) else message
                return f"{msg}\n輸入 '確認' 或 'y' 繼續，輸入 '取消' 或 'n' 結束"
                
            def execute(self, user_input: Any = None) -> StepResult:
                if not user_input:
                    return StepResult.failure("請輸入確認或取消")
                
                user_str = str(user_input).strip().lower()
                
                if user_str in ["確認", "y", "yes", "ok"]:
                    # ✅ 保存確認狀態到 session（使用 step_id 作為鍵）
                    self.session.add_data(step_id, True)
                    return StepResult.success(confirm_message)
                elif user_str in ["取消", "n", "no", "cancel"]:
                    # ✅ 保存取消狀態到 session
                    self.session.add_data(step_id, False)
                    return StepResult.cancel_workflow(cancel_message)
                else:
                    return StepResult.failure("請輸入 '確認' 或 '取消'")
                    
        return ConfirmationStep(session)
        
    @staticmethod
    def create_processing_step(session: WorkflowSession, step_id: str,
                              processor: Callable[[WorkflowSession], StepResult],
                              required_data: Optional[List[str]] = None,
                              auto_advance: bool = True,  # 🔧 修正：PROCESSING 步驟默認應該自動推進
                              description: str = "") -> WorkflowStep:
        """
        創建處理步驟
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            processor: 處理函數，接受 session 並返回 StepResult
            required_data: 必要數據列表
            auto_advance: 是否自動推進到下一步（默認 True）
            description: 步驟描述，用於 LLM 上下文
        """
        class ProcessingStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_PROCESSING)
                self._auto_advance = auto_advance
                if description:
                    self.set_description(description)
                
                if required_data:
                    for req in required_data:
                        self.add_requirement(req)
                        
            def get_prompt(self) -> str:
                return "處理中..."
                
            def execute(self, user_input: Any = None) -> StepResult:
                return processor(self.session)
                
            def should_auto_advance(self) -> bool:
                return self._auto_advance
                
        return ProcessingStep(session)
        
    @staticmethod
    def create_auto_step(session: WorkflowSession, step_id: str,
                        processor: Callable[[WorkflowSession], StepResult],
                        required_data: Optional[List[str]] = None,
                        prompt: str = "自動處理中...",
                        description: str = "") -> WorkflowStep:
        """
        創建自動步驟（總是自動推進）
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            processor: 處理函數，接受 session 並返回 StepResult
            required_data: 必要數據列表
            prompt: 處理時的提示訊息
            description: 步驟描述，用於 LLM 上下文
        """
        class AutoStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_PROCESSING)
                self._prompt = prompt
                if description:
                    self.set_description(description)
                
                if required_data:
                    for req in required_data:
                        self.add_requirement(req)
                        
            def get_prompt(self) -> str:
                return self._prompt
                
            def execute(self, user_input: Any = None) -> StepResult:
                return processor(self.session)
                
            def should_auto_advance(self) -> bool:
                return True
                
        return AutoStep(session)
        
    @staticmethod
    def create_loop_step(session: WorkflowSession, step_id: str,
                        processor: Callable[[WorkflowSession], StepResult],
                        condition: Callable[[WorkflowSession], bool],
                        required_data: Optional[List[str]] = None,
                        prompt: str = "循環處理中...") -> WorkflowStep:
        """
        創建循環步驟（根據條件自動重複執行）
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            processor: 處理函數，接受 session 並返回 StepResult
            condition: 循環條件函數，返回 True 則繼續循環
            required_data: 必要數據列表
            prompt: 處理時的提示訊息
        """
        class LoopStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_PROCESSING)
                self._prompt = prompt
                self._condition = condition
                
                if required_data:
                    for req in required_data:
                        self.add_requirement(req)
                        
            def get_prompt(self) -> str:
                return self._prompt
                
            def execute(self, user_input: Any = None) -> StepResult:
                result = processor(self.session)
                
                # 如果結果要求完成工作流程或取消，直接返回
                if result.complete or result.cancel:
                    return result
                
                # 檢查是否需要繼續循環
                if result.success and self._condition(self.session):
                    # 繼續循環，不推進到下一步
                    return StepResult.success(
                        result.message,
                        result.data,
                        continue_current_step=True
                    )
                
                return result
                
            def should_auto_advance(self) -> bool:
                return True
                
        return LoopStep(session)
        
    @staticmethod
    def create_selection_step(session: WorkflowSession, step_id: str, prompt: str,
                             options: List[str], labels: Optional[List[str]] = None,
                             required_data: Optional[List[str]] = None,
                             skip_if_data_exists: bool = False) -> WorkflowStep:
        """
        創建選擇步驟
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            prompt: 提示訊息
            options: 選項列表
            labels: 選項標籤列表
            required_data: 必要數據列表
            skip_if_data_exists: 是否在數據已存在時跳過步驟
        """
        # 🔧 統一將 options 轉換為字串，與 initial_data 的字串格式保持一致
        str_options = [str(opt) for opt in options]
        
        class SelectionStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                
                if required_data:
                    for req in required_data:
                        self.add_requirement(req)
            
            def should_skip(self) -> bool:
                """檢查是否應該跳過此步驟（因為數據已存在）"""
                if not skip_if_data_exists:
                    return False
                
                # 檢查 session 中是否已有此步驟的有效數據
                existing_data = self.session.get_data(step_id, None)
                
                if existing_data is None:
                    return False
                
                # 檢查數據是否在選項列表中（統一為字串比較）
                if str(existing_data) in str_options:
                    debug_log(2, f"[Workflow] 步驟 {step_id} 跳過：數據已存在 ({existing_data})")
                    return True
                
                return False
                        
            def get_prompt(self) -> str:
                option_labels = labels or str_options
                prompt_text = prompt + "\n"
                for i, label in enumerate(option_labels):
                    prompt_text += f"{i + 1}. {label}\n"
                return prompt_text.strip()
                
            def execute(self, user_input: Any = None) -> StepResult:
                # ✅ 檢查是否應該跳過（數據已存在且 skip_if_data_exists=True）
                if self.should_skip():
                    existing_data = str(self.session.get_data(step_id))
                    try:
                        label_index = str_options.index(existing_data)
                        display_label = labels[label_index] if labels else existing_data
                    except (ValueError, IndexError):
                        display_label = existing_data
                    return StepResult.success(
                        f"使用現有選擇: {display_label}",
                        {step_id: existing_data}
                    )
                
                if not user_input:
                    return StepResult.failure("請選擇選項")
                
                user_str = str(user_input).strip()
                
                # 嘗試按索引選擇
                try:
                    index = int(user_str) - 1
                    if 0 <= index < len(str_options):
                        selected = str_options[index]
                        label = labels[index] if labels else selected
                        return StepResult.success(
                            f"已選擇: {label}",
                            {step_id: selected}
                        )
                except ValueError:
                    pass
                
                # 嘗試按名稱選擇（統一字串比較）
                for option in str_options:
                    if str(option).lower() == user_str.lower():
                        return StepResult.success(
                            f"已選擇: {option}",
                            {step_id: option}
                        )
                
                return StepResult.failure("無效的選擇")
                
        return SelectionStep(session)
        
    @staticmethod
    def create_file_selection_step(session: WorkflowSession, step_id: str, 
                                  prompt: str = "請選擇文件:",
                                  file_types: Optional[List[str]] = None,
                                  multiple: bool = False,
                                  required_data: Optional[List[str]] = None,
                                  skip_if_data_exists: bool = False,
                                  description: str = "") -> WorkflowStep:
        """
        創建文件選擇步驟
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            prompt: 提示訊息
            file_types: 支援的文件類型（例如 [".txt", ".md"]）
            multiple: 是否允許多選
            required_data: 必要數據列表
            skip_if_data_exists: 是否在數據已存在時跳過步驟
            description: 步驟描述，用於 LLM 上下文
        """
        class FileSelectionStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                if description:
                    self.set_description(description)
                
                if required_data:
                    for req in required_data:
                        self.add_requirement(req)
            
            def should_skip(self) -> bool:
                """檢查是否應該跳過此步驟（因為數據已存在）"""
                if not skip_if_data_exists:
                    return False
                
                # 優先順序：
                # 1. WorkingContext 中的 current_file_path（前端拖曳檔案）
                # 2. session 中的 initial_data（LLM 提取或已存在的數據）
                
                # 1. 檢查 WorkingContext（前端拖曳）
                try:
                    from core.working_context import working_context_manager
                    context_path = working_context_manager.get_context_data("current_file_path")
                    if context_path:
                        path_obj = Path(str(context_path).strip().strip('"').strip("'"))
                        if path_obj.exists():
                            # 驗證文件類型
                            if file_types:
                                ext = path_obj.suffix.lower()
                                if ext not in [ft.lower() for ft in file_types]:
                                    return False
                            
                            # 有效的 WorkingContext 路徑，跳過此步驟
                            debug_log(2, f"[Workflow] 步驟 {step_id} 跳過：WorkingContext 中有檔案 ({context_path})")
                            # 確保 session 中也有這個數據
                            self.session.add_data(step_id, str(path_obj))
                            return True
                except Exception as e:
                    debug_log(2, f"[Workflow] 無法從 WorkingContext 讀取: {e}")
                
                # 2. 檢查 session 中是否已有此步驟的有效數據
                existing_path = self.session.get_data(step_id, None)
                
                if existing_path is None:
                    return False
                
                # 轉換為 Path 對象並驗證
                try:
                    path_obj = Path(str(existing_path).strip().strip('"').strip("'"))
                    if not path_obj.exists():
                        return False
                    
                    # 驗證文件類型
                    if file_types:
                        ext = path_obj.suffix.lower()
                        if ext not in [ft.lower() for ft in file_types]:
                            return False
                    
                    # 有有效數據，跳過此步驟
                    debug_log(2, f"[Workflow] 步驟 {step_id} 跳過：session 中有檔案 ({existing_path})")
                    return True
                except Exception:
                    return False
                        
            def get_prompt(self) -> str:
                prompt_text = prompt
                if file_types:
                    prompt_text += f"\n支援的文件類型: {', '.join(file_types)}"
                if multiple:
                    prompt_text += "\n可選擇多個文件，以逗號分隔"
                return prompt_text
                
            def execute(self, user_input: Any = None) -> StepResult:
                # ✅ 檢查是否應該跳過（數據已存在且 skip_if_data_exists=True）
                if self.should_skip():
                    existing_path = self.session.get_data(step_id)
                    path_obj = Path(str(existing_path).strip().strip('"').strip("'"))
                    return StepResult.success(
                        f"使用現有檔案: {path_obj.name}",
                        {step_id: str(path_obj)}
                    )
                
                if not user_input:
                    return StepResult.failure("請提供檔案路徑")
                
                # 解析文件路徑（清理引號）
                file_paths = []
                if isinstance(user_input, str):
                    if multiple:
                        file_paths = [f.strip().strip('"').strip("'") for f in user_input.split(',') if f.strip()]
                    else:
                        file_paths = [user_input.strip().strip('"').strip("'")]
                elif isinstance(user_input, list):
                    file_paths = [str(f).strip().strip('"').strip("'") for f in user_input]
                else:
                    return StepResult.failure("無效的文件選擇格式")
                
                # 驗證文件
                valid_files = []
                for file_path in file_paths:
                    path_obj = Path(file_path)
                    
                    if not path_obj.exists():
                        return StepResult.failure(f"檔案不存在: {file_path}")
                    
                    if not path_obj.is_file():
                        return StepResult.failure(f"請提供檔案路徑，而非資料夾: {file_path}")
                    
                    if file_types:
                        ext = path_obj.suffix.lower()
                        if ext not in [ft.lower() for ft in file_types]:
                            return StepResult.failure(f"不支援的檔案格式 {ext}。支援格式: {', '.join(file_types)}")
                    
                    valid_files.append(str(path_obj))
                
                result_data = {
                    step_id: valid_files if multiple else valid_files[0]
                }
                
                if multiple:
                    result_data[f"{step_id}_count"] = len(valid_files)
                    message = f"已選擇 {len(valid_files)} 個檔案"
                else:
                    message = f"已選擇檔案: {Path(valid_files[0]).name}"
                
                return StepResult.success(message, result_data)
                
        return FileSelectionStep(session)
    
    @staticmethod
    def create_llm_processing_step(
        session: WorkflowSession, 
        step_id: str,
        task_description: str,
        input_data_keys: List[str],
        output_data_key: str,
        required_data: Optional[List[str]] = None,
        llm_prompt_builder: Optional[Callable[[WorkflowSession], str]] = None,
        description: str = ""
    ) -> WorkflowStep:
        """
        創建LLM處理步驟
        
        這個步驟會等待LLM處理完成。工作流程引擎會在執行此步驟時：
        1. 發布事件通知LLM模組
        2. 將步驟標記為等待LLM回應
        3. LLM完成後，將結果存入session並繼續推進
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            task_description: 任務描述（用於LLM理解任務）
            input_data_keys: 需要傳給LLM的數據鍵列表
            output_data_key: LLM處理結果應存儲的數據鍵
            required_data: 必要數據列表
            llm_prompt_builder: 自定義LLM提示詞構建函數
            description: 步驟描述，用於 LLM 上下文
        """
        class LLMProcessingStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_LLM_PROCESSING)
                self._task_description = task_description
                self._input_data_keys = input_data_keys
                self._output_data_key = output_data_key
                self._llm_prompt_builder = llm_prompt_builder
                if description:
                    self.set_description(description)
                
                # 添加必要數據要求
                if required_data:
                    for req in required_data:
                        self.add_requirement(req)
                
            def get_prompt(self) -> str:
                return f"等待LLM處理: {self._task_description}"
            
            def build_llm_request(self) -> Dict[str, Any]:
                """構建LLM請求數據"""
                # 收集輸入數據
                input_data = {}
                for key in self._input_data_keys:
                    value = self.session.get_data(key)
                    if value is not None:
                        input_data[key] = value
                
                # 使用自定義提示詞構建器或默認格式
                if self._llm_prompt_builder:
                    prompt = self._llm_prompt_builder(self.session)
                else:
                    prompt = f"任務: {self._task_description}\n\n輸入數據:\n"
                    for key, value in input_data.items():
                        prompt += f"{key}: {value}\n"
                
                return {
                    "task_description": self._task_description,
                    "prompt": prompt,
                    "input_data": input_data,
                    "output_data_key": self._output_data_key,
                    "step_id": step_id
                }
            
            def execute(self, user_input: Any = None) -> StepResult:
                """
                執行步驟 - 實際由工作流程引擎處理
                
                當引擎檢測到這是LLM_PROCESSING步驟時，會：
                1. 調用 build_llm_request() 獲取請求數據
                2. 發布事件給LLM模組
                3. 返回特殊的等待狀態
                """
                # 檢查是否已有LLM處理結果
                llm_result = self.session.get_data(self._output_data_key)
                if llm_result is not None:
                    debug_log(2, f"[Workflow] LLM處理步驟 {step_id} 已有結果")
                    return StepResult.success(
                        f"LLM處理完成: {self._task_description}",
                        {self._output_data_key: llm_result}
                    )
                
                # 第一次執行，請求LLM處理
                debug_log(2, f"[Workflow] LLM處理步驟 {step_id} 等待LLM回應")
                
                # 返回特殊結果表示需要LLM處理
                result = StepResult.success(
                    f"正在請求LLM處理: {self._task_description}",
                    {"_llm_request": self.build_llm_request()}
                )
                
                # 標記需要LLM處理
                result.llm_review_data = {
                    "action": "llm_processing_request",
                    "step_id": step_id,
                    "task": self._task_description,
                    "request_data": self.build_llm_request(),
                    "requires_llm_processing": True
                }
                
                return result
                
            def should_auto_advance(self) -> bool:
                # LLM處理步驟應該自動推進（由引擎處理LLM請求）
                return True
                
        return LLMProcessingStep(session)
    
    @staticmethod
    def create_conditional_step(
        session: WorkflowSession,
        step_id: str,
        selection_step_id: str,
        branches: Dict[Any, List[WorkflowStep]],
        description: str = ""
    ) -> WorkflowStep:
        """
        創建條件步驟（根據 selection 結果執行不同分支）
        
        這個步驟會：
        1. 從 session 中獲取 selection 步驟的結果
        2. 根據結果選擇對應的分支步驟列表
        3. 依序執行分支中的所有步驟
        4. 統合所有步驟的結果並返回
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            selection_step_id: 依賴的 selection 步驟 ID（用於獲取選擇結果）
            branches: 分支字典，key 是 selection 的可能值，value 是該分支的步驟列表
            description: 步驟描述
            
        Example:
            branches = {
                1: [],  # UTC - 不需要額外步驟
                2: [input_timezone_step],  # 需要輸入時區
                3: []   # Local - 不需要額外步驟
            }
        """
        class ConditionalStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_PROCESSING)
                if description:
                    self.set_description(description)
                
                # Conditional 總是自動推進
                self._auto_advance = True
                
            def get_prompt(self) -> str:
                return f"根據選擇執行對應步驟..."
            
            def execute(self, user_input: Any = None) -> StepResult:
                """執行條件分支"""
                # 1. 獲取 selection 的結果
                selection_value = self.session.get_data(selection_step_id)
                
                if selection_value is None:
                    return StepResult.failure(
                        f"無法獲取選擇結果: {selection_step_id}"
                    )
                
                debug_log(2, f"[ConditionalStep] {step_id}: selection_value = {selection_value}")
                
                # 2. 找到對應的分支
                branch_steps = branches.get(selection_value)
                
                if branch_steps is None:
                    return StepResult.failure(
                        f"未定義的選擇值: {selection_value}"
                    )
                
                # 3. 如果分支為空，直接返回成功
                if not branch_steps:
                    debug_log(2, f"[ConditionalStep] {step_id}: 空分支，直接繼續")
                    return StepResult.success(
                        f"分支 {selection_value}: 無需額外步驟",
                        {}
                    )
                
                # 4. 依序執行分支中的所有步驟
                debug_log(2, f"[ConditionalStep] {step_id}: 執行分支 {selection_value}，共 {len(branch_steps)} 個步驟")
                
                aggregated_data = {}
                
                for i, step in enumerate(branch_steps):
                    debug_log(3, f"[ConditionalStep] {step_id}: 執行分支步驟 {i+1}/{len(branch_steps)}: {step.id}")
                    
                    # 🔧 檢查：如果是 INTERACTIVE 步驟且沒有輸入
                    if step.step_type == step.STEP_TYPE_INTERACTIVE and user_input is None:
                        # 先檢查步驟是否可以跳過（數據已存在）
                        if hasattr(step, 'should_skip') and step.should_skip():
                            debug_log(2, f"[ConditionalStep] {step_id}: 分支步驟 {step.id} 數據已存在，直接執行")
                            # 數據已存在，直接執行步驟（會使用 existing data）
                            step_result = step.execute(None)
                            if not step_result.success:
                                return StepResult.failure(
                                    f"分支步驟執行失敗: {step.id} - {step_result.message}"
                                )
                            # 更新 aggregated_data
                            if step_result.data:
                                aggregated_data.update(step_result.data)
                                for key, value in step_result.data.items():
                                    self.session.add_data(key, value)
                            continue  # 繼續下一個步驟
                        else:
                            debug_log(2, f"[ConditionalStep] {step_id}: 分支步驟 {step.id} 需要用戶輸入，跳轉到該步驟")
                            # 需要用戶輸入，跳轉到該步驟
                            return StepResult.success(
                                f"需要執行互動步驟: {step.id}",
                                {},
                                skip_to=step.id
                            )
                    
                    # 執行步驟
                    step_result = step.execute(user_input)
                    
                    # 檢查執行結果
                    if not step_result.success:
                        return StepResult.failure(
                            f"分支步驟執行失敗: {step.id} - {step_result.message}"
                        )
                    
                    # 聚合數據
                    if step_result.data:
                        aggregated_data.update(step_result.data)
                        # 同時更新 session，讓後續步驟可以使用
                        for key, value in step_result.data.items():
                            self.session.add_data(key, value)
                
                # 5. 返回統合結果
                return StepResult.success(
                    f"分支 {selection_value} 執行完成",
                    aggregated_data
                )
            
            def should_auto_advance(self) -> bool:
                return True
        
        return ConditionalStep(session)
