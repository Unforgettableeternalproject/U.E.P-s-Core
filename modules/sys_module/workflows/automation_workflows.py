"""
modules/sys_module/workflows/automation_workflows.py
自動化背景工作流定義

包含持續性服務的背景工作流：
- 媒體播放控制工作流（服務啟動 + 干涉）
- 提醒設置工作流（時間觸發）
- 資料夾監控工作流（事件觸發）
- 日曆事件管理工作流（CRUD + 時間觸發）

註：一次性實用工具任務（如生成備份腳本）已移至 utility_workflows.py
"""

from typing import Dict, Any, Optional
from datetime import datetime, timedelta

from core.sessions.session_manager import WorkflowSession
from modules.sys_module.workflows import (
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowMode,
    WorkflowType,
    StepResult
)
from modules.sys_module.step_templates import (
    StepTemplate
)
from modules.sys_module.actions.automation_helper import (
    get_monitoring_pool,
    media_control
)
from utils.debug_helper import info_log, error_log, debug_log


# ==================== 媒體播放服務工作流 ====================

def _execute_media_playback(session: WorkflowSession) -> StepResult:
    """
    執行媒體播放（不包含監控註冊，監控註冊由 monitor_creation_step 負責）
    
    根據 playback_type 執行不同的播放邏輯：
    1 = 本地播放
    2 = YouTube
    3 = Spotify
    
    此步驟只負責啟動播放，並將結果保存到 session 中供後續步驟使用。
    """
    try:
        from pathlib import Path
        from configs.config_loader import load_config
        
        # 獲取參數
        playback_type = session.get_data("playback_type_selection")
        query = session.get_data("query_input", "")
        
        if not playback_type:
            return StepResult.failure("缺少播放類型")
        
        # 根據類型執行播放
        playback_type_int = int(playback_type)
        
        if playback_type_int == 1:
            # 本地播放 - 從配置讀取音樂資料夾
            config = load_config()
            music_folder = config.get("system", {}).get("media", {}).get("music_folder")
            if not music_folder:
                music_folder = str(Path.home() / "Music")  # 預設值
            else:
                music_folder = str(Path(music_folder).expanduser())
            result_message = media_control(
                action="play",
                song_query=query,
                music_folder=music_folder,
                youtube=False,
                spotify=False
            )
            playback_mode = "local"
            
        elif playback_type_int == 2:
            # YouTube
            result_message = media_control(
                action="youtube",
                song_query=query,
                youtube=True
            )
            playback_mode = "youtube"
            
        elif playback_type_int == 3:
            # Spotify
            result_message = media_control(
                action="spotify",
                song_query=query,
                spotify=True
            )
            playback_mode = "spotify"
            
        else:
            return StepResult.failure(f"未知的播放類型：{playback_type}")
        
        info_log(f"[MediaPlayback] 播放已啟動 ({playback_mode})：{result_message}")
        
        # 將播放結果保存到 session，供 monitor_creation_step 使用
        return StepResult.success(
            f"媒體播放已啟動 ({playback_mode})\n{result_message}",
            data={
                "playback_mode": playback_mode,
                "initial_result": result_message
            }
        )
        
    except Exception as e:
        error_log(f"[MediaPlayback] 執行失敗：{e}")
        return StepResult.failure(f"媒體播放失敗：{str(e)}")


