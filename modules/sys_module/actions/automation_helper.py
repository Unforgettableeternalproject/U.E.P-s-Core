import sqlite3
import threading
import time
import os
import json
from datetime import datetime

# 導入事件系統
from modules.sys_module.actions.monitoring_events import (
    MonitoringEventType,
    publish_calendar_event,
    publish_todo_event
)
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable
from concurrent.futures import ThreadPoolExecutor
from utils.debug_helper import info_log, error_log, debug_log

# 將資料庫放在 memory 目錄中
_DB_DIR = Path(__file__).parent.parent.parent.parent / "memory"
_DB_DIR.mkdir(exist_ok=True)
_DB = str(_DB_DIR / "uep_tasks.db")

# ==================== 監控線程池管理器 ====================

class MonitoringThreadPool:
    """
    專門用於長期運行監控任務的線程池管理器
    
    與 BackgroundWorkerManager 的區別：
    - BackgroundWorkerManager: 一次性執行完畢的背景工作流（有限步驟）
    - MonitoringThreadPool: 持續運行的監控任務（無限循環直到觸發或取消）
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """單例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """初始化監控線程池"""
        if self._initialized:
            return
        
        self.executor = ThreadPoolExecutor(
            max_workers=10,
            thread_name_prefix="Monitor"
        )
        self.active_monitors: Dict[str, threading.Event] = {}  # task_id -> stop_event
        self.monitor_threads: Dict[str, Any] = {}  # task_id -> Future
        self._initialized = True
        
        info_log("[MonitoringThreadPool] 監控線程池已初始化（max_workers=10）")
    
    def submit_monitor(
        self,
        task_id: str,
        monitor_func: Callable,
        check_interval: int = 60,
        **kwargs
    ) -> bool:
        """
        提交新的監控任務到線程池
        
        Args:
            task_id: 監控任務 ID（唯一識別碼）
            monitor_func: 監控函數，簽名為 func(stop_event, **kwargs) -> None
            check_interval: 檢查間隔（秒）
            **kwargs: 傳遞給監控函數的額外參數
            
        Returns:
            是否成功提交
        """
        if task_id in self.active_monitors:
            error_log(f"[MonitoringThreadPool] 監控任務已存在：{task_id}")
            return False
        
        try:
            # 建立停止事件
            stop_event = threading.Event()
            self.active_monitors[task_id] = stop_event
            
            # 包裝監控函數
            def monitor_wrapper():
                try:
                    info_log(f"[MonitoringThreadPool] 監控任務已啟動：{task_id}")
                    
                    # 執行監控函數（會持續運行直到 stop_event 被設置）
                    monitor_func(stop_event=stop_event, check_interval=check_interval, **kwargs)
                    
                    info_log(f"[MonitoringThreadPool] 監控任務已正常結束：{task_id}")
                    
                except Exception as e:
                    error_log(f"[MonitoringThreadPool] 監控任務異常：{task_id}, 錯誤：{e}")
                    
                    # 更新資料庫狀態
                    update_workflow_status(
                        task_id=task_id,
                        status="FAILED",
                        error_message=str(e)
                    )
                
                finally:
                    # 清理
                    if task_id in self.active_monitors:
                        del self.active_monitors[task_id]
                    if task_id in self.monitor_threads:
                        del self.monitor_threads[task_id]
            
            # 提交到線程池
            future = self.executor.submit(monitor_wrapper)
            self.monitor_threads[task_id] = future
            
            info_log(f"[MonitoringThreadPool] 已提交監控任務：{task_id}（間隔 {check_interval} 秒）")
            return True
            
        except Exception as e:
            error_log(f"[MonitoringThreadPool] 提交監控任務失敗：{task_id}, 錯誤：{e}")
            
            # 清理
            if task_id in self.active_monitors:
                del self.active_monitors[task_id]
            
            return False
    
    def stop_monitor(self, task_id: str, timeout: int = 10) -> bool:
        """
        停止指定的監控任務
        
        Args:
            task_id: 監控任務 ID
            timeout: 等待停止的超時時間（秒）
            
        Returns:
            是否成功停止
        """
        if task_id not in self.active_monitors:
            debug_log(2, f"[MonitoringThreadPool] 監控任務不存在或已停止：{task_id}")
            return False
        
        try:
            # 設置停止事件
            stop_event = self.active_monitors[task_id]
            stop_event.set()
            
            info_log(f"[MonitoringThreadPool] 已發送停止信號：{task_id}")
            
            # 等待線程結束（可選）
            if task_id in self.monitor_threads:
                future = self.monitor_threads[task_id]
                try:
                    future.result(timeout=timeout)
                    info_log(f"[MonitoringThreadPool] 監控任務已停止：{task_id}")
                except TimeoutError:
                    error_log(f"[MonitoringThreadPool] 停止監控任務超時：{task_id}")
                    return False
            
            # 更新資料庫狀態
            update_workflow_status(
                task_id=task_id,
                status="CANCELLED",
                error_message="用戶取消"
            )
            
            return True
            
        except Exception as e:
            error_log(f"[MonitoringThreadPool] 停止監控任務失敗：{task_id}, 錯誤：{e}")
            return False
    
    def stop_all_monitors(self, timeout: int = 10) -> None:
        """停止所有監控任務"""
        info_log(f"[MonitoringThreadPool] 正在停止所有監控任務（共 {len(self.active_monitors)} 個）")
        
        task_ids = list(self.active_monitors.keys())
        for task_id in task_ids:
            self.stop_monitor(task_id, timeout=timeout)
    
    def get_active_monitors(self) -> List[str]:
        """獲取所有活躍的監控任務 ID"""
        return list(self.active_monitors.keys())
    
    def is_monitor_running(self, task_id: str) -> bool:
        """檢查指定監控任務是否正在運行"""
        return task_id in self.active_monitors
    
    def get_monitor_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        獲取指定監控任務的詳細狀態（供前端使用）
        
        Args:
            task_id: 監控任務 ID
            
        Returns:
            監控任務狀態字典，包含：
            - task_id: 任務 ID
            - workflow_type: 工作流類型
            - status: 運行狀態
            - is_running: 是否正在線程池中運行
            - created_at: 建立時間
            - last_check_at: 最後檢查時間
            - next_check_at: 下次檢查時間
            - trigger_conditions: 觸發條件
            - metadata: 元數據
            - uptime_seconds: 運行時長（秒）
        """
        # 從資料庫獲取任務資訊
        workflow = get_workflow_by_id(task_id)
        if not workflow:
            return None
        
        # 計算運行時長
        uptime_seconds = None
        if workflow.get("created_at"):
            created_time = datetime.fromisoformat(workflow["created_at"])
            uptime_seconds = (datetime.now() - created_time).total_seconds()
        
        return {
            "task_id": task_id,
            "workflow_type": workflow["workflow_type"],
            "status": workflow["status"],
            "is_running": self.is_monitor_running(task_id),
            "created_at": workflow["created_at"],
            "last_check_at": workflow.get("last_check_at"),
            "next_check_at": workflow.get("next_check_at"),
            "trigger_conditions": workflow.get("trigger_conditions"),
            "metadata": workflow.get("metadata"),
            "uptime_seconds": uptime_seconds,
            "error_message": workflow.get("error_message")
        }
    
    def get_all_monitor_status(self) -> List[Dict[str, Any]]:
        """
        獲取所有監控任務的狀態（供前端監控視窗使用）
        
        Returns:
            所有監控任務的狀態列表
        """
        all_status = []
        
        # 獲取資料庫中所有活躍的工作流
        active_workflows = get_active_workflows()
        
        for workflow in active_workflows:
            task_id = workflow["task_id"]
            status = self.get_monitor_status(task_id)
            if status:
                all_status.append(status)
        
        return all_status
    
    def prepare_shutdown(self) -> Dict[str, Any]:
        """
        準備系統關機：優雅地停止所有監控任務並保存狀態
        
        這個方法會：
        1. 將所有運行中的監控標記為 SUSPENDED（暫停）
        2. 發送停止信號給所有線程
        3. 保存當前狀態到資料庫
        4. 等待線程優雅退出
        
        Returns:
            關機報告：
            - suspended_count: 暫停的任務數量
            - failed_count: 停止失敗的任務數量
            - suspended_tasks: 暫停的任務 ID 列表
        """
        info_log("[MonitoringThreadPool] 準備系統關機，正在暫停所有監控任務...")
        
        suspended_tasks = []
        failed_tasks = []
        
        # 獲取所有正在運行的監控任務
        running_task_ids = list(self.active_monitors.keys())
        
        for task_id in running_task_ids:
            try:
                # 更新資料庫狀態為 SUSPENDED
                success = update_workflow_status(
                    task_id=task_id,
                    status="SUSPENDED",
                    error_message="系統關機，任務已暫停"
                )
                
                if success:
                    # 發送停止信號
                    stop_event = self.active_monitors[task_id]
                    stop_event.set()
                    suspended_tasks.append(task_id)
                    info_log(f"[MonitoringThreadPool] 已暫停監控任務：{task_id}")
                else:
                    failed_tasks.append(task_id)
                    error_log(f"[MonitoringThreadPool] 暫停監控任務失敗：{task_id}")
                    
            except Exception as e:
                error_log(f"[MonitoringThreadPool] 暫停監控任務異常：{task_id}, 錯誤：{e}")
                failed_tasks.append(task_id)
        
        # 等待所有線程退出（較短超時）
        info_log("[MonitoringThreadPool] 等待監控線程退出...")
        for task_id, future in list(self.monitor_threads.items()):
            try:
                future.result(timeout=5)  # 5 秒超時
            except TimeoutError:
                error_log(f"[MonitoringThreadPool] 監控任務停止超時：{task_id}")
            except Exception as e:
                error_log(f"[MonitoringThreadPool] 監控任務停止異常：{task_id}, 錯誤：{e}")
        
        shutdown_report = {
            "suspended_count": len(suspended_tasks),
            "failed_count": len(failed_tasks),
            "suspended_tasks": suspended_tasks,
            "failed_tasks": failed_tasks
        }
        
        info_log(f"[MonitoringThreadPool] 關機準備完成：已暫停 {len(suspended_tasks)} 個任務")
        return shutdown_report
    
    def restore_monitors(self, monitor_factory: Callable) -> Dict[str, Any]:
        """
        系統啟動時恢復暫停的監控任務
        
        這個方法會：
        1. 從資料庫查詢所有 SUSPENDED 狀態的任務
        2. 使用 monitor_factory 重新建立監控函數
        3. 重新提交到線程池
        4. 更新狀態為 RUNNING
        
        Args:
            monitor_factory: 監控函數工廠，簽名為 factory(workflow_type, metadata) -> monitor_func
                            用於根據工作流類型和元數據重新建立監控函數
        
        Returns:
            恢復報告：
            - restored_count: 恢復的任務數量
            - failed_count: 恢復失敗的任務數量
            - restored_tasks: 恢復的任務 ID 列表
        """
        info_log("[MonitoringThreadPool] 正在恢復暫停的監控任務...")
        
        restored_tasks = []
        failed_tasks = []
        
        try:
            # 從資料庫查詢所有 SUSPENDED 狀態的工作流
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            c.execute("""
                SELECT task_id, workflow_type, trigger_conditions, metadata, next_check_at
                FROM background_workflows
                WHERE status = 'SUSPENDED'
                ORDER BY created_at ASC
            """)
            
            suspended_workflows = []
            for row in c.fetchall():
                suspended_workflows.append({
                    "task_id": row[0],
                    "workflow_type": row[1],
                    "trigger_conditions": json.loads(row[2]) if row[2] else None,
                    "metadata": json.loads(row[3]) if row[3] else None,
                    "next_check_at": row[4]
                })
            
            conn.close()
            
            info_log(f"[MonitoringThreadPool] 找到 {len(suspended_workflows)} 個暫停的監控任務")
            
            # 恢復每個任務
            for workflow in suspended_workflows:
                task_id = workflow["task_id"]
                workflow_type = workflow["workflow_type"]
                metadata = workflow["metadata"] or {}
                
                try:
                    # 使用工廠函數重新建立監控函數
                    monitor_func = monitor_factory(workflow_type, metadata)
                    
                    if monitor_func is None:
                        error_log(f"[MonitoringThreadPool] 無法建立監控函數：{workflow_type}")
                        failed_tasks.append(task_id)
                        continue
                    
                    # 計算檢查間隔
                    check_interval = metadata.get("check_interval", 60)
                    
                    # 重新提交到線程池
                    success = self.submit_monitor(
                        task_id=task_id,
                        monitor_func=monitor_func,
                        check_interval=check_interval,
                        **metadata
                    )
                    
                    if success:
                        # 更新狀態為 RUNNING
                        update_workflow_status(
                            task_id=task_id,
                            status="RUNNING",
                            error_message=None
                        )
                        restored_tasks.append(task_id)
                        info_log(f"[MonitoringThreadPool] 已恢復監控任務：{task_id}")
                    else:
                        failed_tasks.append(task_id)
                        error_log(f"[MonitoringThreadPool] 恢復監控任務失敗：{task_id}")
                        
                except Exception as e:
                    error_log(f"[MonitoringThreadPool] 恢復監控任務異常：{task_id}, 錯誤：{e}")
                    failed_tasks.append(task_id)
            
            restore_report = {
                "restored_count": len(restored_tasks),
                "failed_count": len(failed_tasks),
                "restored_tasks": restored_tasks,
                "failed_tasks": failed_tasks
            }
            
            info_log(f"[MonitoringThreadPool] 監控任務恢復完成：成功 {len(restored_tasks)} 個，失敗 {len(failed_tasks)} 個")
            return restore_report
            
        except Exception as e:
            error_log(f"[MonitoringThreadPool] 恢復監控任務過程異常：{e}")
            return {
                "restored_count": len(restored_tasks),
                "failed_count": len(failed_tasks) + 1,
                "restored_tasks": restored_tasks,
                "failed_tasks": failed_tasks,
                "error": str(e)
            }
    
    def shutdown(self, wait: bool = True, timeout: int = 30) -> None:
        """
        關閉監控線程池
        
        Args:
            wait: 是否等待所有任務完成
            timeout: 等待超時時間（秒）
        """
        info_log("[MonitoringThreadPool] 正在關閉監控線程池...")
        
        # 停止所有監控任務
        self.stop_all_monitors(timeout=timeout)
        
        # 關閉線程池並等待所有任務完成
        try:
            self.executor.shutdown(wait=wait)
            if wait:
                # 確認所有期貨都已完成
                remaining_futures = [f for f in self.monitor_threads.values() if f and not f.done()]
                if remaining_futures:
                    debug_log(1, f"[MonitoringThreadPool] 等待 {len(remaining_futures)} 個未完成的任務...")
                    for future in remaining_futures:
                        try:
                            future.result(timeout=1.0)
                        except Exception as e:
                            debug_log(1, f"[MonitoringThreadPool] 任務完成失敗: {e}")
            info_log("[MonitoringThreadPool] 監控線程池已關閉")
        except Exception as e:
            error_log(f"[MonitoringThreadPool] 關閉線程池失敗: {e}")


# 全域監控線程池實例
_monitoring_pool = None
_monitoring_pool_lock = threading.Lock()


def get_monitoring_pool() -> MonitoringThreadPool:
    """獲取全域監控線程池實例（單例）"""
    global _monitoring_pool
    if _monitoring_pool is None:
        with _monitoring_pool_lock:
            if _monitoring_pool is None:
                _monitoring_pool = MonitoringThreadPool()
    return _monitoring_pool

def _init_db():
    conn = sqlite3.connect(_DB)
    c = conn.cursor()
    
    # 提醒表
    c.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
      id INTEGER PRIMARY KEY,
      time TEXT NOT NULL,
      message TEXT NOT NULL
    )""")
    
    # 日曆事件表
    c.execute("""
    CREATE TABLE IF NOT EXISTS calendar_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      summary TEXT NOT NULL,
      description TEXT,
      start_time TEXT NOT NULL,
      end_time TEXT NOT NULL,
      location TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      last_notified_at TEXT,
      last_notified_stage TEXT
    )""")
    
    # 待辦事項表
    c.execute("""
    CREATE TABLE IF NOT EXISTS todos (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_name TEXT NOT NULL,
      task_description TEXT,
      priority TEXT NOT NULL DEFAULT 'none',
      status TEXT NOT NULL DEFAULT 'pending',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      deadline TEXT,
      completed_at TEXT,
      last_notified_at TEXT,
      last_notified_stage TEXT
    )""")
    
    # 背景工作流追蹤表
    c.execute("""
    CREATE TABLE IF NOT EXISTS background_workflows (
      task_id TEXT PRIMARY KEY,
      workflow_type TEXT NOT NULL,
      trigger_conditions TEXT,
      status TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      last_check_at TEXT,
      next_check_at TEXT,
      metadata TEXT,
      error_message TEXT
    )""")
    
    # 工作流干預審計表
    c.execute("""
    CREATE TABLE IF NOT EXISTS workflow_interventions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id TEXT NOT NULL,
      action TEXT NOT NULL,
      parameters TEXT,
      performed_at TEXT NOT NULL,
      performed_by TEXT,
      result TEXT,
      FOREIGN KEY (task_id) REFERENCES background_workflows(task_id)
    )""")
    
    # 為常用查詢建立索引
    c.execute("CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_todos_priority ON todos(priority)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_todos_deadline ON todos(deadline)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bg_workflows_status ON background_workflows(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bg_workflows_type ON background_workflows(workflow_type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bg_workflows_next_check ON background_workflows(next_check_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_interventions_task ON workflow_interventions(task_id)")
    
    conn.commit()
    
    # 添加缺失的欄位（如果不存在）
    try:
        c.execute("ALTER TABLE calendar_events ADD COLUMN last_notified_at TEXT")
        c.execute("ALTER TABLE calendar_events ADD COLUMN last_notified_stage TEXT")
        conn.commit()
        info_log("已添加 calendar_events 通知追蹤欄位")
    except sqlite3.OperationalError:
        pass  # 欄位已存在
    
    try:
        c.execute("ALTER TABLE todos ADD COLUMN last_notified_at TEXT")
        c.execute("ALTER TABLE todos ADD COLUMN last_notified_stage TEXT")
        conn.commit()
        info_log("已添加 todos 通知追蹤欄位")
    except sqlite3.OperationalError:
        pass  # 欄位已存在
    
    conn.close()

