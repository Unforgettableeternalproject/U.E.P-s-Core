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
    執行本地音樂播放（不包含監控註冊，監控註冊由 monitor_creation_step 負責）
    
    支援功能：
    - 播放指定歌曲或整個資料夾
    - shuffle: 隨機播放
    - loop: 循環播放
    
    此步驟只負責啟動播放，並將結果保存到 session 中供後續步驟使用。
    """
    try:
        from pathlib import Path
        from configs.config_loader import load_config
        
        # 獲取參數
        query = session.get_data("query_input", "")
        shuffle = session.get_data("shuffle", False)
        loop = session.get_data("loop", False)
        
        # 從配置讀取音樂資料夾
        config = load_config()
        music_folder = config.get("system", {}).get("media", {}).get("music_folder")
        if not music_folder:
            music_folder = str(Path.home() / "Music")  # 預設值
        else:
            music_folder = str(Path(music_folder).expanduser())
        
        # 智能判斷循環模式
        loop_mode = "off"
        if loop:
            if query:  # 有指定歌曲 → 單曲循環
                loop_mode = "one"
            else:  # 無指定歌曲 → 播放清單循環
                loop_mode = "all"
        
        # 構建播放參數
        play_params = {
            "action": "play",
            "song_query": query,
            "music_folder": music_folder,
            "shuffle": shuffle,
            "loop_mode": loop_mode
        }
        
        result_message = media_control(**play_params)
        
        # 判斷播放模式
        if query:
            playback_type = "single_song"
            description = f"播放歌曲: {query}"
        else:
            playback_type = "playlist"
            description = f"播放資料夾: {music_folder}"
        
        if shuffle:
            description += " (隨機)"
        if loop_mode == "one":
            description += " (單曲循環)"
        elif loop_mode == "all":
            description += " (播放清單循環)"
        
        info_log(f"[MediaPlayback] {description} - {result_message}")
        
        # 將播放結果保存到 session，供 monitor_creation_step 使用
        return StepResult.success(
            f"本地音樂播放已啟動\n{result_message}",
            data={
                "playback_mode": "local",
                "playback_type": playback_type,
                "query": query,
                "shuffle": shuffle,
                "loop": loop,
                "loop_mode": loop_mode,
                "initial_result": result_message
            }
        )
        
    except Exception as e:
        error_log(f"[MediaPlayback] 執行失敗：{e}")
        return StepResult.failure(f"媒體播放失敗：{str(e)}")


def create_media_playback_workflow(
    session: WorkflowSession,
    query: Optional[str] = None,
    shuffle: bool = False,
    loop: bool = False
) -> WorkflowEngine:
    """
    創建本地音樂播放服務工作流（背景服務啟動）
    
    工作流程：
    1. execute_playback - 執行播放（query 由 LLM 在啟動時提供）
    2. create_monitor - 建立監控任務並提交到執行緒池（自動步驟）
    
    ❌ 已移除 query_input 互動步驟：背景工作流不能有互動步驟
    
    Args:
        query: 歌曲查詢（必需，留空字串則播放整個資料夾）
        shuffle: 是否隨機播放
        loop: 是否循環播放
    
    Returns:
        WorkflowDefinition 實例
        
    播放邏輯：
    - 有指定歌曲：播放該歌曲，完畢後任務結束
    - 無指定歌曲：播放整個資料夾，完畢後任務結束
    - 開啟循環：持續播放直到用戶手動停止
    """
    workflow_def = WorkflowDefinition(
        workflow_type="media_playback",
        name="本地音樂播放",
        description="播放本地音樂（支援隨機、循環）",
        workflow_mode=WorkflowMode.BACKGROUND,  # ✅ 背景工作流
        requires_llm_review=False  # ❌ 背景工作流不需要 LLM 審核（完全自動化）
    )
    
    # 預先保存參數到 session（包括空值）
    # ❌ 移除 Interactive 步驟：背景工作流不能有互動步驟
    # query 現在是必需參數，LLM 必須在啟動工作流時提供（即使是空字串）
    if query is not None:  # 只要有提供（即使是空字符串），就設置
        session.add_data("query_input", query)
    else:
        # 如果 LLM 沒有提供 query（不應該發生），設為空字串
        session.add_data("query_input", "")
    
    if shuffle:
        session.add_data("shuffle", shuffle)
    if loop:
        session.add_data("loop", loop)
    
    # ❌ 步驟 1: 輸入歌曲查詢（已移除 - 背景工作流不能有互動步驟）
    # query_input_step = StepTemplate.create_input_step(
    #     session=session,
    #     step_id="query_input",
    #     prompt="請輸入歌曲名稱（留空則播放整個音樂資料夾）：",
    #     required_data=[],
    #     skip_if_data_exists=True,  # 如果 initial_data 已提供則跳過
    #     optional=True  # 標記為可選
    # )
    
    # 步驟 2: 執行播放（自動步驟）
    execute_step = StepTemplate.create_auto_step(
        session=session,
        step_id="execute_playback",
        processor=_execute_media_playback,
        required_data=[],  # query 是可選的
        prompt="正在啟動本地音樂播放...",
        description="執行本地音樂播放"
    )
    
    # 步驟 3: 建立監控任務（自訂處理步驟）
    def create_media_monitor(sess: WorkflowSession) -> StepResult:
        """建立本地音樂播放監控任務"""
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
            playback_type = sess.get_data("playback_type", "")
            query = sess.get_data("query_input", "")
            shuffle = sess.get_data("shuffle", False)
            loop = sess.get_data("loop", False)
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
                    "playback_type": playback_type,
                    "query": query,
                    "shuffle": shuffle,
                    "loop": loop,
                    "initial_result": initial_result
                }
            )
            
            if not success:
                return StepResult.failure("無法註冊背景服務到資料庫")
            
            # 定義監控函數（本地播放專用）
            def media_monitor_func(stop_event, check_interval, **kwargs):
                """本地音樂播放監控函數"""
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
                        playback_type = metadata.get("playback_type", "playlist")  # ✅ 從 metadata 獲取
                        
                        # 檢查播放器狀態
                        from modules.sys_module.actions.automation_helper import get_music_player_status
                        player_status = get_music_player_status()
                        
                        # 判斷是否應該結束任務
                        is_looping = player_status.get("is_looping", False)
                        is_finished = player_status.get("is_finished", False)
                        
                        # 結束條件：
                        # 1. 沒有開啟循環 且 播放完成
                        # 2. 用戶要求停止
                        if is_finished and not is_looping:
                            info_log(f"[MediaMonitor] 播放完成，結束監控：{task_id}")
                            
                            completion_reason = "單曲播放完成" if playback_type == "single_song" else "播放清單完成"
                            info_log(f"[MediaMonitor] {completion_reason}")
                            
                            update_workflow_status(
                                task_id=task_id,
                                status="COMPLETED",
                                metadata={
                                    **metadata,
                                    "completion_reason": completion_reason,
                                    "last_song": player_status.get("current_song", "Unknown")
                                }
                            )
                            break
                        
                        # 處理控制指令
                        if control_action:
                            control_params = metadata.get("control_params", {})
                            
                            # 構建控制參數
                            control_kwargs = {
                                "action": control_action,
                                "music_folder": music_folder
                            }
                            
                            # 根據不同控制動作添加參數
                            if control_action in ["search", "play"]:
                                control_kwargs["song_query"] = control_params.get("song_query", "")
                            elif control_action == "shuffle":
                                control_kwargs["shuffle"] = control_params.get("shuffle", True)
                            elif control_action == "loop":
                                # 智能判斷循環模式（基於當前播放狀態）
                                # 獲取當前播放器狀態
                                from modules.sys_module.actions.automation_helper import get_music_player_status
                                player_status = get_music_player_status()
                                
                                # 如果當前沒有循環，根據 playback_type 設定適當的循環模式
                                if not player_status.get("is_looping", False):
                                    playback_type = metadata.get("playback_type", "playlist")
                                    if playback_type == "single_song":
                                        # 單曲播放 → 直接設定為單曲循環
                                        control_kwargs["action"] = "set_loop_mode"  # 自定義動作
                                        control_kwargs["loop_mode"] = "one"
                                        debug_log(2, f"[MediaMonitor] 單曲播放，設定為單曲循環")
                                    else:
                                        # 播放清單播放 → 直接設定為播放清單循環
                                        control_kwargs["action"] = "set_loop_mode"  # 自定義動作
                                        control_kwargs["loop_mode"] = "all"
                                        debug_log(2, f"[MediaMonitor] 播放清單播放，設定為播放清單循環")
                                else:
                                    # 如果已經有循環，則使用 toggle（切換到下一個模式）
                                    control_kwargs["action"] = "loop"
                                    debug_log(2, f"[MediaMonitor] 已有循環模式，使用 toggle 切換")
                            
                            result = media_control(**control_kwargs)
                            
                            info_log(f"[MediaMonitor] 執行控制：{control_action} -> {result}")
                            
                            # 更新 metadata 中的 shuffle/loop 狀態
                            if control_action == "shuffle":
                                metadata["shuffle"] = control_params.get("shuffle", True)
                            elif control_action == "loop":
                                metadata["loop"] = control_params.get("loop", True)
                            
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
                            info_log(f"[MediaMonitor] 用戶要求停止：{task_id}")
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
            
            # 提交到監控線程池
            monitoring_pool = get_monitoring_pool()
            submitted = monitoring_pool.submit_monitor(
                task_id=task_id,
                monitor_func=media_monitor_func,
                check_interval=5
            )
            
            if not submitted:
                return StepResult.failure("無法啟動背景監控服務")
            
            info_log(f"[MediaPlayback] 背景監控已啟動，任務 ID: {task_id}")
            
            # 保存 task_id 到 session
            sess.add_data("task_id", task_id)
            
            # 構建完成訊息
            mode_desc = ""
            if shuffle:
                mode_desc += "隨機"
            if loop:
                mode_desc += "循環"
            if mode_desc:
                mode_desc = f" ({mode_desc})"
            
            completion_msg = f"本地音樂播放已啟動{mode_desc}！\n{initial_result}\n\n任務 ID: {task_id}\n隨時可以控制播放。"
            
            # 工作流完成
            return StepResult.complete_workflow(
                completion_msg,
                data={
                    "task_id": task_id,
                    "playback_mode": playback_mode,
                    "playback_type": playback_type,
                    "query": query,
                    "shuffle": shuffle,
                    "loop": loop
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
    # ❌ 移除 query_input_step（背景工作流不能有互動步驟）
    # workflow_def.add_step(query_input_step)
    workflow_def.add_step(execute_step)
    workflow_def.add_step(monitor_creation_step)
    
    # ✅ 直接從 execute_playback 開始（query 由 LLM 在啟動時提供）
    workflow_def.set_entry_point("execute_playback")
    # workflow_def.add_transition("query_input", "execute_playback")  # ❌ 移除
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
        # 媒體播放工作流（與 YAML 中的命名一致）
        "media_playback": create_media_playback_workflow,
        "media_playback_start": create_media_playback_workflow,  # 別名，向後兼容
        
        # 媒體控制工作流
        "control_media": create_media_control_intervention_workflow,
        "media_control_intervention": create_media_control_intervention_workflow,  # 別名，向後兼容
        
        # 待辦事項工作流
        "create_todo": create_todo_workflow,
        "manage_todo": manage_todo_workflow,
        
        # 行事曆工作流
        "create_calendar": create_calendar_workflow,
        "manage_calendar": manage_calendar_workflow,
    }
    
    return creators.get(workflow_type)


# ==================== 媒體播放干涉工作流 ====================

def _media_control_intervention_processor(
    task_id: str,
    control_action: str,
    control_params: Optional[Dict[str, Any]] = None
) -> StepResult:
    """
    本地音樂播放控制干涉處理器
    
    用於控制正在運行的本地音樂播放服務：
    - play, pause, stop, next, previous
    - search (搜尋並播放歌曲)
    - shuffle (開啟/關閉隨機播放)
    - loop (開啟/關閉循環播放)
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
    創建本地音樂播放控制干涉工作流
    
    用於控制正在運行的本地音樂播放服務，這是一個「干涉工作流」：
    1. 獲取要控制的任務 ID 和動作
    2. 將控制指令寫入資料庫
    3. 監控線程會讀取並執行
    4. 工作流完成，系統回到 IDLE
    
    Args:
        task_id: 要控制的媒體播放任務 ID（如未提供則自動獲取）
        control_action: 控制動作（play, pause, stop, next, previous, search, shuffle, loop, stop_service）
        control_params: 控制參數（如 song_query, shuffle, loop）
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
        name="本地音樂播放控制",
        description="控制正在運行的本地音樂播放服務",
        workflow_mode=WorkflowMode.DIRECT,  # 干涉工作流是 DIRECT（快速完成）
        requires_llm_review=True  # ✅ 啟用 LLM 審核，讓 LLM 在干涉時給予回應
    )
    workflow_def.add_step(control_step)
    workflow_def.set_entry_point(control_step.id)
    
    # ✅ 返回 WorkflowDefinition（sys_module 會創建 WorkflowEngine）
    return workflow_def


