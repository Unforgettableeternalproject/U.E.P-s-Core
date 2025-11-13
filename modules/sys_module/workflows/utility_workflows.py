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


# ==================== Translate Document Workflow ====================

def create_translate_document_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    文件翻譯工作流
    
    步驟：
    1. 輸入檔案路徑
    2. 選擇來源語言
    3. 選擇目標語言
    4. 輸入輸出路徑（可選）
    5. 執行翻譯
    """
    workflow_def = WorkflowDefinition(
        workflow_type="translate_document",
        name="文件翻譯",
        description="使用 LLM 翻譯文件（支援 TXT, PDF, DOCX）",
        workflow_mode=WorkflowMode.DIRECT
    )
    
    # 步驟 1: 輸入檔案路徑
    file_path_input_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_file_path",
        prompt="Enter file path to translate:",
        skip_if_data_exists=True,
        description="收集檔案路徑"
    )
    
    # 步驟 2: 驗證檔案
    def validate_file(session: WorkflowSession) -> StepResult:
        import os
        
        file_path = session.get_data("input_file_path", "").strip().strip('"').strip("'")
        
        if not file_path:
            return StepResult.failure("Please provide a file path")
        
        if not os.path.exists(file_path):
            return StepResult.failure(f"File not found: {file_path}")
        
        # 檢查副檔名
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in ['.txt', '.pdf', '.docx']:
            return StepResult.failure(f"Unsupported file format: {ext} (Supported: .txt, .pdf, .docx)")
        
        return StepResult.success(
            f"File validated: {os.path.basename(file_path)}",
            {"validated_file_path": file_path, "file_ext": ext}
        )
    
    validate_file_step = StepTemplate.create_processing_step(
        session=session,
        step_id="validate_file",
        processor=validate_file,
        required_data=["input_file_path"],
        description="驗證檔案"
    )
    
    # 步驟 3: 選擇來源語言
    # 支援的語言列表
    SUPPORTED_LANGUAGES = [
        ("auto", "Auto Detect"),
        ("en", "English"),
        ("zh-TW", "Traditional Chinese"),
        ("zh-CN", "Simplified Chinese"),
        ("ja", "Japanese"),
        ("ko", "Korean"),
        ("fr", "French"),
        ("de", "German"),
        ("es", "Spanish"),
        ("it", "Italian"),
        ("pt", "Portuguese"),
        ("ru", "Russian"),
        ("ar", "Arabic"),
        ("th", "Thai"),
        ("vi", "Vietnamese"),
        ("id", "Indonesian"),
        ("ms", "Malay"),
        ("hi", "Hindi"),
        ("tr", "Turkish"),
        ("nl", "Dutch")
    ]
    
    source_lang_step = StepTemplate.create_selection_step(
        session=session,
        step_id="select_source_lang",
        prompt="Select source language:",
        options=[lang[0] for lang in SUPPORTED_LANGUAGES],
        labels=[lang[1] for lang in SUPPORTED_LANGUAGES],
        required_data=[]
    )
    
    # 步驟 4: 選擇目標語言
    target_lang_step = StepTemplate.create_selection_step(
        session=session,
        step_id="select_target_lang",
        prompt="Select target language:",
        options=[lang[0] for lang in SUPPORTED_LANGUAGES if lang[0] != "auto"],
        labels=[lang[1] for lang in SUPPORTED_LANGUAGES if lang[0] != "auto"],
        required_data=[]
    )
    
    # 步驟 5: 輸入輸出路徑（可選）
    output_path_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_output_path",
        prompt="Enter output file path (press Enter to use default path [original_filename_translated.ext]):",
        skip_if_data_exists=False,
        description="收集輸出路徑"
    )
    
    # 步驟 6: 處理輸出路徑
    def process_output_path(session: WorkflowSession) -> StepResult:
        import os
        
        output_path = session.get_data("input_output_path", "").strip().strip('"').strip("'")
        file_path = session.get_data("validated_file_path", "")
        
        if not output_path:
            # 使用預設路徑
            base, ext = os.path.splitext(file_path)
            output_path = f"{base}_translated{ext}"
        
        return StepResult.success(
            f"Output path: {os.path.basename(output_path)}",
            {"output_path": output_path}
        )
    
    process_output_step = StepTemplate.create_processing_step(
        session=session,
        step_id="process_output_path",
        processor=process_output_path,
        required_data=["input_output_path", "validated_file_path"],
        description="處理輸出路徑"
    )
    
    # 步驟 7: 執行翻譯
    def execute_translation(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.file_interaction import translate_document
        
        file_path = session.get_data("validated_file_path", "")
        source_lang = session.get_data("select_source_lang", "auto")
        target_lang = session.get_data("select_target_lang", "zh-TW")
        output_path = session.get_data("output_path", "")
        
        info_log(f"[Workflow] 翻譯文件：{file_path} -> {output_path}, {source_lang} -> {target_lang}")
        
        try:
            output_file = translate_document(
                file_path=file_path,
                source_lang=source_lang,
                target_lang=target_lang,
                output_path=output_path
            )  # 返回輸出文件路徑（字符串）
            
            return StepResult.complete_workflow(
                f"Translation completed successfully!\nOutput file: {output_file}",
                {
                    "output_file": output_file,
                    "source_lang": source_lang,
                    "target_lang": target_lang
                }
            )
        except Exception as e:
            return StepResult.failure(f"Translation failed: {str(e)}")
    
    translation_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_translation",
        processor=execute_translation,
        required_data=["validated_file_path", "select_source_lang", "select_target_lang", "output_path"],
        description="執行翻譯"
    )
    
    # 組裝工作流
    workflow_def.add_step(file_path_input_step)
    workflow_def.add_step(validate_file_step)
    workflow_def.add_step(source_lang_step)
    workflow_def.add_step(target_lang_step)
    workflow_def.add_step(output_path_step)
    workflow_def.add_step(process_output_step)
    workflow_def.add_step(translation_step)
    
    workflow_def.set_entry_point("input_file_path")
    workflow_def.add_transition("input_file_path", "validate_file")
    workflow_def.add_transition("validate_file", "select_source_lang")
    workflow_def.add_transition("select_source_lang", "select_target_lang")
    workflow_def.add_transition("select_target_lang", "input_output_path")
    workflow_def.add_transition("input_output_path", "process_output_path")
    workflow_def.add_transition("process_output_path", "execute_translation")
    workflow_def.add_transition("execute_translation", "END")
    
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True  # 啟用自動推進
    return engine


# ==================== Workflow Registry ====================

def get_available_utility_workflows() -> list:
    """獲取可用的工具工作流列表"""
    return ["clean_trash_bin", "translate_document"]


def create_utility_workflow(workflow_type: str, session: WorkflowSession) -> WorkflowEngine:
    """創建工具工作流"""
    workflows = {
        "clean_trash_bin": create_clean_trash_bin_workflow,
        "translate_document": create_translate_document_workflow
    }
    
    if workflow_type not in workflows:
        raise ValueError(f"未知的工作流類型：{workflow_type}")
    
    return workflows[workflow_type](session)
