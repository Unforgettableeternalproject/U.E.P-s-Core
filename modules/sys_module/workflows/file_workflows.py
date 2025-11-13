"""
modules/sys_module/workflows/file_workflows.py
File processing workflow definitions for the SYS module

包含各種文件處理工作流程的定義，用於實現文件操作功能。
"""

import os
import asyncio
from typing import Dict, Any, List, Optional, Tuple, Callable, Union
import datetime
from pathlib import Path

from core.sessions.session_manager import WorkflowSession
from utils.debug_helper import info_log, error_log, debug_log

# Import the workflow engine components
# We need to import directly from the module file to avoid circular imports
import sys
import os
import importlib.util

# Load workflows.py directly
workflows_path = os.path.join(os.path.dirname(__file__), '..', 'workflows.py')
spec = importlib.util.spec_from_file_location("workflows", workflows_path)
workflows_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflows_module)

# Get the classes we need
WorkflowDefinition = workflows_module.WorkflowDefinition
WorkflowEngine = workflows_module.WorkflowEngine
WorkflowStep = workflows_module.WorkflowStep
StepResult = workflows_module.StepResult
StepTemplate = workflows_module.StepTemplate
WorkflowType = workflows_module.WorkflowType
WorkflowMode = workflows_module.WorkflowMode

# Import file interaction actions
from ..actions.file_interaction import (
    drop_and_read,
    intelligent_archive,
    summarize_tag,
)


