"""
音樂播放引擎
支援 VLC 和 pydub 兩種播放引擎
"""

from abc import ABC, abstractmethod
from pathlib import Path
import threading
import time
from typing import Optional, Callable

from utils.debug_helper import info_log, error_log, debug_log


class BaseMusicEngine(ABC):
    """音樂播放引擎基類"""
    
    @abstractmethod
    def play(self, file_path: str, volume: int, on_finished: Optional[Callable] = None):
        """播放音訊檔案"""
        pass
    
    @abstractmethod
    def pause(self):
        """暫停播放"""
        pass
    
    @abstractmethod
    def resume(self):
        """恢復播放"""
        pass
    
    @abstractmethod
    def stop(self):
        """停止播放"""
        pass
    
    @abstractmethod
    def set_volume(self, volume: int):
        """設定音量 (0-100)"""
        pass
    
    @abstractmethod
    def get_position(self) -> int:
        """獲取當前播放位置（毫秒）"""
        pass
    
    @abstractmethod
    def get_duration(self) -> int:
        """獲取歌曲總時長（毫秒）"""
        pass
    
    @abstractmethod
    def seek(self, position_ms: int):
        """跳轉到指定位置（毫秒）"""
        pass
    
    @abstractmethod
    def is_playing(self) -> bool:
        """是否正在播放"""
        pass
    
    @abstractmethod
    def is_paused(self) -> bool:
        """是否已暫停"""
        pass
    
    @abstractmethod
    def get_engine_name(self) -> str:
        """獲取引擎名稱"""
        pass
    
    @abstractmethod
    def get_capabilities(self) -> dict:
        """獲取引擎功能支援情況"""
        pass


class VLCEngine(BaseMusicEngine):
    """VLC 播放引擎 - 支援完整功能"""
    
    def __init__(self):
        try:
            import vlc
            self.vlc = vlc
            self.instance = vlc.Instance()
            self.player = self.instance.media_player_new()
            self._is_playing = False
            self._is_paused = False
            self._on_finished_callback = None
            
            # 設定播放完成事件
            self.events = self.player.event_manager()
            self.events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_media_ended)
            
            info_log("[MusicEngine] VLC 引擎初始化成功")
        except Exception as e:
            error_log(f"[MusicEngine] VLC 引擎初始化失敗: {e}")
            raise
    
    def play(self, file_path: str, volume: int, on_finished: Optional[Callable] = None):
        """播放音訊檔案"""
        try:
            media = self.instance.media_new(file_path)
            self.player.set_media(media)
            self.player.audio_set_volume(volume)
            self.player.play()
            
            self._is_playing = True
            self._is_paused = False
            self._on_finished_callback = on_finished
            
            # 等待一下讓 VLC 載入媒體資訊
            time.sleep(0.1)
            
            info_log(f"[VLC] 播放: {Path(file_path).name}")
        except Exception as e:
            error_log(f"[VLC] 播放失敗: {e}")
            self._is_playing = False
    
    def pause(self):
        """暫停播放（保持當前位置）"""
        if self._is_playing:
            self.player.pause()
            self._is_playing = False
            self._is_paused = True
            info_log("[VLC] 已暫停")
    
    def resume(self):
        """恢復播放（從暫停位置繼續）"""
        if self._is_paused:
            self.player.play()
            self._is_playing = True
            self._is_paused = False
            info_log("[VLC] 已恢復播放")
    
    def stop(self):
        """停止播放"""
        self.player.stop()
        self._is_playing = False
        self._is_paused = False
        info_log("[VLC] 已停止")
    
    def set_volume(self, volume: int):
        """設定音量 (0-100) - 即時生效"""
        if 0 <= volume <= 100:
            self.player.audio_set_volume(volume)
            debug_log(3, f"[VLC] 音量設定為 {volume}%")
    
    def get_position(self) -> int:
        """獲取當前播放位置（毫秒）"""
        if not self._is_playing and not self._is_paused:
            return 0
        pos = self.player.get_time()
        return pos if pos > 0 else 0
    
    def get_duration(self) -> int:
        """獲取歌曲總時長（毫秒）"""
        duration = self.player.get_length()
        return duration if duration > 0 else 0
    
    def seek(self, position_ms: int):
        """跳轉到指定位置（毫秒）"""
        if self.get_duration() > 0:
            self.player.set_time(position_ms)
            debug_log(3, f"[VLC] 跳轉到 {position_ms}ms")
    
    def is_playing(self) -> bool:
        """是否正在播放"""
        return self._is_playing
    
    def is_paused(self) -> bool:
        """是否已暫停"""
        return self._is_paused
    
    def get_engine_name(self) -> str:
        """獲取引擎名稱"""
        return "VLC"
    
    def get_capabilities(self) -> dict:
        """獲取引擎功能支援情況"""
        return {
            'true_pause': True,      # 真正的暫停/恢復
            'realtime_volume': True,  # 即時音量調整
            'seek': True,             # 進度拖動
            'position_tracking': True # 精確位置追蹤
        }
    
    def _on_media_ended(self, event):
        """播放完成回調"""
        self._is_playing = False
        self._is_paused = False
        info_log("[VLC] 播放完成")
        
        if self._on_finished_callback:
            self._on_finished_callback()


