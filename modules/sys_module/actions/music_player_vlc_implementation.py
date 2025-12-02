# VLC 實現方案（參考用）
# 只需要替換 MusicPlayer 類中的播放相關方法

"""
需要安裝: pip install python-vlc

VLC 播放器實現 - 支援真正的暫停、即時音量調整、進度條 seek
"""
from utils.debug_helper import debug_log, info_log, error_log
from pathlib import Path


class MusicPlayerVLC:
    """使用 VLC 的音樂播放器（修改後的版本）"""
    
    def __init__(self, music_folder: str):
        # ... 原有的初始化代碼保持不變 ...
        
        # VLC 初始化
        import vlc
        self.vlc_instance = vlc.Instance()
        self.vlc_player = self.vlc_instance.media_player_new()
        
        # 設定音量（0-100）
        self.vlc_player.audio_set_volume(self.volume)
        
        # 播放完成事件處理
        self.vlc_events = self.vlc_player.event_manager()
        self.vlc_events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_song_ended)
    
    def play(self):
        """播放當前歌曲"""
        if not self.playlist:
            error_log("[AUTO] 播放清單為空")
            return
        
        # 如果是從暫停恢復
        if self.is_paused:
            self.vlc_player.play()  # VLC 真正的恢復播放
            self.is_paused = False
            self.is_playing = True
            info_log("[AUTO] 從暫停位置恢復播放")
            return
        
        # 播放新歌曲
        if self.current_index >= len(self.playlist):
            self.current_index = 0
        
        song_path = self.playlist[self.current_index]
        self.current_song = Path(song_path).stem
        
        # 使用 VLC 播放
        media = self.vlc_instance.media_new(song_path)
        self.vlc_player.set_media(media)
        self.vlc_player.play()
        
        self.is_playing = True
        self.is_paused = False
        self.is_finished = False
        
        # 等待 VLC 載入媒體以獲取時長
        import time
        time.sleep(0.1)  # 給 VLC 一點時間載入
        self.current_duration_ms = self.vlc_player.get_length()
        
        info_log(f"[AUTO] 正在播放：{self.current_song}")
    
    def pause(self):
        """暫停播放（VLC 原生支援）"""
        if self.is_playing:
            self.vlc_player.pause()
            self.is_playing = False
            self.is_paused = True
            info_log("[AUTO] 已暫停（可從當前位置恢復）")
    
    def stop(self):
        """停止播放"""
        self.vlc_player.stop()
        self.is_playing = False
        self.is_paused = False
        self.current_index = 0
        self.current_song = None
        info_log("[AUTO] 已停止")
    
    def set_volume(self, volume: int):
        """即時設定音量 (0-100)"""
        if 0 <= volume <= 100:
            self.volume = volume
            self.vlc_player.audio_set_volume(volume)
            info_log(f"[AUTO] 音量已設定為 {volume}%")
        else:
            error_log(f"[AUTO] 無效的音量值: {volume}")
    
    def get_playback_position(self) -> int:
        """獲取當前播放位置（毫秒）"""
        if not self.is_playing and not self.is_paused:
            return 0
        
        position_ms = self.vlc_player.get_time()
        return position_ms if position_ms > 0 else 0
    
    def seek(self, position_ms: int):
        """跳轉到指定位置（毫秒）"""
        if self.current_duration_ms > 0:
            self.vlc_player.set_time(position_ms)
            info_log(f"[AUTO] 跳轉到 {position_ms}ms")
    
    def _on_song_ended(self, event):
        """歌曲播放完成回調"""
        self.is_playing = False
        
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
            # 普通播放
            self.is_finished = True
            info_log(f"[AUTO] 歌曲播放完成：{self.current_song}")
    
    # 其他方法（next_song, previous_song, toggle_loop 等）保持不變

# ==================== 改動總結 ====================
"""
需要修改的方法：
1. __init__ - 添加 VLC 初始化（+10 行）
2. play() - 使用 VLC API（-40 +30 行）
3. pause() - 簡化為 VLC 原生暫停（-10 +5 行）
4. set_volume() - 使用 VLC 即時調整（-5 +5 行）
5. get_playback_position() - 使用 VLC API（-10 +5 行）
6. 添加 seek() 方法（+5 行）
7. 添加 _on_song_ended() 回調（+15 行）

保持不變的方法（無需修改）：
- _load_playlist()
- search_song()
- search_and_play()
- stop()
- next_song()
- previous_song()
- toggle_loop()
- set_loop()
- set_loop_mode()
- toggle_shuffle()
- set_shuffle()
- get_status()

總計：約 60-80 行需要修改，核心邏輯保持不變
UI 層：完全不需要修改！

優點：
✅ 解決暫停問題
✅ 解決音量調整問題
✅ 可以實現進度條拖動
✅ 更穩定的播放

缺點：
❌ 需要用戶安裝 VLC（但大多數人都有）
❌ 增加一個外部依賴

建議：可以做成可選功能，保留 pydub 作為 fallback
"""
