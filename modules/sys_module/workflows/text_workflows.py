"""
文字處理相關工作流
包含：clipboard_tracker, quick_phrases, ocr_extract
"""

from typing import Dict, Any
from pathlib import Path

from core.sessions.session_manager import WorkflowSession
from modules.sys_module.workflows import (
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowMode,
    StepResult,
    StepTemplate
)
from utils.debug_helper import info_log, error_log, debug_log


# ==================== Clipboard Tracker Workflow ====================

def create_clipboard_tracker_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    剪貼簿追蹤工作流
    
    步驟：
    1. 輸入搜尋關鍵字（可選，空白則返回全部）
    2. 輸入最大結果數（可選，預設5）
    3. 執行搜尋
    4. 詢問是否複製（可選）
    """
    workflow_def = WorkflowDefinition(
        workflow_type="clipboard_tracker",
        name="剪貼簿追蹤",
        description="搜尋剪貼簿歷史記錄",
        workflow_mode=WorkflowMode.DIRECT
    )
    
    # 步驟 1: 輸入關鍵字（可選）
    keyword_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_keyword",
        prompt="請輸入搜尋關鍵字（直接按Enter返回全部歷史）：",
        optional=True,
        skip_if_data_exists=True,
        description="收集搜尋關鍵字"
    )
    
    # 步驟 2: 輸入最大結果數（可選）
    max_results_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_max_results",
        prompt="請輸入最大結果數（預設5）：",
        optional=True,
        skip_if_data_exists=True,
        validator=lambda x: (x.isdigit() and int(x) > 0, "請輸入正整數"),
        description="收集最大結果數"
    )
    
    # 步驟 3: 執行搜尋
    def search_clipboard(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.text_processing import clipboard_tracker
        
        keyword = session.get_data("input_keyword", "")
        max_results_str = session.get_data("input_max_results", "5")
        max_results = int(max_results_str) if max_results_str.isdigit() else 5
        
        info_log(f"[Workflow] 搜尋剪貼簿：關鍵字='{keyword}', 最大結果={max_results}")
        
        result = clipboard_tracker(
            keyword=keyword,
            max_results=max_results,
            copy_index=-1  # 不立即複製
        )
        
        if result["status"] == "ok":
            results = result.get("results", [])
            if not results:
                return StepResult.complete_workflow("未找到相關記錄")
            
            # 格式化結果
            formatted_results = []
            for i, item in enumerate(results):
                preview = item[:50] + "..." if len(item) > 50 else item
                formatted_results.append(f"{i+1}. {preview}")
            
            message = f"找到 {len(results)} 條記錄：\n" + "\n".join(formatted_results)
            
            return StepResult.success(
                message,
                {
                    "search_results": results,
                    "result_count": len(results)
                }
            )
        else:
            return StepResult.failure(f"搜尋失敗：{result.get('message', '未知錯誤')}")
    
    search_step = StepTemplate.create_processing_step(
        session=session,
        step_id="search_clipboard",
        processor=search_clipboard,
        required_data=["input_keyword", "input_max_results"],
        description="執行剪貼簿搜尋"
    )
    
    # 步驟 4: 詢問是否複製
    copy_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_copy_index",
        prompt="請輸入要複製的項目編號（直接按Enter跳過）：",
        optional=True,
        validator=lambda x: (x.isdigit(), "請輸入數字"),
        description="選擇要複製的項目"
    )
    
    # 步驟 5: 執行複製
    def execute_copy(session: WorkflowSession) -> StepResult:
        copy_index_str = session.get_data("input_copy_index", "")
        
        # 如果沒有輸入，跳過複製
        if not copy_index_str:
            return StepResult.complete_workflow("已完成搜尋（未複製）")
        
        from modules.sys_module.actions.text_processing import clipboard_tracker
        
        copy_index = int(copy_index_str) - 1  # 轉換為0-based索引
        results = session.get_data("search_results", [])
        
        if copy_index < 0 or copy_index >= len(results):
            return StepResult.failure("無效的項目編號")
        
        # 重新調用 clipboard_tracker 執行複製
        keyword = session.get_data("input_keyword", "")
        max_results = session.get_data("result_count", 5)
        
        result = clipboard_tracker(
            keyword=keyword,
            max_results=max_results,
            copy_index=copy_index
        )
        
        if result["status"] == "ok" and "copied" in result:
            copied_text = result["copied"]
            preview = copied_text[:50] + "..." if len(copied_text) > 50 else copied_text
            return StepResult.complete_workflow(f"已複製：{preview}")
        else:
            return StepResult.failure("複製失敗")
    
    copy_execution_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_copy",
        processor=execute_copy,
        description="執行複製操作"
    )
    
    # 組裝工作流
    workflow_def.add_step(keyword_step)
    workflow_def.add_step(max_results_step)
    workflow_def.add_step(search_step)
    workflow_def.add_step(copy_step)
    workflow_def.add_step(copy_execution_step)
    
    workflow_def.set_entry_point("input_keyword")
    workflow_def.add_transition("input_keyword", "input_max_results")
    workflow_def.add_transition("input_max_results", "search_clipboard")
    workflow_def.add_transition("search_clipboard", "input_copy_index")
    workflow_def.add_transition("input_copy_index", "execute_copy")
    workflow_def.add_transition("execute_copy", "END")
    
    return WorkflowEngine(workflow_def, session)


# ==================== Quick Phrases Workflow ====================

def create_quick_phrases_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    快速範本工作流
    
    步驟：
    1. 選擇模式（預設範本 / LLM 生成）
    2a. 預設範本：選擇範本名稱
    2b. LLM 生成：輸入自訂提示詞（英文）
    3. 執行生成
    4. 詢問是否複製到剪貼簿
    """
    workflow_def = WorkflowDefinition(
        workflow_type="quick_phrases",
        name="快速範本",
        description="取得預設範本或使用 LLM 生成自訂範本",
        workflow_mode=WorkflowMode.DIRECT
    )
    
    # 步驟 1: 選擇模式
    mode_step = StepTemplate.create_selection_step(
        session=session,
        step_id="select_mode",
        prompt="請選擇範本模式：",
        options=["preset", "llm"],
        labels=["預設範本", "LLM 自訂生成"],
        required_data=[]
    )
    
    # 步驟 2a: 選擇預設範本
    preset_templates = ["email", "signature", "meeting", "thanks", "greeting", "apology", "followup"]
    preset_step = StepTemplate.create_selection_step(
        session=session,
        step_id="select_template",
        prompt="請選擇範本：",
        options=preset_templates,
        labels=[
            "電子郵件", "簽名檔", "會議議程", "感謝", 
            "問候", "道歉", "後續追蹤"
        ],
        required_data=[]
    )
    
    # 步驟 2b: 輸入 LLM 提示詞
    llm_prompt_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_llm_prompt",
        prompt="請輸入範本需求（使用英文，例如：Generate a formal business meeting invitation email）：",
        skip_if_data_exists=True,
        description="收集 LLM 生成提示詞"
    )
    
    # 步驟 3: 執行生成
    def generate_phrase(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.text_processing import quick_phrases
        
        mode = session.get_data("select_mode")
        
        if mode == "preset":
            template_name = session.get_data("select_template")
            info_log(f"[Workflow] 取得預設範本：{template_name}")
            
            result = quick_phrases(
                template_name=template_name,
                copy_to_clipboard=False
            )
        else:  # llm
            custom_prompt = session.get_data("input_llm_prompt")
            info_log(f"[Workflow] LLM 生成範本：{custom_prompt[:50]}...")
            
            result = quick_phrases(
                custom_prompt=custom_prompt,
                copy_to_clipboard=False
            )
        
        if result["status"] == "ok":
            content = result.get("content", "")
            is_generated = result.get("generated", False)
            
            # 限制顯示長度
            preview = content[:200] + "..." if len(content) > 200 else content
            
            message = f"{'LLM 生成' if is_generated else '預設'}範本內容：\n{preview}"
            
            return StepResult.success(
                message,
                {
                    "phrase_content": content,
                    "is_generated": is_generated
                }
            )
        else:
            return StepResult.failure(f"生成失敗：{result.get('message', '未知錯誤')}")
    
    generate_step = StepTemplate.create_processing_step(
        session=session,
        step_id="generate_phrase",
        processor=generate_phrase,
        description="生成範本內容"
    )
    
    # 步驟 4: 詢問是否複製
    confirm_copy_step = StepTemplate.create_confirmation_step(
        session=session,
        step_id="confirm_copy",
        message="是否複製到剪貼簿？",
        confirm_message="已複製到剪貼簿",
        cancel_message="未複製",
        description="確認是否複製"
    )
    
    # 步驟 5: 執行複製
    def execute_copy_phrase(session: WorkflowSession) -> StepResult:
        import win32clipboard
        
        content = session.get_data("phrase_content", "")
        if not content:
            return StepResult.failure("無內容可複製")
        
        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, content)
            win32clipboard.CloseClipboard()
            
            return StepResult.complete_workflow("已複製到剪貼簿")
        except Exception as e:
            error_log(f"[Workflow] 複製失敗：{e}")
            return StepResult.complete_workflow(f"複製失敗：{e}")
    
    copy_phrase_step = StepTemplate.create_processing_step(
        session=session,
        step_id="copy_phrase",
        processor=execute_copy_phrase,
        description="複製到剪貼簿"
    )
    
    # 組裝工作流
    workflow_def.add_step(mode_step)
    workflow_def.add_step(preset_step)
    workflow_def.add_step(llm_prompt_step)
    workflow_def.add_step(generate_step)
    workflow_def.add_step(confirm_copy_step)
    workflow_def.add_step(copy_phrase_step)
    
    workflow_def.set_entry_point("select_mode")
    
    # 根據模式分支
    workflow_def.add_transition("select_mode", "select_template", 
                                lambda r: r.data.get("select_mode") == "preset")
    workflow_def.add_transition("select_mode", "input_llm_prompt",
                                lambda r: r.data.get("select_mode") == "llm")
    
    workflow_def.add_transition("select_template", "generate_phrase")
    workflow_def.add_transition("input_llm_prompt", "generate_phrase")
    workflow_def.add_transition("generate_phrase", "confirm_copy")
    workflow_def.add_transition("confirm_copy", "copy_phrase")
    workflow_def.add_transition("copy_phrase", "END")
    
    return WorkflowEngine(workflow_def, session)


