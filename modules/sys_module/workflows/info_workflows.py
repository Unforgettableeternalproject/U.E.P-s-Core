"""
資訊查詢相關工作流
包含：news_summary, get_weather, get_world_time
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


# ==================== News Summary Workflow ====================

def create_news_summary_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    新聞摘要工作流
    
    快速查看台灣最新新聞標題（固定抓取 5-6 則）
    LLM 會總結這些新聞標題並用英文回應使用者
    """
    workflow_def = WorkflowDefinition(
        workflow_type="news_summary",
        name="新聞摘要",
        description="快速查看台灣 Google 新聞標題",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=True  # 🔧 啟用 LLM 審核以生成步驟間的提示
    )
    
    # 固定參數：來源固定為 google_news_tw，數量固定為 6
    session.add_data("news_source", "google_news_tw")
    session.add_data("news_count", 6)
    debug_log(2, f"[news_summary] 使用固定參數: source=google_news_tw, count=6")
    
    # 唯一步驟: 執行新聞抓取
    def execute_news_fetch(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.integrations import news_summary
        
        source = session.get_data("news_source", "google_news_tw")
        max_items = session.get_data("news_count", 6)
        
        info_log(f"[Workflow] 快速查看新聞：來源={source}, 數量={max_items}")
        
        result = news_summary(source=source, max_items=max_items)
        
        if result["status"] == "ok":
            # 🔧 修正：news_summary 返回的鍵是 'titles' 而不是 'news'
            news_list = result.get("titles", [])
            
            # 格式化新聞列表
            formatted_news = "\n".join([f"{i+1}. {item}" for i, item in enumerate(news_list)])
            
            return StepResult.complete_workflow(
                f"成功抓取 {len(news_list)} 則新聞：\n{formatted_news}",
                {
                    "news_list": news_list,
                    "source": source,
                    "count": len(news_list),
                    "full_result": result
                }
            )
        else:
            return StepResult.failure(f"抓取失敗：{result.get('message', '未知錯誤')}")
    
    fetch_news_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_news_fetch",
        processor=execute_news_fetch,
        required_data=["news_source", "news_count"],
        description="執行新聞抓取"
    )
    
    # 組裝工作流（只有一個處理步驟）
    workflow_def.add_step(fetch_news_step)
    
    workflow_def.set_entry_point("execute_news_fetch")
    workflow_def.add_transition("execute_news_fetch", "END")
    
    return WorkflowEngine(workflow_def, session)


# ==================== Get Weather Workflow ====================