def create_drop_and_read_workflow(session: WorkflowSession) -> WorkflowEngine:
    """創建檔案讀取工作流程"""
    workflow_def = WorkflowDefinition(
        workflow_type="drop_and_read",
        name="檔案讀取工作流程",
        description="讀取檔案內容並提供摘要",
        workflow_mode=WorkflowMode.DIRECT,  # 使用同步模式以支援 LLM 互動
        requires_llm_review=True,  # ✅ 啟用 LLM 審核，讓 LLM 知道每個步驟的結果
        auto_advance_on_approval=True
    )
    
    # 步驟1: 開啟檔案選擇對話框並獲取檔案路徑（自動處理步驟）
    def get_file_path_via_dialog(session):
        """使用檔案選擇對話框獲取檔案路徑（線程安全）"""
        try:
            # 🔧 使用線程安全的檔案對話框，避免 Tcl_AsyncDelete 錯誤
            from utils.safe_file_dialog import open_file_dialog_sync
            
            info_log("[Workflow] 開啟檔案選擇對話框...")
            
            # 調用線程安全的檔案對話框
            file_path = open_file_dialog_sync(
                title="請選擇要讀取的檔案",
                filetypes=[
                    ("所有檔案", "*.*"),
                    ("文字檔案", "*.txt"),
                    ("Markdown", "*.md"),
                    ("Python", "*.py"),
                    ("JSON", "*.json"),
                ]
            )
            
            if not file_path:
                return StepResult.failure("No file path provided")
            
            if not os.path.exists(file_path):
                return StepResult.failure(f"檔案不存在: {file_path}")
            
            info_log(f"[Workflow] 已選擇檔案: {file_path}")
            return StepResult.success(
                f"使用者選擇了檔案: {Path(file_path).name}",
                {"file_path_input": file_path}
            )
        except Exception as e:
            error_log(f"[Workflow] 獲取檔案路徑失敗: {e}")
            return StepResult.failure(f"獲取檔案路徑失敗: {e}")
    
    # 🔧 步驟 1：檔案選擇步驟（SYSTEM 類型 - 系統操作，不需要用戶輸入）
    # 這個步驟會在 start_workflow 時自動執行，開啟檔案對話框
    # 完成後不需要生成審核回應（LLM 已經在正常流程中告知用戶工作流已啟動）
    class FileDialogStep(WorkflowStep):
        def __init__(self, session):
            super().__init__(session)
            self.set_id("file_path_input")
            self.set_step_type(self.STEP_TYPE_SYSTEM)  # 系統操作步驟
            self.set_description("透過檔案選擇對話框獲取要讀取的檔案路徑")
            
        def get_prompt(self) -> str:
            return "開啟檔案選擇對話框..."
            
        def execute(self, user_input: Any = None) -> StepResult:
            # 🔧 優先順序：
            # 1. session 中的 initial_data（由 LLM 通過 MCP 傳遞）
            # 2. WorkingContext 中的先行資料（由系統設置）
            # 3. 開啟檔案對話框（手動選擇）
            
            # 1. 檢查 session 中是否已有路徑（透過 initial_data 提供）
            existing_path = self.session.get_data("file_path_input", "")
            if existing_path:
                info_log(f"[Workflow] 使用 session 中的檔案路徑: {existing_path}")
                if not os.path.exists(existing_path):
                    return StepResult.failure(f"檔案不存在: {existing_path}")
                return StepResult.success(
                    f"使用者提供了檔案: {Path(existing_path).name}",
                    {"file_path_input": existing_path}
                )
            
            # 2. 檢查 WorkingContext 中是否有路徑
            try:
                from core.working_context import working_context_manager
                context_path = working_context_manager.get_context_data("current_file_path")
                if context_path and os.path.exists(str(context_path)):
                    info_log(f"[Workflow] 使用 WorkingContext 中的檔案路徑: {context_path}")
                    return StepResult.success(
                        f"使用上下文中的檔案: {Path(context_path).name}",
                        {"file_path_input": str(context_path)}
                    )
            except Exception as e:
                debug_log(2, f"[Workflow] 無法從 WorkingContext 讀取檔案路徑: {e}")
            
            # 3. 都沒有，開啟對話框
            return get_file_path_via_dialog(self.session)
            
        def should_auto_advance(self) -> bool:
            return False  # 需要 LLM 批准後才能繼續到步驟 2
    
    file_input_step = FileDialogStep(session)
    
    # 步驟2: 自動執行檔案讀取（自動步驟）
    def execute_file_read(session):
        file_path = session.get_data("file_path_input", "")
        
        try:
            debug_log(2, f"[Workflow] 開始讀取檔案: {file_path}")
            
            # 顯示檔案資訊
            file_size = ""
            try:
                size_bytes = os.path.getsize(file_path)
                if size_bytes < 1024:
                    file_size = f" ({size_bytes} bytes)"
                elif size_bytes < 1024 * 1024:
                    file_size = f" ({size_bytes/1024:.1f} KB)"
                else:
                    file_size = f" ({size_bytes/(1024*1024):.1f} MB)"
            except:
                pass
            
            info_log(f"正在讀取檔案: {Path(file_path).name}{file_size}")
            
            content = drop_and_read(file_path)
            
            # ✅ 返回成功結果並提供 LLM 審核數據，讓 LLM 知道檔案內容
            result = StepResult.complete_workflow(
                f"📄 檔案讀取完成！檔案: {Path(file_path).name}, 內容長度: {len(content)} 字符",
                {
                    "file_path": file_path,
                    "content": content,
                    "content_length": len(content),
                    "completion_time": datetime.datetime.now().isoformat()
                }
            )
            
            # ✅ 添加 LLM 審核數據，包含檔案內容供 LLM 處理
            result.llm_review_data = {
                "action": "file_read_completed",
                "file_name": Path(file_path).name,
                "file_path": file_path,
                "content_preview": content[:500] if len(content) > 500 else content,  # 提供內容預覽
                "content_length": len(content),
                "full_content": content,  # 完整內容供 LLM 分析
                "requires_user_response": True,  # 需要 LLM 生成回應告訴用戶
                "should_end_session": True  # 建議 LLM 在回應後結束會話
            }
            
            return result
        except Exception as e:
            error_log(f"[Workflow] 檔案讀取失敗: {e}")
            return StepResult.failure(f"檔案讀取失敗: {e}")
    
    read_step = StepTemplate.create_auto_step(
        session,
        "execute_read",
        execute_file_read,
        ["file_path_input"],
        "正在讀取檔案...",
        description="自動讀取選定的檔案內容並完成工作流程"
    )
    
    # 建立工作流程
    workflow_def.add_step(file_input_step)
    workflow_def.add_step(read_step)
    
    workflow_def.set_entry_point("file_path_input")
    workflow_def.add_transition("file_path_input", "execute_read")
    
    # 創建引擎並啟用自動推進
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