def create_media_playback_workflow(
    session: WorkflowSession,
    playback_type: Optional[int] = None,
    query: Optional[str] = None
) -> WorkflowEngine:
    """
    創建媒體播放服務工作流（背景服務啟動）
    
    工作流程：
    1. playback_type_selection - 選擇播放類型（可跳過）
    2. playback_type_conditional - 根據類型分支
    3. query_input - 輸入查詢（可跳過）
    4. execute_playback - 執行播放
    5. create_monitor - 建立監控任務並提交到執行緒池（自動步驟）
    
    Args:
        playback_type: 播放類型 (1=local, 2=youtube, 3=spotify)
        query: 歌曲/藝人查詢
    
    Returns:
        WorkflowDefinition 實例
    """
    workflow_def = WorkflowDefinition(
        workflow_type="media_playback",
        name="媒體播放服務",
        description="啟動媒體播放服務（本地/YouTube/Spotify）",
        workflow_mode=WorkflowMode.BACKGROUND,  # ✅ 背景工作流
        requires_llm_review=True  # ✅ 啟用 LLM 審核
    )
    
    # 步驟 1: 選擇播放類型（可跳過）
    type_selection_step = StepTemplate.create_selection_step(
        session=session,
        step_id="playback_type_selection",
        prompt="請選擇播放類型：",
        options=["1", "2", "3"],
        labels=["本地音樂播放", "YouTube 播放", "Spotify 播放"],
        required_data=[],
        skip_if_data_exists=True  # 如果 initial_data 已提供則跳過
    )
    
    # 步驟 2: 條件式步驟 - 所有類型都導向查詢輸入（query_input 支援跳過）
    type_conditional_step = StepTemplate.create_conditional_step(
        session=session,
        step_id="playback_type_conditional",
        selection_step_id="playback_type_selection",
        branches={
            "1": [],  # 本地 - 進入查詢輸入
            "2": [],  # YouTube - 進入查詢輸入
            "3": []   # Spotify - 進入查詢輸入
        },
        description="根據播放類型分支"
    )
    
    # 步驟 3: 輸入查詢（可跳過）
    query_input_step = StepTemplate.create_input_step(
        session=session,
        step_id="query_input",
        prompt="請輸入歌曲或藝人名稱（本地播放可留空）：",
        required_data=[],
        skip_if_data_exists=True  # 如果 initial_data 已提供則跳過
    )
    
    # 步驟 4: 執行播放（自動步驟）
    execute_step = StepTemplate.create_auto_step(
        session=session,
        step_id="execute_playback",
        processor=_execute_media_playback,
        required_data=["playback_type_selection"],
        prompt="正在啟動媒體播放...",
        description="執行播放操作"
    )
    
    # 步驟 5: 建立監控任務（自訂處理步驟）
    def create_media_monitor(sess: WorkflowSession) -> StepResult:
        """建立媒體播放監控任務"""
        try:
            import uuid
            from pathlib import Path
            from modules.sys_module.actions.automation_helper import (
                register_background_workflow,
                get_monitoring_pool,
                get_workflow_by_id,
                update_workflow_status
            )
            
            # 獲取播放信息
            playback_mode = sess.get_data("playback_mode", "")
            query = sess.get_data("query_input", "")
            initial_result = sess.get_data("initial_result", "")
            
            if not playback_mode:
                return StepResult.failure("缺少播放模式信息")
            
            # 生成唯一任務 ID
            task_id = f"media_{uuid.uuid4().hex[:8]}"
            
            # 註冊到資料庫
            success = register_background_workflow(
                task_id=task_id,
                workflow_type="media_playback",
                metadata={
                    "playback_mode": playback_mode,
                    "query": query,
                    "initial_result": initial_result
                }
            )
            
            if not success:
                return StepResult.failure("無法註冊背景服務到資料庫")
            
            # 定義監控函數（僅用於本地播放）
            def media_monitor_func(stop_event, check_interval, **kwargs):
                """媒體播放監控函數"""
                # YouTube 和 Spotify 不需要監控（它們在瀏覽器中運行）
                if playback_mode != "local":
                    info_log(f"[MediaMonitor] {playback_mode} 模式不需要背景監控")
                    update_workflow_status(task_id=task_id, status="COMPLETED")
                    return
                
                # 從配置讀取音樂資料夾
                from configs.config_loader import load_config
                config = load_config()
                music_folder = config.get("system", {}).get("media", {}).get("music_folder")
                if not music_folder:
                    music_folder = str(Path.home() / "Music")  # 預設值
                else:
                    music_folder = str(Path(music_folder).expanduser())
                
                while not stop_event.is_set():
                    try:
                        workflow = get_workflow_by_id(task_id)
                        if not workflow:
                            break
                        
                        metadata = workflow.get("metadata", {})
                        control_action = metadata.get("control_action")
                        
                        # 檢查播放器狀態（如果播放完成，結束監控）
                        from modules.sys_module.actions.automation_helper import get_music_player_status
                        player_status = get_music_player_status()
                        
                        if player_status["is_finished"] and not player_status["is_looping"]:
                            info_log(f"[MediaMonitor] 播放完成，結束監控：{task_id}")
                            info_log(f"[MediaMonitor] 最後播放歌曲：{player_status.get('current_song', 'Unknown')}")
                            update_workflow_status(
                                task_id=task_id,
                                status="COMPLETED",
                                metadata={
                                    **metadata,
                                    "completion_reason": "播放完成",
                                    "last_song": player_status.get("current_song")
                                }
                            )
                            break
                        
                        # 處理控制指令
                        if control_action:
                            control_params = metadata.get("control_params", {})
                            result = media_control(
                                action=control_action,
                                song_query=control_params.get("song_query", ""),
                                music_folder=music_folder
                            )
                            
                            info_log(f"[MediaMonitor] 執行控制：{control_action} -> {result}")
                            
                            # 清除控制指令
                            metadata["control_action"] = None
                            metadata["control_params"] = {}
                            metadata["last_result"] = result
                            
                            update_workflow_status(
                                task_id=task_id,
                                status="RUNNING",
                                metadata=metadata
                            )
                            
                            from core.event_bus import event_bus, SystemEvent
                            event_bus.publish(
                                SystemEvent.MEDIA_CONTROL_EXECUTED,
                                {"task_id": task_id, "action": control_action, "result": result},
                                source="sys"
                            )
                        
                        # 檢查是否要求停止
                        if metadata.get("stop_requested", False):
                            break
                        
                        # 更新狀態
                        update_workflow_status(
                            task_id=task_id,
                            status="RUNNING",
                            last_check_at=datetime.now().isoformat()
                        )
                        
                    except Exception as e:
                        error_log(f"[MediaMonitor] 監控錯誤：{e}")
                    
                    stop_event.wait(check_interval)
                
                info_log(f"[MediaMonitor] 監控結束：{task_id}")
                update_workflow_status(task_id=task_id, status="COMPLETED")
            
            # 提交到監控線程池（僅本地播放需要）
            if playback_mode == "local":
                monitoring_pool = get_monitoring_pool()
                submitted = monitoring_pool.submit_monitor(
                    task_id=task_id,
                    monitor_func=media_monitor_func,
                    check_interval=5
                )
                
                if not submitted:
                    return StepResult.failure("無法啟動背景監控服務")
                
                info_log(f"[MediaPlayback] 背景監控已啟動，任務 ID: {task_id}")
            else:
                info_log(f"[MediaPlayback] {playback_mode} 模式不需要背景監控")
            
            # 保存 task_id 到 session
            sess.add_data("task_id", task_id)
            
            # 工作流完成
            return StepResult.complete_workflow(
                f"媒體播放服務已啟動！\n{initial_result}\n\n任務 ID: {task_id}\n您可以隨時說「暫停音樂」「下一首」等來控制播放。",
                data={
                    "task_id": task_id,
                    "playback_mode": playback_mode,
                    "query": query
                }
            )
            
        except Exception as e:
            error_log(f"[MediaMonitor] 建立監控失敗：{e}")
            return StepResult.failure(f"建立監控失敗：{str(e)}")
    
    monitor_creation_step = StepTemplate.create_auto_step(
        session=session,
        step_id="create_monitor",
        processor=create_media_monitor,
        required_data=["playback_mode", "initial_result"],
        prompt="正在建立背景監控服務...",
        description="建立監控任務並提交到執行緒池"
    )
    
    # 組裝工作流
    workflow_def.add_step(type_selection_step)
    workflow_def.add_step(type_conditional_step)
    workflow_def.add_step(query_input_step)
    workflow_def.add_step(execute_step)
    workflow_def.add_step(monitor_creation_step)
    
    workflow_def.set_entry_point("playback_type_selection")
    workflow_def.add_transition("playback_type_selection", "playback_type_conditional")
    workflow_def.add_transition("playback_type_conditional", "query_input")
    workflow_def.add_transition("query_input", "execute_playback")
    workflow_def.add_transition("execute_playback", "create_monitor")
    workflow_def.add_transition("create_monitor", "END")
    
    # ✅ 返回 WorkflowDefinition（sys_module 會創建 WorkflowEngine）
    return workflow_def