_init_db()

def set_reminder(dt, message: str):
    """新增提醒
    
    Args:
        dt: datetime 物件或 ISO 格式字串
        message: 提醒訊息
    """
    try:
        # 如果傳入的是字串，轉換為 datetime
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        
        conn = sqlite3.connect(_DB)
        conn.execute("INSERT INTO reminders (time, message) VALUES (?, ?)",
                     (dt.isoformat(), message))
        conn.commit()
        conn.close()
        info_log(f"[AUTO] 設定提醒：{dt} -> {message}")
    except Exception as e:
        error_log(f"[AUTO] 設定提醒失敗: {e}")

def _checker_loop():
    while True:
        now = datetime.utcnow().isoformat()
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        for row in c.execute("SELECT id, time, message FROM reminders WHERE time<=?", (now,)):
            _, t, msg = row
            info_log(f"[AUTO] 提醒觸發：{msg}")
            c.execute("DELETE FROM reminders WHERE id=?", (row[0],))
        conn.commit()
        conn.close()
        time.sleep(30)

# 啟動背景檢查
threading.Thread(target=_checker_loop, daemon=True).start()

def generate_backup_script(target_folder: str, dest_folder: str, output_path: str):
    """產生備份腳本 (.bat / .sh)"""
    try:
        if os.name == "nt":
            content = f'xcopy /E /I "{target_folder}" "{dest_folder}"'
            suffix = ".bat"
        else:
            content = f'#!/bin/bash\nrsync -a "{target_folder}/" "{dest_folder}/"'
            suffix = ".sh"
        p = os.path.splitext(output_path)[0] + suffix
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        info_log(f"[AUTO] 備份腳本已生成：{p}")
        return p
    except Exception as e:
        error_log(f"[AUTO] 生成腳本失敗: {e}")
        return None