# ==================== OCR Extract Workflow ====================

def create_ocr_extract_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    OCR 辨識工作流
    
    步驟：
    1. 輸入圖片路徑
    2. 選擇輸出模式（文字 / PDF）
    3. 執行 OCR
    """
    workflow_def = WorkflowDefinition(
        workflow_type="ocr_extract",
        name="OCR 文字辨識",
        description="從圖片中辨識文字",
        workflow_mode=WorkflowMode.DIRECT
    )
    
    # 步驟 1: 輸入圖片路徑
    def validate_image_path(path: str) -> tuple:
        import os
        path = path.strip().strip('"').strip("'")
        if not os.path.exists(path):
            return False, "檔案不存在"
        
        ext = os.path.splitext(path)[1].lower()
        if ext not in ['.png', '.jpg', '.jpeg', '.bmp', '.tiff']:
            return False, "不支援的圖片格式"
        
        return True, ""
    
    image_path_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_image_path",
        prompt="Enter image path:",
        skip_if_data_exists=True,
        validator=validate_image_path,
        description="收集圖片路徑"
    )
    
    # 步驟 2: 選擇輸出模式
    output_mode_selection_step = StepTemplate.create_selection_step(
        session=session,
        step_id="select_output_mode",
        prompt="Select output mode:",
        options=["text", "pdf"],
        labels=["Plain Text", "PDF Document"],
        required_data=[]
    )
    
    # 步驟 3: 執行 OCR
    def execute_ocr(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.text_processing import ocr_extract
        
        image_path = session.get_data("input_image_path", "").strip().strip('"').strip("'")
        output_mode = session.get_data("select_output_mode")
        
        target_num = 1 if output_mode == "text" else 2
        
        info_log(f"[Workflow] 執行 OCR：路徑={image_path}, 模式={output_mode}")
        
        try:
            result = ocr_extract(image_path=image_path, target_num=target_num)
            
            if target_num == 1:
                # 文字模式
                preview = result[:200] + "..." if len(result) > 200 else result
                return StepResult.complete_workflow(
                    f"辨識結果：\n{preview}",
                    {"ocr_result": result}
                )
            else:
                # PDF 模式
                return StepResult.complete_workflow(
                    f"PDF 已生成：{result}",
                    {"pdf_path": result}
                )
        except Exception as e:
            error_log(f"[Workflow] OCR 執行失敗：{e}")
            return StepResult.failure(f"OCR 執行失敗：{e}")
    
    ocr_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_ocr",
        processor=execute_ocr,
        required_data=["input_image_path", "select_output_mode"],
        description="執行 OCR 辨識"
    )
    
    # 組裝工作流
    workflow_def.add_step(image_path_step)
    workflow_def.add_step(output_mode_selection_step)
    workflow_def.add_step(ocr_step)
    
    workflow_def.set_entry_point("input_image_path")
    workflow_def.add_transition("input_image_path", "select_output_mode")
    workflow_def.add_transition("select_output_mode", "execute_ocr")
    workflow_def.add_transition("execute_ocr", "END")
    
    return WorkflowEngine(workflow_def, session)


# ==================== Workflow Registry ====================

def get_available_text_workflows() -> list:
    """獲取可用的文字處理工作流列表"""
    return [
        "clipboard_tracker",
        "quick_phrases",
        "ocr_extract"
    ]


def create_text_workflow(workflow_type: str, session: WorkflowSession) -> WorkflowEngine:
    """創建文字處理工作流"""
    workflows = {
        "clipboard_tracker": create_clipboard_tracker_workflow,
        "quick_phrases": create_quick_phrases_workflow,
        "ocr_extract": create_ocr_extract_workflow
    }
    
    if workflow_type not in workflows:
        raise ValueError(f"未知的工作流類型：{workflow_type}")
    
    return workflows[workflow_type](session)
