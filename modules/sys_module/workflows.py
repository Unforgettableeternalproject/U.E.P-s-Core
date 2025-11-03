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
                next_step: Optional[str] = None, continue_current_step: bool = False):
        """成功結果的工廠方法"""
        return cls(True, message, data, next_step, continue_current_step=continue_current_step)
        
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
            return self._auto_advance_condition()
        return self.step_type == self.STEP_TYPE_PROCESSING
        
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
        self.auto_advance = False
        self.max_auto_steps = 50  # 防止無限循環，但允許更多步驟
        self.llm_review_timeout = 60  # LLM 審核超時時間（秒）
        self.awaiting_llm_review = False  # 是否正在等待 LLM 審核
        self.pending_review_result: Optional[StepResult] = None  # 待審核的步驟結果
        self.waiting_for_input = False  # 是否正在等待用戶輸入（防止重複請求）
        
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
            
            # 🔧 如果設置了自動推進，則移動到下一步
            if self.definition.auto_advance_on_approval:
                current_step_id = self.session.get_data("current_step")
                next_step_id = self.definition.get_next_step(current_step_id, result)
                
                if next_step_id:
                    self.session.add_data("current_step", next_step_id)
                    
                    # 🔧 關鍵修復：不要在這裡同步執行下一步！
                    # 發布事件讓 SYS 模組在背景執行（通過事件系統觸發）
                    next_step = self.definition.steps.get(next_step_id)
                    if next_step and next_step.should_auto_advance():
                        debug_log(2, f"[WorkflowEngine] 下一步 {next_step_id} 是自動推進步驟，發布事件通知 SYS")
                        # 不在這裡執行，讓 SystemLoop 通過 _trigger_workflow_auto_advance 來執行
                        # 返回成功結果，讓流程繼續
                        return StepResult.success(
                            "步驟已批准，等待下一步執行",
                            {"approved": True, "next_step": next_step_id}
                        )
                else:
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
        
        # 返回特殊結果，指示需要 LLM 審核
        return StepResult(
            success=True,
            message="步驟執行完成，等待 LLM 審核",
            data=result.data,
            llm_review_data=review_data,
            requires_user_confirmation=False
        )
        
    def process_input(self, user_input: Any = None) -> StepResult:
        """處理用戶輸入並執行步驟"""
        # 檢查是否正在等待 LLM 審核
        if self.awaiting_llm_review:
            return StepResult.failure("工作流程正在等待 LLM 審核，請稍候")
        
        current_step = self.get_current_step()
        if not current_step:
            return StepResult.complete_workflow("工作流程已完成")
        
        # 階段三：如果是 Interactive 步驟且沒有提供輸入，發布事件請求輸入
        # 注意：空字符串也視為無效輸入
        if current_step.step_type == current_step.STEP_TYPE_INTERACTIVE and not user_input:
            # 如果已經在等待輸入，不要重複請求，直接返回當前提示
            if self.waiting_for_input:
                return StepResult(
                    success=False,
                    message=current_step.get_prompt(),
                    data={"requires_input": True, "step_id": current_step.id, "already_waiting": True}
                )
            
            try:
                from core.event_bus import event_bus, SystemEvent
                
                # 設置等待輸入標記
                self.waiting_for_input = True
                
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
            
        # 執行步驟（有實際輸入時，重置等待標記）
        # 注意：空字符串不視為有效輸入
        if user_input:
            self.waiting_for_input = False
            
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
                self.session.add_data("current_step", None)
                return result
            elif result.success:
                # 更新會話數據
                if result.data:
                    for key, value in result.data.items():
                        self.session.add_data(key, value)
                
                # **檢查是否需要 LLM 審核**
                # 🔧 Interactive 步驟不需要審核，因為它們只是收集輸入參數
                # 審核應該在下一個實際執行步驟完成後進行
                if self.definition.requires_llm_review and current_step.step_type != current_step.STEP_TYPE_INTERACTIVE:
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
                next_step_id = self.definition.get_next_step(current_step.id, result)
                if next_step_id:
                    self.session.add_data("current_step", next_step_id)
                    # 檢查下一步是否可以自動推進
                    if self.auto_advance:
                        next_step = self.definition.steps.get(next_step_id)
                        if next_step and next_step.should_auto_advance():
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
            if current_step.step_type != current_step.STEP_TYPE_PROCESSING:
                # 非處理步驟不應該進入這個方法
                return current_result
                
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
                # 步驟成功完成且不要求繼續，退出循環
                break
                
        # 如果達到最大循環次數，返回警告
        if auto_steps >= max_loop_steps:
            return StepResult.failure(f"循環步驟執行次數超過限制 ({max_loop_steps})")
            
        return current_result
    
    def _auto_advance(self, last_result: StepResult) -> StepResult:
        """自動推進工作流程"""
        auto_steps = 0
        current_result = last_result
        
        while auto_steps < self.max_auto_steps:
            current_step_id = self.session.get_data("current_step")
            if not current_step_id:
                break
                
            current_step = self.definition.steps.get(current_step_id)
            if not current_step or not current_step.should_auto_advance():
                # 如果當前步驟不能自動推進，返回之前的結果
                return current_result
                
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
                         description: str = "") -> WorkflowStep:
        """
        創建輸入步驟
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            prompt: 提示訊息
            validator: 驗證函數，返回 (是否有效, 錯誤訊息)
            required_data: 必要數據列表
            optional: 是否為可選輸入，可選輸入允許空值並自動跳過
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
                        
            def get_prompt(self) -> str:
                if optional:
                    return f"{prompt} (留空跳過)"
                return prompt
                
            def execute(self, user_input: Any = None) -> StepResult:
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
                    return StepResult.success(confirm_message)
                elif user_str in ["取消", "n", "no", "cancel"]:
                    return StepResult.cancel_workflow(cancel_message)
                else:
                    return StepResult.failure("請輸入 '確認' 或 '取消'")
                    
        return ConfirmationStep(session)
        
    @staticmethod
    def create_processing_step(session: WorkflowSession, step_id: str,
                              processor: Callable[[WorkflowSession], StepResult],
                              required_data: Optional[List[str]] = None,
                              auto_advance: bool = False,
                              description: str = "") -> WorkflowStep:
        """
        創建處理步驟
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            processor: 處理函數，接受 session 並返回 StepResult
            required_data: 必要數據列表
            auto_advance: 是否自動推進到下一步
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
                             required_data: Optional[List[str]] = None) -> WorkflowStep:
        """
        創建選擇步驟
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            prompt: 提示訊息
            options: 選項列表
            labels: 選項標籤列表
            required_data: 必要數據列表
        """
        class SelectionStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                
                if required_data:
                    for req in required_data:
                        self.add_requirement(req)
                        
            def get_prompt(self) -> str:
                option_labels = labels or options
                prompt_text = prompt + "\n"
                for i, label in enumerate(option_labels):
                    prompt_text += f"{i + 1}. {label}\n"
                return prompt_text.strip()
                
            def execute(self, user_input: Any = None) -> StepResult:
                if not user_input:
                    return StepResult.failure("請選擇選項")
                
                user_str = str(user_input).strip()
                
                # 嘗試按索引選擇
                try:
                    index = int(user_str) - 1
                    if 0 <= index < len(options):
                        selected = options[index]
                        label = labels[index] if labels else selected
                        return StepResult.success(
                            f"已選擇: {label}",
                            {step_id: selected}
                        )
                except ValueError:
                    pass
                
                # 嘗試按名稱選擇
                for option in options:
                    if option.lower() == user_str.lower():
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
                                  required_data: Optional[List[str]] = None) -> WorkflowStep:
        """
        創建文件選擇步驟
        
        Args:
            session: 工作流程會話
            step_id: 步驟 ID
            prompt: 提示訊息
            file_types: 支援的文件類型
            multiple: 是否允許多選
            required_data: 必要數據列表
        """
        class FileSelectionStep(WorkflowStep):
            def __init__(self, session):
                super().__init__(session)
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                
                if required_data:
                    for req in required_data:
                        self.add_requirement(req)
                        
            def get_prompt(self) -> str:
                prompt_text = prompt
                if file_types:
                    prompt_text += f"\n支援的文件類型: {', '.join(file_types)}"
                if multiple:
                    prompt_text += "\n可選擇多個文件，以逗號分隔"
                return prompt_text
                
            def execute(self, user_input: Any = None) -> StepResult:
                if not user_input:
                    return StepResult.failure("請選擇文件")
                
                # 解析文件路徑
                file_paths = []
                if isinstance(user_input, str):
                    if multiple:
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
                    
                    if file_types:
                        _, ext = os.path.splitext(file_path)
                        if ext.lower() not in [ft.lower() for ft in file_types]:
                            return StepResult.failure(f"不支援的文件類型: {ext}")
                    
                    valid_files.append(file_path)
                
                result_data = {
                    step_id: valid_files if multiple else valid_files[0],
                    f"{step_id}_count": len(valid_files)
                }
                
                return StepResult.success(
                    f"已選擇 {len(valid_files)} 個文件",
                    result_data
                )
                
        return FileSelectionStep(session)