def monitor_folder(path: str, callback, interval: int = 10):
    """監控資料夾變更"""
    def _loop():
        prev = set(os.listdir(path))
        while True:
            curr = set(os.listdir(path))
            added = curr - prev
            if added:
                for f in added:
                    info_log(f"[AUTO] 資料夾新增：{f}")
                    callback(f)
            prev = curr
            time.sleep(interval)
    threading.Thread(target=_loop, daemon=True).start()


# ==================== 媒體控制（背景任務）====================

# 全域音樂播放器實例
_music_player = None

def get_music_player_status() -> Dict[str, Any]:
    """
    獲取音樂播放器當前狀態
    
    Returns:
        包含播放器狀態的字典：
        - is_playing: 是否正在播放
        - is_finished: 是否播放完成
        - is_looping: 是否單曲循環
        - is_shuffled: 是否隨機播放
        - current_song: 當前歌曲名稱
    """
    global _music_player
    
    if _music_player is None:
        return {
            "is_playing": False,
            "is_paused": False,
            "is_finished": False,
            "is_looping": False,  # 向後兼容
            "loop_one": False,
            "loop_all": False,
            "is_shuffled": False,
            "current_song": None,
            "position_ms": 0,
            "duration_ms": 0
        }
    
    status = _music_player.get_status()
    return {
        "is_playing": status['is_playing'],
        "is_paused": status['is_paused'],
        "is_finished": _music_player.is_finished,
        "is_looping": _music_player.is_looping,  # 向後兼容
        "loop_one": _music_player.loop_one,
        "loop_all": _music_player.loop_all,
        "is_shuffled": _music_player.is_shuffled,
        "current_song": _music_player.current_song or "Unknown",
        "total_songs": len(_music_player.playlist),
        "position_ms": status['position_ms'],
        "duration_ms": status['duration_ms'],
        "engine": status['engine'],
        "capabilities": status['capabilities']
    }


def media_control(
    action: str,
    song_query: str = "",
    music_folder: str = "",
    youtube: bool = False,
    spotify: bool = False,
    shuffle: bool = False,
    loop: bool = False,
    loop_mode: str = "",
    volume: int = None,
    seek_position: int = None,
    engine_type: str = "auto"
) -> str:
    """
    音樂播放控制器 - 支援本地音樂、YouTube、Spotify（背景運行）
    
    Args:
        action: 動作指令 (play, pause, stop, next, previous, search, shuffle, loop, set_loop_mode, youtube, spotify)
        song_query: 歌曲查詢關鍵字
        music_folder: 本地音樂資料夾路徑
        youtube: 是否使用 YouTube 播放
        spotify: 是否使用 Spotify 播放
        shuffle: 是否開啟隨機播放（僅在 action="play" 時使用）
        loop: 是否開啟循環播放（僅在 action="play" 時使用，向後兼容）
        loop_mode: 循環模式 ('off', 'one', 'all')，優先於 loop 參數
        
    Returns:
        操作結果訊息
        
    Actions:
        - play: 播放歌曲或整個資料夾
        - pause: 暫停播放
        - stop: 停止播放
        - next: 下一首
        - previous: 上一首
        - search: 搜尋歌曲
        - shuffle: 切換隨機播放模式
        - loop: 切換循環模式 (off → loop_one → loop_all → off)
        - set_loop_mode: 直接設定循環模式（需配合 loop_mode 參數）
    """
    global _music_player
    
    try:
        if action == "youtube" or youtube:
            return _play_youtube(song_query)
        elif action == "spotify" or spotify:
            return _play_spotify(song_query)
        else:
            # 本地播放
            if not music_folder:
                music_folder = str(Path.home() / "Music")
            
            # 確保單例：如果播放器不存在或資料夾變更，則重新初始化
            if _music_player is None:
                _music_player = MusicPlayer(music_folder, engine_type)
                info_log("[AUTO] 音樂播放器已初始化")
            elif str(_music_player.music_folder) != music_folder:
                # 資料夾變更：停止當前播放，重新初始化
                info_log(f"[AUTO] 音樂資料夾變更: {_music_player.music_folder} -> {music_folder}")
                _music_player.stop()
                _music_player = MusicPlayer(music_folder, engine_type)
                info_log("[AUTO] 音樂播放器已重新初始化")
            
            if action == "play":
                # 應用 shuffle 和 loop 設定
                if shuffle:
                    _music_player.set_shuffle(True)
                
                # 優先使用 loop_mode，若未指定則使用舊的 loop 參數（向後兼容）
                if loop_mode:
                    _music_player.set_loop_mode(loop_mode)
                elif loop:
                    _music_player.set_loop(True)
                
                if song_query:
                    # 搜尋並播放
                    found = _music_player.search_and_play(song_query)
                    if found:
                        return f"正在播放：{song_query}"
                    else:
                        return f"找不到歌曲：{song_query}"
                else:
                    # 繼續播放
                    _music_player.play()
                    return "繼續播放"
            
            elif action == "pause":
                _music_player.pause()
                return "已暫停"
            
            elif action == "stop":
                _music_player.stop()
                return "已停止"
            
            elif action == "next":
                _music_player.next_song()
                return "下一首"
            
            elif action == "previous":
                _music_player.previous_song()
                return "上一首"
            
            elif action == "search":
                results = _music_player.search_song(song_query)
                if results:
                    return f"找到 {len(results)} 首歌曲：" + ", ".join(results[:5])
                else:
                    return f"找不到：{song_query}"
            
            elif action == "shuffle":
                _music_player.toggle_shuffle()
                return f"隨機播放：{'開啟' if _music_player.is_shuffled else '關閉'}"
            
            elif action == "loop":
                # 使用 loop_mode 參數設定循環模式
                if loop_mode:
                    _music_player.set_loop_mode(loop_mode)
                    if loop_mode == "one":
                        return "單曲循環：開啟"
                    elif loop_mode == "all":
                        return "播放清單循環：開啟"
                    else:
                        return "循環播放：關閉"
                else:
                    # 向後相容：如果沒有 loop_mode，使用 toggle
                    _music_player.toggle_loop()
                    return f"循環播放：{'開啟' if _music_player.is_looping else '關閉'}"
            
            elif action == "set_loop_mode":
                # 直接設定循環模式（用於智能推斷）
                if not loop_mode:
                    return "錯誤：缺少 loop_mode 參數"
                _music_player.set_loop_mode(loop_mode)
                
                # 返回對應的狀態訊息
                if loop_mode == "one":
                    return "單曲循環：開啟"
                elif loop_mode == "all":
                    return "播放清單循環：開啟"
                else:
                    return "循環播放：關閉"
            
            elif action == "volume":
                # 設定音量
                if volume is None:
                    return f"當前音量：{_music_player.volume}%"
                
                if not 0 <= volume <= 100:
                    return "錯誤：音量必須在 0-100 之間"
                
                _music_player.set_volume(volume)
                return f"音量已設定為 {volume}%"
            
            elif action == "seek":
                # 跳轉到指定位置
                if seek_position is None:
                    return "錯誤：缺少 seek_position 參數"
                
                if not hasattr(_music_player.engine, 'seek') or not _music_player.engine.get_capabilities().get('seek', False):
                    return "錯誤：當前引擎不支援 seek 功能"
                
                success = _music_player.engine.seek(seek_position)
                if success:
                    minutes = seek_position // 60000
                    seconds = (seek_position % 60000) // 1000
                    return {"status": "success", "message": f"已跳轉至 {minutes}:{seconds:02d}"}
                else:
                    return {"status": "error", "message": "跳轉失敗"}
            
            elif action == "status":
                # 獲取當前播放狀態
                status = _music_player.get_status()
                # 避免在 f-string 表達式中使用反斜線轉義，先計算文字
                play_state_text = "播放中" if status.get('is_playing') else "暫停"
                current_song_text = status.get('current_song') or "無"
                playlist_text = f"{status.get('current_index', 0) + 1}/{status.get('playlist_length', 0)}"
                volume_text = f"{status.get('volume', 0)}%"
                shuffle_text = "開" if status.get('is_shuffled') else "關"
                loop_text = status.get('loop_mode')
                return (
                    f"播放狀態：{play_state_text}\n"
                    f"當前歌曲：{current_song_text}\n"
                    f"播放清單：{playlist_text}\n"
                    f"音量：{volume_text}\n"
                    f"隨機：{shuffle_text}\n"
                    f"循環：{loop_text}"
                )
            
            else:
                return f"未知指令：{action}"
    
    except Exception as e:
        error_log(f"[AUTO] 媒體控制失敗: {e}")
        return f"錯誤：{str(e)}"


def _play_youtube(query: str) -> str:
    """使用 pywhatkit 播放 YouTube"""
    try:
        import pywhatkit
        info_log(f"[AUTO] 正在 YouTube 上播放：{query}")
        pywhatkit.playonyt(query)
        return f"已在 YouTube 上播放：{query}"
    except ImportError:
        error_log("[AUTO] pywhatkit 未安裝，請執行：pip install pywhatkit")
        return "錯誤：需要安裝 pywhatkit"
    except Exception as e:
        error_log(f"[AUTO] YouTube 播放失敗：{e}")
        return f"YouTube 播放失敗：{str(e)}"


def _play_spotify(query: str) -> str:
    """開啟 Spotify 搜尋（使用瀏覽器）"""
    try:
        import webbrowser
        search_url = f"https://open.spotify.com/search/{query.replace(' ', '%20')}"
        webbrowser.open(search_url)
        info_log(f"[AUTO] 已在 Spotify 上搜尋：{query}")
        return f"已在 Spotify 上搜尋：{query}"
    except Exception as e:
        error_log(f"[AUTO] Spotify 搜尋失敗：{e}")
        return f"Spotify 搜尋失敗：{str(e)}"


