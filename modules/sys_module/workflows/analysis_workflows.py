"""
分析相關工作流
包含：code_analysis
"""

from typing import Dict, Any

from core.sessions.session_manager import WorkflowSession
from modules.sys_module.workflows import (
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowMode,
    StepResult
)
from modules.sys_module.step_templates import StepTemplate
from utils.debug_helper import info_log, error_log, debug_log


# ==================== Code Analysis Workflow ====================

def create_code_analysis_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    程式碼分析工作流 (簡化版 - 基於檔案)
    
    步驟：
    1. 檔案選擇
    2. 輸入分析重點
    3. LLM 處理
    4. 輸出分析結果
    """
    workflow_def = WorkflowDefinition(
        workflow_type="code_analysis",
        name="程式碼分析",
        description="使用 LLM 進行智能程式碼分析",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=True,
        auto_advance_on_approval=True
    )
    
    # 步驟 1: 檔案選擇
    file_selection_step = StepTemplate.create_file_selection_step(
        session=session,
        step_id="select_file",
        prompt="Select a code file to analyze:",
        file_types=[".py", ".js", ".ts", ".java", ".cpp", ".c", ".go", ".rs", ".rb"],
        multiple=False,
        skip_if_data_exists=True,
        description="選擇要分析的程式碼檔案"
    )
    
    # 步驟 2: 輸入分析重點
    analysis_focus_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_analysis_focus",
        prompt="What should I focus on in the analysis? (e.g., security, performance, code quality, or leave blank for general analysis)",
        validator=lambda focus: (True, ""),  # 接受任何輸入包含空白
        required_data=["select_file"],
        optional=True,
        skip_if_data_exists=True,
        description="輸入分析重點（可選）"
    )
    
    # 步驟 3: LLM 處理分析
    def execute_analysis(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.integrations import code_analysis
        import os
        
        file_path = session.get_data("select_file", "")
        analysis_focus = session.get_data("input_analysis_focus", "").strip()
        
        # 讀取檔案內容
        if not os.path.exists(file_path):
            return StepResult.failure(f"檔案不存在：{file_path}")
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                code = f.read()
        except Exception as e:
            error_log(f"[Workflow] 讀取檔案失敗：{e}")
            return StepResult.failure(f"讀取檔案失敗：{e}")
        
        # 決定分析類型
        if not analysis_focus:
            analysis_type = "general"
        elif "security" in analysis_focus.lower():
            analysis_type = "security"
        elif "optim" in analysis_focus.lower() or "perform" in analysis_focus.lower():
            analysis_type = "optimize"
        elif "explain" in analysis_focus.lower():
            analysis_type = "explain"
        else:
            # 使用用戶的自定義焦點作為 general 分析的提示
            analysis_type = "general"
        
        info_log(f"[Workflow] 執行程式碼分析：檔案={file_path}, 類型={analysis_type}, 焦點={analysis_focus}")
        
        # 執行分析
        result = code_analysis(code=code, analysis_type=analysis_type)
        
        if result["status"] == "ok":
            analysis_result = result.get("analysis", "")
            
            # 儲存分析結果到 session
            session.add_data("analysis_result", analysis_result)
            session.add_data("analysis_type", analysis_type)
            
            return StepResult.success(f"分析完成：{analysis_type}")
        else:
            return StepResult.failure(f"分析失敗：{result.get('message', '未知錯誤')}")
    
    analysis_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_analysis",
        processor=execute_analysis,
        required_data=["select_file", "input_analysis_focus"],
        description="執行 LLM 分析"
    )
    
    # 步驟 4: 儲存分析報告到桌面
    def save_analysis_report(session: WorkflowSession) -> StepResult:
        import os
        from pathlib import Path
        from datetime import datetime
        
        file_path = session.get_data("select_file", "")
        analysis_focus = session.get_data("input_analysis_focus", "").strip()
        analysis_result = session.get_data("analysis_result", "")
        analysis_type = session.get_data("analysis_type", "general")
        
        # 準備桌面路徑
        desktop_path = Path(os.path.expanduser("~/Desktop"))
        
        # 生成檔案名稱
        original_filename = Path(file_path).stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_filename = f"{original_filename}_analysis_{timestamp}.txt"
        report_path = desktop_path / report_filename
        
        try:
            # 組合報告內容
            report_content = "=" * 80 + "\n"
            report_content += "程式碼分析報告\n"
            report_content += "=" * 80 + "\n\n"
            report_content += f"檔案名稱: {Path(file_path).name}\n"
            report_content += f"檔案路徑: {file_path}\n"
            report_content += f"分析時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            report_content += f"分析類型: {analysis_type}\n"
            if analysis_focus:
                report_content += f"分析焦點: {analysis_focus}\n"
            report_content += "\n" + "=" * 80 + "\n"
            report_content += "分析結果\n"
            report_content += "=" * 80 + "\n\n"
            report_content += analysis_result
            report_content += "\n\n" + "=" * 80 + "\n"
            report_content += "報告結束\n"
            report_content += "=" * 80 + "\n"
            
            # 寫入檔案
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report_content)
            
            info_log(f"[Workflow] 分析報告已儲存: {report_path}")
            
            # 組合輸出訊息
            output_msg = f"✅ 程式碼分析完成！\n\n"
            output_msg += f"📄 檔案: {Path(file_path).name}\n"
            if analysis_focus:
                output_msg += f"🔍 分析焦點: {analysis_focus}\n"
            output_msg += f"📊 分析類型: {analysis_type}\n"
            output_msg += f"💾 報告已儲存至桌面: {report_filename}\n\n"
            output_msg += f"分析摘要:\n{analysis_result[:300]}{'...' if len(analysis_result) > 300 else ''}"
            
            return StepResult.complete_workflow(
                output_msg,
                {
                    "analysis_result": analysis_result,
                    "report_path": str(report_path),
                    "file_path": file_path,
                    "analysis_focus": analysis_focus,
                    "analysis_type": analysis_type
                }
            )
        except Exception as e:
            error_log(f"[Workflow] 儲存分析報告失敗：{e}")
            return StepResult.failure(f"儲存分析報告失敗：{e}")
    
    save_report_step = StepTemplate.create_auto_step(
        session,
        "save_analysis_report",
        save_analysis_report,
        ["select_file", "analysis_result"],
        "正在儲存分析報告...",
        description="將分析結果儲存為文字檔案到桌面"
    )
    
    # 組裝工作流
    workflow_def.add_step(file_selection_step)
    workflow_def.add_step(analysis_focus_step)
    workflow_def.add_step(analysis_step)
    workflow_def.add_step(save_report_step)
    
    workflow_def.set_entry_point("select_file")
    workflow_def.add_transition("select_file", "input_analysis_focus")
    workflow_def.add_transition("input_analysis_focus", "execute_analysis")
    workflow_def.add_transition("execute_analysis", "save_analysis_report")
    
    # 創建引擎並啟用自動推進
    engine = WorkflowEngine(workflow_def, session)
    engine.auto_advance = True
    
    return engine


# ==================== Workflow Registry ====================

def get_available_analysis_workflows() -> list:
    """獲取可用的分析工作流列表"""
    return ["code_analysis"]


def create_analysis_workflow(workflow_type: str, session: WorkflowSession) -> WorkflowEngine:
    """創建分析工作流"""
    workflows = {
        "code_analysis": create_code_analysis_workflow
    }
    
    if workflow_type not in workflows:
        raise ValueError(f"未知的工作流類型：{workflow_type}")
    
    return workflows[workflow_type](session)