def create_intelligent_archive_workflow(session: WorkflowSession) -> WorkflowEngine:
    """創建智慧歸檔工作流程"""
    workflow_def = WorkflowDefinition(
        workflow_type="intelligent_archive",
        name="智慧歸檔工作流程",
        description="選擇要歸檔的檔案，可選目標資料夾，確認後執行歸檔",
        workflow_mode=WorkflowMode.DIRECT,  # 使用同步模式以支援 LLM 互動
        requires_llm_review=True,  # ✅ 啟用 LLM 審核，讓 LLM 知道每個步驟的結果
        auto_advance_on_approval=True
    )
    
    # 檢查初始數據，決定入口點
    # 注意：intelligent_archive 使用 "file_selection" 而不是 "file_path_input"
    initial_file_path = session.get_data("file_selection", "")
    initial_target_dir = session.get_data("target_dir_input", "")
    
    # 🔧 步驟1: 開啟檔案選擇對話框（SYSTEM 步驟）
    def get_archive_file_path_via_dialog(session):
        """使用檔案選擇對話框獲取要歸檔的檔案路徑（線程安全）"""
        try:
            from utils.safe_file_dialog import open_file_dialog_sync
            
            info_log("[Workflow] 開啟檔案選擇對話框（智慧歸檔）...")
            
            file_path = open_file_dialog_sync(
                title="請選擇要歸檔的檔案",
                filetypes=[
                    ("所有檔案", "*.*"),
                    ("文件", "*.txt;*.doc;*.docx;*.pdf;*.md"),
                    ("圖片", "*.jpg;*.jpeg;*.png;*.gif;*.bmp"),
                    ("音樂", "*.mp3;*.wav;*.flac;*.ogg"),
                    ("影片", "*.mp4;*.avi;*.mkv;*.mov"),
                ]
            )
            
            if not file_path:
                return StepResult.cancel_workflow("用戶取消了檔案選擇")
            
            if not os.path.exists(file_path):
                return StepResult.failure(f"檔案不存在: {file_path}")
            
            info_log(f"[Workflow] 已選擇要歸檔的檔案: {file_path}")
            result = StepResult.success(
                f"使用者選擇了檔案: {Path(file_path).name}",
                {"file_selection": file_path}
            )
            
            # ✅ 添加 LLM 審核數據，讓 LLM 知道檔案選擇結果
            result.llm_review_data = {
                "action": "file_selected_for_archive",
                "file_name": Path(file_path).name,
                "file_path": file_path,
                "requires_user_response": True,  # 需要 LLM 告訴用戶已選擇檔案
                "should_end_session": False  # 工作流還要繼續
            }
            
            return result
        except Exception as e:
            error_log(f"[Workflow] 獲取檔案路徑失敗: {e}")
            return StepResult.failure(f"獲取檔案路徑失敗: {e}")
    
    class ArchiveFileDialogStep(WorkflowStep):
        def __init__(self, session):
            super().__init__(session)
            self.set_id("file_selection")
            self.set_step_type(self.STEP_TYPE_SYSTEM)  # 系統操作步驟
            self.set_description("透過檔案選擇對話框獲取要歸檔的檔案路徑")
            
        def get_prompt(self) -> str:
            return "開啟檔案選擇對話框（智慧歸檔）..."
            
        def execute(self, user_input: Any = None) -> StepResult:
            # 🔧 優先順序：
            # 1. session 中的 initial_data
            # 2. WorkingContext 中的先行資料
            # 3. 開啟檔案對話框
            
            # 1. 檢查 session 中是否已有路徑
            existing_path = self.session.get_data("file_selection", "")
            if existing_path:
                info_log(f"[Workflow] 使用 session 中的檔案路徑: {existing_path}")
                if not os.path.exists(existing_path):
                    return StepResult.failure(f"檔案不存在: {existing_path}")
                
                result = StepResult.success(
                    f"使用者提供了檔案: {Path(existing_path).name}",
                    {"file_selection": existing_path}
                )
                
                result.llm_review_data = {
                    "action": "file_selected_for_archive",
                    "file_name": Path(existing_path).name,
                    "file_path": existing_path,
                    "requires_user_response": True,
                    "should_end_session": False
                }
                
                return result
            
            # 2. 檢查 WorkingContext 中是否有路徑
            try:
                from core.working_context import working_context_manager
                context_path = working_context_manager.get_context_data("current_file_path")
                if context_path and os.path.exists(str(context_path)):
                    info_log(f"[Workflow] 使用 WorkingContext 中的檔案路徑: {context_path}")
                    
                    result = StepResult.success(
                        f"使用上下文中的檔案: {Path(context_path).name}",
                        {"file_selection": str(context_path)}
                    )
                    
                    result.llm_review_data = {
                        "action": "file_selected_for_archive",
                        "file_name": Path(context_path).name,
                        "file_path": str(context_path),
                        "requires_user_response": True,
                        "should_end_session": False
                    }
                    
                    return result
            except Exception as e:
                debug_log(2, f"[Workflow] 無法從 WorkingContext 讀取檔案路徑: {e}")
            
            # 3. 都沒有，開啟對話框
            return get_archive_file_path_via_dialog(self.session)
            
        def should_auto_advance(self) -> bool:
            return False  # 需要 LLM 批准後才能繼續
    
    file_input_step = ArchiveFileDialogStep(session)
    
    # 步驟2: 詢問目標資料夾 (可選)
    def validate_and_resolve_path(path: str) -> tuple[bool, str]:
        """驗證並解析路徑
        
        TODO: 技術債務 - 目前固定返回 D:\\ 用於測試
        未來需要實現自然語言路徑解析，例如：
        - 'd drive root' -> 'D:\\'
        - 'documents' -> 'C:\\Users\\{user}\\Documents'
        - 'desktop' -> 'C:\\Users\\{user}\\Desktop'
        """
        if not path.strip():
            return (True, "")
        
        # 🔧 暫時固定使用 D:\\ 進行測試
        resolved_path = "D:\\"
        
        if os.path.exists(resolved_path):
            return (True, "")
        else:
            return (False, f"目標資料夾不存在: {resolved_path}")
    
    target_input_step = StepTemplate.create_input_step(
        session,
        "target_dir_input",
        "請輸入目標資料夾路徑:",
        validator=validate_and_resolve_path,
        required_data=["file_selection"],
        optional=True,  # 接受沒有輸入（fallback）
        skip_if_data_exists=True,  # 接受初始數據（有數據就跳過）
        description="詢問用戶是否指定目標資料夾，留空則自動選擇。如果 initial_data 中已有目標路徑則直接跳過。"
    )
    
    # 步驟3: 確認歸檔操作
    def get_archive_confirmation_message():
        file_path = session.get_data("file_selection", "")
        target_dir = session.get_data("target_dir_input", "").strip()
        
        # 🔧 如果有輸入，使用固定的 D:\\ 路徑
        if target_dir:
            resolved_target = "D:\\"
            return f"確認要將檔案 {Path(file_path).name} 歸檔到 {resolved_target} ?"
        else:
            return f"確認要自動歸檔檔案 {Path(file_path).name} ?"
    
    archive_confirm_step = StepTemplate.create_confirmation_step(
        session,
        "archive_confirm",
        get_archive_confirmation_message,
        "確認歸檔",
        "取消歸檔",
        ["file_selection"],
        description="等待用戶確認是否執行歸檔操作"
    )
    
    # 步驟4: 執行歸檔
    def execute_archive(session):
        file_path = session.get_data("file_selection", "")
        target_dir = session.get_data("target_dir_input", "").strip()
        
        # 🔧 如果有輸入，使用固定的 D:\\ 路徑
        if target_dir:
            target_dir = "D:\\"
        
        try:
            debug_log(2, f"[Workflow] 開始歸檔檔案: {file_path} -> {target_dir or '自動選擇'}")
            result_path = intelligent_archive(file_path, target_dir)
            
            # 🔧 工作流完成：直接返回結果，不需要 LLM 審核
            # 最後一個步驟已經是最終結果，不應該再讓 LLM 生成回應
            return StepResult.complete_workflow(
                f"📁 智慧歸檔工作流程完成！原檔案: {Path(file_path).name}, 新位置: {result_path}",
                {
                    "original_path": file_path,
                    "archived_path": result_path,
                    "target_dir": target_dir,
                    "completion_time": datetime.datetime.now().isoformat()
                }
            )
        except Exception as e:
            error_log(f"[Workflow] 檔案歸檔失敗: {e}")
            return StepResult.failure(f"檔案歸檔失敗: {e}")
    
    archive_step = StepTemplate.create_auto_step(
        session,
        "execute_archive",
        execute_archive,
        ["file_selection", "target_dir_input"],
        "正在歸檔檔案...",
        description="自動執行檔案歸檔操作並完成工作流程"
    )
    
    # 建立工作流程
    workflow_def.add_step(file_input_step)
    workflow_def.add_step(target_input_step)
    workflow_def.add_step(archive_confirm_step)
    workflow_def.add_step(archive_step)
    
    # 根據初始數據決定入口點和轉換
    if initial_file_path and os.path.exists(initial_file_path):
        # 已有檔案路徑，跳過檔案選擇步驟
        info_log(f"[Workflow] 使用初始檔案路徑: {initial_file_path}")
        session.add_data("file_selection", initial_file_path)
        
        if initial_target_dir:
            # 已有目標資料夾，跳過目標輸入步驟
            info_log(f"[Workflow] 使用初始目標資料夾: {initial_target_dir}")
            session.add_data("target_dir_input", initial_target_dir)
            # 直接進入確認步驟
            workflow_def.set_entry_point("archive_confirm")
        else:
            # 只有檔案路徑，進入目標輸入步驟
            workflow_def.set_entry_point("target_dir_input")
            workflow_def.add_transition("target_dir_input", "archive_confirm")
    else:
        # 沒有初始數據，從檔案選擇開始
        workflow_def.set_entry_point("file_selection")
        workflow_def.add_transition("file_selection", "target_dir_input")
        workflow_def.add_transition("target_dir_input", "archive_confirm")
    
    workflow_def.add_transition("archive_confirm", "execute_archive")
    
    # 創建引擎並啟用自動推進
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