class MusicPlayer:
    """本地音樂播放器（背景運行，無 UI）- 支援 VLC 和 pydub 雙引擎"""
    
    def __init__(self, music_folder: str, engine_type: str = "auto"):
        self.music_folder = Path(music_folder)
        self.playlist = []
        self.original_playlist = []  # 保存原始播放順序
        self.current_index = 0
        self.loop_one = False  # 單曲循環
        self.loop_all = False  # 播放清單循環
        self.is_shuffled = False
        self.is_finished = False  # ✅ 初始化完成標記
        self.current_song = None
        self.volume = 70  # 預設音量 70%
        
        # 並發保護：避免背景播放與控制同時操作導致競態
        import threading
        self._lock = threading.Lock()
        
        # 初始化播放引擎
        from modules.sys_module.actions.music_engines import create_music_engine
        self.engine = create_music_engine(engine_type)
        info_log(f"[AUTO] 使用播放引擎: {self.engine.get_engine_name()}")
        
        # 載入播放清單
        self._load_playlist()
    
    @property
    def is_looping(self) -> bool:
        """向後兼容：返回是否有任何循環模式"""
        return self.loop_one or self.loop_all
        
    def _load_playlist(self):
        """載入音樂資料夾中的歌曲"""
        if not self.music_folder.exists():
            error_log(f"[AUTO] 音樂資料夾不存在：{self.music_folder}")
            return
        
        # 支援的音樂格式
        audio_formats = ['.mp3', '.wav', '.flac', '.ogg', '.m4a']
        
        for file in self.music_folder.rglob('*'):
            if file.suffix.lower() in audio_formats:
                self.playlist.append(str(file))
        
        # 保存原始順序
        self.original_playlist = self.playlist.copy()
        
        info_log(f"[AUTO] 載入 {len(self.playlist)} 首歌曲")
    
    def search_song(self, query: str) -> list:
        """
        搜尋歌曲（使用模糊比對）
        
        改進策略：
        1. 優先完全匹配（不區分大小寫）
        2. 使用 token_set_ratio 提升部分匹配準確度
        3. 降低相似度閾值到 50%，擴大搜尋範圍
        4. 按相似度排序結果
        """
        try:
            from rapidfuzz import process, fuzz
            
            # 從檔名中提取歌曲名稱
            song_names = [Path(song).stem for song in self.playlist]
            query_lower = query.lower()
            
            # 策略 1: 優先檢查完全匹配（不區分大小寫）
            exact_matches = []
            for name in song_names:
                if query_lower in name.lower():
                    exact_matches.append(name)
            
            # 如果有完全匹配，優先返回（最多 10 個）
            if exact_matches:
                debug_log(3, f"[MusicSearch] 完全匹配找到 {len(exact_matches)} 首")
                return exact_matches[:10]
            
            # 策略 2: 使用 token_set_ratio 進行模糊搜尋
            # token_set_ratio 會將字串分割成 token，更適合處理包含括號、符號的歌曲名
            results = process.extract(
                query, 
                song_names, 
                scorer=fuzz.token_set_ratio,  # 改用 token_set_ratio
                limit=15  # 增加候選數量
            )
            
            # 降低閾值到 50%，並按相似度排序
            matched = [(r[0], r[1]) for r in results if r[1] > 50]
            
            # 按相似度從高到低排序，只返回歌曲名
            matched.sort(key=lambda x: x[1], reverse=True)
            matched_names = [m[0] for m in matched[:10]]  # 最多返回 10 首
            
            debug_log(3, f"[MusicSearch] 模糊搜尋找到 {len(matched_names)} 首 (閾值 > 50%)")
            return matched_names
        
        except ImportError:
            info_log("[AUTO] rapidfuzz 未安裝，使用簡單搜尋")
            # 簡單字串比對（fallback）
            matched = []
            query_lower = query.lower()
            for song in self.playlist:
                song_name = Path(song).stem.lower()
                if query_lower in song_name:
                    matched.append(Path(song).stem)
            return matched[:10]  # 限制最多 10 個結果
    
    def search_and_play(self, query: str) -> bool:
        """搜尋並播放歌曲"""
        results = self.search_song(query)
        if results:
            # 找到第一首符合的歌曲
            for i, song in enumerate(self.playlist):
                if Path(song).stem == results[0]:
                    self.current_index = i
                    self.play()
                    return True
        return False
    
    def play(self):
        """播放當前歌曲"""
        if not self.playlist:
            error_log("[AUTO] 播放清單為空")
            return
        
        # 檢查是否從暫停恢復
        if self.engine.is_paused():
            # VLC 支援真正的恢復，pydub 需要重新播放
            capabilities = self.engine.get_capabilities()
            if capabilities['true_pause']:
                self.engine.resume()
                info_log("[AUTO] 從暫停位置恢復播放")
                return
        
        # 播放新歌曲或重新播放
        if self.current_index >= len(self.playlist):
            self.current_index = 0
        
        song_path = self.playlist[self.current_index]
        self.current_song = Path(song_path).stem
        
        # 使用引擎播放，傳入播放完成回調
        def on_finished():
            """播放完成回調 - 使用 Timer 延遲執行避免線程衝突"""
            def _deferred_action():
                if self.loop_one:
                    # 單曲循環
                    info_log(f"[AUTO] 單曲循環：重新播放 {self.current_song}")
                    self.play()
                elif self.loop_all:
                    # 播放清單循環
                    self.current_index = (self.current_index + 1) % len(self.playlist)
                    info_log(f"[AUTO] 播放清單循環：下一首")
                    self.play()
                else:
                    # 普通播放：自動播放下一首，直到清單結束
                    if self.current_index + 1 < len(self.playlist):
                        self.current_index += 1
                        info_log(f"[AUTO] 自動播放下一首：{Path(self.playlist[self.current_index]).stem}")
                        self.play()
                    else:
                        # 清單播放完畢
                        self.is_finished = True
                        info_log(f"[AUTO] 播放清單已完成")
            
            # 使用 Timer 延遲 10ms 執行，避免直接從回調線程調用 play()
            timer = threading.Timer(0.01, _deferred_action)
            timer.daemon = True
            timer.start()
        
        self.engine.play(song_path, self.volume, on_finished)
        self.is_finished = False
        info_log(f"[AUTO] 正在播放：{self.current_song}")
    
    def pause(self):
        """暫停播放"""
        self.engine.pause()
    
    def stop(self):
        """停止播放"""
        self.engine.stop()
        self.current_index = 0
        self.current_song = None
        info_log("[AUTO] 已停止")
    
    def next_song(self):
        """下一首"""
        self.engine.stop()
        self.current_index = (self.current_index + 1) % len(self.playlist)
        self.is_finished = False
        info_log(f"[AUTO] 切換到下一首 (索引: {self.current_index}/{len(self.playlist)})")
        self.play()
    
    def previous_song(self):
        """上一首"""
        self.engine.stop()
        self.current_index = (self.current_index - 1) % len(self.playlist)
        self.is_finished = False
        info_log(f"[AUTO] 切換到上一首 (索引: {self.current_index}/{len(self.playlist)})")
        self.play()
    
    def toggle_loop(self):
        """切換循環模式：off -> loop_one -> loop_all -> off"""
        if not self.loop_one and not self.loop_all:
            # off -> loop_one
            self.loop_one = True
            self.loop_all = False
            info_log(f"[AUTO] 單曲循環：開啟")
        elif self.loop_one:
            # loop_one -> loop_all
            self.loop_one = False
            self.loop_all = True
            info_log(f"[AUTO] 播放清單循環：開啟")
        else:
            # loop_all -> off
            self.loop_one = False
            self.loop_all = False
            info_log(f"[AUTO] 循環播放：關閉")
    
    def set_loop(self, enabled: bool):
        """設定循環播放狀態（向後兼容：設定單曲循環）"""
        self.loop_one = enabled
        self.loop_all = False
        info_log(f"[AUTO] 單曲循環：{'開啟' if enabled else '關閉'}")
    
    def set_loop_mode(self, mode: str):
        """設定循環模式：'off', 'one', 'all'"""
        if mode == "off":
            self.loop_one = False
            self.loop_all = False
            info_log(f"[AUTO] 循環播放：關閉")
        elif mode == "one":
            self.loop_one = True
            self.loop_all = False
            info_log(f"[AUTO] 單曲循環：開啟")
        elif mode == "all":
            self.loop_one = False
            self.loop_all = True
            info_log(f"[AUTO] 播放清單循環：開啟")
        else:
            error_log(f"[AUTO] 未知的循環模式：{mode}")
    
    def toggle_shuffle(self):
        """切換隨機播放"""
        import random
        
        self.is_shuffled = not self.is_shuffled
        
        if self.is_shuffled:
            # 隨機打亂播放清單
            random.shuffle(self.playlist)
            
            # 如果正在播放歌曲，將當前歌曲移到第一位（保持播放連續性）
            # 但如果還沒開始播放（current_index == 0 且 is_playing == False），
            # 就不需要移動，讓它從隨機後的第一首開始
            if self.engine.is_playing() and self.current_index < len(self.playlist):
                current_song = self.playlist[self.current_index]
                # 找到當前歌曲在打亂後清單中的位置，移到第一位
                if current_song in self.playlist:
                    idx = self.playlist.index(current_song)
                    self.playlist[0], self.playlist[idx] = self.playlist[idx], self.playlist[0]
                    self.current_index = 0
            else:
                # 還沒開始播放，從隨機後的第一首開始
                self.current_index = 0
        else:
            # 恢復原始順序
            current_song = self.playlist[self.current_index] if self.current_index < len(self.playlist) else None
            self.playlist = self.original_playlist.copy()
            
            # 找回當前歌曲的位置
            if current_song and current_song in self.playlist:
                self.current_index = self.playlist.index(current_song)
        
        info_log(f"[AUTO] 隨機播放：{'開啟' if self.is_shuffled else '關閉'}")
    
    def set_shuffle(self, enabled: bool):
        """設定隨機播放狀態"""
        if enabled != self.is_shuffled:
            self.toggle_shuffle()
    
    def set_volume(self, volume: int):
        """設定音量 (0-100)"""
        if 0 <= volume <= 100:
            self.volume = volume
            self.engine.set_volume(volume)
        else:
            error_log(f"[AUTO] 無效的音量值: {volume}")
    
    def get_status(self) -> dict:
        """獲取當前播放狀態"""
        loop_mode = "off"
        if self.loop_one:
            loop_mode = "one"
        elif self.loop_all:
            loop_mode = "all"
        
        return {
            'is_playing': self.engine.is_playing(),
            'is_paused': self.engine.is_paused(),
            'current_song': self.current_song,
            'current_index': self.current_index,
            'playlist_length': len(self.playlist),
            'volume': self.volume,
            'is_shuffled': self.is_shuffled,
            'loop_mode': loop_mode,
            'duration_ms': self.engine.get_duration(),
            'position_ms': self.engine.get_position(),
            'engine': self.engine.get_engine_name(),
            'capabilities': self.engine.get_capabilities()
        }
    
    def get_playback_position(self) -> int:
        """獲取當前播放位置（毫秒）"""
        return self.engine.get_position()


