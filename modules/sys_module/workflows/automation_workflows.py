"""
modules/sys_module/workflows/automation_workflows.py
自動化背景工作流定義

包含：
- 媒體播放控制工作流（持續性服務）
- 生成備份腳本工作流（一次性任務）
- 提醒設置工作流（時間觸發）
- 資料夾監控工作流（事件觸發）
- 日曆事件管理工作流（CRUD + 時間觸發）
"""

from typing import Dict, Any, Optional
from datetime import datetime, timedelta

from core.sessions.session_manager import WorkflowSession
from modules.sys_module.workflows import (
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowMode,
    WorkflowStep,
    StepResult
)
from modules.sys_module.step_templates import (
    StepTemplate
)
from modules.sys_module.actions.automation_helper import (
    get_monitoring_pool,
    media_control,
    generate_backup_script
)
from utils.debug_helper import info_log, error_log, debug_log


# ==================== 媒體播放控制工作流 ====================

def create_media_player_check_function(session):
    """
    媒體播放器監控檢查函數
    
    檢查是否有新的控制指令並執行：
    - play / pause / stop
    - next / previous
    - search (搜尋歌曲)
    
    Args:
        session: 工作流程會話
        
    Returns:
        檢查結果字典 {triggered, message, data, should_stop}
    """
    try:
        # 從 session 獲取控制指令
        control_action = session.get_data("control_action")
        control_params = session.get_data("control_params", {})
        
        if control_action:
            # 執行控制指令
            music_folder = session.get_data("music_folder", "")
            youtube = control_params.get("youtube", False)
            spotify = control_params.get("spotify", False)
            song_query = control_params.get("song_query", "")
            
            result_message = media_control(
                action=control_action,
                song_query=song_query,
                music_folder=music_folder,
                youtube=youtube,
                spotify=spotify
            )
            
            info_log(f"[MediaPlayerMonitor] 執行控制指令：{control_action} -> {result_message}")
            
            # 清除已處理的指令
            session.set_data("control_action", None)
            session.set_data("control_params", {})
            
            # 發布控制完成事件
            from core.event_bus import event_bus, SystemEvent
            event_bus.publish(
                SystemEvent.MEDIA_CONTROL_EXECUTED,
                {
                    "task_id": session.metadata.get("task_id"),
                    "action": control_action,
                    "result": result_message
                },
                source="sys"
            )
            
            return {
                "triggered": True,
                "message": f"媒體控制：{control_action}",
                "data": {"action": control_action, "result": result_message},
                "should_stop": False  # 繼續監控
            }
        
        # 檢查是否需要停止監控
        should_stop = session.get_data("stop_monitor", False)
        if should_stop:
            return {
                "triggered": False,
                "message": "用戶停止媒體監控",
                "data": {},
                "should_stop": True
            }
        
        # 沒有新指令，繼續監控
        return {
            "triggered": False,
            "message": "等待控制指令",
            "data": {},
            "should_stop": False
        }
        
    except Exception as e:
        error_log(f"[MediaPlayerMonitor] 檢查控制指令失敗：{e}")
        return {
            "triggered": False,
            "message": f"檢查失敗：{str(e)}",
            "data": {},
            "should_stop": False
        }


class MediaPlayerInitStep(WorkflowStep):
    """媒體播放器初始化步驟"""
    
    def __init__(self, session: WorkflowSession):
        super().__init__(session)
        self.set_step_type(self.STEP_TYPE_PROCESSING)
        self.set_description("初始化媒體播放器並開始播放")
    
    def get_prompt(self) -> str:
        return "正在初始化媒體播放器..."
    
    def execute(self, user_input: Any = None) -> StepResult:
        """
        初始化媒體播放器並執行初始播放動作
        """
        try:
            # 獲取初始參數
            initial_action = self.session.get_data("initial_action", "play")
            song_query = self.session.get_data("song_query", "")
            music_folder = self.session.get_data("music_folder", "")
            youtube = self.session.get_data("youtube", False)
            spotify = self.session.get_data("spotify", False)
            
            # 執行初始播放
            result_message = media_control(
                action=initial_action,
                song_query=song_query,
                music_folder=music_folder,
                youtube=youtube,
                spotify=spotify
            )
            
            info_log(f"[MediaPlayerInit] 媒體播放器已初始化：{result_message}")
            
            return StepResult.success(
                f"媒體播放器已啟動：{result_message}",
                data={"initial_result": result_message}
            )
            
        except Exception as e:
            error_log(f"[MediaPlayerInit] 初始化失敗：{e}")
            return StepResult.failure(f"初始化媒體播放器失敗：{str(e)}")