# ==================== 待辦事項工作流 ====================

def create_todo_workflow(
    session: WorkflowSession,
    task_name: str = "General Task",
    task_description: str = "",
    priority: str = "none"
) -> WorkflowDefinition:
    """
    創建待辦事項工作流（背景服務）
    
    簡單的一次性資料庫寫入操作，不需要監控。
    
    Args:
        task_name: 任務名稱（預設：General Task）
        task_description: 任務描述（可選）
        priority: 優先級（none, low, medium, high，預設：none）
    
    Returns:
        WorkflowDefinition 實例
    """
    workflow_def = WorkflowDefinition(
        workflow_type="create_todo",
        name="創建待辦事項",
        description="建立新的待辦任務",
        workflow_mode=WorkflowMode.BACKGROUND,
        requires_llm_review=False
    )
    
    # 驗證並設定優先級
    valid_priorities = ["none", "low", "medium", "high"]
    if priority not in valid_priorities:
        priority = "none"
    
    # 使用 create_processing_step 直接調用 automation_helper
    def execute_create_todo(sess: WorkflowSession) -> StepResult:
        """執行創建待辦事項"""
        try:
            from modules.sys_module.actions.automation_helper import local_todo
            
            # 調用 CRUD 函數創建任務
            result = local_todo(
                action="create",
                task_name=task_name,
                task_description=task_description,
                priority=priority
            )
            
            if result.get("status") == "ok":
                task_id = result.get("task_id")
                info_log(f"[CreateTodo] 已建立待辦事項：{task_name} (ID: {task_id}, 優先級: {priority})")
                return StepResult.success(
                    f"已建立待辦事項「{task_name}」（優先級：{priority}）",
                    {"task_id": task_id, "task_name": task_name, "priority": priority}
                )
            else:
                error_msg = result.get("message", "未知錯誤")
                error_log(f"[CreateTodo] 建立失敗：{error_msg}")
                return StepResult.failure(f"建立待辦事項失敗：{error_msg}")
        
        except Exception as e:
            error_log(f"[CreateTodo] 執行失敗：{e}")
            return StepResult.failure(f"建立待辦事項時發生錯誤：{str(e)}")
    
    # 使用 create_processing_step
    create_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_create_todo",
        processor=execute_create_todo,
        required_data=[],
        description="創建待辦事項並保存到資料庫"
    )
    
    workflow_def.add_step(create_step)
    workflow_def.set_entry_point(create_step.id)
    
    return workflow_def