# ==================== 本地日曆功能 ====================

def local_calendar(
    action: str,
    summary: str = "",
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    location: str = "",
    event_id: int = -1
) -> dict:
    """
    本地日曆管理 - 使用 SQLite 儲存事件
    
    Args:
        action: 動作 (create, list, get, update, delete)
        summary: 事件標題
        start_time: 開始時間（ISO 格式）
        end_time: 結束時間（ISO 格式）
        description: 事件描述
        location: 地點
        event_id: 事件 ID（用於 get, update, delete）
        
    Returns:
        操作結果（dict）
    """
    try:
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        now = datetime.now().isoformat()
        
        if action == "create":
            # 建立事件
            if not summary or not start_time:
                return {"status": "error", "message": "缺少必要參數：summary, start_time"}
            
            # 如果沒有指定結束時間，預設 1 小時後
            if not end_time:
                from datetime import timedelta
                start_dt = datetime.fromisoformat(start_time)
                end_dt = start_dt + timedelta(hours=1)
                end_time = end_dt.isoformat()
            
            c.execute("""
                INSERT INTO calendar_events 
                (summary, description, start_time, end_time, location, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (summary, description, start_time, end_time, location, now, now))
            
            event_id = c.lastrowid
            conn.commit()
            conn.close()
            
            info_log(f"[AUTO] 已建立日曆事件：{summary} ({start_time})")
            
            # 發布事件：新增項目
            publish_calendar_event(
                MonitoringEventType.ITEM_ADDED,
                item_id=event_id,
                item_data={
                    "summary": summary,
                    "description": description,
                    "start_time": start_time,
                    "end_time": end_time,
                    "location": location
                }
            )
            
            return {
                "status": "ok",
                "message": f"已建立事件：{summary}",
                "event_id": event_id
            }
        
        elif action == "list":
            # 列出事件（可選時間範圍）
            if start_time:
                # 列出指定時間之後的事件
                c.execute("""
                    SELECT id, summary, description, start_time, end_time, location
                    FROM calendar_events
                    WHERE start_time >= ?
                    ORDER BY start_time ASC
                """, (start_time,))
            else:
                # 列出所有未來事件
                c.execute("""
                    SELECT id, summary, description, start_time, end_time, location
                    FROM calendar_events
                    WHERE start_time >= ?
                    ORDER BY start_time ASC
                """, (now,))
            
            events = []
            for row in c.fetchall():
                events.append({
                    "id": row[0],
                    "summary": row[1],
                    "description": row[2],
                    "start_time": row[3],
                    "end_time": row[4],
                    "location": row[5]
                })
            
            conn.close()
            info_log(f"[AUTO] 查詢到 {len(events)} 個事件")
            return {"status": "ok", "events": events}
        
        elif action == "get":
            # 取得單一事件
            if event_id < 0:
                return {"status": "error", "message": "缺少 event_id"}
            
            c.execute("""
                SELECT id, summary, description, start_time, end_time, location, created_at, updated_at
                FROM calendar_events
                WHERE id = ?
            """, (event_id,))
            
            row = c.fetchone()
            conn.close()
            
            if not row:
                return {"status": "error", "message": f"找不到事件 ID: {event_id}"}
            
            return {
                "status": "ok",
                "event": {
                    "id": row[0],
                    "summary": row[1],
                    "description": row[2],
                    "start_time": row[3],
                    "end_time": row[4],
                    "location": row[5],
                    "created_at": row[6],
                    "updated_at": row[7]
                }
            }
        
        elif action == "update":
            # 更新事件
            if event_id < 0:
                return {"status": "error", "message": "缺少 event_id"}
            
            # 構建更新語句
            updates = []
            params = []
            
            if summary:
                updates.append("summary = ?")
                params.append(summary)
            if description:
                updates.append("description = ?")
                params.append(description)
            if start_time:
                updates.append("start_time = ?")
                params.append(start_time)
            if end_time:
                updates.append("end_time = ?")
                params.append(end_time)
            if location:
                updates.append("location = ?")
                params.append(location)
            
            if not updates:
                return {"status": "error", "message": "沒有要更新的欄位"}
            
            updates.append("updated_at = ?")
            params.append(now)
            params.append(event_id)
            
            c.execute(f"""
                UPDATE calendar_events
                SET {', '.join(updates)}
                WHERE id = ?
            """, params)
            
            conn.commit()
            conn.close()
            
            info_log(f"[AUTO] 已更新事件 ID: {event_id}")
            
            # 發布事件：更新項目
            update_data = {}
            if summary:
                update_data["summary"] = summary
            if description:
                update_data["description"] = description
            if start_time:
                update_data["start_time"] = start_time
            if end_time:
                update_data["end_time"] = end_time
            if location:
                update_data["location"] = location
            
            publish_calendar_event(
                MonitoringEventType.ITEM_UPDATED,
                item_id=event_id,
                item_data=update_data
            )
            
            return {"status": "ok", "message": f"已更新事件 ID: {event_id}"}
        
        elif action == "delete":
            # 刪除事件
            if event_id < 0:
                return {"status": "error", "message": "缺少 event_id"}
            
            c.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
            conn.commit()
            conn.close()
            
            info_log(f"[AUTO] 已刪除事件 ID: {event_id}")
            
            # 發布事件：刪除項目
            publish_calendar_event(
                MonitoringEventType.ITEM_DELETED,
                item_id=event_id
            )
            return {"status": "ok", "message": f"已刪除事件 ID: {event_id}"}
        
        else:
            conn.close()
            return {"status": "error", "message": f"未知動作：{action}"}
    
    except Exception as e:
        error_log(f"[AUTO] 本地日曆操作失敗：{e}")
        return {"status": "error", "message": str(e)}


# ==================== 待辦事項管理 ====================

def local_todo(
    action: str,
    task_name: str = "",
    task_description: str = "",
    priority: str = "none",
    deadline: str = "",
    task_id: int = -1,
    search_query: str = ""
) -> dict:
    """
    本地待辦事項管理 - 使用 SQLite 儲存任務
    
    Args:
        action: 動作 (create, list, get, update, delete, complete, search)
        task_name: 任務名稱
        task_description: 任務描述
        priority: 優先級 (none, low, medium, high)
        deadline: 截止時間（ISO 格式）
        task_id: 任務 ID（用於 get, update, delete, complete）
        search_query: 搜尋關鍵字（用於 search）
        
    Returns:
        操作結果（dict）
    """
    try:
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        now = datetime.now().isoformat()
        
        # 驗證優先級
        valid_priorities = ["none", "low", "medium", "high"]
        if priority not in valid_priorities:
            priority = "none"
        
        if action == "create":
            # 建立任務
            if not task_name:
                return {"status": "error", "message": "缺少必要參數：task_name"}
            
            c.execute("""
                INSERT INTO todos 
                (task_name, task_description, priority, status, created_at, updated_at, deadline)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
            """, (task_name, task_description, priority, now, now, deadline or None))
            
            task_id = c.lastrowid
            conn.commit()
            conn.close()
            
            info_log(f"[AUTO] 已建立待辦事項：{task_name} (優先級：{priority})")
            
            # 發布事件：新增項目
            publish_todo_event(
                MonitoringEventType.ITEM_ADDED,
                item_id=task_id,
                item_data={
                    "task_name": task_name,
                    "task_description": task_description,
                    "priority": priority,
                    "deadline": deadline,
                    "status": "pending"
                }
            )
            
            return {
                "status": "ok",
                "message": f"已建立任務：{task_name}",
                "task_id": task_id
            }
        
        elif action == "list":
            # 列出任務（僅未完成）
            c.execute("""
                SELECT id, task_name, task_description, priority, status, deadline, created_at
                FROM todos
                WHERE status != 'completed'
                ORDER BY 
                    CASE priority
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 3
                        ELSE 4
                    END,
                    deadline ASC,
                    created_at ASC
            """)
            
            tasks = []
            for row in c.fetchall():
                tasks.append({
                    "id": row[0],
                    "task_name": row[1],
                    "task_description": row[2],
                    "priority": row[3],
                    "status": row[4],
                    "deadline": row[5],
                    "created_at": row[6]
                })
            
            conn.close()
            info_log(f"[AUTO] 查詢到 {len(tasks)} 個待辦事項")
            return {"status": "ok", "tasks": tasks}
        
        elif action == "search":
            # 搜尋任務（支援分詞模糊匹配）
            if not search_query:
                return {"status": "error", "message": "缺少 search_query"}
            
            # 分詞：移除常見的連接詞，提取關鍵字
            keywords = [word.strip().lower() for word in search_query.split() 
                       if word.strip().lower() not in ['the', 'a', 'an', 'one', 'some', 'my']]
            
            if not keywords:
                # 如果過濾後沒有關鍵字，使用原始查詢
                keywords = [search_query.lower()]
            
            # 構建查詢條件：任何一個關鍵字匹配即可
            where_conditions = []
            params = []
            for keyword in keywords:
                where_conditions.append("(task_name LIKE ? OR task_description LIKE ?)")
                params.extend([f"%{keyword}%", f"%{keyword}%"])
            
            query = f"""
                SELECT id, task_name, task_description, priority, status, deadline, created_at
                FROM todos
                WHERE ({' OR '.join(where_conditions)})
                AND status != 'completed'
                ORDER BY 
                    CASE priority
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 3
                        ELSE 4
                    END,
                    created_at DESC
            """
            
            c.execute(query, params)
            
            tasks = []
            for row in c.fetchall():
                tasks.append({
                    "id": row[0],
                    "task_name": row[1],
                    "task_description": row[2],
                    "priority": row[3],
                    "status": row[4],
                    "deadline": row[5],
                    "created_at": row[6]
                })
            
            conn.close()
            info_log(f"[AUTO] 搜尋「{search_query}」找到 {len(tasks)} 個結果")
            return {"status": "ok", "tasks": tasks}
        
        elif action == "get":
            # 取得單一任務
            if task_id < 0:
                return {"status": "error", "message": "缺少 task_id"}
            
            c.execute("""
                SELECT id, task_name, task_description, priority, status, deadline, created_at, updated_at, completed_at
                FROM todos
                WHERE id = ?
            """, (task_id,))
            
            row = c.fetchone()
            conn.close()
            
            if not row:
                return {"status": "error", "message": f"找不到任務 ID: {task_id}"}
            
            return {
                "status": "ok",
                "task": {
                    "id": row[0],
                    "task_name": row[1],
                    "task_description": row[2],
                    "priority": row[3],
                    "status": row[4],
                    "deadline": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                    "completed_at": row[8]
                }
            }
        
        elif action == "update":
            # 更新任務
            if task_id < 0:
                return {"status": "error", "message": "缺少 task_id"}
            
            # 構建更新語句
            updates = []
            params = []
            
            if task_name:
                updates.append("task_name = ?")
                params.append(task_name)
            if task_description:
                updates.append("task_description = ?")
                params.append(task_description)
            if priority:
                updates.append("priority = ?")
                params.append(priority)
            if deadline:
                updates.append("deadline = ?")
                params.append(deadline)
            
            if not updates:
                return {"status": "error", "message": "沒有要更新的欄位"}
            
            updates.append("updated_at = ?")
            params.append(now)
            params.append(task_id)
            
            c.execute(f"""
                UPDATE todos
                SET {', '.join(updates)}
                WHERE id = ?
            """, params)
            
            conn.commit()
            conn.close()
            
            info_log(f"[AUTO] 已更新任務 ID: {task_id}")
            
            # 發布事件：更新項目
            update_data = {}
            if task_name:
                update_data["task_name"] = task_name
            if task_description:
                update_data["task_description"] = task_description
            if priority:
                update_data["priority"] = priority
            if deadline:
                update_data["deadline"] = deadline
            
            publish_todo_event(
                MonitoringEventType.ITEM_UPDATED,
                item_id=task_id,
                item_data=update_data
            )
            
            return {"status": "ok", "message": f"已更新任務 ID: {task_id}"}
        
        elif action == "complete":
            # 完成任務
            if task_id < 0:
                return {"status": "error", "message": "缺少 task_id"}
            
            c.execute("""
                UPDATE todos
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE id = ?
            """, (now, now, task_id))
            
            conn.commit()
            conn.close()
            
            info_log(f"[AUTO] 已完成任務 ID: {task_id}")
            
            # 發布事件：項目完成
            publish_todo_event(
                MonitoringEventType.ITEM_COMPLETED,
                item_id=task_id,
                item_data={"completed_at": now}
            )
            
            return {"status": "ok", "message": f"已完成任務 ID: {task_id}"}
        
        elif action == "delete":
            # 刪除任務
            if task_id < 0:
                return {"status": "error", "message": "缺少 task_id"}
            
            c.execute("DELETE FROM todos WHERE id = ?", (task_id,))
            conn.commit()
            conn.close()
            
            info_log(f"[AUTO] 已刪除任務 ID: {task_id}")
            
            # 發布事件：刪除項目
            publish_todo_event(
                MonitoringEventType.ITEM_DELETED,
                item_id=task_id
            )
            
            return {"status": "ok", "message": f"已刪除任務 ID: {task_id}"}
        
        else:
            conn.close()
            return {"status": "error", "message": f"未知動作：{action}"}
    
    except Exception as e:
        error_log(f"[AUTO] 待辦事項操作失敗：{e}")
        return {"status": "error", "message": str(e)}


# ==================== 背景工作流資料庫管理 ====================

def register_background_workflow(
    task_id: str,
    workflow_type: str,
    trigger_conditions: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    next_check_at: Optional[str] = None
) -> bool:
    """
    註冊新的背景工作流到資料庫
    
    Args:
        task_id: 工作流任務 ID（唯一識別碼）
        workflow_type: 工作流類型（如 set_reminder, monitor_folder）
        trigger_conditions: 觸發條件（JSON 格式，如 {"type": "time", "target": "2025-12-01T10:00:00"}）
        metadata: 額外的元數據（JSON 格式）
        next_check_at: 下次檢查時間（ISO 格式字串）
        
    Returns:
        是否註冊成功
    """
    try:
        conn = sqlite3.connect(_DB)
        now = datetime.now().isoformat()
        
        trigger_json = json.dumps(trigger_conditions) if trigger_conditions else None
        metadata_json = json.dumps(metadata) if metadata else None
        
        conn.execute("""
            INSERT INTO background_workflows 
            (task_id, workflow_type, trigger_conditions, status, created_at, updated_at, next_check_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id,
            workflow_type,
            trigger_json,
            "RUNNING",
            now,
            now,
            next_check_at,
            metadata_json
        ))
        
        conn.commit()
        conn.close()
        
        info_log(f"[AUTO] 已註冊背景工作流：{task_id} (類型: {workflow_type})")
        return True
        
    except Exception as e:
        error_log(f"[AUTO] 註冊背景工作流失敗：{e}")
        return False


