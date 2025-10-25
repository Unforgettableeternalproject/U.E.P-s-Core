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
    # translate,  # TODO: 尚未實現
    # clean_trash_bin  # TODO: 尚未實現
)


def create_drop_and_read_workflow(session: WorkflowSession) -> WorkflowEngine:
    """創建檔案讀取工作流程"""
    workflow_def = WorkflowDefinition(
        workflow_type="drop_and_read",
        name="檔案讀取工作流程",
        description="讀取檔案內容並提供摘要",
        workflow_mode=WorkflowMode.BACKGROUND,  # 檔案工作流使用背景模式
        requires_llm_review=True,  # 啟用 LLM 審核
        auto_advance_on_approval=True
    )
    
    # 步驟1: 等待檔案路徑輸入
    file_input_step = StepTemplate.create_input_step(
        session,
        "file_path_input",
        "請輸入要讀取的檔案路徑:",
        validator=lambda path: (os.path.exists(path), f"檔案不存在: {path}") if path.strip() else (False, "請提供檔案路徑")
    )
    
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
            
            return StepResult.complete_workflow(
                f"📄 檔案讀取工作流程完成！檔案: {Path(file_path).name}, 內容長度: {len(content)} 字符",
                {
                    "file_path": file_path,
                    "content": content,
                    "content_length": len(content),
                    "completion_time": datetime.datetime.now().isoformat()
                }
            )
        except Exception as e:
            error_log(f"[Workflow] 檔案讀取失敗: {e}")
            return StepResult.failure(f"檔案讀取失敗: {e}")
    
    read_step = StepTemplate.create_auto_step(
        session,
        "execute_read",
        execute_file_read,
        ["file_path_input"],
        "正在讀取檔案..."
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
        workflow_mode=WorkflowMode.BACKGROUND,
        requires_llm_review=True,
        auto_advance_on_approval=True
    )
    
    # 檢查初始數據，決定入口點
    initial_file_path = session.get_data("file_path_input", "")
    initial_target_dir = session.get_data("target_dir_input", "")
    
    # 步驟1: 檔案路徑輸入（使用文字輸入配合檔案選擇視窗）
    file_input_step = StepTemplate.create_input_step(
        session,
        "file_selection",
        "請選擇要歸檔的檔案路徑:",
        validator=lambda path: (os.path.exists(path), f"檔案不存在: {path}") if path.strip() else (False, "請提供檔案路徑")
    )
    
    # 步驟2: 詢問目標資料夾 (可選)
    target_input_step = StepTemplate.create_input_step(
        session,
        "target_dir_input",
        "請輸入目標資料夾路徑:",
        validator=lambda path: (True, "") if not path.strip() or os.path.exists(path) else (False, f"目標資料夾不存在: {path}"),
        required_data=["file_selection"],
        optional=True
    )
    
    # 步驟3: 確認歸檔操作
    def get_archive_confirmation_message():
        file_path = session.get_data("file_selection", "")
        target_dir = session.get_data("target_dir_input", "").strip()
        
        if target_dir:
            return f"確認要將檔案 {Path(file_path).name} 歸檔到 {target_dir} ?"
        else:
            return f"確認要自動歸檔檔案 {Path(file_path).name} ?"
    
    archive_confirm_step = StepTemplate.create_confirmation_step(
        session,
        "archive_confirm",
        get_archive_confirmation_message,
        "確認歸檔",
        "取消歸檔",
        ["file_selection", "target_dir_input"]
    )
    
    # 步驟4: 執行歸檔
    def execute_archive(session):
        file_path = session.get_data("file_selection", "")
        target_dir = session.get_data("target_dir_input", "").strip()
        
        try:
            debug_log(2, f"[Workflow] 開始歸檔檔案: {file_path} -> {target_dir or '自動選擇'}")
            result_path = intelligent_archive(file_path, target_dir)
            
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
        "正在歸檔檔案..."
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
        workflow_mode=WorkflowMode.BACKGROUND,
        requires_llm_review=True,
        auto_advance_on_approval=True
    )
    
    # 步驟1: 等待檔案路徑輸入
    file_input_step = StepTemplate.create_input_step(
        session,
        "file_path_input",
        "請輸入要生成摘要的檔案路徑:",
        validator=lambda path: (os.path.exists(path), f"檔案不存在: {path}") if path.strip() else (False, "請提供檔案路徑")
    )
    
    # 步驟2: 詢問標籤數量 (可選)
    tag_count_step = StepTemplate.create_input_step(
        session,
        "tag_count_input",
        "請輸入要生成的標籤數量 (預設為3個，直接按Enter使用預設值):",
        validator=lambda count: (True, "") if not count.strip() else (count.strip().isdigit() and int(count.strip()) > 0, "標籤數量必須是正整數"),
        optional=True
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
        ["file_path_input", "tag_count_input"]
    )
    
    # 步驟4: 執行摘要生成 (使用LLM內部調用)
    def execute_summary(session):
        file_path = session.get_data("file_path_input", "")
        tag_count_input = session.get_data("tag_count_input", "").strip()
        tag_count = int(tag_count_input) if tag_count_input else 3
        
        try:
            debug_log(2, f"[Workflow] 開始生成摘要: {file_path}, 標籤數量: {tag_count}")
            result = summarize_tag(file_path, tag_count)
            
            return StepResult.complete_workflow(
                f"📝 摘要標籤工作流程完成！檔案: {Path(file_path).name}, 摘要檔案: {result['summary_file']}, 標籤: {', '.join(result['tags'])}",
                {
                    "original_file": file_path,
                    "summary_file": result["summary_file"],
                    "tags": result["tags"],
                    "tag_count": len(result["tags"]),
                    "completion_time": datetime.datetime.now().isoformat()
                }
            )
        except Exception as e:
            error_log(f"[Workflow] 摘要生成失敗: {e}")
            return StepResult.failure(f"摘要生成失敗: {e}")
    
    summary_step = StepTemplate.create_auto_step(
        session,
        "execute_summary",
        execute_summary,
        ["file_path_input", "tag_count_input"],
        "正在生成摘要和標籤..."
    )
    
    # 建立工作流程
    workflow_def.add_step(file_input_step)
    workflow_def.add_step(tag_count_step)
    workflow_def.add_step(summary_confirm_step)
    workflow_def.add_step(summary_step)
    
    workflow_def.set_entry_point("file_path_input")
    workflow_def.add_transition("file_path_input", "tag_count_input")
    workflow_def.add_transition("tag_count_input", "summary_confirm")
    workflow_def.add_transition("summary_confirm", "execute_summary")
    
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
            return StepResult.failure("無效的操作選擇")
    
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