def create_media_playback_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    建立媒體播放控制工作流
    
    這是一個「服務式」背景工作流：
    1. 初始化播放器並開始播放
    2. 進入持續監控狀態
    3. 等待並響應用戶控制指令（play, pause, stop, next, previous, search）
    4. 直到用戶手動停止監控
    
    Args:
        session: 工作流程會話，應包含以下數據：
            - initial_action: 初始動作（play, youtube, spotify）
            - song_query: 歌曲關鍵字（可選）
            - music_folder: 本地音樂資料夾（可選）
            - youtube: 是否使用 YouTube（可選）
            - spotify: 是否使用 Spotify（可選）
            
    Returns:
        WorkflowEngine 實例
    """
    workflow_def = WorkflowDefinition(
        workflow_type="media_playback",
        name="媒體播放控制",
        description="持續性媒體播放監控，支援播放控制指令",
        workflow_mode=WorkflowMode.BACKGROUND,
        requires_llm_review=False
    )
    
    # 步驟 1: 初始化播放器
    init_step = MediaPlayerInitStep(session)
    workflow_def.add_step(init_step)
    
    # 步驟 2: 持續監控（響應控制指令）
    # 使用工廠方法創建週期性檢查步驟
    monitor_step = StepTemplate.create_periodic_check_step(
        session=session,
        step_id="media_player_monitor",
        check_interval=5,
        check_function=lambda: create_media_player_check_function(session),
        description="媒體播放器監控（持續運行，等待控制指令）"
    )
    workflow_def.add_step(monitor_step)
    
    # 定義步驟轉換
    workflow_def.add_transition(init_step.id, monitor_step.id)
    
    # 設置入口點
    workflow_def.set_entry_point(init_step.id)
    
    return WorkflowEngine(workflow_def, session)


# ==================== 生成備份腳本工作流 ====================

class BackupScriptGenerationStep(WorkflowStep):
    """生成備份腳本步驟"""
    
    def __init__(self, session: WorkflowSession):
        super().__init__(session)
        self.set_step_type(self.STEP_TYPE_PROCESSING)
        self.set_description("生成系統備份腳本")
    
    def get_prompt(self) -> str:
        return "正在生成備份腳本..."
    
    def execute(self, user_input: Any = None) -> StepResult:
        """
        生成備份腳本
        
        這個功能目前是暫位符，未來會擴展為：
        - 根據 UEP 對用戶的認知生成個性化備份腳本
        - 包含用戶習慣、偏好設置、重要檔案路徑等資訊
        - 支援跨平台（Windows/Linux/macOS）
        """
        try:
            # 獲取參數
            target_folder = self.session.get_data("target_folder")
            dest_folder = self.session.get_data("dest_folder")
            output_path = self.session.get_data("output_path")
            
            if not all([target_folder, dest_folder, output_path]):
                return StepResult.failure("缺少必要參數：target_folder, dest_folder, output_path")
            
            # 生成備份腳本
            script_path = generate_backup_script(
                target_folder=target_folder,
                dest_folder=dest_folder,
                output_path=output_path
            )
            
            if script_path:
                info_log(f"[BackupScript] 備份腳本已生成：{script_path}")
                
                # TODO: 未來擴展 - 加入 UEP 個性化資訊
                # - 用戶偏好設置
                # - 重要檔案清單
                # - 排除規則
                # - 備份頻率建議
                
                return StepResult.complete_workflow(
                    f"備份腳本已生成：{script_path}",
                    data={
                        "script_path": script_path,
                        "target_folder": target_folder,
                        "dest_folder": dest_folder
                    }
                )
            else:
                return StepResult.failure("生成備份腳本失敗")
                
        except Exception as e:
            error_log(f"[BackupScript] 生成失敗：{e}")
            return StepResult.failure(f"生成備份腳本失敗：{str(e)}")


def create_backup_script_workflow(session: WorkflowSession) -> WorkflowEngine:
    """
    建立生成備份腳本工作流
    
    這是一個「一次性任務」工作流：
    1. 生成備份腳本（.bat / .sh）
    2. 完成
    
    未來擴展：
    - 整合 UEP 對用戶的認知（記憶系統）
    - 生成個性化備份策略
    - 包含用戶習慣分析
    
    Args:
        session: 工作流程會話，應包含以下數據：
            - target_folder: 要備份的資料夾路徑
            - dest_folder: 備份目標路徑
            - output_path: 腳本輸出路徑
            
    Returns:
        WorkflowEngine 實例
    """
    workflow_def = WorkflowDefinition(
        workflow_type="generate_backup_script",
        name="生成備份腳本",
        description="生成系統備份腳本（未來會包含 UEP 個性化認知）",
        workflow_mode=WorkflowMode.BACKGROUND,
        requires_llm_review=False
    )
    
    # 單一步驟：生成腳本
    generation_step = BackupScriptGenerationStep(session)
    workflow_def.add_step(generation_step)
    
    # 設置入口點
    workflow_def.set_entry_point(generation_step.id)
    
    return WorkflowEngine(workflow_def, session)


# ==================== 工作流註冊表 ====================

def get_automation_workflow_creator(workflow_type: str):
    """
    獲取自動化工作流建立函數
    
    Args:
        workflow_type: 工作流類型
        
    Returns:
        工作流建立函數，簽名為 func(session: WorkflowSession) -> WorkflowEngine
    """
    creators = {
        "media_playback": create_media_playback_workflow,
        "generate_backup_script": create_backup_script_workflow,
    }
    
    return creators.get(workflow_type)


# ==================== 媒體播放控制 API（供干預使用）====================

def send_media_control(task_id: str, action: str, **params) -> Dict[str, Any]:
    """
    發送媒體控制指令到正在運行的媒體播放工作流
    
    這個函數允許用戶在播放過程中發送控制指令：
    - play: 播放
    - pause: 暫停
    - stop: 停止
    - next: 下一首
    - previous: 上一首
    - search: 搜尋並播放（需要 song_query 參數）
    
    Args:
        task_id: 媒體播放工作流的 task_id
        action: 控制動作
        **params: 額外參數（如 song_query）
        
    Returns:
        操作結果
        
    Example:
        # 暫停播放
        send_media_control("workflow_media_playback_abc123", "pause")
         
        # 搜尋並播放
        send_media_control("workflow_media_playback_abc123", "search", song_query="周杰倫")
    """
    try:
        from core.sessions.session_manager import session_manager
        
        # 從 task_id 獲取 session
        # 獲取與該 task_id 相關的 session
        # 注意：這需要在 session 的 metadata 中存儲 task_id
        active_sessions = session_manager.get_active_workflow_sessions()
        target_session = None
        
        for session in active_sessions:
            if session and hasattr(session, 'metadata') and session.metadata.get("background_task_id") == task_id:
                target_session = session
                break
        
        # 如果在 workflow sessions 中找不到，也檢查 chatting sessions
        if not target_session:
            chatting_sessions = session_manager.get_active_chatting_sessions()
            for session in chatting_sessions:
                if session and hasattr(session, 'metadata') and session.metadata.get("background_task_id") == task_id:
                    target_session = session
                    break
        
        if not target_session:
            return {
                "status": "error",
                "message": f"找不到對應的 session (task_id: {task_id})"
            }
        
        # 設置控制指令到 session
        target_session.add_data("control_action", action)
        target_session.add_data("control_params", params)
        
        info_log(f"[MediaControl] 已發送控制指令：{action} (task_id: {task_id})")
        
        return {
            "status": "ok",
            "message": f"已發送 {action} 指令",
            "task_id": task_id,
            "action": action
        }
        
    except Exception as e:
        error_log(f"[MediaControl] 發送控制指令失敗：{e}")
        return {
            "status": "error",
            "message": str(e)
        }


def stop_media_playback(task_id: str) -> Dict[str, Any]:
    """
    停止媒體播放監控
    
    Args:
        task_id: 媒體播放工作流的 task_id
        
    Returns:
        操作結果
    """
    try:
        from core.sessions.session_manager import session_manager
        
        # 獲取所有活躍的 workflow sessions
        active_sessions = session_manager.get_active_workflow_sessions()
        target_session = None
        
        for session in active_sessions:
            if session and hasattr(session, 'metadata') and session.metadata.get("background_task_id") == task_id:
                target_session = session
                break
        
        # 如果在 workflow sessions 中找不到，也檢查 chatting sessions
        if not target_session:
            chatting_sessions = session_manager.get_active_chatting_sessions()
            for session in chatting_sessions:
                if session and hasattr(session, 'metadata') and session.metadata.get("background_task_id") == task_id:
                    target_session = session
                    break
        
        if not target_session:
            return {
                "status": "error",
                "message": f"找不到對應的 session (task_id: {task_id})"
            }
        
        # 設置停止標記
        target_session.add_data("stop_monitor", True)
        
        # 停止監控線程
        pool = get_monitoring_pool()
        pool.stop_monitor(task_id)
        
        info_log(f"[MediaControl] 已停止媒體播放監控 (task_id: {task_id})")
        
        return {
            "status": "ok",
            "message": "已停止媒體播放監控",
            "task_id": task_id
        }
        
    except Exception as e:
        error_log(f"[MediaControl] 停止監控失敗：{e}")
        return {
            "status": "error",
            "message": str(e)
        }