def create_summarize_tag_workflow(session: WorkflowSession) -> WorkflowEngine:
    """創建摘要標籤工作流程"""
    workflow_def = WorkflowDefinition(
        workflow_type="summarize_tag",
        name="摘要標籤工作流程",
        description="等待使用者提供檔案路徑，可選標籤數量，確認後使用LLM生成摘要和標籤",
        workflow_mode=WorkflowMode.DIRECT,  # 使用同步模式以支援 LLM 互動
        requires_llm_review=True,  # ✅ 啟用 LLM 審核，讓 LLM 知道每個步驟的結果
        auto_advance_on_approval=True
    )
    
    # 🔧 步驟1: 開啟檔案選擇對話框（SYSTEM 步驟）
    def get_summary_file_path_via_dialog(session):
        """使用檔案選擇對話框獲取要生成摘要的檔案路徑（線程安全）"""
        try:
            from utils.safe_file_dialog import open_file_dialog_sync
            
            info_log("[Workflow] 開啟檔案選擇對話框（摘要標籤）...")
            
            file_path = open_file_dialog_sync(
                title="請選擇要生成摘要的檔案",
                filetypes=[
                    ("所有檔案", "*.*"),
                    ("文字檔案", "*.txt"),
                    ("Markdown", "*.md"),
                    ("文件", "*.doc;*.docx;*.pdf"),
                    ("程式碼", "*.py;*.js;*.java;*.cpp;*.cs"),
                ]
            )
            
            if not file_path:
                return StepResult.failure("No file selected")
            
            if not os.path.exists(file_path):
                return StepResult.failure(f"檔案不存在: {file_path}")
            
            info_log(f"[Workflow] 已選擇要生成摘要的檔案: {file_path}")
            return StepResult.success(
                f"使用者選擇了檔案: {Path(file_path).name}",
                {"file_path_input": file_path}
            )
        except Exception as e:
            error_log(f"[Workflow] 獲取檔案路徑失敗: {e}")
            return StepResult.failure(f"獲取檔案路徑失敗: {e}")
    
    class SummaryFileDialogStep(WorkflowStep):
        def __init__(self, session):
            super().__init__(session)
            self.set_id("file_path_input")
            self.set_step_type(self.STEP_TYPE_SYSTEM)  # 系統操作步驟
            self.set_description("透過檔案選擇對話框獲取要生成摘要的檔案路徑")
            
        def get_prompt(self) -> str:
            return "開啟檔案選擇對話框（摘要標籤）..."
            
        def execute(self, user_input: Any = None) -> StepResult:
            # 🔧 優先順序：
            # 1. session 中的 initial_data
            # 2. WorkingContext 中的先行資料
            # 3. 開啟檔案對話框
            
            # 1. 檢查 session 中是否已有路徑
            existing_path = self.session.get_data("file_path_input", "")
            if existing_path:
                info_log(f"[Workflow] 使用 session 中的檔案路徑: {existing_path}")
                if not os.path.exists(existing_path):
                    return StepResult.failure(f"檔案不存在: {existing_path}")
                return StepResult.success(
                    f"使用者提供了檔案: {Path(existing_path).name}",
                    {"file_path_input": existing_path}
                )
            
            # 2. 檢查 WorkingContext 中是否有路徑
            try:
                from core.working_context import working_context_manager
                context_path = working_context_manager.get_context_data("current_file_path")
                if context_path and os.path.exists(str(context_path)):
                    info_log(f"[Workflow] 使用 WorkingContext 中的檔案路徑: {context_path}")
                    return StepResult.success(
                        f"使用上下文中的檔案: {Path(context_path).name}",
                        {"file_path_input": str(context_path)}
                    )
            except Exception as e:
                debug_log(2, f"[Workflow] 無法從 WorkingContext 讀取檔案路徑: {e}")
            
            # 3. 都沒有，開啟對話框
            return get_summary_file_path_via_dialog(self.session)
            
        def should_auto_advance(self) -> bool:
            return False  # 需要 LLM 批准後才能繼續
    
    file_input_step = SummaryFileDialogStep(session)
    
    # 步驟2: 詢問標籤數量 (可選)
    tag_count_step = StepTemplate.create_input_step(
        session,
        "tag_count_input",
        "請輸入要生成的標籤數量 (預設為3個，直接按Enter使用預設值):",
        validator=lambda count: (True, "") if not count.strip() else (count.strip().isdigit() and int(count.strip()) > 0, "標籤數量必須是正整數"),
        required_data=["file_path_input"],
        optional=True,
        description="詢問用戶想要生成多少個標籤，留空使用預設值 3"
    )
    
    # 步驟3: 確認摘要操作
    def get_summary_confirmation_message():
        file_path = session.get_data("file_path_input", "")
        tag_count_input = session.get_data("tag_count_input", "").strip()
        tag_count = int(tag_count_input) if tag_count_input else 3
        
        return f"確認要為檔案 {Path(file_path).name} 生成摘要和 {tag_count} 個標籤嗎?"
    
    summary_confirm_step = StepTemplate.create_confirmation_step(
        session,
        "summary_confirm",
        get_summary_confirmation_message,
        "確認生成摘要",
        "取消摘要",
        ["file_path_input"],
        description="等待用戶確認是否使用 LLM 生成摘要和標籤"
    )
    
    # 🔧 步驟4: 讀取檔案內容 (SYSTEM 步驟)
    def read_file_content(session):
        """讀取檔案內容，準備給LLM處理"""
        file_path = session.get_data("file_path_input", "")
        
        try:
            from modules.sys_module.actions.file_interaction import drop_and_read
            
            debug_log(2, f"[Workflow] 讀取檔案內容: {file_path}")
            content = drop_and_read(file_path)
            
            # 限制內容長度（避免過長）
            max_length = 5000
            truncated_content = content[:max_length]
            if len(content) > max_length:
                truncated_content += f"\n\n...(內容已截斷，原始長度: {len(content)} 字符)"
            
            return StepResult.success(
                f"已讀取檔案內容，長度: {len(content)} 字符",
                {"file_content": truncated_content, "full_content_length": len(content)}
            )
        except Exception as e:
            error_log(f"[Workflow] 讀取檔案失敗: {e}")
            return StepResult.failure(f"讀取檔案失敗: {e}")
    
    read_step = StepTemplate.create_auto_step(
        session,
        "read_file_content",
        read_file_content,
        ["file_path_input"],
        "正在讀取檔案內容...",
        description="讀取用戶選擇的檔案內容，準備進行摘要生成"
    )
    
    # 🔧 步驟5: LLM生成摘要和標籤 (LLM_PROCESSING 步驟)
    def build_summary_prompt(session):
        """構建摘要生成的提示詞"""
        file_path = session.get_data("file_path_input", "")
        file_content = session.get_data("file_content", "")
        tag_count_input = session.get_data("tag_count_input", "").strip()
        tag_count = int(tag_count_input) if tag_count_input else 3
        
        debug_log(2, f"[Workflow] build_summary_prompt - file_path: {file_path}")
        debug_log(2, f"[Workflow] build_summary_prompt - file_content length: {len(file_content) if file_content else 0}")
        debug_log(2, f"[Workflow] build_summary_prompt - tag_count: {tag_count}")
        
        prompt = f"""Please generate a summary and tags for the following file content:

File name: {Path(file_path).name}
File content:
{file_content}

Please respond in the following format:
Tags: tag1, tag2, tag3{', ...' if tag_count > 3 else ''}
Summary: [Write the summary content here]

Requirements:
1. Generate {tag_count} relevant key tags
2. Provide a concise but comprehensive summary (approximately 100-300 words)
3. Tags should reflect the main themes and content characteristics of the file
4. Summary should outline the core content and key points of the file
"""
        return prompt
    
    llm_summary_step = StepTemplate.create_llm_processing_step(
        session,
        "llm_generate_summary",
        "為檔案生成摘要和標籤",
        ["file_path_input", "file_content", "tag_count_input"],
        "llm_summary_result",
        required_data=["file_path_input", "file_content"],
        llm_prompt_builder=build_summary_prompt,
        description="使用LLM分析檔案內容，生成摘要和相關標籤"
    )
    
    # 🔧 步驟6: 保存摘要到檔案 (SYSTEM 步驟)
    def save_summary_file(session):
        """將LLM生成的摘要保存到檔案"""
        file_path = session.get_data("file_path_input", "")
        llm_result = session.get_data("llm_summary_result", "")
        tag_count_input = session.get_data("tag_count_input", "").strip()
        tag_count = int(tag_count_input) if tag_count_input else 3
        
        try:
            debug_log(2, f"[Workflow] 解析LLM摘要結果並保存檔案")
            
            # 解析LLM回應（提取標籤和摘要）
            tags = []
            summary = ""
            
            lines = llm_result.split('\n')
            for line in lines:
                line = line.strip()
                # 支援英文和中文格式
                if (("Tags:" in line or "Tags：" in line or "標籤：" in line or "標籤:" in line) and not tags):
                    # 找到冒號後的內容
                    if "：" in line:
                        tags_line = line.split("：")[1]
                    else:
                        tags_line = line.split(":")[1]
                    tags = [tag.strip() for tag in tags_line.split(',') if tag.strip()]
                elif (("Summary:" in line or "Summary：" in line or "摘要：" in line or "摘要:" in line) and not summary):
                    # 找到冒號後的內容
                    if "：" in line:
                        summary = line.split("：")[1]
                    else:
                        summary = line.split(":")[1]
                elif summary and line:  # 摘要可能跨多行
                    summary += " " + line
            
            # 如果沒有解析到，使用整個回應
            if not tags:
                tags = ["未能解析標籤"]
            if not summary:
                summary = llm_result
            
            # 確保標籤數量正確
            if len(tags) > tag_count:
                tags = tags[:tag_count]
            
            # 生成摘要檔案路徑
            file_path_obj = Path(file_path)
            desktop_path = Path.home() / "Desktop"
            summary_file_name = f"{file_path_obj.stem}_summary.txt"
            summary_file_path = desktop_path / summary_file_name
            
            # 寫入摘要檔案
            summary_content = f"檔案: {file_path_obj.name}\n"
            summary_content += f"生成時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            summary_content += f"標籤: {', '.join(tags)}\n\n"
            summary_content += f"摘要:\n{summary}\n"
            
            with open(summary_file_path, 'w', encoding='utf-8') as f:
                f.write(summary_content)
            
            info_log(f"[Workflow] 摘要檔案已保存: {summary_file_path}")
            
            return StepResult.complete_workflow(
                f"📝 摘要標籤工作流程完成！\n檔案: {file_path_obj.name}\n摘要檔案: {summary_file_path}\n標籤: {', '.join(tags)}",
                {
                    "original_file": file_path,
                    "summary_file": str(summary_file_path),
                    "tags": tags,
                    "summary": summary,
                    "tag_count": len(tags),
                    "completion_time": datetime.datetime.now().isoformat()
                }
            )
        except Exception as e:
            error_log(f"[Workflow] 保存摘要檔案失敗: {e}")
            return StepResult.failure(f"保存摘要檔案失敗: {e}")
    
    save_step = StepTemplate.create_auto_step(
        session,
        "save_summary_file",
        save_summary_file,
        ["file_path_input", "llm_summary_result"],
        "正在保存摘要檔案...",
        description="將LLM生成的摘要和標籤保存到桌面上的txt檔案"
    )
    
    # 建立工作流程
    workflow_def.add_step(file_input_step)
    workflow_def.add_step(tag_count_step)
    workflow_def.add_step(summary_confirm_step)
    workflow_def.add_step(read_step)
    workflow_def.add_step(llm_summary_step)
    workflow_def.add_step(save_step)
    
    workflow_def.set_entry_point("file_path_input")
    workflow_def.add_transition("file_path_input", "tag_count_input")
    workflow_def.add_transition("tag_count_input", "summary_confirm")
    workflow_def.add_transition("summary_confirm", "read_file_content")
    workflow_def.add_transition("read_file_content", "llm_generate_summary")
    workflow_def.add_transition("llm_generate_summary", "save_summary_file")
    
    # 創建引擎並啟用自動推進
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


