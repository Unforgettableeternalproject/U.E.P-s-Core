import sqlite3
import threading
import time
import os
from datetime import datetime
from pathlib import Path
from utils.debug_helper import info_log, error_log

# 將資料庫放在 memory 目錄中
_DB_DIR = Path(__file__).parent.parent.parent.parent / "memory"
_DB_DIR.mkdir(exist_ok=True)
_DB = str(_DB_DIR / "uep_tasks.db")

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
      updated_at TEXT NOT NULL
    )""")
    
    conn.commit()
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

def media_control(
    action: str,
    song_query: str = "",
    music_folder: str = "",
    youtube: bool = False,
    spotify: bool = False
) -> str:
    """
    音樂播放控制器 - 支援本地音樂、YouTube、Spotify（背景運行）
    
    Args:
        action: 動作指令 (play, pause, stop, next, previous, search, youtube, spotify)
        song_query: 歌曲查詢關鍵字
        music_folder: 本地音樂資料夾路徑
        youtube: 是否使用 YouTube 播放
        spotify: 是否使用 Spotify 播放
        
    Returns:
        操作結果訊息
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
            
            if _music_player is None:
                _music_player = MusicPlayer(music_folder)
                info_log("[AUTO] 音樂播放器已初始化")
            
            if action == "play":
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
    """本地音樂播放器（背景運行，無 UI）"""
    
    def __init__(self, music_folder: str):
        self.music_folder = Path(music_folder)
        self.playlist = []
        self.current_index = 0
        self.is_playing = False
        self.is_looping = False
        self.current_song = None
        self.playback_obj = None
        
        # 載入播放清單
        self._load_playlist()
        
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
        
        info_log(f"[AUTO] 載入 {len(self.playlist)} 首歌曲")
    
    def search_song(self, query: str) -> list:
        """搜尋歌曲（使用模糊比對）"""
        try:
            from rapidfuzz import process, fuzz
            
            # 從檔名中提取歌曲名稱
            song_names = [Path(song).stem for song in self.playlist]
            
            # 使用 rapidfuzz 進行模糊搜尋
            results = process.extract(
                query, 
                song_names, 
                scorer=fuzz.WRatio, 
                limit=10
            )
            
            # 回傳相似度 > 60 的結果
            matched = [r[0] for r in results if r[1] > 60]
            return matched
        
        except ImportError:
            info_log("[AUTO] rapidfuzz 未安裝，使用簡單搜尋")
            # 簡單字串比對
            matched = []
            query_lower = query.lower()
            for song in self.playlist:
                song_name = Path(song).stem.lower()
                if query_lower in song_name:
                    matched.append(Path(song).stem)
            return matched
    
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
        
        try:
            from pydub import AudioSegment
            from pydub.playback import _play_with_simpleaudio
            
            if self.current_index >= len(self.playlist):
                self.current_index = 0
            
            song_path = self.playlist[self.current_index]
            self.current_song = Path(song_path).stem
            
            info_log(f"[AUTO] 正在播放：{self.current_song}")
            
            # 載入音訊
            audio = AudioSegment.from_file(song_path)
            
            # 播放（在背景執行緒）
            self.is_playing = True
            
            def _play_thread():
                try:
                    self.playback_obj = _play_with_simpleaudio(audio)
                    self.playback_obj.wait_done()
                    
                    # 播放完成後
                    if self.is_looping:
                        self.play()
                    else:
                        self.next_song()
                except Exception as e:
                    error_log(f"[AUTO] 播放錯誤：{e}")
                    self.is_playing = False
            
            play_thread = threading.Thread(target=_play_thread, daemon=True)
            play_thread.start()
            
        except ImportError:
            error_log("[AUTO] pydub 未安裝，請執行：pip install pydub simpleaudio")
        except Exception as e:
            error_log(f"[AUTO] 播放失敗：{e}")
    
    def pause(self):
        """暫停播放"""
        if self.playback_obj:
            self.playback_obj.stop()
            self.is_playing = False
            info_log("[AUTO] 已暫停")
    
    def stop(self):
        """停止播放"""
        if self.playback_obj:
            self.playback_obj.stop()
            self.is_playing = False
            self.current_index = 0
            info_log("[AUTO] 已停止")
    
    def next_song(self):
        """下一首"""
        self.stop()
        self.current_index = (self.current_index + 1) % len(self.playlist)
        self.play()
    
    def previous_song(self):
        """上一首"""
        self.stop()
        self.current_index = (self.current_index - 1) % len(self.playlist)
        self.play()
    
    def toggle_loop(self):
        """切換單曲循環"""
        self.is_looping = not self.is_looping
        info_log(f"[AUTO] 單曲循環：{'開啟' if self.is_looping else '關閉'}")


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
            return {"status": "ok", "message": f"已更新事件 ID: {event_id}"}
        
        elif action == "delete":
            # 刪除事件
            if event_id < 0:
                return {"status": "error", "message": "缺少 event_id"}
            
            c.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
            conn.commit()
            conn.close()
            
            info_log(f"[AUTO] 已刪除事件 ID: {event_id}")
            return {"status": "ok", "message": f"已刪除事件 ID: {event_id}"}
        
        else:
            conn.close()
            return {"status": "error", "message": f"未知動作：{action}"}
    
    except Exception as e:
        error_log(f"[AUTO] 本地日曆操作失敗：{e}")
        return {"status": "error", "message": str(e)}