def create_get_weather_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    天氣查詢工作流
    
    步驟：
    1. 輸入位置（可選，可從 initial_data.location 提取）
    2. 執行查詢
    """
    workflow_def = WorkflowDefinition(
        workflow_type="get_weather",
        name="天氣查詢",
        description="查詢指定位置的天氣資訊",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=True  # 🔧 啟用 LLM 審核以生成步驟間的提示
    )
    
    # 注意：initial_data 的參數映射已在 sys_module.start_unified_workflow 中處理
    # session 中已經包含映射後的數據（location_input 等）
    
    # 步驟 1: 輸入位置（ID 改為與 YAML 一致：location_input）
    location_input_step = StepTemplate.create_input_step(
        session=session,
        step_id="location_input",
        prompt="Enter location to query (city name, e.g., Taipei, London, New York):",
        optional=True,
        skip_if_data_exists=True,
        description="收集位置資訊"
    )
    
    # 步驟 2: 執行天氣查詢
    def execute_weather_query(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.integrations import get_weather
        
        location = session.get_data("location_input", "").strip()
        
        if not location:
            return StepResult.failure("Please provide a valid location")
        
        info_log(f"[Workflow] 查詢天氣：位置={location}")
        
        result = get_weather(location=location)
        
        # 🔧 修正：get_weather 返回天氣數據 dict，不包含 status 鍵
        # 檢查是否包含必要的天氣數據
        if result and "location" in result:
            # 格式化天氣訊息
            weather_parts = []
            if result.get("condition"):
                weather_parts.append(result["condition"])
            if result.get("temperature"):
                weather_parts.append(result["temperature"])
            if result.get("wind"):
                weather_parts.append(f"風速: {result['wind']}")
            if result.get("humidity"):
                weather_parts.append(f"濕度: {result['humidity']}")
            
            weather_info = " | ".join(weather_parts) if weather_parts else result.get("raw_text", "無天氣資訊")
            
            return StepResult.complete_workflow(
                f"{result['location']} 的天氣：{weather_info}",
                {
                    "location": result["location"],
                    "weather_info": weather_info,
                    "weather_data": result
                }
            )
        else:
            return StepResult.failure(f"查詢失敗：無法取得 {location} 的天氣資訊")
    
    weather_query_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_weather_query",
        processor=execute_weather_query,
        required_data=["location_input"],
        description="執行天氣查詢"
    )
    
    # 組裝工作流
    workflow_def.add_step(location_input_step)
    workflow_def.add_step(weather_query_step)
    
    workflow_def.set_entry_point("location_input")
    workflow_def.add_transition("location_input", "execute_weather_query")
    workflow_def.add_transition("execute_weather_query", "END")
    
    return WorkflowEngine(workflow_def, session)


# ==================== Get World Time Workflow ====================

def create_get_world_time_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    世界時間查詢工作流
    
    步驟：
    1. 選擇查詢模式（UTC/指定時區/本地時間）（可選，可從 initial_data.target_num 提取）
    2. （條件）如果是指定時區，輸入時區（可選，可從 initial_data.tz 提取）
    3. 執行查詢
    """
    workflow_def = WorkflowDefinition(
        workflow_type="get_world_time",
        name="世界時間查詢",
        description="查詢世界各地的時間",
        workflow_mode=WorkflowMode.DIRECT,
        requires_llm_review=True  # 🔧 啟用 LLM 審核以生成步驟間的提示
    )
    
    # 注意：initial_data 的參數映射和推斷邏輯已在 sys_module.start_unified_workflow 中處理
    # session 中已經包含映射後的數據（mode_selection, timezone_input 等）
    
    # 步驟 1: 選擇查詢模式（ID 改為與 YAML 一致：mode_selection）
    mode_selection_step = StepTemplate.create_selection_step(
        session=session,
        step_id="mode_selection",
        prompt="Select time query mode:",
        options=["1", "2", "3"],  # 🔧 使用字串與 initial_data 保持一致
        labels=["UTC Time", "Specific Timezone", "Local Time"],
        required_data=[],
        skip_if_data_exists=True  # 🔧 支援從 initial_data 提取模式
    )
    
    # 步驟 2: 輸入時區（僅當選擇 timezone 模式時需要，ID 改為與 YAML 一致：timezone_input）
    timezone_input_step = StepTemplate.create_input_step(
        session=session,
        step_id="timezone_input",
        prompt="Please enter timezone (e.g., Asia/Taipei, America/New_York, Europe/London):",
        optional=False,  # 🔧 改為 required - 必須提供時區
        skip_if_data_exists=True,
        description="收集時區資訊"
    )
     
    # 步驟 3: 使用 ConditionalStep 處理分支邏輯
    timezone_conditional_step = StepTemplate.create_conditional_step(
        session=session,
        step_id="timezone_conditional",
        selection_step_id="mode_selection",
        branches={
            "1": [],  # UTC - 不需要額外輸入
            "2": [timezone_input_step],  # Timezone - 需要輸入時區
            "3": []   # Local - 不需要額外輸入
        },
        description="根據模式選擇決定是否需要輸入時區"
    )
    
    # 步驟 4: 執行時間查詢
    def execute_time_query(session: WorkflowSession) -> StepResult:
        from modules.sys_module.actions.integrations import get_world_time
        
        # 從 session 獲取參數（可能來自 selection 或 initial_data）
        target_num_str = session.get_data("mode_selection", "1")  # 預設 UTC
        target_num = int(target_num_str)  # 轉換為整數給 API 使用
        timezone_name = session.get_data("timezone_input", "").strip() if target_num_str == "2" else None
        
        # 驗證：如果是模式 2，必須有時區
        if target_num_str == "2" and not timezone_name:
            return StepResult.failure("Please provide a valid timezone name")
        
        info_log(f"[Workflow] 查詢時間：target_num={target_num}, 時區={timezone_name}")
        
        result = get_world_time(target_num=target_num, tz=timezone_name or "")
        
        # 🔧 處理新的 dict 格式返回值
        if isinstance(result, dict):
            if result.get("status") == "ok":
                time_info = result.get("time", "")
                message = result.get("message", time_info)
                
                return StepResult.complete_workflow(
                    message,
                    {
                        "target_num": target_num,
                        "timezone": timezone_name or result.get("timezone"),
                        "time_info": time_info,
                        "full_result": result
                    }
                )
            else:
                # 錯誤情況
                error_msg = result.get("message", "Unknown error")
                return StepResult.failure(error_msg)
        else:
            # 向後兼容：舊的字符串格式
            return StepResult.complete_workflow(
                str(result),
                {
                    "target_num": target_num,
                    "timezone": timezone_name,
                    "time_info": str(result)
                }
            )
    
    time_query_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_time_query",
        processor=execute_time_query,
        required_data=["mode_selection"],
        description="執行時間查詢"
    )
    
    # 組裝工作流（使用 ConditionalStep）
    workflow_def.add_step(mode_selection_step)
    workflow_def.add_step(timezone_input_step)  # 🔧 將 timezone_input 添加為正式步驟
    workflow_def.add_step(timezone_conditional_step)
    workflow_def.add_step(time_query_step)
    
    workflow_def.set_entry_point("mode_selection")
    workflow_def.add_transition("mode_selection", "timezone_conditional")
    workflow_def.add_transition("timezone_conditional", "timezone_input")  # 🔧 ConditionalStep 可以跳轉到 timezone_input
    workflow_def.add_transition("timezone_conditional", "execute_time_query")  # 🔧 或直接到 execute_time_query
    workflow_def.add_transition("timezone_input", "execute_time_query")  # 🔧 timezone_input 完成後到 execute_time_query
    workflow_def.add_transition("execute_time_query", "END")
    
    return WorkflowEngine(workflow_def, session)


# ==================== Workflow Registry ====================

def get_available_info_workflows() -> list:
    """獲取可用的資訊工作流列表"""
    return ["news_summary", "get_weather", "get_world_time"]


def create_info_workflow(workflow_type: str, session: WorkflowSession) -> WorkflowEngine:
    """創建資訊工作流"""
    workflows = {
        "news_summary": create_news_summary_workflow,
        "get_weather": create_get_weather_workflow,
        "get_world_time": create_get_world_time_workflow
    }
    
    if workflow_type not in workflows:
        raise ValueError(f"未知的工作流類型：{workflow_type}")
    
    return workflows[workflow_type](session)
