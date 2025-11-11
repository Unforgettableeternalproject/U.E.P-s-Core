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
    StepResult,
    StepTemplate
)
from utils.debug_helper import info_log, error_log, debug_log


# ==================== Code Analysis Workflow ====================

def create_code_analysis_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    程式碼分析工作流
    
    步驟：
    1. 輸入程式碼（或檔案路徑）
    2. 選擇分析類型
    3. 執行分析（使用 LLM）
    """
    workflow_def = WorkflowDefinition(
        workflow_type="code_analysis",
        name="程式碼分析",
        description="使用 LLM 進行智能程式碼分析",
        workflow_mode=WorkflowMode.DIRECT
    )
    
    # 步驟 1: 輸入程式碼
    code_input_step = StepTemplate.create_input_step(
        session=session,
        step_id="input_code",
        prompt="Enter code (or type 'file:path' to read from file):",
        skip_if_data_exists=True,
        description="收集程式碼"
    )
    
    # 步驟 2: 處理程式碼輸入（可能需要從檔案讀取）
    def process_code_input(session: WorkflowSession) -> StepResult:
        import os
        
        code_input = session.get_data("input_code", "").strip()
        
        # 檢查是否為檔案路徑
        if code_input.startswith("file:"):
            file_path = code_input[5:].strip().strip('"').strip("'")
            
            if not os.path.exists(file_path):
                return StepResult.failure(f"檔案不存在：{file_path}")
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    code = f.read()
                
                info_log(f"[Workflow] 從檔案讀取程式碼：{file_path}, 長度={len(code)}")
                
                return StepResult.success(
                    f"已從檔案讀取程式碼（{len(code)} 字元）",
                    {"code_content": code, "from_file": True, "file_path": file_path}
                )
            except Exception as e:
                error_log(f"[Workflow] 讀取檔案失敗：{e}")
                return StepResult.failure(f"讀取檔案失敗：{e}")
        else:
            # 直接使用輸入的程式碼
            return StepResult.success(
                f"已輸入程式碼（{len(code_input)} 字元）",
                {"code_content": code_input, "from_file": False}
            )
    
    process_code_step = StepTemplate.create_processing_step(
        session=session,
        step_id="process_code_input",
        processor=process_code_input,
        required_data=["input_code"],
        description="處理程式碼輸入"
    )
    
    # 步驟 2: 選擇分析類型
    analysis_type_selection_step = StepTemplate.create_selection_step(
        session=session,
        step_id="select_analysis_type",
        prompt="Select analysis type:",
        options=["general", "security", "optimize", "explain"],
        labels=["General Analysis", "Security Analysis", "Optimization Suggestions", "Code Explanation"],
        required_data=[]
    )
    
    # 步驟 4: 執行分析
    def execute_analysis(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.integrations import code_analysis
        
        code = session.get_data("code_content", "")
        analysis_type = session.get_data("select_analysis_type", "general")
        
        info_log(f"[Workflow] 執行程式碼分析：類型={analysis_type}, 程式碼長度={len(code)}")
        
        result = code_analysis(code=code, analysis_type=analysis_type)
        
        if result["status"] == "ok":
            analysis_result = result.get("analysis", "")
            
            # 限制顯示長度
            preview = analysis_result[:500] + "..." if len(analysis_result) > 500 else analysis_result
            
            return StepResult.complete_workflow(
                f"分析完成（{analysis_type}）：\n{preview}",
                {
                    "analysis_result": analysis_result,
                    "analysis_type": analysis_type,
                    "full_result": result
                }
            )
        else:
            return StepResult.failure(f"分析失敗：{result.get('message', '未知錯誤')}")
    
    analysis_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_analysis",
        processor=execute_analysis,
        required_data=["code_content", "select_analysis_type"],
        description="執行 LLM 分析"
    )
    
    # 組裝工作流
    workflow_def.add_step(code_input_step)
    workflow_def.add_step(process_code_step)
    workflow_def.add_step(analysis_type_selection_step)
    workflow_def.add_step(analysis_step)
    
    workflow_def.set_entry_point("input_code")
    workflow_def.add_transition("input_code", "process_code_input")
    workflow_def.add_transition("process_code_input", "select_analysis_type")
    workflow_def.add_transition("select_analysis_type", "execute_analysis")
    workflow_def.add_transition("execute_analysis", "END")
    
    return WorkflowEngine(workflow_def, session)


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