def get_active_workflows(workflow_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    取得所有活躍的背景工作流
    
    Args:
        workflow_type: 可選的工作流類型過濾器
        
    Returns:
        工作流資訊列表
    """
    try:
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        
        if workflow_type:
            c.execute("""
                SELECT task_id, workflow_type, trigger_conditions, status, 
                       created_at, updated_at, last_check_at, next_check_at, metadata
                FROM background_workflows
                WHERE status IN ('RUNNING', 'QUEUED') AND workflow_type = ?
                ORDER BY created_at DESC
            """, (workflow_type,))
        else:
            c.execute("""
                SELECT task_id, workflow_type, trigger_conditions, status, 
                       created_at, updated_at, last_check_at, next_check_at, metadata
                FROM background_workflows
                WHERE status IN ('RUNNING', 'QUEUED')
                ORDER BY created_at DESC
            """)
        
        workflows = []
        for row in c.fetchall():
            workflows.append({
                "task_id": row[0],
                "workflow_type": row[1],
                "trigger_conditions": json.loads(row[2]) if row[2] else None,
                "status": row[3],
                "created_at": row[4],
                "updated_at": row[5],
                "last_check_at": row[6],
                "next_check_at": row[7],
                "metadata": json.loads(row[8]) if row[8] else None
            })
        
        conn.close()
        return workflows
        
    except Exception as e:
        error_log(f"[AUTO] 取得活躍工作流失敗：{e}")
        return []


def get_workflow_by_id(task_id: str) -> Optional[Dict[str, Any]]:
    """
    根據 task_id 取得工作流詳細資訊
    
    Args:
        task_id: 工作流任務 ID
        
    Returns:
        工作流資訊，若不存在則返回 None
    """
    try:
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        
        c.execute("""
            SELECT task_id, workflow_type, trigger_conditions, status, 
                   created_at, updated_at, last_check_at, next_check_at, metadata, error_message
            FROM background_workflows
            WHERE task_id = ?
        """, (task_id,))
        
        row = c.fetchone()
        conn.close()
        
        if not row:
            return None
        
        return {
            "task_id": row[0],
            "workflow_type": row[1],
            "trigger_conditions": json.loads(row[2]) if row[2] else None,
            "status": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "last_check_at": row[6],
            "next_check_at": row[7],
            "metadata": json.loads(row[8]) if row[8] else None,
            "error_message": row[9]
        }
        
    except Exception as e:
        error_log(f"[AUTO] 取得工作流失敗：{e}")
        return None


def update_workflow_status(
    task_id: str,
    status: str,
    error_message: Optional[str] = None,
    last_check_at: Optional[str] = None,
    next_check_at: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """
    更新工作流狀態
    
    Args:
        task_id: 工作流任務 ID
        status: 新狀態（RUNNING, QUEUED, COMPLETED, FAILED, CANCELLED）
        error_message: 錯誤訊息（如果有）
        last_check_at: 最後檢查時間
        next_check_at: 下次檢查時間
        metadata: 更新的元數據
        
    Returns:
        是否更新成功
    """
    try:
        conn = sqlite3.connect(_DB)
        now = datetime.now().isoformat()
        
        # 構建動態更新語句
        updates = ["status = ?", "updated_at = ?"]
        params = [status, now]
        
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)
        
        if last_check_at is not None:
            updates.append("last_check_at = ?")
            params.append(last_check_at)
        
        if next_check_at is not None:
            updates.append("next_check_at = ?")
            params.append(next_check_at)
        
        if metadata is not None:
            updates.append("metadata = ?")
            params.append(json.dumps(metadata))
        
        params.append(task_id)
        
        conn.execute(f"""
            UPDATE background_workflows
            SET {', '.join(updates)}
            WHERE task_id = ?
        """, params)
        
        conn.commit()
        conn.close()
        
        debug_log(3, f"[AUTO] 已更新工作流狀態：{task_id} -> {status}")
        return True
        
    except Exception as e:
        error_log(f"[AUTO] 更新工作流狀態失敗：{e}")
        return False


def delete_workflow(task_id: str) -> bool:
    """
    從資料庫刪除工作流記錄
    
    Args:
        task_id: 工作流任務 ID
        
    Returns:
        是否刪除成功
    """
    try:
        conn = sqlite3.connect(_DB)
        conn.execute("DELETE FROM background_workflows WHERE task_id = ?", (task_id,))
        conn.commit()
        conn.close()
        
        info_log(f"[AUTO] 已刪除工作流記錄：{task_id}")
        return True
        
    except Exception as e:
        error_log(f"[AUTO] 刪除工作流記錄失敗：{e}")
        return False


def log_intervention(
    task_id: str,
    action: str,
    parameters: Optional[Dict[str, Any]] = None,
    performed_by: str = "system",
    result: Optional[str] = None
) -> bool:
    """
    記錄工作流干預操作到審計日誌
    
    Args:
        task_id: 工作流任務 ID
        action: 干預動作（edit, cancel, pause, resume 等）
        parameters: 干預參數（JSON 格式）
        performed_by: 執行者（預設為 system）
        result: 執行結果
        
    Returns:
        是否記錄成功
    """
    try:
        conn = sqlite3.connect(_DB)
        now = datetime.now().isoformat()
        
        params_json = json.dumps(parameters) if parameters else None
        
        conn.execute("""
            INSERT INTO workflow_interventions 
            (task_id, action, parameters, performed_at, performed_by, result)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (task_id, action, params_json, now, performed_by, result))
        
        conn.commit()
        conn.close()
        
        debug_log(3, f"[AUTO] 已記錄干預操作：{action} on {task_id}")
        return True
        
    except Exception as e:
        error_log(f"[AUTO] 記錄干預操作失敗：{e}")
        return False