def create_file_workflow(workflow_type: str, session: WorkflowSession) -> WorkflowEngine:
    """
    根據工作流程類型創建對應的文件工作流程引擎
    
    Args:
        workflow_type: 工作流程類型 (drop_and_read, intelligent_archive, summarize_tag, file_processing)
        session: 工作流程會話
        
    Returns:
        對應的工作流程引擎
    """
    info_log(f"[FileWorkflows] 創建文件工作流程: {workflow_type}")
    
    if workflow_type == "drop_and_read":
        return create_drop_and_read_workflow(session)
    elif workflow_type == "intelligent_archive":
        return create_intelligent_archive_workflow(session)
    elif workflow_type == "summarize_tag":
        return create_summarize_tag_workflow(session)
    elif workflow_type in ["file_processing", "file_interaction"]:
        # 通用文件處理工作流程，讓用戶選擇具體操作
        return create_file_selection_workflow(session)
    else:
        raise ValueError(f"未知的文件工作流程類型: {workflow_type}")


def create_file_selection_workflow(session: WorkflowSession) -> WorkflowEngine:
    """創建文件操作選擇工作流程"""
    workflow_def = WorkflowDefinition(
        workflow_type="file_selection",
        name="文件操作選擇工作流程",
        description="讓用戶選擇要執行的文件操作類型",
        workflow_mode=WorkflowMode.DIRECT,  # 選擇流程使用直接模式
        requires_llm_review=False
    )
    
    # 步驟1: 選擇文件操作類型
    operation_step = StepTemplate.create_selection_step(
        session,
        "operation_selection",
        "請選擇要執行的文件操作:",
        ["drop_and_read", "intelligent_archive", "summarize_tag"],
        ["讀取檔案內容", "智慧歸檔檔案", "生成摘要標籤"]
    )
    
    # 步驟2: 重定向到對應的工作流程
    def redirect_to_workflow(session):
        operation = session.get_data("operation_selection", "")
        
        if operation in ["drop_and_read", "intelligent_archive", "summarize_tag"]:
            return StepResult.complete_workflow(
                f"已選擇操作: {operation}，請使用 start_workflow 啟動對應的工作流程",
                {
                    "selected_operation": operation,
                    "redirect_workflow": operation,
                    "completion_time": datetime.datetime.now().isoformat()
                }
            )
        else:
            return StepResult.failure("Invalid operation selection")
    
    redirect_step = StepTemplate.create_processing_step(
        session,
        "redirect_workflow",
        redirect_to_workflow,
        ["operation_selection"]
    )
    
    # 建立工作流程
    workflow_def.add_step(operation_step)
    workflow_def.add_step(redirect_step)
    
    workflow_def.set_entry_point("operation_selection")
    workflow_def.add_transition("operation_selection", "redirect_workflow")
    
    return WorkflowEngine(workflow_def, session)


def get_available_file_workflows() -> List[str]:
    """獲取可用的文件工作流程列表"""
    return [
        "drop_and_read",
        "intelligent_archive", 
        "summarize_tag",
        "file_processing",
        "file_interaction"
    ]


def get_file_workflows_info() -> List[Dict[str, Any]]:
    """Get detailed information about file workflows (for NLP querying)"""
    return [
        {
            "workflow_type": "drop_and_read",
            "name": "File Reading Workflow",
            "description": "Read file content and provide summary",
            "work_mode": "direct",  # Direct work - immediate execution
            "keywords": ["read", "file", "content", "open", "view", "show", "display"],
        },
        {
            "workflow_type": "intelligent_archive",
            "name": "Intelligent Archive Workflow",
            "description": "Archive files with intelligent organization",
            "work_mode": "direct",
            "keywords": ["archive", "organize", "sort", "categorize", "files", "folder"],
        },
        {
            "workflow_type": "summarize_tag",
            "name": "Summarize and Tag Workflow",
            "description": "Summarize file content and add tags",
            "work_mode": "direct",
            "keywords": ["summarize", "tag", "label", "categorize", "metadata", "summary"],
        }
    ]