class PydubEngine(BaseMusicEngine):
    """pydub 播放引擎 - 基本功能"""
    
    def __init__(self):
        try:
            from pydub import AudioSegment
            from pydub.playback import _play_with_simpleaudio
            self.AudioSegment = AudioSegment
            self._play_with_simpleaudio = _play_with_simpleaudio
            
            self.playback_obj = None
            self._is_playing = False
            self._is_paused = False
            self._current_volume = 100
            self._duration_ms = 0
            self._start_time = None
            self._lock = threading.Lock()
            
            info_log("[MusicEngine] pydub 引擎初始化成功")
        except ImportError as e:
            error_log(f"[MusicEngine] pydub 引擎初始化失敗: {e}")
            raise
    
    def play(self, file_path: str, volume: int, on_finished: Optional[Callable] = None):
        """播放音訊檔案"""
        try:
            # 停止當前播放
            with self._lock:
                if self.playback_obj:
                    try:
                        self.playback_obj.stop()
                    except:
                        pass
                    self.playback_obj = None
            
            # 載入音訊
            audio = self.AudioSegment.from_file(file_path)
            self._duration_ms = len(audio)
            
            # 應用音量
            if volume > 0:
                db_change = (volume - 100) * 0.6
                audio = audio + db_change
            else:
                audio = audio - 60  # 靜音
            
            self._current_volume = volume
            self._is_playing = True
            self._is_paused = False
            self._start_time = time.time()
            
            # 在背景執行緒播放
            def _play_thread():
                try:
                    self.playback_obj = self._play_with_simpleaudio(audio)
                    self.playback_obj.wait_done()
                    
                    self._is_playing = False
                    info_log(f"[pydub] 播放完成: {Path(file_path).name}")
                    
                    if on_finished:
                        on_finished()
                except Exception as e:
                    error_log(f"[pydub] 播放錯誤: {e}")
                    self._is_playing = False
            
            play_thread = threading.Thread(target=_play_thread, daemon=True)
            play_thread.start()
            
            info_log(f"[pydub] 播放: {Path(file_path).name}")
            
        except Exception as e:
            error_log(f"[pydub] 播放失敗: {e}")
            self._is_playing = False
    
    def pause(self):
        """暫停播放（停止播放對象，標記為暫停）"""
        with self._lock:
            if self.playback_obj and self._is_playing:
                self.playback_obj.stop()
                self.playback_obj = None
                self._is_playing = False
                self._is_paused = True
                info_log("[pydub] 已暫停（恢復時將從頭播放）")
    
    def resume(self):
        """恢復播放（pydub 不支援真正的恢復，需要重新播放）"""
        # 這個方法由 MusicPlayer 處理（重新調用 play）
        self._is_paused = False
    
    def stop(self):
        """停止播放"""
        with self._lock:
            if self.playback_obj:
                try:
                    self.playback_obj.stop()
                except:
                    pass
                self.playback_obj = None
            self._is_playing = False
            self._is_paused = False
            info_log("[pydub] 已停止")
    
    def set_volume(self, volume: int):
        """設定音量 (0-100) - 下次播放時生效"""
        if 0 <= volume <= 100:
            self._current_volume = volume
            info_log(f"[pydub] 音量設定為 {volume}%（將於下次播放時生效）")
    
    def get_position(self) -> int:
        """獲取當前播放位置（毫秒）- 估算值"""
        if not self._is_playing or self._start_time is None:
            return 0
        
        elapsed_ms = int((time.time() - self._start_time) * 1000)
        return min(elapsed_ms, self._duration_ms)
    
    def get_duration(self) -> int:
        """獲取歌曲總時長（毫秒）"""
        return self._duration_ms
    
    def seek(self, position_ms: int):
        """跳轉到指定位置（pydub 不支援）"""
        debug_log(3, "[pydub] 不支援 seek 功能")
    
    def is_playing(self) -> bool:
        """是否正在播放"""
        return self._is_playing
    
    def is_paused(self) -> bool:
        """是否已暫停"""
        return self._is_paused
    
    def get_engine_name(self) -> str:
        """獲取引擎名稱"""
        return "pydub"
    
    def get_capabilities(self) -> dict:
        """獲取引擎功能支援情況"""
        return {
            'true_pause': False,      # 不支援真正的暫停（會從頭播放）
            'realtime_volume': False, # 不支援即時音量調整
            'seek': False,            # 不支援進度拖動
            'position_tracking': True # 支援位置追蹤（估算）
        }


def create_music_engine(engine_type: str = "auto") -> BaseMusicEngine:
    """
    創建音樂播放引擎
    
    Args:
        engine_type: 引擎類型 ("auto", "vlc", "pydub")
    
    Returns:
        BaseMusicEngine: 播放引擎實例
    """
    if engine_type == "vlc":
        try:
            return VLCEngine()
        except Exception as e:
            error_log(f"[MusicEngine] 無法使用 VLC 引擎: {e}")
            info_log("[MusicEngine] 改用 pydub 引擎")
            return PydubEngine()
    
    elif engine_type == "pydub":
        return PydubEngine()
    
    else:  # auto
        # 優先嘗試 VLC
        try:
            return VLCEngine()
        except:
            info_log("[MusicEngine] VLC 不可用，使用 pydub 引擎")
            return PydubEngine()