def get_workflow_interventions(task_id: str) -> List[Dict[str, Any]]:
    """
    取得工作流的所有干預歷史記錄
    
    Args:
        task_id: 工作流任務 ID
        
    Returns:
        干預記錄列表
    """
    try:
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        
        c.execute("""
            SELECT id, action, parameters, performed_at, performed_by, result
            FROM workflow_interventions
            WHERE task_id = ?
            ORDER BY performed_at DESC
        """, (task_id,))
        
        interventions = []
        for row in c.fetchall():
            interventions.append({
                "id": row[0],
                "action": row[1],
                "parameters": json.loads(row[2]) if row[2] else None,
                "performed_at": row[3],
                "performed_by": row[4],
                "result": row[5]
            })
        
        conn.close()
        return interventions
        
    except Exception as e:
        error_log(f"[AUTO] 取得干預記錄失敗：{e}")
        return []


def get_workflows_due_for_check(current_time: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    取得需要檢查的工作流（next_check_at 時間已到）
    
    Args:
        current_time: 當前時間（ISO 格式），預設使用現在時間
        
    Returns:
        需要檢查的工作流列表
    """
    try:
        if current_time is None:
            current_time = datetime.now().isoformat()
        
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        
        c.execute("""
            SELECT task_id, workflow_type, trigger_conditions, status, 
                   created_at, updated_at, last_check_at, next_check_at, metadata
            FROM background_workflows
            WHERE status = 'RUNNING' 
              AND next_check_at IS NOT NULL 
              AND next_check_at <= ?
            ORDER BY next_check_at ASC
        """, (current_time,))
        
        workflows = []
        for row in c.fetchall():
            workflows.append({
                "task_id": row[0],
                "workflow_type": row[1],
                "trigger_conditions": json.loads(row[2]) if row[2] else None,
                "status": row[3],
                "created_at": row[4],
                "updated_at": row[5],
                "last_check_at": row[6],
                "next_check_at": row[7],
                "metadata": json.loads(row[8]) if row[8] else None
            })
        
        conn.close()
        return workflows
        
    except Exception as e:
        error_log(f"[AUTO] 取得待檢查工作流失敗：{e}")
        return []


# ==================== 系統自主事件循環觸發器 ====================

class BackgroundEventScheduler:
    """
    背景事件排程器 - 處理非使用者導致的系統循環
    
    這個類負責：
    1. 定期檢查資料庫中到期的觸發條件（提醒、日曆事件等）
    2. 在條件滿足時主動發布系統事件
    3. 觸發 Controller 的事件處理流程（非使用者輸入觸發）
    4. 管理自己的背景檢查線程
    
    與 MonitoringThreadPool 的區別：
    - MonitoringThreadPool: 管理用戶創建的監控工作流（多個獨立線程）
    - BackgroundEventScheduler: 系統級定時檢查器（單一線程，輕量級）
    """
    
    # 通知階段定義
    NOTIFICATION_STAGES = {
        "calendar_events": [
            {"hours_before": 24, "stage_name": "24h_before", "message": "明天有行程"},
            {"hours_before": 1, "stage_name": "1h_before", "message": "一小時後有行程"},
            {"minutes_before": 15, "stage_name": "15min_before", "message": "即將開始"}
        ],
        "todos": [
            {"hours_before": 24, "stage_name": "24h_before", "message": "明天到期"},
            {"hours_before": 1, "stage_name": "1h_before", "message": "一小時後到期"},
            {"at_deadline": True, "stage_name": "at_deadline", "message": "已到期"}
        ]
    }
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """單例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """初始化背景事件排程器"""
        if self._initialized:
            return
        
        self.check_interval = 30  # 預設每 30 秒檢查一次
        self.is_running = False
        self.stop_event = threading.Event()
        self.scheduler_thread = None
        self._initialized = True
        
        info_log("[BackgroundEventScheduler] 背景事件排程器已初始化")
    
    def start(self, check_interval: int = 30) -> bool:
        """
        啟動背景事件排程器
        
        Args:
            check_interval: 檢查間隔（秒），預設 30 秒
            
        Returns:
            是否成功啟動
        """
        if self.is_running:
            debug_log(2, "[BackgroundEventScheduler] 排程器已在運行中")
            return False
        
        try:
            self.check_interval = check_interval
            self.stop_event.clear()
            self.is_running = True
            
            # 啟動背景檢查線程
            self.scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="BackgroundEventScheduler",
                daemon=True
            )
            self.scheduler_thread.start()
            
            info_log(f"[BackgroundEventScheduler] 排程器已啟動（檢查間隔 {check_interval} 秒）")
            return True
            
        except Exception as e:
            error_log(f"[BackgroundEventScheduler] 啟動排程器失敗：{e}")
            self.is_running = False
            return False
    
    def stop(self, timeout: int = 10) -> bool:
        """
        停止背景事件排程器
        
        Args:
            timeout: 等待停止的超時時間（秒）
            
        Returns:
            是否成功停止
        """
        if not self.is_running:
            debug_log(2, "[BackgroundEventScheduler] 排程器未在運行")
            return False
        
        try:
            info_log("[BackgroundEventScheduler] 正在停止排程器...")
            
            # 設置停止事件
            self.stop_event.set()
            
            # 等待線程退出
            if self.scheduler_thread and self.scheduler_thread.is_alive():
                self.scheduler_thread.join(timeout=timeout)
                
                if self.scheduler_thread.is_alive():
                    error_log("[BackgroundEventScheduler] 停止排程器超時")
                    return False
            
            self.is_running = False
            info_log("[BackgroundEventScheduler] 排程器已停止")
            return True
            
        except Exception as e:
            error_log(f"[BackgroundEventScheduler] 停止排程器失敗：{e}")
            return False
    
    def _scheduler_loop(self) -> None:
        """
        排程器主循環（在獨立線程中運行）
        
        定期檢查：
        1. 提醒（reminders 表）
        2. 日曆事件（calendar_events 表）
        3. 待辦事項（todos 表）- 檢查過期任務
        4. 其他到期的觸發條件（background_workflows 表）
        """
        info_log("[BackgroundEventScheduler] 排程器循環已啟動")
        
        while not self.stop_event.is_set():
            try:
                # 檢查到期的提醒
                self._check_reminders()
                
                # 檢查即將開始的日曆事件（提前 15 分鐘通知）
                self._check_calendar_events()
                
                # 檢查過期的待辦事項
                self._check_todos()
                
                # 檢查其他到期的背景工作流觸發條件
                self._check_workflow_triggers()
                
            except Exception as e:
                error_log(f"[BackgroundEventScheduler] 檢查循環異常：{e}")
            
            # 等待下次檢查（可被中斷）
            self.stop_event.wait(self.check_interval)
        
        info_log("[BackgroundEventScheduler] 排程器循環已退出")
    
    def _check_reminders(self) -> None:
        """檢查到期的提醒並發布事件"""
        try:
            now = datetime.now().isoformat()
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            # 查詢到期的提醒
            c.execute("""
                SELECT id, time, message 
                FROM reminders 
                WHERE time <= ?
                ORDER BY time ASC
            """, (now,))
            
            triggered_reminders = c.fetchall()
            
            for reminder in triggered_reminders:
                reminder_id, trigger_time, message = reminder
                
                try:
                    # 發布提醒觸發事件
                    from core.event_bus import event_bus, SystemEvent
                    
                    event_bus.publish(
                        SystemEvent.REMINDER_TRIGGERED,
                        {
                            "reminder_id": reminder_id,
                            "trigger_time": trigger_time,
                            "message": message,
                            "source": "background_scheduler"
                        }
                    )
                    
                    info_log(f"[BackgroundEventScheduler] 提醒已觸發：{message}")
                    
                    # 刪除已觸發的提醒
                    c.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                    
                except Exception as e:
                    error_log(f"[BackgroundEventScheduler] 處理提醒失敗：{e}")
            
            conn.commit()
            conn.close()
            
            if triggered_reminders:
                debug_log(2, f"[BackgroundEventScheduler] 已處理 {len(triggered_reminders)} 個提醒")
                
        except Exception as e:
            error_log(f"[BackgroundEventScheduler] 檢查提醒失敗：{e}")
    
    def _check_calendar_events(self) -> None:
        """檢查日曆事件並根據通知階段發布事件"""
        try:
            from datetime import timedelta
            from core.event_bus import EventBus, SystemEvent
            from core.states.state_manager import UEPState
            
            now = datetime.now()
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            # 查詢所有未開始的日曆事件
            c.execute("""
                SELECT id, summary, description, start_time, location,
                       last_notified_at, last_notified_stage
                FROM calendar_events
                WHERE start_time >= ?
                ORDER BY start_time ASC
            """, (now.isoformat(),))
            
            events = c.fetchall()
            notifications_sent = 0
            
            for event in events:
                event_id, summary, description, start_time_str, location, last_notified_at, last_notified_stage = event
                
                try:
                    start_time = datetime.fromisoformat(start_time_str)
                    time_until_start = start_time - now
                    
                    # 判斷當前應該處於哪個通知階段
                    current_stage = None
                    
                    if time_until_start.total_seconds() <= 900:
                        # 15 分鐘內開始
                        current_stage = "15min_before"
                    elif time_until_start.total_seconds() <= 3600:
                        # 1 小時內開始
                        current_stage = "1h_before"
                    elif time_until_start.total_seconds() <= 86400:
                        # 24 小時內開始
                        current_stage = "24h_before"
                    
                    # 如果有應該通知的階段，且該階段還沒通知過
                    if current_stage and current_stage != last_notified_stage:
                        # 發布事件
                        from core.event_bus import event_bus
                        event_bus.publish(
                            SystemEvent.CALENDAR_EVENT_STARTING,
                            {
                                "event_id": event_id,
                                "summary": summary,
                                "description": description,
                                "start_time": start_time_str,
                                "location": location,
                                "stage": current_stage,
                                "source": "background_scheduler"
                            }
                        )
                        
                        # 更新資料庫中的通知記錄
                        c.execute("""
                            UPDATE calendar_events 
                            SET last_notified_at = ?, last_notified_stage = ?
                            WHERE id = ?
                        """, (now.isoformat(), current_stage, event_id))
                        conn.commit()
                        
                        notifications_sent += 1
                        info_log(f"[BackgroundEventScheduler] 日曆事件通知（{current_stage}）：{summary} (start: {start_time_str})")
                    
                except Exception as e:
                    error_log(f"[BackgroundEventScheduler] 處理日曆事件 {event_id} 失敗：{e}")
            
            conn.close()
            
            if notifications_sent > 0:
                debug_log(2, f"[BackgroundEventScheduler] 已發送 {notifications_sent} 個日曆事件通知")
                
        except Exception as e:
            error_log(f"[BackgroundEventScheduler] 檢查日曆事件失敗：{e}")
    
    def _check_todos(self) -> None:
        """檢查待辦事項並根據通知階段發布事件"""
        try:
            from datetime import timedelta
            from core.event_bus import EventBus, SystemEvent
            from core.states.state_manager import UEPState
            
            now = datetime.now()
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            # 查詢所有未完成且有 deadline 的待辦事項
            c.execute("""
                SELECT id, task_name, task_description, priority, deadline, 
                       last_notified_at, last_notified_stage
                FROM todos
                WHERE deadline IS NOT NULL 
                  AND status != 'completed'
                ORDER BY priority DESC, deadline ASC
            """)
            
            todos = c.fetchall()
            notifications_sent = 0
            
            for todo in todos:
                todo_id, task_name, task_description, priority, deadline_str, last_notified_at, last_notified_stage = todo
                
                try:
                    deadline = datetime.fromisoformat(deadline_str)
                    time_until_deadline = deadline - now
                    
                    # 判斷當前應該處於哪個通知階段
                    current_stage = None
                    event_type = None
                    
                    if time_until_deadline.total_seconds() <= 0:
                        # 已到期
                        current_stage = "at_deadline"
                        event_type = SystemEvent.TODO_OVERDUE
                    elif time_until_deadline.total_seconds() <= 3600:
                        # 1 小時內到期
                        current_stage = "1h_before"
                        event_type = SystemEvent.TODO_UPCOMING
                    elif time_until_deadline.total_seconds() <= 86400:
                        # 24 小時內到期
                        current_stage = "24h_before"
                        event_type = SystemEvent.TODO_UPCOMING
                    
                    # 如果有應該通知的階段，且該階段還沒通知過
                    if current_stage and current_stage != last_notified_stage:
                        # 發布事件
                        from core.event_bus import event_bus
                        event_bus.publish(
                            event_type,
                            {
                                "todo_id": todo_id,
                                "task_name": task_name,
                                "task_description": task_description,
                                "priority": priority,
                                "deadline": deadline_str,
                                "stage": current_stage,
                                "source": "background_scheduler"
                            }
                        )
                        
                        # 更新資料庫中的通知記錄
                        c.execute("""
                            UPDATE todos 
                            SET last_notified_at = ?, last_notified_stage = ?
                            WHERE id = ?
                        """, (now.isoformat(), current_stage, todo_id))
                        conn.commit()
                        
                        notifications_sent += 1
                        info_log(f"[BackgroundEventScheduler] 待辦事項通知（{current_stage}）：{task_name} (deadline: {deadline_str})")
                    
                except Exception as e:
                    error_log(f"[BackgroundEventScheduler] 處理待辦事項 {todo_id} 失敗：{e}")
            
            conn.close()
            
            if notifications_sent > 0:
                debug_log(2, f"[BackgroundEventScheduler] 已發送 {notifications_sent} 個待辦事項通知")
                
        except Exception as e:
            error_log(f"[BackgroundEventScheduler] 檢查待辦事項失敗：{e}")
    
    def check_overdue_items_on_startup(self) -> Dict[str, Any]:
        """
        系統啟動時檢查所有過期的項目並返回報告
        
        這個方法應該在系統初始化時被調用，用於：
        1. 檢查資料庫中所有過期的待辦事項
        2. 檢查資料庫中所有已過時的提醒
        3. 檢查資料庫中所有已過期的日曆事件
        4. 生成報告並可選地發布事件
        
        Returns:
            包含過期項目統計的報告字典
        """
        try:
            now = datetime.now()
            report = {
                "overdue_todos": [],
                "missed_reminders": [],
                "past_calendar_events": []
            }
            
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            # 1. 檢查過期待辦事項
            c.execute("""
                SELECT id, task_name, task_description, priority, deadline
                FROM todos
                WHERE deadline IS NOT NULL 
                  AND deadline < ?
                  AND status != 'completed'
                ORDER BY deadline ASC
            """, (now.isoformat(),))
            
            for row in c.fetchall():
                todo_id, task_name, task_description, priority, deadline = row
                report["overdue_todos"].append({
                    "id": todo_id,
                    "task_name": task_name,
                    "task_description": task_description,
                    "priority": priority,
                    "deadline": deadline
                })
            
            # 2. 檢查錯過的提醒
            c.execute("""
                SELECT id, time, message
                FROM reminders
                WHERE time < ?
                ORDER BY time ASC
            """, (now.isoformat(),))
            
            for row in c.fetchall():
                reminder_id, trigger_time, message = row
                report["missed_reminders"].append({
                    "id": reminder_id,
                    "time": trigger_time,
                    "message": message
                })
            
            # 3. 檢查已過期的日曆事件（過去 24 小時內）
            from datetime import timedelta
            past_24h = now - timedelta(hours=24)
            
            c.execute("""
                SELECT id, summary, description, start_time, end_time
                FROM calendar_events
                WHERE end_time >= ? AND end_time < ?
                ORDER BY start_time ASC
            """, (past_24h.isoformat(), now.isoformat()))
            
            for row in c.fetchall():
                event_id, summary, description, start_time, end_time = row
                report["past_calendar_events"].append({
                    "id": event_id,
                    "summary": summary,
                    "description": description,
                    "start_time": start_time,
                    "end_time": end_time
                })
            
            conn.close()
            
            # 統計
            total_overdue = len(report["overdue_todos"])
            total_missed = len(report["missed_reminders"])
            total_past = len(report["past_calendar_events"])
            
            if total_overdue > 0 or total_missed > 0 or total_past > 0:
                info_log(
                    f"[BackgroundEventScheduler] 系統啟動檢查完成：\n"
                    f"  - {total_overdue} 個過期待辦事項\n"
                    f"  - {total_missed} 個錯過的提醒\n"
                    f"  - {total_past} 個最近結束的日曆事件"
                )
                
                # 可選：發布系統啟動報告事件
                try:
                    from core.event_bus import event_bus, SystemEvent
                    event_bus.publish(
                        SystemEvent.SYSTEM_STARTUP_REPORT,
                        {
                            "report": report,
                            "total_overdue": total_overdue,
                            "total_missed": total_missed,
                            "total_past": total_past,
                            "source": "background_scheduler"
                        }
                    )
                except Exception as e:
                    error_log(f"[BackgroundEventScheduler] 發布啟動報告失敗：{e}")
            else:
                info_log("[BackgroundEventScheduler] 系統啟動檢查完成：沒有過期項目")
            
            return report
            
        except Exception as e:
            error_log(f"[BackgroundEventScheduler] 啟動檢查失敗：{e}")
            return {
                "overdue_todos": [],
                "missed_reminders": [],
                "past_calendar_events": [],
                "error": str(e)
            }
    
    def _check_workflow_triggers(self) -> None:
        """檢查背景工作流的觸發條件"""
        try:
            # 獲取所有到期需要檢查的工作流
            due_workflows = get_workflows_due_for_check()
            
            for workflow in due_workflows:
                task_id = workflow["task_id"]
                workflow_type = workflow["workflow_type"]
                trigger_conditions = workflow.get("trigger_conditions", {})
                
                try:
                    # 根據觸發條件類型處理
                    trigger_type = trigger_conditions.get("type")
                    
                    if trigger_type == "time":
                        # 時間觸發（已在 _check_reminders 中處理）
                        pass
                    
                    elif trigger_type == "file_change":
                        # 檔案變更觸發（由 MonitoringThreadPool 中的監控函數處理）
                        pass
                    
                    else:
                        # 其他自定義觸發條件
                        debug_log(3, f"[BackgroundEventScheduler] 未知觸發類型：{trigger_type} (task_id: {task_id})")
                    
                except Exception as e:
                    error_log(f"[BackgroundEventScheduler] 處理工作流觸發失敗 (task_id: {task_id})：{e}")
            
            if due_workflows:
                debug_log(3, f"[BackgroundEventScheduler] 已檢查 {len(due_workflows)} 個到期工作流")
                
        except Exception as e:
            error_log(f"[BackgroundEventScheduler] 檢查工作流觸發失敗：{e}")


# 全域背景事件排程器實例
_background_scheduler = None
_scheduler_lock = threading.Lock()


def get_background_scheduler() -> BackgroundEventScheduler:
    """獲取全域背景事件排程器實例（單例）"""
    global _background_scheduler
    if _background_scheduler is None:
        with _scheduler_lock:
            if _background_scheduler is None:
                _background_scheduler = BackgroundEventScheduler()
    return _background_scheduler