def manage_todo_workflow(
    session: WorkflowSession,
    operation: Optional[str] = None,
    **kwargs  # 接收其他 initial_data 參數（如 task_name_hint, update_intent）
) -> WorkflowDefinition:
    """
    管理待辦事項工作流（直接工作流，用於查詢、修改、刪除）
    
    支援操作：
    - list: 列出所有待辦事項
    - search: 搜尋待辦事項
    - update: 更新待辦事項
    - delete: 刪除待辦事項
    - complete: 完成待辦事項
    
    工作流程：
    1. 選擇操作類型（list/search/update/delete/complete）
    2. 根據操作類型條件輸入：
       - list: 無需額外輸入
       - search: 輸入搜尋關鍵字
       - update: 選擇任務 → 輸入更新欄位
       - delete: 選擇任務
       - complete: 選擇任務
    3. 執行操作並顯示結果
    
    Args:
        operation: 操作類型（可選，可從 initial_data 提取）
    
    Returns:
        WorkflowDefinition 實例
    """
    workflow_def = WorkflowDefinition(
        workflow_type="manage_todo",
        name="管理待辦事項",
        description="查詢、修改或刪除待辦事項",
        workflow_mode=WorkflowMode.DIRECT,  # 直接工作流
        requires_llm_review=True  # ✅ DIRECT 工作流需要審核以生成步驟間提示
    )
    
    # 如果從 initial_data 提供了 operation，保存到 session
    if operation:
        session.add_data("action_selection", operation)
    
    # 步驟 1: 選擇操作類型
    # SelectionStep 現在支援模糊匹配，可以從 "update a task" 中提取 "update"
    action_selection_step = StepTemplate.create_selection_step(
        session=session,
        step_id="action_selection",
        prompt="請選擇要執行的操作：",
        options=["list", "search", "update", "delete", "complete"],
        labels=["列出所有待辦", "搜尋待辦", "更新待辦", "刪除待辦", "完成待辦"],
        required_data=[],
        skip_if_data_exists=True  # 支援從 initial_data 提取
    )
    
    # 步驟 2a: 通用輸入步驟（可用於 search/update/delete/complete）
    # 對於 search：輸入搜尋關鍵字
    # 對於 update/delete/complete：輸入任務關鍵字或 ID，LLM 會解析
    search_input_step = StepTemplate.create_input_step(
        session=session,
        step_id="search_query_input",
        prompt="請輸入搜尋關鍵字或任務 ID：",
        optional=False,
        skip_if_data_exists=True,
        description="收集搜尋關鍵字或任務標識"
    )
    
    # 步驟 2c: 更新欄位輸入（僅 update 需要）
    update_fields_input_step = StepTemplate.create_input_step(
        session=session,
        step_id="update_fields_input",
        prompt="請輸入要更新的內容（可包含：task_name, task_description, priority, deadline）：",
        optional=False,
        skip_if_data_exists=True,
        description="收集更新欄位"
    )
    

    # 步驟 3: 條件分支（根據操作類型決定需要哪些輸入）
    action_conditional_step = StepTemplate.create_conditional_step(
        session=session,
        step_id="action_conditional",
        selection_step_id="action_selection",
        branches={
            "list": [],  # 無需額外輸入
            "search": [search_input_step],  # 需要搜尋關鍵字
            "update": [search_input_step, update_fields_input_step],  # 需要任務關鍵字 + 更新欄位（LLM 會在審核時解析自然語言）
            "delete": [search_input_step],  # 需要任務關鍵字
            "complete": [search_input_step],  # 需要任務關鍵字
        },
        description="根據操作類型決定需要的輸入"
    )
    
    # 步驟 4: 執行管理任務
    def execute_manage_todo(sess: WorkflowSession) -> StepResult:
        """執行管理待辦事項"""
        try:
            from modules.sys_module.actions.automation_helper import local_todo
            
            # 獲取參數
            action = sess.get_data("action_selection", "list")
            search_query = sess.get_data("search_query_input", "")
            update_fields_str = sess.get_data("update_fields_input", "")
            
            # 對於 update/delete/complete 操作，search_query_input 包含任務關鍵字或 ID
            # 需要先搜尋找到任務 ID
            task_id = None
            if action in ["update", "delete", "complete"] and search_query:
                # 嘗試直接解析為 ID
                try:
                    task_id = int(search_query)
                except ValueError:
                    # 如果不是數字，則用關鍵字搜尋
                    result = local_todo(action="search", search_query=search_query)
                    if result.get("status") == "ok":
                        tasks = result.get("tasks", [])
                        if tasks:
                            # 使用第一個匹配的任務
                            task_id = tasks[0]["id"]
                            info_log(f"[ManageTodo] 從關鍵字「{search_query}」找到任務 ID: {task_id}")
                        else:
                            # 找不到任務，中止工作流
                            return StepResult.failure(
                                f"找不到包含「{search_query}」的待辦事項"
                            )
                    else:
                        return StepResult.failure(
                            f"搜尋失敗：{result.get('message', '未知錯誤')}"
                        )
            
            # 解析更新欄位（如果有）
            # LLM 應該已經將自然語言轉換為結構化數據（JSON 或 key=value）
            update_fields = {}
            if update_fields_str:
                try:
                    import json
                    # 嘗試 JSON 格式（LLM 應該提供這個）
                    update_fields = json.loads(update_fields_str)
                except:
                    # 嘗試簡單的 key=value 格式
                    for pair in update_fields_str.split(","):
                        if "=" in pair:
                            key, value = pair.split("=", 1)
                            update_fields[key.strip()] = value.strip()
                
                # 如果仍然無法解析（純自然語言），返回明確錯誤讓 LLM 看到
                if not update_fields:
                    return StepResult.failure(
                        f"無法解析更新欄位：「{update_fields_str}」。"
                        f"請提供 JSON 格式（例如：{{\"priority\": \"medium\"}}）或 key=value 格式（例如：priority=medium）"
                    )
            
            # 根據不同操作調用 CRUD 函數
            if action == "list":
                result = local_todo(action="list")
                
                if result.get("status") == "ok":
                    tasks = result.get("tasks", [])
                    if not tasks:
                        return StepResult.complete_workflow("目前沒有待辦事項", {"tasks": []})
                    
                    # 格式化輸出（移除 emojis）
                    task_list = []
                    for task in tasks:
                        priority_text = {"high": "[高]", "medium": "[中]", "low": "[低]", "none": ""}.get(task["priority"], "")
                        task_list.append(
                            f"{priority_text} [{task['id']}] {task['task_name']}"
                            + (f" - {task['task_description']}" if task.get("task_description") else "")
                        )
                    
                    info_log(f"[ManageTodo] 列出 {len(tasks)} 個待辦事項")
                    return StepResult.complete_workflow(
                        f"您有 {len(tasks)} 個待辦事項：\n" + "\n".join(task_list),
                        {"tasks": tasks}
                    )
            
            elif action == "search":
                if not search_query:
                    return StepResult.failure("搜尋需要提供關鍵字")
                
                result = local_todo(action="search", search_query=search_query)
                
                if result.get("status") == "ok":
                    tasks = result.get("tasks", [])
                    if not tasks:
                        return StepResult.complete_workflow(f"找不到包含「{search_query}」的待辦事項", {"tasks": []})
                    
                    # 格式化輸出（移除 emojis）
                    task_list = []
                    for task in tasks:
                        priority_text = {"high": "[高]", "medium": "[中]", "low": "[低]", "none": ""}.get(task["priority"], "")
                        task_list.append(
                            f"{priority_text} [{task['id']}] {task['task_name']}"
                            + (f" - {task['task_description']}" if task.get("task_description") else "")
                        )
                    
                    info_log(f"[ManageTodo] 搜尋「{search_query}」找到 {len(tasks)} 個結果")
                    return StepResult.complete_workflow(
                        f"找到 {len(tasks)} 個結果：\n" + "\n".join(task_list),
                        {"tasks": tasks}
                    )
            
            elif action == "update":
                if task_id is None:
                    return StepResult.failure("更新任務需要選擇任務")
                if not update_fields:
                    return StepResult.failure("更新任務需要提供更新欄位")
                
                result = local_todo(
                    action="update",
                    task_id=task_id,
                    task_name=update_fields.get("task_name", ""),
                    task_description=update_fields.get("task_description", ""),
                    priority=update_fields.get("priority", ""),
                    deadline=update_fields.get("deadline", "")
                )
                
                if result.get("status") == "ok":
                    info_log(f"[ManageTodo] 已更新任務 ID: {task_id}")
                    return StepResult.complete_workflow(
                        f"✅ 已更新任務 ID: {task_id}",
                        {"task_id": task_id, "update_fields": update_fields}
                    )
                else:
                    error_msg = result.get("message", "未知錯誤")
                    return StepResult.failure(f"更新失敗：{error_msg}")
            
            elif action == "delete":
                if task_id is None:
                    return StepResult.failure("刪除任務需要選擇任務")
                
                result = local_todo(action="delete", task_id=task_id)
                
                if result.get("status") == "ok":
                    info_log(f"[ManageTodo] 已刪除任務 ID: {task_id}")
                    return StepResult.complete_workflow(
                        f"🗑️ 已刪除任務 ID: {task_id}",
                        {"task_id": task_id}
                    )
                else:
                    error_msg = result.get("message", "未知錯誤")
                    return StepResult.failure(f"刪除失敗：{error_msg}")
            
            elif action == "complete":
                if task_id is None:
                    return StepResult.failure("完成任務需要選擇任務")
                
                result = local_todo(action="complete", task_id=task_id)
                
                if result.get("status") == "ok":
                    info_log(f"[ManageTodo] 已完成任務 ID: {task_id}")
                    return StepResult.complete_workflow(
                        f"✅ 已完成任務 ID: {task_id}",
                        {"task_id": task_id}
                    )
                else:
                    error_msg = result.get("message", "未知錯誤")
                    return StepResult.failure(f"完成失敗：{error_msg}")
            
            else:
                return StepResult.failure(f"不支援的操作：{action}")
        
        except Exception as e:
            error_log(f"[ManageTodo] 執行失敗：{e}")
            return StepResult.failure(f"管理待辦事項時發生錯誤：{str(e)}")
    
    # 創建執行步驟
    execute_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_manage_todo",
        processor=execute_manage_todo,
        required_data=["action_selection"],
        description="執行待辦事項管理操作"
    )
    
    # 組裝工作流
    workflow_def.add_step(action_selection_step)
    workflow_def.add_step(search_input_step)
    workflow_def.add_step(update_fields_input_step)
    workflow_def.add_step(action_conditional_step)
    workflow_def.add_step(execute_step)
    
    workflow_def.set_entry_point("action_selection")
    workflow_def.add_transition("action_selection", "action_conditional")
    # 🔧 分支步驟完成後需要回到 conditional 繼續執行
    workflow_def.add_transition("search_query_input", "action_conditional")
    workflow_def.add_transition("update_fields_input", "action_conditional")
    workflow_def.add_transition("action_conditional", "execute_manage_todo")
    workflow_def.add_transition("execute_manage_todo", "END")
    
    return workflow_def


