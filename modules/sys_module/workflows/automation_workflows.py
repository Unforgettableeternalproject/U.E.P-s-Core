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
