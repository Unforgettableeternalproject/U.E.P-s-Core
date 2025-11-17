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
from modules.sys_module.workflows import (
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowMode,
    WorkflowStep,
    StepResult
)
from modules.sys_module.step_templates import StepTemplate
from modules.sys_module.actions.file_interaction import (
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
    
    # 步驟1: 檔案選擇
    file_input_step = StepTemplate.create_file_selection_step(
        session,
        "file_path_input",
        "請選擇要讀取的檔案:",
        file_types=[".txt", ".md", ".py", ".json"],
        multiple=False,
        skip_if_data_exists=True,
        description="選擇要讀取的檔案（支援 .txt, .md, .py, .json 格式）"
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
    
    # 步驟1: 檔案選擇
    file_input_step = StepTemplate.create_file_selection_step(
        session,
        "file_selection",
        "請選擇要歸檔的檔案:",
        file_types=[],  # 接受所有檔案類型
        multiple=False,
        skip_if_data_exists=True,
        description="選擇要歸檔的檔案（支援所有格式）"
    )
    
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
    # 🔧 優先檢查 WorkingContext（前端拖曳檔案）
    context_file_path = None
    try:
        from core.working_context import working_context_manager
        context_path = working_context_manager.get_context_data("current_file_path")
        if context_path and os.path.exists(str(context_path)):
            context_file_path = str(context_path)
            debug_log(2, f"[Workflow] 檢測到 WorkingContext 中的檔案路徑: {context_file_path}")
    except Exception as e:
        debug_log(2, f"[Workflow] 無法讀取 WorkingContext: {e}")
    
    # 決定有效的檔案路徑（WorkingContext 優先）
    effective_file_path = context_file_path or (initial_file_path if initial_file_path and os.path.exists(initial_file_path) else None)
    
    if effective_file_path:
        # 已有有效檔案路徑，跳過檔案選擇步驟
        info_log(f"[Workflow] 使用檔案路徑: {effective_file_path}")
        session.add_data("file_selection", effective_file_path)
        
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
        # 沒有有效檔案路徑
        if initial_target_dir:
            # 只有目標資料夾，從檔案選擇開始但跳過目標輸入
            info_log(f"[Workflow] 使用初始目標資料夾: {initial_target_dir}")
            session.add_data("target_dir_input", initial_target_dir)
            workflow_def.set_entry_point("file_selection")
            workflow_def.add_transition("file_selection", "archive_confirm")
        else:
            # 沒有初始數據，從檔案選擇開始，完整流程
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
    
    # 步驟1: 檔案選擇
    file_input_step = StepTemplate.create_file_selection_step(
        session,
        "file_path_input",
        "請選擇要生成摘要的檔案:",
        file_types=[".txt", ".md", ".doc", ".docx", ".pdf", ".py", ".js", ".java", ".cpp", ".cs"],
        multiple=False,
        skip_if_data_exists=True,
        description="選擇要生成摘要的檔案（支援文字、文件和程式碼格式）"
    )
    
    # 步驟2: 詢問標籤數量 (可選)
    tag_count_step = StepTemplate.create_input_step(
        session,
        "tag_count_input",
        "請輸入要生成的標籤數量 (預設為3個，直接按Enter使用預設值):",
        validator=lambda count: (True, "") if not count.strip() else (count.strip().isdigit() and int(count.strip()) > 0, "標籤數量必須是正整數"),
        required_data=["file_path_input"],
        optional=True,
        skip_if_data_exists=True,  # 🔧 如果 initial_data 提供了數據，跳過此步驟
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
        workflow_type: 工作流程類型 (drop_and_read, intelligent_archive, summarize_tag, translate_document, ocr_extract)
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
    elif workflow_type == "translate_document":
        return create_translate_document_workflow(session)
    elif workflow_type == "ocr_extract":
        return create_ocr_extract_workflow(session)
    else:
        raise ValueError(f"未知的文件工作流程類型: {workflow_type}")


def get_available_file_workflows() -> List[str]:
    """獲取可用的文件工作流程列表"""
    return [
        "drop_and_read",
        "intelligent_archive", 
        "summarize_tag",
        "translate_document",
        "ocr_extract"
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
        },
        {
            "workflow_type": "translate_document",
            "name": "Document Translation Workflow",
            "description": "Translate document to target language using LLM",
            "work_mode": "direct",
            "keywords": ["translate", "translation", "language", "convert"],
        },
        {
            "workflow_type": "ocr_extract",
            "name": "OCR Text Recognition Workflow",
            "description": "Extract text from images using OCR",
            "work_mode": "direct",
            "keywords": ["ocr", "text", "recognition", "image", "extract", "辨識", "圖片"],
        }
    ]


def create_translate_document_workflow(session: WorkflowSession) -> WorkflowEngine:
    """創建文件翻譯工作流程
    
    工作流程步驟:
    1. 檔案選擇 - 選擇要翻譯的檔案
    2. 目標語言輸入 - 輸入目標語言
    3. LLM處理 - 使用LLM翻譯文件內容
    4. 寫入檔案 - 將翻譯結果保存到桌面
    """
    workflow_def = WorkflowDefinition(
        workflow_type="translate_document",
        name="文件翻譯工作流程",
        description="使用LLM將文件翻譯為目標語言",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=True
    )
    
    # 步驟1: 檔案選擇
    file_input_step = StepTemplate.create_file_selection_step(
        session,
        "file_path_input",
        "請選擇要翻譯的檔案:",
        file_types=[".txt", ".md"],
        multiple=False,
        skip_if_data_exists=True,
        description="選擇要翻譯的檔案（支援 .txt, .md 格式）"
    )
    
    # 步驟2: 目標語言輸入
    target_lang_step = StepTemplate.create_input_step(
        session,
        "target_language",
        "請輸入目標語言 (例如: English, 日本語, 한국어):",
        validator=lambda lang: (bool(lang.strip()), "目標語言不能為空"),
        required_data=["file_path_input"],
        optional=False,
        skip_if_data_exists=True,
        description="輸入要翻譯成的目標語言"
    )
    
    # 步驟3: 讀取檔案內容 (SYSTEM 步驟)
    def read_file_for_translation(session):
        """讀取檔案內容，準備翻譯"""
        file_path = session.get_data("file_path_input", "")
        
        try:
            from modules.sys_module.actions.file_interaction import drop_and_read
            
            debug_log(2, f"[Workflow] 讀取檔案內容進行翻譯: {file_path}")
            content = drop_and_read(file_path)
            
            if not content.strip():
                return StepResult.failure("檔案內容為空")
            
            # 限制內容長度（避免超出 token 限制）
            # 翻譯需要較大空間，設定為 10000 字符
            max_length = 10000
            if len(content) > max_length:
                return StepResult.failure(
                    f"檔案內容過長（{len(content)} 字符），超過限制（{max_length} 字符）。\n"
                    f"請選擇較小的檔案或將內容分段翻譯。"
                )
            
            return StepResult.success(
                f"已讀取檔案內容，長度: {len(content)} 字符",
                {"file_content": content, "original_content_length": len(content)}
            )
        except Exception as e:
            error_log(f"[Workflow] 讀取檔案失敗: {e}")
            return StepResult.failure(f"讀取檔案失敗: {e}")
    
    read_step = StepTemplate.create_auto_step(
        session,
        "read_file_content",
        read_file_for_translation,
        ["file_path_input"],
        "正在讀取檔案內容...",
        description="讀取要翻譯的檔案內容"
    )
    
    # 步驟4: LLM翻譯 (LLM_PROCESSING 步驟)
    def build_translation_prompt(session):
        """構建翻譯提示詞"""
        file_path = session.get_data("file_path_input", "")
        file_content = session.get_data("file_content", "")
        target_language = session.get_data("target_language", "")
        
        debug_log(2, f"[Workflow] build_translation_prompt - 目標語言: {target_language}")
        debug_log(2, f"[Workflow] build_translation_prompt - 內容長度: {len(file_content) if file_content else 0}")
        
        prompt = f"""Please translate the following file content into {target_language}:

File name: {Path(file_path).name}

Original content:
{file_content}

Translation requirements:
1. Maintain the original format and structure
2. Ensure the translation is accurate and conforms to the target language conventions
3. Preserve special characters, punctuation, and line breaks
4. Only output the translated content, no additional explanations

Please directly output the translation result:"""
        return prompt
    
    llm_translate_step = StepTemplate.create_llm_processing_step(
        session,
        "llm_translate",
        "使用LLM翻譯文件內容",
        ["file_path_input", "file_content", "target_language"],
        "translation_result",
        required_data=["file_path_input", "file_content", "target_language"],
        llm_prompt_builder=build_translation_prompt,
        description="使用LLM將文件內容翻譯為目標語言"
    )
    
    # 步驟5: 保存翻譯結果到桌面 (SYSTEM 步驟)
    def save_translation_file(session):
        """將翻譯結果保存到桌面"""
        file_path = session.get_data("file_path_input", "")
        translation_result = session.get_data("translation_result", "")
        target_language = session.get_data("target_language", "")
        
        try:
            debug_log(2, f"[Workflow] 保存翻譯結果到桌面")
            
            # 構建輸出檔案名稱
            original_path = Path(file_path)
            original_name = original_path.stem
            original_ext = original_path.suffix
            
            # 生成桌面路徑
            desktop_path = Path.home() / "Desktop"
            output_file_name = f"{original_name}_translated_to_{target_language}{original_ext}"
            output_file_path = desktop_path / output_file_name
            
            # 如果檔案已存在，添加數字後綴
            counter = 1
            while output_file_path.exists():
                output_file_name = f"{original_name}_translated_to_{target_language}_{counter}{original_ext}"
                output_file_path = desktop_path / output_file_name
                counter += 1
            
            # 寫入翻譯結果
            with open(output_file_path, 'w', encoding='utf-8') as f:
                f.write(translation_result)
            
            info_log(f"[Workflow] 翻譯結果已保存: {output_file_path}")
            
            return StepResult.complete_workflow(
                f"翻譯完成！檔案已保存到桌面: {output_file_name}",
                {
                    "output_file_path": str(output_file_path),
                    "output_file_name": output_file_name,
                    "target_language": target_language,
                    "translation_length": len(translation_result),
                    "completion_time": datetime.datetime.now().isoformat()
                }
            )
        except Exception as e:
            error_log(f"[Workflow] 保存翻譯結果失敗: {e}")
            return StepResult.failure(f"保存翻譯結果失敗: {e}")
    
    save_step = StepTemplate.create_auto_step(
        session,
        "save_translation",
        save_translation_file,
        ["file_path_input", "translation_result", "target_language"],
        "正在保存翻譯結果...",
        description="將翻譯結果保存到桌面"
    )
    
    # 建立工作流程
    workflow_def.add_step(file_input_step)
    workflow_def.add_step(target_lang_step)
    workflow_def.add_step(read_step)
    workflow_def.add_step(llm_translate_step)
    workflow_def.add_step(save_step)
    
    workflow_def.set_entry_point("file_path_input")
    workflow_def.add_transition("file_path_input", "target_language")
    workflow_def.add_transition("target_language", "read_file_content")
    workflow_def.add_transition("read_file_content", "llm_translate")
    workflow_def.add_transition("llm_translate", "save_translation")
    
    return WorkflowEngine(workflow_def, session)


def create_ocr_extract_workflow(session: WorkflowSession) -> WorkflowEngine:
    """創建 OCR 文字辨識工作流程
    
    工作流程步驟:
    1. 檔案選擇 - 選擇要辨識的圖片檔案
    2. 確認儲存 - 詢問是否要將辨識結果儲存為檔案
    3. OCR 處理 - 執行 OCR 辨識並根據選擇儲存或返回結果
    """
    workflow_def = WorkflowDefinition(
        workflow_type="ocr_extract",
        name="OCR 文字辨識工作流程",
        description="從圖片中辨識文字內容",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=False
    )
    
    # 步驟1: 圖片檔案選擇
    file_input_step = StepTemplate.create_file_selection_step(
        session,
        "file_path_input",
        "請選擇要辨識的圖片:",
        file_types=[".png", ".jpg", ".jpeg", ".bmp", ".tiff"],
        multiple=False,
        skip_if_data_exists=True,
        description="選擇要進行 OCR 辨識的圖片檔案"
    )
    
    # 步驟2: 確認是否儲存為檔案
    def get_save_confirmation_message():
        file_path = session.get_data("file_path_input", "")
        return f"是否要將 {Path(file_path).name} 的 OCR 辨識結果儲存為 txt 檔案到桌面？"
    
    save_confirm_step = StepTemplate.create_confirmation_step(
        session,
        "save_confirm",
        get_save_confirmation_message,
        "是，儲存為檔案",
        "否，只顯示結果",
        ["file_path_input"],
        description="詢問用戶是否要將 OCR 結果儲存為檔案"
    )
    
    # 步驟3: 執行 OCR 並處理結果
    def execute_ocr_and_save(session):
        """執行 OCR 辨識並根據用戶選擇儲存或返回結果"""
        from modules.sys_module.actions.text_processing import ocr_extract
        
        file_path = session.get_data("file_path_input", "")
        should_save = session.get_data("save_confirm", False)  # 讀取布爾值而非字串
        
        try:
            debug_log(2, f"[Workflow] 執行 OCR 辨識: {file_path}")
            debug_log(2, f"[Workflow] 儲存選項: {should_save}")
            
            # 執行 OCR 辨識（始終返回文字）
            ocr_result = ocr_extract(image_path=file_path, target_num=1)
            
            if not ocr_result or not ocr_result.strip():
                return StepResult.failure("OCR 辨識結果為空，可能圖片中沒有可辨識的文字")
            
            # 清理結果（移除 "辨識結果：" 前綴）
            if ocr_result.startswith("辨識結果："):
                ocr_result = ocr_result[6:].strip()
            
            if should_save:
                # 儲存為檔案到桌面
                original_path = Path(file_path)
                desktop_path = Path.home() / "Desktop"
                output_file_name = f"{original_path.stem}_OCR.txt"
                output_file_path = desktop_path / output_file_name
                
                # 如果檔案已存在，添加數字後綴
                counter = 1
                while output_file_path.exists():
                    output_file_name = f"{original_path.stem}_OCR_{counter}.txt"
                    output_file_path = desktop_path / output_file_name
                    counter += 1
                
                # 寫入檔案
                with open(output_file_path, 'w', encoding='utf-8') as f:
                    f.write(ocr_result)
                
                info_log(f"[Workflow] OCR 結果已儲存: {output_file_path}")
                
                # 生成預覽（前 200 字符）
                preview = ocr_result[:200] + "..." if len(ocr_result) > 200 else ocr_result
                
                return StepResult.complete_workflow(
                    f"OCR 辨識完成！檔案已儲存到桌面: {output_file_name}\n\n辨識結果預覽:\n{preview}",
                    {
                        "ocr_result": ocr_result,
                        "output_file_path": str(output_file_path),
                        "output_file_name": output_file_name,
                        "result_length": len(ocr_result),
                        "completion_time": datetime.datetime.now().isoformat()
                    }
                )
            else:
                # 只返回結果，不儲存 - 將完整結果放在 message 中讓 LLM 能向用戶回報
                info_log(f"[Workflow] OCR 辨識完成，結果長度: {len(ocr_result)}")
                
                return StepResult.complete_workflow(
                    f"OCR 辨識完成！\n\n辨識結果:\n{ocr_result}",
                    {
                        "ocr_result": ocr_result,
                        "result_length": len(ocr_result),
                        "completion_time": datetime.datetime.now().isoformat()
                    }
                )
                
        except Exception as e:
            error_log(f"[Workflow] OCR 辨識失敗: {e}")
            return StepResult.failure(f"OCR 辨識失敗: {e}")
    
    ocr_step = StepTemplate.create_auto_step(
        session,
        "execute_ocr",
        execute_ocr_and_save,
        ["file_path_input", "save_confirm"],
        "正在執行 OCR 辨識...",
        description="執行 OCR 辨識並處理結果"
    )
    
    # 建立工作流程
    workflow_def.add_step(file_input_step)
    workflow_def.add_step(save_confirm_step)
    workflow_def.add_step(ocr_step)
    
    workflow_def.set_entry_point("file_path_input")
    workflow_def.add_transition("file_path_input", "save_confirm")
    workflow_def.add_transition("save_confirm", "execute_ocr")
    
    return WorkflowEngine(workflow_def, session)
