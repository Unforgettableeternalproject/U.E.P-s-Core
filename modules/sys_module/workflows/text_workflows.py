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
    剪貼簿追蹤工作流（重構版）
    
    步驟：
    1. 輸入搜尋關鍵字（可選，初始參數）
    2. 執行搜尋（固定5筆，最近期優先）
    3. LLM 回應搜尋結果
    4. 使用者選擇要複製的項目（可選）
    5. 執行複製
    
    注意：
    - 本工作流依賴背景監控服務追蹤剪貼簿歷史
    - 如果系統未啟動監控，歷史記錄可能為空
    """
    workflow_def = WorkflowDefinition(
        workflow_type="clipboard_tracker",
        name="剪貼簿歷史搜尋",
        description="搜尋剪貼簿歷史記錄並選擇複製",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=True,  # LLM 需要回應搜尋結果
        auto_advance_on_approval=True
    )
    
    # 步驟 1: 輸入關鍵字（可選，可作為初始參數）
    keyword_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_keyword",
        prompt="請輸入搜尋關鍵字（直接按 Enter 查看全部歷史）：",
        optional=True,
        skip_if_data_exists=True,
        description="收集搜尋關鍵字（可選）"
    )
    
    # 步驟 2: 執行搜尋（固定5筆）
    def search_clipboard(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.text_processing import clipboard_tracker
        
        keyword = session.get_data("input_keyword", "").strip()
        max_results = 5  # 固定為5筆
        
        info_log(f"[Workflow] 搜尋剪貼簿：關鍵字='{keyword}', 限制={max_results}筆")
        
        result = clipboard_tracker(
            keyword=keyword,
            max_results=max_results,
            copy_index=-1  # 不立即複製
        )
        
        if result["status"] == "ok":
            results = result.get("results", [])
            if not results:
                return StepResult.complete_workflow(
                    "未找到符合的剪貼簿記錄。\n\n"
                    "提示：本功能需要系統背景監控服務運行。如果系統剛啟動，歷史記錄可能為空。"
                )
            
            # 格式化結果供 LLM 使用
            formatted_results = []
            for i, item in enumerate(results, 1):
                preview = item[:80] + "..." if len(item) > 80 else item
                formatted_results.append(f"{i}. {preview}")
            
            results_text = "\n".join(formatted_results)
            
            # 儲存結果供後續使用
            return StepResult.success(
                f"找到 {len(results)} 條剪貼簿記錄",
                {
                    "search_results": results,
                    "result_count": len(results),
                    "formatted_results": results_text
                }
            )
        else:
            return StepResult.failure(f"搜尋失敗：{result.get('message', '未知錯誤')}")
    
    search_step = StepTemplate.create_processing_step(
        session=session,
        step_id="search_clipboard",
        processor=search_clipboard,
        required_data=["input_keyword"],
        description="執行剪貼簿搜尋（固定5筆）"
    )
    
    # 步驟 3: LLM 回應搜尋結果
    def build_results_prompt(session: WorkflowSession) -> str:
        """構建 LLM 提示詞來回應搜尋結果（內部處理，僅需簡單確認）"""
        keyword = session.get_data("input_keyword", "").strip()
        formatted_results = session.get_data("formatted_results", "")
        result_count = session.get_data("result_count", 0)
        
        # 簡化 prompt：只需要 LLM 確認處理完成即可
        # 實際的用戶提示（包含選項列表）會在互動步驟提示中生成
        if keyword:
            prompt = f"""You searched clipboard history with keyword: "{keyword}".
Found {result_count} records. 

Simply acknowledge this result in ONE brief sentence (e.g., "Found X email addresses in your clipboard history")."""
        else:
            prompt = f"""You searched clipboard history without a keyword.
Found {result_count} records.