# ==================== 行事曆工作流 ====================

def create_calendar_workflow(
    session: WorkflowSession,
    start_time: str,
    end_time: Optional[str] = None,
    event_name: str = "General Event"
) -> WorkflowDefinition:
    """
    創建行事曆事件工作流（背景服務）
    
    簡單的一次性資料庫寫入操作，不需要監控。
    
    Args:
        start_time: 開始時間（ISO 格式，必填）
        end_time: 結束時間（ISO 格式，可選，預設為當天 23:59）
        event_name: 事件名稱（預設：General Event）
    
    Returns:
        WorkflowDefinition 實例
    """
    workflow_def = WorkflowDefinition(
        workflow_type="create_calendar",
        name="創建行事曆事件",
        description="建立新的行事曆事件",
        workflow_mode=WorkflowMode.BACKGROUND,
        requires_llm_review=False
    )
    
    # 處理 end_time 預設值（當天 23:59）
    if not end_time:
        try:
            start_dt = datetime.fromisoformat(start_time)
            end_dt = start_dt.replace(hour=23, minute=59, second=59)
            end_time = end_dt.isoformat()
        except Exception as e:
            error_log(f"[CreateCalendar] 無法解析 start_time: {e}")
            end_time = ""
    
    # 使用 create_processing_step 直接調用 automation_helper
    def execute_create_calendar(sess: WorkflowSession) -> StepResult:
        """執行創建行事曆事件"""
        try:
            from modules.sys_module.actions.automation_helper import local_calendar
            
            # 驗證必要參數
            if not start_time:
                return StepResult.failure("缺少開始時間")
            if not end_time:
                return StepResult.failure("缺少結束時間")
            
            # 調用 CRUD 函數創建事件
            result = local_calendar(
                action="create",
                summary=event_name,
                start_time=start_time,
                end_time=end_time
            )
            
            if result.get("status") == "ok":
                event_id = result.get("event_id")
                info_log(f"[CreateCalendar] 已建立事件：{event_name} ({start_time} ~ {end_time})")
                return StepResult.success(
                    f"已建立行事曆事件「{event_name}」（{start_time} ~ {end_time}）",
                    {"event_id": event_id, "event_name": event_name, "start_time": start_time, "end_time": end_time}
                )
            else:
                error_msg = result.get("message", "未知錯誤")
                error_log(f"[CreateCalendar] 建立失敗：{error_msg}")
                return StepResult.failure(f"建立行事曆事件失敗：{error_msg}")
        
        except Exception as e:
            error_log(f"[CreateCalendar] 執行失敗：{e}")
            return StepResult.failure(f"建立行事曆事件時發生錯誤：{str(e)}")
    
    # 使用 create_processing_step
    create_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_create_calendar",
        processor=execute_create_calendar,
        required_data=[],
        description="創建行事曆事件並保存到資料庫"
    )
    
    workflow_def.add_step(create_step)
    workflow_def.set_entry_point(create_step.id)
    
    return workflow_def


