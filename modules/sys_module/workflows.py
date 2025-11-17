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
        
        # 🔧 工作流完成步驟（complete=True）不需要審核
        # 這是最終結果，不應該再讓 LLM 生成回應
        if result.complete:
            return False
        
        # LLM_PROCESSING 步驟特殊處理
        if current_step.step_type == current_step.STEP_TYPE_LLM_PROCESSING:
            # 只有當步驟自己提供了 llm_review_data 時才需要審核
            # 第二次執行時（已有結果）不需要審核，讓 _auto_advance 自然推進
            return result.llm_review_data is not None
        
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
        
        # 🔧 準備審核數據
        review_data = None
        if result.llm_review_data is not None:
            # 步驟已提供審核數據，使用它
            review_data = result.llm_review_data.copy()
            review_data.update({
                "step_id": current_step.id,
                "step_type": current_step.step_type,
                "message": result.message,
                "data": result.data,
                "workflow_type": self.definition.workflow_type,
                "workflow_name": self.definition.name
            })
        else:
            # 步驟沒有提供審核數據，使用基本數據
            review_data = {
                "step_id": current_step.id,
                "step_type": current_step.step_type,
                "message": result.message,
                "data": result.data,
                "workflow_type": self.definition.workflow_type,
                "workflow_name": self.definition.name
            }
        
        # 🔧 檢查下一步是否為 INTERACTIVE，如果是則添加 next_step_info
        # 這樣 LLM 可以檢測到並生成適當的提示
        if review_data and "next_step_info" not in review_data:
            next_step_id = self.definition.get_next_step(current_step.id, result)
            if next_step_id:
                next_step = self.definition.steps.get(next_step_id)
                if next_step and next_step.step_type == next_step.STEP_TYPE_INTERACTIVE:
                    # 添加 next_step_info 讓 LLM 知道下一步需要互動
                    review_data["next_step_info"] = {
                        "step_id": next_step.id,
                        "step_type": next_step.step_type,
                        "requires_input": True,
                        "prompt": next_step.get_prompt()
                    }
                    review_data["requires_user_response"] = True
                    review_data["should_end_session"] = False
                    debug_log(2, f"[WorkflowEngine] 為下一個 INTERACTIVE 步驟添加 next_step_info: {next_step.id}")
        
        # 🆕 發布 WORKFLOW_STEP_COMPLETED 事件讓 LLM 模組接收審核請求
        if review_data:
            try:
                from core.event_bus import event_bus, SystemEvent
                
                # 🔍 調試：檢查發布的數據
                event_data = {
                    "session_id": self.session.session_id,
                    "workflow_type": self.definition.workflow_type,
                    "step_result": result.to_dict(),
                    "requires_llm_review": True,
                    "llm_review_data": review_data,
                    "timestamp": time.time()
                }
                debug_log(2, f"[WorkflowEngine] 準備發布審核事件，review_data keys: {list(review_data.keys())}")
                debug_log(3, f"[WorkflowEngine] event_data keys: {list(event_data.keys())}")
                
                event_bus.publish(
                    SystemEvent.WORKFLOW_STEP_COMPLETED,
                    event_data,
                    source="sys"
                )
                debug_log(2, f"[WorkflowEngine] 已發布 WORKFLOW_STEP_COMPLETED 事件供 LLM 審核")
            except Exception as e:
                error_log(f"[WorkflowEngine] 發布審核事件失敗: {e}")
        
        # 如果沒有審核數據，直接返回原始結果，讓 _auto_advance 繼續
        if not review_data:
            return result
        
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
                
            # 顯示當前步驟的提示（如果有且不為空）
            prompt = current_step.get_prompt()
            if prompt and prompt.strip() and prompt != "處理中...":
                print(f"🔄 {prompt}")
                
            # 執行當前步驟（所有步驟統一處理，包括 LLM_PROCESSING）
            step_result = current_step.execute()
            
            # 🔧 檢查是否需要等待 LLM 處理
            if step_result.llm_review_data and step_result.llm_review_data.get("requires_llm_processing"):
                debug_log(2, f"[WorkflowEngine] [_auto_advance] 步驟需要 LLM 處理: {current_step.id}")
                # 直接調用審核邏輯，發布事件給 LLM 模組
                if self._should_request_review(step_result, current_step):
                    debug_log(2, f"[WorkflowEngine] [_auto_advance] 請求 LLM 審核")
                    return self._request_llm_review(step_result, current_step)
                # 如果不需要審核，直接返回
                return step_result
            
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
