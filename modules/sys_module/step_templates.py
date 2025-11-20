"""
modules/sys_module/step_templates.py
Step template factory methods for creating common workflow steps

提供預定義的步驟模板，用於快速創建常見的工作流程步驟：
- 輸入步驟 (Input)
- 確認步驟 (Confirmation)
- 處理步驟 (Processing)
- 自動步驟 (Auto)
- 循環步驟 (Loop)
- 選擇步驟 (Selection)
- 文件選擇步驟 (File Selection)
- LLM 處理步驟 (LLM Processing)
- 條件步驟 (Conditional)
"""

from typing import Dict, Any, List, Optional, Tuple, Callable, Union
from pathlib import Path
import uuid
from datetime import datetime, timedelta

from core.sessions.session_manager import WorkflowSession
from utils.debug_helper import debug_log, info_log, error_log

# Import base classes from workflows module (使用絕對導入避免動態載入時的問題)
from modules.sys_module.workflows import WorkflowStep, StepResult


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
                2. session 中已有該步驟的數據（包括空字符串）
                3. 數據不是 None
                
                注意：空字符串算作有效數據（例如：query="" 表示播放整個資料夾）
                """
                if not skip_if_data_exists:
                    return False
                
                # 檢查 session 中是否已有此步驟的數據
                # 使用特殊標記來區分「沒有數據」和「空字符串數據」
                _SENTINEL = object()
                existing_data = self.session.get_data(step_id, _SENTINEL)
                
                # 只有 None 或未設置才算沒有數據
                if existing_data is _SENTINEL or existing_data is None:
                    return False
                
                # 有數據（包括空字符串），跳過此步驟
                debug_log(2, f"[Workflow] 步驟 {step_id} 跳過：數據已存在 (值: '{existing_data}')")
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
                
                user_str = str(user_input).strip().lower()
                
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
                
                # 1. 嘗試精確匹配選項
                for option in str_options:
                    if str(option).lower() == user_str:
                        return StepResult.success(
                            f"已選擇: {option}",
                            {step_id: option}
                        )
                
                # 2. 嘗試精確匹配標籤
                if labels:
                    for i, label in enumerate(labels):
                        if str(label).lower() == user_str:
                            selected = str_options[i]
                            return StepResult.success(
                                f"已選擇: {label}",
                                {step_id: selected}
                            )
                
                # 3. 嘗試部分匹配選項（選項包含在用戶輸入中）
                for option in str_options:
                    if str(option).lower() in user_str:
                        return StepResult.success(
                            f"已選擇: {option}",
                            {step_id: option}
                        )
                
                # 4. 嘗試部分匹配標籤（標籤包含在用戶輸入中）
                if labels:
                    for i, label in enumerate(labels):
                        label_lower = str(label).lower()
                        if label_lower in user_str or user_str in label_lower:
                            selected = str_options[i]
                            return StepResult.success(
                                f"已選擇: {label}",
                                {step_id: selected}
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
                # LLM處理步驟應該自動推進（統一處理）
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
                # 0. 檢查是否從中斷處恢復
                loop_continue_key = f"loop_continue_{step_id}"
                resume_index = self.session.get_data(loop_continue_key)
                
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
                if resume_index is not None:
                    start_index = resume_index
                    debug_log(2, f"[ConditionalStep] {step_id}: 從中斷處恢復執行（索引 {start_index}）")
                    # 清除 loop_continue 標記
                    self.session.add_data(loop_continue_key, None)
                else:
                    debug_log(2, f"[ConditionalStep] {step_id}: 執行分支 {selection_value}，共 {len(branch_steps)} 個步驟")
                    start_index = 0
                
                aggregated_data = {}
                
                for i, step in enumerate(branch_steps[start_index:], start=start_index):
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
                            # 保存當前進度（下一個要執行的索引）
                            self.session.add_data(loop_continue_key, i + 1)
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
    
    @staticmethod
    def create_periodic_check_step(
        session: WorkflowSession,
        step_id: str,
        check_interval: int,
        check_function: Callable[[], Dict[str, Any]],
        description: str = "週期性檢查步驟"
    ) -> WorkflowStep:
        """
        創建週期性檢查步驟（用於背景工作流）
        
        Args:
            session: 工作流會話
            step_id: 步驟唯一識別碼
            check_interval: 檢查間隔（秒）
            check_function: 檢查函數，返回 Dict 包含 {triggered, data, should_stop}
            description: 步驟描述
            
        Returns:
            配置好的週期性檢查步驟
            
        Example:
            def my_check():
                # 檢查邏輯
                return {"triggered": False, "data": {}, "should_stop": False}
            
            step = StepTemplate.create_periodic_check_step(
                session, "periodic_check", 60, my_check
            )
        """
        class _PeriodicCheckStep(WorkflowStep):
            def __init__(self):
                super().__init__(session)
                self.check_interval = check_interval
                self.check_function = check_function
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_PROCESSING)
                self.set_description(description)
            
            def get_prompt(self) -> str:
                return f"正在進行週期性檢查（間隔 {self.check_interval} 秒）..."
            
            def execute(self, user_input: Any = None) -> StepResult:
                try:
                    # 執行檢查函數
                    check_result = self.check_function()
                    
                    # 計算下次檢查時間
                    next_check_time = datetime.now() + timedelta(seconds=self.check_interval)
                    next_check_at = next_check_time.isoformat()
                    
                    # 更新資料庫狀態
                    task_id = session.metadata.get("task_id")
                    if task_id:
                        from modules.sys_module.actions.automation_helper import update_workflow_status
                        update_workflow_status(
                            task_id=task_id,
                            status="RUNNING",
                            last_check_at=datetime.now().isoformat(),
                            next_check_at=next_check_at
                        )
                    
                    # 檢查是否應該停止
                    if check_result.get("should_stop", False):
                        return StepResult.complete_workflow(
                            "監控已停止",
                            data=check_result.get("data", {})
                        )
                    
                    # 檢查是否觸發條件
                    if check_result.get("triggered", False):
                        return StepResult.success(
                            f"條件已觸發：{check_result.get('message', '未知觸發')}",
                            data=check_result.get("data", {}),
                            continue_current_step=True
                        )
                    
                    # 繼續監控
                    return StepResult.success(
                        "檢查完成，繼續監控",
                        data={"next_check_at": next_check_at},
                        continue_current_step=True
                    )
                    
                except Exception as e:
                    error_log(f"[PeriodicCheckStep] 檢查失敗：{e}")
                    return StepResult.failure(f"檢查失敗：{str(e)}")
        
        return _PeriodicCheckStep()
    
    @staticmethod
    def create_scheduled_trigger_step(
        session: WorkflowSession,
        step_id: str,
        check_interval: int = 30,
        description: str = "時間排程觸發步驟"
    ) -> WorkflowStep:
        """
        創建時間排程觸發步驟（用於提醒、日曆事件）
        
        Args:
            session: 工作流會話
            step_id: 步驟唯一識別碼
            check_interval: 檢查間隔（秒），預設 30 秒
            description: 步驟描述
            
        Returns:
            配置好的排程觸發步驟
        """
        class _ScheduledTriggerStep(WorkflowStep):
            def __init__(self):
                super().__init__(session)
                self.check_interval = check_interval
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_PROCESSING)
                self.set_description(description)
            
            def get_prompt(self) -> str:
                return "正在檢查排程觸發條件..."
            
            def execute(self, user_input: Any = None) -> StepResult:
                try:
                    # 從 session 獲取觸發條件
                    trigger_conditions = session.get_data("trigger_conditions", {})
                    trigger_type = trigger_conditions.get("type", "time")
                    target_time_str = trigger_conditions.get("target_time")
                    
                    if not target_time_str:
                        return StepResult.failure("缺少目標觸發時間")
                    
                    # 解析目標時間
                    target_time = datetime.fromisoformat(target_time_str)
                    current_time = datetime.now()
                    
                    # 計算下次檢查時間
                    next_check_time = current_time + timedelta(seconds=self.check_interval)
                    next_check_at = next_check_time.isoformat()
                    
                    # 更新資料庫
                    task_id = session.metadata.get("task_id")
                    if task_id:
                        from modules.sys_module.actions.automation_helper import update_workflow_status
                        update_workflow_status(
                            task_id=task_id,
                            status="RUNNING",
                            last_check_at=current_time.isoformat(),
                            next_check_at=next_check_at
                        )
                    
                    # 檢查是否到達觸發時間
                    if current_time >= target_time:
                        trigger_data = session.get_data("trigger_data", {})
                        
                        # 發布觸發事件
                        from core.event_bus import event_bus, SystemEvent
                        
                        if trigger_type == "reminder":
                            event_bus.publish(
                                SystemEvent.REMINDER_TRIGGERED,
                                {
                                    "task_id": task_id,
                                    "message": trigger_data.get("message", "提醒時間到"),
                                    "trigger_time": target_time_str
                                },
                                source="sys"
                            )
                            info_log(f"[ScheduledTriggerStep] 提醒已觸發：{trigger_data.get('message')}")
                        
                        elif trigger_type == "calendar_event":
                            event_bus.publish(
                                SystemEvent.CALENDAR_EVENT_STARTING,
                                {
                                    "task_id": task_id,
                                    "event_id": trigger_data.get("event_id"),
                                    "summary": trigger_data.get("summary", "日曆事件"),
                                    "start_time": target_time_str
                                },
                                source="sys"
                            )
                            info_log(f"[ScheduledTriggerStep] 日曆事件已觸發：{trigger_data.get('summary')}")
                        
                        # 觸發後完成工作流
                        return StepResult.complete_workflow(
                            f"排程觸發完成：{trigger_data.get('message', '觸發成功')}",
                            data={"triggered_at": current_time.isoformat()}
                        )
                    
                    # 尚未到達觸發時間
                    time_remaining = (target_time - current_time).total_seconds()
                    return StepResult.success(
                        f"等待觸發中，剩餘 {int(time_remaining)} 秒",
                        data={
                            "next_check_at": next_check_at,
                            "time_remaining": time_remaining
                        },
                        continue_current_step=True
                    )
                    
                except Exception as e:
                    error_log(f"[ScheduledTriggerStep] 觸發檢查失敗：{e}")
                    return StepResult.failure(f"觸發檢查失敗：{str(e)}")
        
        return _ScheduledTriggerStep()
    
    @staticmethod
    def create_monitor_creation_step(
        session: WorkflowSession,
        step_id: str,
        workflow_type: str,
        param_keys: List[str],
        prompt_template: str = "請提供監控參數：",
        description: str = "建立監控任務"
    ) -> WorkflowStep:
        """
        創建監控建立步驟（用於啟動新的監控工作流）
        
        Args:
            session: 工作流會話
            step_id: 步驟唯一識別碼
            workflow_type: 工作流類型（如 monitor_folder, set_reminder）
            param_keys: 需要收集的參數鍵列表
            prompt_template: 提示訊息模板
            description: 步驟描述
            
        Returns:
            配置好的監控建立步驟
        """
        class _MonitorCreationStep(WorkflowStep):
            def __init__(self):
                super().__init__(session)
                self.workflow_type = workflow_type
                self.param_keys = param_keys
                self.prompt_template = prompt_template
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_INTERACTIVE)
                self.set_description(description)
            
            def get_prompt(self) -> str:
                params_info = "、".join(self.param_keys)
                return f"{self.prompt_template}\n需要的參數：{params_info}"
            
            def execute(self, user_input: Any = None) -> StepResult:
                try:
                    # 驗證必要參數
                    for key in self.param_keys:
                        if not session.has_data(key):
                            return StepResult.failure(f"缺少必要參數：{key}")
                    
                    # 生成唯一的 task_id
                    task_id = f"workflow_{self.workflow_type}_{uuid.uuid4().hex[:8]}"
                    
                    # 收集觸發條件
                    trigger_conditions = {}
                    if session.has_data("trigger_time"):
                        trigger_conditions["type"] = "time"
                        trigger_conditions["target_time"] = session.get_data("trigger_time")
                    elif session.has_data("check_path"):
                        trigger_conditions["type"] = "file_change"
                        trigger_conditions["path"] = session.get_data("check_path")
                    
                    # 收集元數據
                    metadata = {
                        "created_by": "user",
                        "workflow_type": self.workflow_type
                    }
                    for key in self.param_keys:
                        metadata[key] = session.get_data(key)
                    
                    # 計算下次檢查時間
                    check_interval = session.get_data("check_interval", 60)
                    next_check_time = datetime.now() + timedelta(seconds=check_interval)
                    
                    # 註冊到資料庫
                    from modules.sys_module.actions.automation_helper import register_background_workflow
                    success = register_background_workflow(
                        task_id=task_id,
                        workflow_type=self.workflow_type,
                        trigger_conditions=trigger_conditions,
                        metadata=metadata,
                        next_check_at=next_check_time.isoformat()
                    )
                    
                    if not success:
                        return StepResult.failure("註冊監控任務失敗")
                    
                    # 保存 task_id 到 session
                    session.set_data("task_id", task_id)
                    session.metadata["task_id"] = task_id
                    
                    info_log(f"[MonitorCreationStep] 已建立監控任務：{task_id}")
                    
                    return StepResult.success(
                        f"監控任務已建立：{task_id}",
                        data={
                            "task_id": task_id,
                            "workflow_type": self.workflow_type,
                            "trigger_conditions": trigger_conditions,
                            "next_check_at": next_check_time.isoformat()
                        }
                    )
                    
                except Exception as e:
                    error_log(f"[MonitorCreationStep] 建立監控失敗：{e}")
                    return StepResult.failure(f"建立監控失敗：{str(e)}")
        
        return _MonitorCreationStep()
    
    @staticmethod
    def create_intervention_step(
        session: WorkflowSession,
        step_id: str,
        action: str = "list",
        target_task_id: Optional[str] = None,
        description: str = "工作流干預操作"
    ) -> WorkflowStep:
        """
        創建干預步驟（用於編輯或中斷現有的背景工作流）
        
        Args:
            session: 工作流會話
            step_id: 步驟唯一識別碼
            action: 干預動作（list, edit, cancel）
            target_task_id: 目標工作流的 task_id（list 操作不需要）
            description: 步驟描述
            
        Returns:
            配置好的干預步驟
        """
        class _InterventionStep(WorkflowStep):
            def __init__(self):
                super().__init__(session)
                self.action = action
                self.target_task_id = target_task_id
                self.set_id(step_id)
                self.set_step_type(self.STEP_TYPE_PROCESSING)
                self.set_description(description)
            
            def get_prompt(self) -> str:
                if self.action == "list":
                    return "正在查詢活躍的背景工作流..."
                elif self.action == "edit":
                    return f"正在編輯工作流：{self.target_task_id}"
                elif self.action == "cancel":
                    return f"正在取消工作流：{self.target_task_id}"
                else:
                    return f"正在執行干預操作：{self.action}"
            
            def execute(self, user_input: Any = None) -> StepResult:
                try:
                    from modules.sys_module.actions.automation_helper import (
                        get_active_workflows,
                        get_workflow_by_id,
                        update_workflow_status,
                        log_intervention
                    )
                    
                    if self.action == "list":
                        workflows = get_active_workflows()
                        
                        if not workflows:
                            return StepResult.complete_workflow(
                                "目前沒有活躍的背景工作流",
                                data={"workflows": []}
                            )
                        
                        workflow_info = []
                        for wf in workflows:
                            workflow_info.append({
                                "task_id": wf["task_id"],
                                "type": wf["workflow_type"],
                                "status": wf["status"],
                                "created_at": wf["created_at"],
                                "next_check_at": wf.get("next_check_at", "N/A")
                            })
                        
                        return StepResult.complete_workflow(
                            f"找到 {len(workflows)} 個活躍的背景工作流",
                            data={"workflows": workflow_info}
                        )
                    
                    elif self.action == "cancel":
                        if not self.target_task_id:
                            return StepResult.failure("缺少目標 task_id")
                        
                        workflow = get_workflow_by_id(self.target_task_id)
                        if not workflow:
                            return StepResult.failure(f"找不到工作流：{self.target_task_id}")
                        
                        success = update_workflow_status(
                            task_id=self.target_task_id,
                            status="CANCELLED",
                            error_message="用戶取消"
                        )
                        
                        if not success:
                            return StepResult.failure("取消工作流失敗")
                        
                        log_intervention(
                            task_id=self.target_task_id,
                            action="cancel",
                            performed_by="user",
                            result="success"
                        )
                        
                        from core.event_bus import event_bus, SystemEvent
                        event_bus.publish(
                            SystemEvent.BACKGROUND_WORKFLOW_CANCELLED,
                            {"task_id": self.target_task_id},
                            source="sys"
                        )
                        
                        info_log(f"[InterventionStep] 已取消工作流：{self.target_task_id}")
                        
                        return StepResult.complete_workflow(
                            f"已取消工作流：{self.target_task_id}",
                            data={"task_id": self.target_task_id, "action": "cancelled"}
                        )
                    
                    elif self.action == "edit":
                        if not self.target_task_id:
                            return StepResult.failure("缺少目標 task_id")
                        
                        workflow = get_workflow_by_id(self.target_task_id)
                        if not workflow:
                            return StepResult.failure(f"找不到工作流：{self.target_task_id}")
                        
                        new_params = session.get_data("edit_params", {})
                        if not new_params:
                            return StepResult.failure("缺少編輯參數")
                        
                        current_metadata = workflow.get("metadata", {})
                        current_metadata.update(new_params)
                        
                        success = update_workflow_status(
                            task_id=self.target_task_id,
                            status="RUNNING",
                            metadata=current_metadata
                        )
                        
                        if not success:
                            return StepResult.failure("編輯工作流失敗")
                        
                        log_intervention(
                            task_id=self.target_task_id,
                            action="edit",
                            parameters=new_params,
                            performed_by="user",
                            result="success"
                        )
                        
                        info_log(f"[InterventionStep] 已編輯工作流：{self.target_task_id}")
                        
                        return StepResult.complete_workflow(
                            f"已編輯工作流：{self.target_task_id}",
                            data={
                                "task_id": self.target_task_id,
                                "action": "edited",
                                "new_params": new_params
                            }
                        )
                    
                    else:
                        return StepResult.failure(f"不支援的干預操作：{self.action}")
                        
                except Exception as e:
                    error_log(f"[InterventionStep] 干預操作失敗：{e}")
                    return StepResult.failure(f"干預操作失敗：{str(e)}")
        
        return _InterventionStep()