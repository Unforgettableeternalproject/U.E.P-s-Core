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
    StepResult
)
from modules.sys_module.step_templates import StepTemplate
from utils.debug_helper import info_log, error_log, debug_log


# ==================== Clipboard Tracker Workflow ====================

def create_clipboard_tracker_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    剪貼簿追蹤工作流（重構版）
    
    步驟：
    1. 輸入搜尋關鍵字（可選，初始參數）
    2. 執行搜尋並生成選項（固定5筆，最近期優先）
    3. 使用者選擇要複製的項目（selection step，動態選項）
    4. 執行複製
    
    改進：
    - 移除了不必要的 LLM 回應步驟
    - 使用 selection step 替代 input step，提供動態選項
    - 簡化流程，減少步驟轉換
    
    注意：
    - 本工作流依賴背景監控服務追蹤剪貼簿歷史
    - 背景監控執行緒已在 text_processing.py 啟動
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
    
    # 步驟 2: 執行搜尋並生成選項
    def search_and_prepare_options(session: WorkflowSession) -> StepResult:
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
            
            # 為 selection step 生成動態選項
            selection_values = []  # 選項值列表（用於 create_selection_step）
            selection_labels = []  # 選項標籤列表（用於顯示）
            
            for i, item in enumerate(results, 1):
                # 截取預覽（最多60字元）
                preview = item[:60] + "..." if len(item) > 60 else item
                # 選項值為索引（1-based）
                selection_values.append(str(i))
                # 🔧 不在標籤內加編號，由 SelectionStep.get_prompt() 統一處理
                selection_labels.append(preview)
            
            # 加入「取消」選項
            selection_values.append("cancel")
            selection_labels.append("cancel the operation")
            
            # 儲存結果和選項
            return StepResult.success(
                f"找到 {len(results)} 條剪貼簿記錄",
                {
                    "search_results": results,
                    "result_count": len(results),
                    "selection_values": selection_values,
                    "selection_labels": selection_labels
                }
            )
        else:
            return StepResult.failure(f"搜尋失敗：{result.get('message', '未知錯誤')}")
    
    search_step = StepTemplate.create_processing_step(
        session=session,
        step_id="search_clipboard",
        processor=search_and_prepare_options,
        required_data=["input_keyword"],
        description="執行剪貼簿搜尋並生成選項"
    )
    
    # 步驟 3: 使用者選擇要複製的項目（selection step，動態選項）
    # 注意：這裡使用佔位符列表，實際選項在搜尋步驟完成後由 session 數據提供
    selection_step = StepTemplate.create_selection_step(
        session=session,
        step_id="copy_selection",
        prompt="請選擇要複製的項目：",
        options=session.get_data("selection_values", ["1", "2", "3", "4", "5", "cancel"]),  # 佔位符
        labels=session.get_data("selection_labels", ["載入中...", "載入中...", "載入中...", "載入中...", "載入中...", "取消"]),  # 佔位符
        required_data=["search_results", "selection_values", "selection_labels"],  # 依賴搜尋結果
        description="選擇要複製的項目"
    )
    
    # 步驟 4: 執行複製
    def execute_copy(session: WorkflowSession) -> StepResult:
        selected_value = session.get_data("copy_selection", "").strip()
        
        # 如果選擇取消
        if selected_value == "cancel":
            return StepResult.complete_workflow("⏭️ 已取消複製操作")
        
        from modules.sys_module.actions.text_processing import clipboard_tracker
        
        # 轉換為0-based索引
        try:
            copy_index = int(selected_value) - 1
        except ValueError:
            return StepResult.failure("無效的選擇")
        
        results = session.get_data("search_results", [])
        
        if copy_index < 0 or copy_index >= len(results):
            return StepResult.failure("選擇超出範圍")
        
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
    
    copy_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_copy",
        processor=execute_copy,
        description="執行複製操作"
    )
    
    # 組裝工作流
    workflow_def.add_step(keyword_step)
    workflow_def.add_step(search_step)
    workflow_def.add_step(selection_step)
    workflow_def.add_step(copy_step)
    
    workflow_def.set_entry_point("input_keyword")
    workflow_def.add_transition("input_keyword", "search_clipboard")
    workflow_def.add_transition("search_clipboard", "copy_selection")
    workflow_def.add_transition("copy_selection", "execute_copy")
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
    3. 選擇輸出方式（複製到剪貼簿 / 儲存為文件 / 取消）
    4. Conditional 根據選擇執行相應操作
       4a. copy → 複製到剪貼簿
       4b. save → 儲存為文件到桌面
       4c. cancel/其他 → 直接結束（default 分支）
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
    
    # 步驟 3: 選擇輸出方式
    output_method_selection_step = StepTemplate.create_selection_step(
        session=session,
        step_id="output_method_selection",
        prompt="請選擇輸出方式：",
        options=["copy", "save", "cancel"],
        labels=["複製到剪貼簿", "儲存為文件到桌面", "取消"],
        required_data=["generated_template"],
        skip_if_data_exists=True
    )
    
    # 步驟 4a: 複製到剪貼簿
    def copy_to_clipboard(session: WorkflowSession) -> StepResult:
        content = session.get_data("generated_template", "")
        template_request = session.get_data("input_template_request", "template")
        
        if not content:
            return StepResult.failure("沒有可複製的內容")
        
        try:
            import pyperclip
            pyperclip.copy(content)
            
            info_log("[Workflow] 範本已複製到剪貼簿")
            
            # 顯示預覽
            preview = content[:200] + "..." if len(content) > 200 else content
            
            # 返回成功結果（工作流完成由 conditional 處理）
            return StepResult.success(
                f"✅ 已為您生成 '{template_request}' 範本並複製到剪貼簿。\n\n預覽:\n{preview}",
                {
                    "output_method": "clipboard",
                    "template_content": content,
                    "template_request": template_request
                }
            )
        except Exception as e:
            error_log(f"[Workflow] 複製失敗：{e}")
            return StepResult.failure(f"複製失敗：{e}")
    
    copy_step = StepTemplate.create_auto_step(
        session,
        "copy_to_clipboard",
        copy_to_clipboard,
        ["generated_template"],
        "正在複製到剪貼簿...",
        description="複製範本到剪貼簿"
    )
    
    # 步驟 4b: 儲存為文件到桌面
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
            
            # 返回成功結果（工作流完成由 conditional 處理）
            return StepResult.success(
                f"✅ 已為您生成 '{template_request}' 範本並儲存到桌面！\n\n📄 檔案名稱: {filename}\n\n預覽:\n{preview}",
                {
                    "output_method": "file",
                    "file_path": str(file_path),
                    "filename": filename,
                    "template_content": content,
                    "template_request": template_request
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
    
    # 步驟 4c: 取消操作
    def cancel_operation(session: WorkflowSession) -> StepResult:
        """處理取消操作"""
        template_request = session.get_data("input_template_request", "template")
        
        info_log("[Workflow] 使用者取消範本生成")
        
        # 返回成功結果（工作流完成由 conditional 處理）
        return StepResult.success(
            f"⏭️ 已取消 '{template_request}' 範本生成。如需其他幫助，請隨時告訴我！",
            {
                "output_method": "cancel",
                "template_request": template_request,
                "cancelled": True
            }
        )
    
    cancel_step = StepTemplate.create_auto_step(
        session,
        "cancel_operation",
        cancel_operation,
        [],
        "正在取消操作...",
        description="處理取消操作"
    )
    
    # 步驟 4: Conditional 根據選擇執行相應操作（作為最後一步）
    output_conditional_step = StepTemplate.create_conditional_step(
        session=session,
        step_id="output_conditional",
        selection_step_id="output_method_selection",
        branches={
            "copy": [copy_step],  # 複製到剪貼簿
            "save": [save_step],  # 儲存為文件
            "cancel": [cancel_step]  # 取消操作
        },
        description="根據使用者選擇執行相應的輸出操作",
        is_final_step=True  # 🔧 標記為最後一步，執行完成後自動完成工作流
    )
    
    # 組裝工作流
    workflow_def.add_step(template_request_step)
    workflow_def.add_step(llm_generate_step)
    workflow_def.add_step(output_method_selection_step)
    workflow_def.add_step(copy_step)
    workflow_def.add_step(save_step)
    workflow_def.add_step(cancel_step)
    workflow_def.add_step(output_conditional_step)
    
    workflow_def.set_entry_point("input_template_request")
    workflow_def.add_transition("input_template_request", "llm_generate_template")
    workflow_def.add_transition("llm_generate_template", "output_method_selection")
    workflow_def.add_transition("output_method_selection", "output_conditional")
    # 🔧 分支步驟完成後需要回到 conditional 繼續執行
    workflow_def.add_transition("copy_to_clipboard", "output_conditional")
    workflow_def.add_transition("save_to_file", "output_conditional")
    workflow_def.add_transition("cancel_operation", "output_conditional")
    # 🔧 conditional 作為最後一步，直接到 END（分支中的步驟已使用 complete_workflow）
    workflow_def.add_transition("output_conditional", "END")
    
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