Simply acknowledge this result in ONE brief sentence (e.g., "Found X items in your clipboard history")."""
        
        return prompt
    
    llm_response_step = StepTemplate.create_llm_processing_step(
        session,
        "llm_respond_results",
        "Present search results to user",
        ["search_results", "formatted_results"],
        "llm_presentation",
        required_data=["search_results", "formatted_results"],
        llm_prompt_builder=build_results_prompt,
        description="LLM 向使用者呈現搜尋結果"
    )
    
    # 步驟 4: 使用者選擇要複製的項目
    copy_selection_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_copy_index",
        prompt="請輸入要複製的項目編號（或按 Enter 跳過）：",
        optional=True,
        validator=lambda x: (x.isdigit() and 1 <= int(x) <= 5, "請輸入 1-5 的數字"),
        description="選擇要複製的項目"
    )
    
    # 步驟 5: 執行複製
    def execute_copy(session: WorkflowSession) -> StepResult:
        copy_index_str = session.get_data("input_copy_index", "").strip()
        
        # 如果沒有輸入，跳過複製
        if not copy_index_str:
            return StepResult.complete_workflow("搜尋完成（未複製任何內容）")
        
        from modules.sys_module.actions.text_processing import clipboard_tracker
        
        copy_index = int(copy_index_str) - 1  # 轉換為0-based索引
        results = session.get_data("search_results", [])
        
        if copy_index < 0 or copy_index >= len(results):
            return StepResult.failure("編號超出範圍")
        
        # 重新調用 clipboard_tracker 執行複製
        keyword = session.get_data("input_keyword", "")
        
        result = clipboard_tracker(
            keyword=keyword,
            max_results=5,
            copy_index=copy_index
        )
        
        if result["status"] == "ok" and "copied" in result:
            copied_text = result["copied"]
            preview = copied_text[:100] + "..." if len(copied_text) > 100 else copied_text
            return StepResult.complete_workflow(
                f"✅ 已複製到剪貼簿！\n\n內容預覽：\n{preview}"
            )
        else:
            return StepResult.failure(f"複製失敗：{result.get('message', '未知錯誤')}")
    
    copy_execution_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_copy",
        processor=execute_copy,
        description="執行複製操作"
    )
    
    # 組裝工作流
    workflow_def.add_step(keyword_step)
    workflow_def.add_step(search_step)
    workflow_def.add_step(llm_response_step)
    workflow_def.add_step(copy_selection_step)
    workflow_def.add_step(copy_execution_step)
    
    workflow_def.set_entry_point("input_keyword")
    workflow_def.add_transition("input_keyword", "search_clipboard")
    workflow_def.add_transition("search_clipboard", "llm_respond_results")
    workflow_def.add_transition("llm_respond_results", "input_copy_index")
    workflow_def.add_transition("input_copy_index", "execute_copy")
    workflow_def.add_transition("execute_copy", "END")
    
    # 創建引擎並啟用自動推進
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


# ==================== Quick Phrases Workflow ====================

def create_quick_phrases_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    快速範本工作流
    
    步驟：
    1. 輸入範本需求（使用者描述想要的範本類型：信件、履歷等）
    2. LLM 處理生成範本
    3. 選擇輸出方式（複製到剪貼簿 / 儲存為文件）
    4a. 複製到剪貼簿
    4b. 儲存為文件到桌面
    """
    workflow_def = WorkflowDefinition(
        workflow_type="quick_phrases",
        name="快速範本生成",
        description="使用 LLM 根據使用者需求生成文字範本",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=True,
        auto_advance_on_approval=True
    )
    
    # 步驟 1: 輸入範本需求
    template_request_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_template_request",
        prompt="Describe the template you need (e.g., business email, cover letter, meeting agenda, thank you note):",
        validator=lambda x: (bool(x.strip()), "請提供範本需求描述"),
        skip_if_data_exists=True,
        description="收集使用者的範本需求描述"
    )
    
    # 步驟 2: LLM 生成範本
    def build_template_prompt(session: WorkflowSession) -> str:
        """構建 LLM 提示詞"""
        template_request = session.get_data("input_template_request", "").strip()
        
        prompt = f"""Please generate a text template based on the user's request.

User's request: {template_request}

Requirements:
1. Generate a professional and well-formatted template
2. Include placeholders where users should fill in specific information (use [brackets] for placeholders)
3. Make it practical and ready to use
4. Keep it concise but complete

Generate the template now:"""
        
        return prompt
    
    llm_generate_step = StepTemplate.create_llm_processing_step(
        session,
        "llm_generate_template",
        "Generate text template based on user request",
        ["input_template_request"],
        "generated_template",
        required_data=["input_template_request"],
        llm_prompt_builder=build_template_prompt,
        description="使用 LLM 生成範本內容"
    )
    
    # 步驟 3: 儲存為文件到桌面
    def save_to_file(session: WorkflowSession) -> StepResult:
        import os
        from datetime import datetime
        
        content = session.get_data("generated_template", "")
        template_request = session.get_data("input_template_request", "template")
        
        if not content:
            return StepResult.failure("沒有可儲存的內容")
        
        try:
            # 準備桌面路徑
            desktop_path = Path(os.path.expanduser("~/Desktop"))
            
            # 生成檔案名稱（從請求中提取簡短描述）
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # 清理檔案名稱（移除特殊字元）
            safe_name = "".join(c for c in template_request[:30] if c.isalnum() or c in (' ', '_')).strip()
            safe_name = safe_name.replace(' ', '_') or "template"
            filename = f"{safe_name}_{timestamp}.txt"
            file_path = desktop_path / filename
            
            # 寫入檔案
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write("=" * 80 + "\n")
                f.write("Generated Template\n")
                f.write("=" * 80 + "\n\n")
                f.write(f"Request: {template_request}\n")
                f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write("=" * 80 + "\n")
                f.write("Template Content\n")
                f.write("=" * 80 + "\n\n")
                f.write(content)
                f.write("\n\n" + "=" * 80 + "\n")
            
            info_log(f"[Workflow] 範本已儲存：{file_path}")
            
            # 顯示預覽
            preview = content[:200] + "..." if len(content) > 200 else content
            
            return StepResult.complete_workflow(
                f"✅ 範本已儲存到桌面！\n\n📄 檔案名稱: {filename}\n\n預覽:\n{preview}",
                {
                    "output_method": "file",
                    "file_path": str(file_path),
                    "template_content": content
                }
            )
        except Exception as e:
            error_log(f"[Workflow] 儲存失敗：{e}")
            return StepResult.failure(f"儲存失敗：{e}")
    
    save_step = StepTemplate.create_auto_step(
        session,
        "save_to_file",
        save_to_file,
        ["generated_template"],
        "正在儲存到桌面...",
        description="將範本儲存為文件到桌面"
    )
    
    # 組裝工作流（簡化版：只有三個步驟）
    workflow_def.add_step(template_request_step)
    workflow_def.add_step(llm_generate_step)
    workflow_def.add_step(save_step)
    
    workflow_def.set_entry_point("input_template_request")
    workflow_def.add_transition("input_template_request", "llm_generate_template")
    workflow_def.add_transition("llm_generate_template", "save_to_file")
    
    # 創建引擎並啟用自動推進
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine

# ==================== Workflow Registry ====================

def get_available_text_workflows() -> list:
    """獲取可用的文字處理工作流列表"""
    return [
        "clipboard_tracker",
        "quick_phrases"
        # ocr_extract 已移至 file_workflows.py
    ]


def create_text_workflow(workflow_type: str, session: WorkflowSession) -> WorkflowEngine:
    """創建文字處理工作流"""
    workflows = {
        "clipboard_tracker": create_clipboard_tracker_workflow,
        "quick_phrases": create_quick_phrases_workflow
        # ocr_extract 已移至 file_workflows.py
    }
    
    if workflow_type not in workflows:
        raise ValueError(f"未知的工作流類型：{workflow_type}")
    
    return workflows[workflow_type](session)