# ==================== 工作流註冊表 ====================

def get_automation_workflow_creator(workflow_type: str):
    """
    獲取自動化工作流建立函數
    
    Args:
        workflow_type: 工作流類型
        
    Returns:
        工作流建立函數，簽名為 func(session: WorkflowSession, **kwargs) -> WorkflowDefinition
    """
    creators = {
        # 啟動工作流（與 YAML 中的命名一致）
        "media_playback": create_media_playback_workflow,
        "media_playback_start": create_media_playback_workflow,  # 別名，向後兼容
        
        # 干涉工作流
        "control_media": create_media_control_intervention_workflow,
        "media_control_intervention": create_media_control_intervention_workflow,  # 別名，向後兼容
    }
    
    return creators.get(workflow_type)


# ==================== 媒體播放干涉工作流 ====================

def _media_control_intervention_processor(
    task_id: str,
    control_action: str,
    control_params: Optional[Dict[str, Any]] = None
) -> StepResult:
    """
    媒體播放控制干涉處理器
    
    用於控制正在運行的媒體播放服務：
    - play, pause, stop, next, previous
    - search (搜尋並播放歌曲)
    - stop_service (停止整個監控服務)
    
    注意：背景服務是跨會話的，所有參數通過函數參數傳遞，不依賴 session
    """
    try:
        from modules.sys_module.actions.automation_helper import (
            get_workflow_by_id,
            update_workflow_status,
            log_intervention,
            get_monitoring_pool
        )
        
        # 使用傳入的參數
        action = control_action
        params = control_params or {}
        
        if not task_id:
            return StepResult.failure("缺少任務 ID")
        
        if not action:
            return StepResult.failure("缺少控制動作")
        
        # 檢查任務是否存在
        workflow = get_workflow_by_id(task_id)
        if not workflow:
            return StepResult.failure(f"找不到媒體播放任務：{task_id}")
        
        # 特殊處理：停止服務
        if action == "stop_service":
            monitoring_pool = get_monitoring_pool()
            success = monitoring_pool.stop_monitor(task_id)
            
            if success:
                log_intervention(
                    task_id=task_id,
                    action="stop_service",
                    result="監控服務已停止"
                )
                
                # 注意：任務狀態已在資料庫中更新為 COMPLETED，不需要清除 WorkingContext
                
                return StepResult.complete_workflow(
                    f"媒體播放服務已停止（任務 ID: {task_id}）",
                    data={"task_id": task_id, "action": "stop_service"}
                )
            else:
                return StepResult.failure("無法停止監控服務")
        
        # 一般控制指令：更新資料庫中的 metadata
        metadata = workflow.get("metadata", {})
        metadata["control_action"] = action
        metadata["control_params"] = params
        
        success = update_workflow_status(
            task_id=task_id,
            status="RUNNING",
            metadata=metadata
        )
        
        if not success:
            return StepResult.failure("無法更新控制指令")
        
        # 記錄干涉操作
        log_intervention(
            task_id=task_id,
            action=action,
            parameters=params,
            result="控制指令已發送"
        )
        
        info_log(f"[MediaIntervention] 已發送控制指令 {action} 到任務 {task_id}")
        
        return StepResult.complete_workflow(
            f"已發送媒體控制指令：{action}",
            data={
                "task_id": task_id,
                "action": action,
                "params": params
            }
        )
        
    except Exception as e:
        error_log(f"[MediaIntervention] 執行失敗：{e}")
        return StepResult.failure(f"媒體控制失敗：{str(e)}")