def manage_calendar_workflow(
    session: WorkflowSession,
    operation: Optional[str] = None,
    **kwargs  # 接收其他 initial_data 參數（如 event_name_hint, time_context, update_intent）
) -> WorkflowDefinition:
    """
    管理行事曆事件工作流（直接工作流，用於查詢、修改、刪除）
    
    支援操作：
    - list: 列出行事曆事件
    - search: 搜尋事件
    - update: 更新事件
    - delete: 刪除事件
    - find_free_time: 查找空閒時段
    
    工作流程：
    1. 選擇操作類型（list/search/update/delete/find_free_time）
    2. 根據操作類型條件輸入：
       - list: 無需額外輸入（或可選時間範圍）
       - search: 輸入搜尋關鍵字
       - update: 選擇事件 → 輸入更新欄位
       - delete: 選擇事件
       - find_free_time: 無需額外輸入
    3. 執行操作並顯示結果
    
    Args:
        operation: 操作類型（可選，可從 initial_data 提取）
    
    Returns:
        WorkflowDefinition 實例
    """
    workflow_def = WorkflowDefinition(
        workflow_type="manage_calendar",
        name="管理行事曆事件",
        description="查詢、修改或刪除行事曆事件",
        workflow_mode=WorkflowMode.DIRECT,  # 直接工作流
        requires_llm_review=True  # ✅ DIRECT 工作流需要審核以生成步驟間提示
    )
    
    # 如果從 initial_data 提供了 operation，保存到 session
    if operation:
        session.add_data("action_selection", operation)
    
    # 步驟 1: 選擇操作類型
    action_selection_step = StepTemplate.create_selection_step(
        session=session,
        step_id="action_selection",
        prompt="請選擇要執行的操作：",
        options=["list", "search", "update", "delete", "find_free_time"],
        labels=["列出行事曆", "搜尋事件", "更新事件", "刪除事件", "查找空閒時段"],
        required_data=[],
        skip_if_data_exists=True
    )
    
    # 步驟 2a: 通用輸入步驟（可用於 search/update/delete）
    # 對於 search：輸入搜尋關鍵字
    # 對於 update/delete：輸入事件關鍵字或 ID，LLM 會解析
    search_input_step = StepTemplate.create_input_step(
        session=session,
        step_id="search_query_input",
        prompt="請輸入搜尋關鍵字或事件 ID：",
        optional=False,
        skip_if_data_exists=True,
        description="收集搜尋關鍵字或事件標識"
    )
    
    # 步驟 2b: 更新欄位輸入（僅 update 需要）
    update_fields_input_step = StepTemplate.create_input_step(
        session=session,
        step_id="update_fields_input",
        prompt="請輸入要更新的內容（可包含：event_name, start_time, end_time, location, description）：",
        optional=False,
        skip_if_data_exists=True,
        description="收集更新欄位"
    )
    
    # 步驟 3: 條件分支（根據操作類型決定需要哪些輸入）
    action_conditional_step = StepTemplate.create_conditional_step(
        session=session,
        step_id="action_conditional",
        selection_step_id="action_selection",
        branches={
            "list": [],  # 無需額外輸入
            "search": [search_input_step],  # 需要搜尋關鍵字
            "update": [search_input_step, update_fields_input_step],  # 需要事件關鍵字 + 更新欄位（LLM 會在審核時解析自然語言）
            "delete": [search_input_step],  # 需要事件關鍵字
            "find_free_time": [],  # 無需額外輸入
        },
        description="根據操作類型決定需要的輸入"
    )
    
    # 步驟 4: 執行管理事件
    def execute_manage_calendar(sess: WorkflowSession) -> StepResult:
        """執行管理行事曆事件"""
        try:
            from modules.sys_module.actions.automation_helper import local_calendar
            from datetime import datetime
            
            # 獲取參數
            action = sess.get_data("action_selection", "list")
            search_query = sess.get_data("search_query_input", "")
            update_fields_str = sess.get_data("update_fields_input", "")
            
            # 對於 update/delete 操作，search_query_input 包含事件關鍵字或 ID
            # 需要先搜尋找到事件 ID
            event_id = None
            if action in ["update", "delete"] and search_query:
                # 嘗試直接解析為 ID
                try:
                    event_id = int(search_query)
                except ValueError:
                    # 如果不是數字，則用關鍵字搜尋
                    # 先列出所有事件
                    result = local_calendar(action="list")
                    if result.get("status") == "ok":
                        events = result.get("events", [])
                        # 過濾包含關鍵字的事件
                        search_lower = search_query.lower() if search_query else ""
                        matching_events = [
                            e for e in events
                            if search_lower in (e.get("summary") or "").lower() or
                               search_lower in (e.get("description") or "").lower()
                        ]
                        
                        if matching_events:
                            # 使用第一個匹配的事件
                            event_id = matching_events[0]["id"]
                            info_log(f"[ManageCalendar] 從關鍵字「{search_query}」找到事件 ID: {event_id}")
                        else:
                            # 找不到事件，中止工作流
                            return StepResult.failure(
                                f"找不到包含「{search_query}」的行事曆事件"
                            )
                    else:
                        return StepResult.failure(
                            f"搜尋失敗：{result.get('message', '未知錯誤')}"
                        )
            
            # 解析更新欄位（如果有）
            # LLM 應該已經將自然語言轉換為結構化數據（JSON 或 key=value）
            update_fields = {}
            if update_fields_str:
                try:
                    import json
                    # 嘗試 JSON 格式（LLM 應該提供這個）
                    update_fields = json.loads(update_fields_str)
                except:
                    # 嘗試簡單的 key=value 格式
                    for pair in update_fields_str.split(","):
                        if "=" in pair:
                            key, value = pair.split("=", 1)
                            update_fields[key.strip()] = value.strip()
            
            # 根據不同操作調用 CRUD 函數
            if action == "list":
                result = local_calendar(action="list")
                
                if result.get("status") == "ok":
                    events = result.get("events", [])
                    if not events:
                        return StepResult.success("目前沒有行事曆事件")
                    
                    # 格式化輸出
                    event_list = []
                    for event in events:
                        start_str = event.get("start_time", "")
                        end_str = event.get("end_time", "")
                        event_list.append(
                            f"[{event['id']}] {event['summary']}: {start_str} ~ {end_str}"
                            + (f"\n    📍 {event['location']}" if event.get("location") else "")
                        )
                    
                    info_log(f"[ManageCalendar] 列出 {len(events)} 個事件")
                    return StepResult.complete_workflow(
                        f"您有 {len(events)} 個行事曆事件：\n" + "\n".join(event_list),
                        {"events": events}
                    )
            
            elif action == "search":
                if not search_query:
                    return StepResult.failure("搜尋需要提供關鍵字")
                
                # 使用 list 然後手動過濾（因為 local_calendar 沒有 search action）
                result = local_calendar(action="list")
                
                if result.get("status") == "ok":
                    all_events = result.get("events", [])
                    # 手動過濾
                    events = [
                        e for e in all_events
                        if search_query.lower() in e.get("summary", "").lower()
                        or search_query.lower() in e.get("description", "").lower()
                    ]
                    
                    if not events:
                        return StepResult.complete_workflow(f"找不到包含「{search_query}」的行事曆事件", {"events": []})
                    
                    # 格式化輸出（移除 emoji）
                    event_list = []
                    for event in events:
                        start_str = event.get("start_time", "")
                        end_str = event.get("end_time", "")
                        event_list.append(
                            f"[{event['id']}] {event['summary']}: {start_str} ~ {end_str}"
                        )
                    
                    info_log(f"[ManageCalendar] 搜尋「{search_query}」找到 {len(events)} 個結果")
                    return StepResult.complete_workflow(
                        f"找到 {len(events)} 個結果：\n" + "\n".join(event_list),
                        {"events": events}
                    )
            
            elif action == "update":
                if event_id is None:
                    return StepResult.failure("更新事件需要選擇事件")
                if not update_fields:
                    return StepResult.failure("更新事件需要提供更新欄位")
                
                result = local_calendar(
                    action="update",
                    event_id=event_id,
                    summary=update_fields.get("event_name", ""),
                    start_time=update_fields.get("start_time", ""),
                    end_time=update_fields.get("end_time", ""),
                    description=update_fields.get("description", ""),
                    location=update_fields.get("location", "")
                )
                
                if result.get("status") == "ok":
                    info_log(f"[ManageCalendar] 已更新事件 ID: {event_id}")
                    return StepResult.complete_workflow(
                        f"✅ 已更新事件 ID: {event_id}",
                        {"event_id": event_id, "update_fields": update_fields}
                    )
                else:
                    error_msg = result.get("message", "未知錯誤")
                    return StepResult.failure(f"更新失敗：{error_msg}")
            
            elif action == "delete":
                if event_id is None:
                    return StepResult.failure("刪除事件需要選擇事件")
                
                result = local_calendar(action="delete", event_id=event_id)
                
                if result.get("status") == "ok":
                    info_log(f"[ManageCalendar] 已刪除事件 ID: {event_id}")
                    return StepResult.complete_workflow(
                        f"🗑️ 已刪除事件 ID: {event_id}",
                        {"event_id": event_id}
                    )
                else:
                    error_msg = result.get("message", "未知錯誤")
                    return StepResult.failure(f"刪除失敗：{error_msg}")
            
            elif action == "find_free_time":
                # 簡單實現：列出所有事件，讓 LLM 分析空閒時段
                result = local_calendar(action="list")
                
                if result.get("status") == "ok":
                    events = result.get("events", [])
                    
                    # 按時間排序
                    events_sorted = sorted(events, key=lambda e: e.get("start_time", ""))
                    
                    # 格式化事件列表
                    event_list = []
                    for event in events_sorted:
                        start_str = event.get("start_time", "")
                        end_str = event.get("end_time", "")
                        event_list.append(f"{start_str} ~ {end_str}: {event['summary']}")
                    
                    info_log(f"[ManageCalendar] 查找空閒時段（已排序 {len(events)} 個事件）")
                    return StepResult.complete_workflow(
                        f"您的行程如下（共 {len(events)} 個事件）：\n" + "\n".join(event_list),
                        {"events": events_sorted}
                    )
                else:
                    return StepResult.complete_workflow(
                        "🕐 目前沒有行事曆事件，所有時間都是空閒的",
                        {"events": []}
                    )
            
            else:
                return StepResult.failure(f"不支援的操作：{action}")
        
        except Exception as e:
            error_log(f"[ManageCalendar] 執行失敗：{e}")
            return StepResult.failure(f"管理行事曆事件時發生錯誤：{str(e)}")
    
    # 創建執行步驟
    execute_step = StepTemplate.create_processing_step(
        session=session,
        step_id="execute_manage_calendar",
        processor=execute_manage_calendar,
        required_data=["action_selection"],
        description="執行行事曆事件管理操作"
    )
    
    # 組裝工作流
    workflow_def.add_step(action_selection_step)
    workflow_def.add_step(search_input_step)
    workflow_def.add_step(update_fields_input_step)
    workflow_def.add_step(action_conditional_step)
    workflow_def.add_step(execute_step)
    
    workflow_def.set_entry_point("action_selection")
    workflow_def.add_transition("action_selection", "action_conditional")
    # 🔧 分支步驟完成後需要回到 conditional 繼續執行
    workflow_def.add_transition("search_query_input", "action_conditional")
    workflow_def.add_transition("update_fields_input", "action_conditional")
    workflow_def.add_transition("action_conditional", "execute_manage_calendar")
    workflow_def.add_transition("execute_manage_calendar", "END")
    
    return workflow_def