def create_media_control_intervention_workflow(
    session: WorkflowSession,
    task_id: Optional[str] = None,
    control_action: str = "",
    control_params: Optional[Dict[str, Any]] = None
) -> WorkflowEngine:
    """
    創建媒體播放控制干涉工作流
    
    用於控制正在運行的媒體播放服務，這是一個「干涉工作流」：
    1. 獲取要控制的任務 ID 和動作
    2. 將控制指令寫入資料庫
    3. 監控線程會讀取並執行
    4. 工作流完成，系統回到 IDLE
    
    Args:
        task_id: 要控制的媒體播放任務 ID（如未提供則自動獲取）
        control_action: 控制動作（play, pause, stop, next, previous, search, stop_service）
        control_params: 控制參數（如 song_query）
    """
    # 如果未提供 task_id，從資料庫獲取最近的活躍媒體任務
    # 注意：不使用 WorkingContext 因為它會在 GS 結束時清空
    # 資料庫是持久化的，可以跨 GS 查詢
    if not task_id:
        try:
            from modules.sys_module.actions.automation_helper import get_active_workflows
            active_workflows = get_active_workflows(workflow_type="media_playback")
            if active_workflows:
                # 取最近創建的任務（已按 created_at DESC 排序）
                task_id = active_workflows[0]["task_id"]
                info_log(f"[MediaIntervention] 自動獲取活躍的媒體任務: {task_id}")
            else:
                debug_log(2, f"[MediaIntervention] 沒有找到活躍的媒體任務")
        except Exception as e:
            error_log(f"[MediaIntervention] 無法從資料庫獲取 task_id: {e}")
    # 使用閉包捕獲參數，避免依賴 session（背景服務是跨會話的）
    def processor(sess: WorkflowSession) -> StepResult:
        # 如果到這裡還沒有 task_id，返回錯誤
        if not task_id:
            return StepResult.failure("找不到活躍的媒體播放任務，請先啟動播放服務")
        
        return _media_control_intervention_processor(
            task_id=task_id,
            control_action=control_action,
            control_params=control_params or {}
        )
    
    # 使用 StepTemplate 創建步驟
    control_step = StepTemplate.create_processing_step(
        session=session,
        step_id="media_control_intervention",
        processor=processor,
        required_data=[],  # 參數通過閉包傳遞，不依賴 session 數據
        description="執行媒體播放控制指令"
    )
    
    # 創建工作流定義（干涉工作流使用 DIRECT 模式）
    workflow_def = WorkflowDefinition(
        workflow_type="media_control_intervention",
        name="媒體播放控制",
        description="控制正在運行的媒體播放服務",
        workflow_mode=WorkflowMode.DIRECT,  # 干涉工作流是 DIRECT（快速完成）
        requires_llm_review=True  # ✅ 啟用 LLM 審核，讓 LLM 在干涉時給予回應
    )
    workflow_def.add_step(control_step)
    workflow_def.set_entry_point(control_step.id)
    
    # ✅ 返回 WorkflowDefinition（sys_module 會創建 WorkflowEngine）
    return workflow_def
