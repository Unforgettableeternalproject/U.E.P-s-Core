"""
TTS Module - 重構版本
使用 IndexTTS Lite 引擎，支持情感控制和串流播放
"""

import asyncio
import os
import time
import uuid
import enum
from typing import Optional, Dict, Any, List

from core.bases.module_base import BaseModule
from core.working_context import working_context_manager
from core.status_manager import status_manager
from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log

from .schemas import TTSInput, TTSOutput
from .lite_engine import IndexTTSLite
from .emotion_mapper import EmotionMapper
from utils.tts_chunker import TTSChunker

import numpy as np
import soundfile as sf
try:
    import simpleaudio as sa
except ImportError:
    sa = None
    error_log("[TTS] simpleaudio library not found. Audio playback will be disabled.")


class PlaybackState(enum.Enum):
    """音頻播放狀態"""
    IDLE = "idle"              # 未播放
    PLAYING = "playing"        # 正在播放
    COMPLETED = "completed"    # 播放完畢
    ERROR = "error"            # 播放錯誤


class TTSModule(BaseModule):
    def __init__(self, config=None):
        """
        初始化 TTS 模組
        
        Args:
            config: 模組配置 (如果為 None 則從 config_tts.yaml 加載)
        """
        self.config = config or load_module_config("tts_module")
        
        # IndexTTS 配置
        self.model_dir = self.config.get("model_dir", "modules/tts_module/checkpoints")
        self.character_dir = self.config.get("character_dir", "modules/tts_module/checkpoints")
        self.default_character = self.config.get("default_character", "uep")
        self.use_fp16 = self.config.get("use_fp16", True)
        self.device = self.config.get("device", "cuda")
        
        # Chunking 配置
        chunking_config = self.config.get("chunking", {})
        self.chunking_threshold = chunking_config.get("threshold", 200)  # 字符數閾值
        self.chunking_enabled = chunking_config.get("enabled", True)
        self.max_tokens_per_chunk = chunking_config.get("max_tokens", 200)  # BPE tokens
        
        # Emotion 配置
        emotion_config = self.config.get("emotion", {})
        self.emotion_max_strength = emotion_config.get("max_strength", 0.3)
        self.default_emotion = emotion_config.get("default", None)  # None = 使用角色原聲
        
        # 播放狀態追踪
        self._playback_state = PlaybackState.IDLE
        self._current_playback_obj = None
        self._playback_lock = asyncio.Lock()
        
        # 初始化組件
        self.engine: Optional[IndexTTSLite] = None
        self.emotion_mapper = EmotionMapper(max_strength=self.emotion_max_strength)
        
        # 初始化 TTSChunker（用於長文本分段）
        self.chunker = TTSChunker(
            max_chars=self.chunking_threshold,
            min_chars=50,
            respect_punctuation=True,
            pause_between_chunks=0.1  # 段落間短暫停頓
        )
        
        # Working Context 和 Status Manager 引用 (使用全局單例)
        self.working_context_manager = working_context_manager
        self.status_manager = status_manager
        
        # 創建必要目錄
        os.makedirs(os.path.join("temp", "tts"), exist_ok=True)
        os.makedirs(os.path.join("outputs", "tts"), exist_ok=True)
    
    def debug(self):
        """Debug 模式 - 顯示模組配置"""
        debug_log(1, "[TTS] Debug 模式啟用")
        debug_log(2, f"[TTS] 模型目錄: {self.model_dir}")
        debug_log(2, f"[TTS] 角色目錄: {self.character_dir}")
        debug_log(2, f"[TTS] 預設角色: {self.default_character}")
        debug_log(2, f"[TTS] 設備: {self.device}, FP16: {self.use_fp16}")
        debug_log(2, f"[TTS] Chunking 閾值: {self.chunking_threshold}")
        debug_log(2, f"[TTS] 情感強度上限: {self.emotion_max_strength}")
        debug_log(2, f"[TTS] Working Context Manager: {'已連接' if self.working_context_manager else '未連接'}")
        debug_log(2, f"[TTS] Status Manager: {'已連接' if self.status_manager else '未連接'}")
        debug_log(3, f"[TTS] 完整配置: {self.config}")
    
    def initialize(self):
        """初始化 TTS 模組，加載 IndexTTS 引擎和角色"""
        try:
            info_log("[TTS] 初始化 IndexTTS Lite 引擎...")
            
            # 初始化 IndexTTS 引擎
            # ✅ 啟用 CUDA kernel 以加速 BigVGAN (需要 CUDA Toolkit)
            self.engine = IndexTTSLite(
                cfg_path=os.path.join(self.model_dir, "config.yaml"),
                model_dir=self.model_dir,
                use_fp16=self.use_fp16,
                device=self.device,
                use_cuda_kernel=True  # 🚀 加速 15-25%
            )
            
            info_log(f"[TTS] IndexTTS 引擎初始化成功")
            info_log(f"[TTS] 設備: {self.device}, FP16: {self.use_fp16}")
            
            # 加載預設角色
            character_path = os.path.join(self.character_dir, f"{self.default_character}.pt")
            if os.path.exists(character_path):
                self.engine.load_character(character_path)
                info_log(f"[TTS] 已加載角色: {self.default_character}")
            else:
                error_log(f"[TTS] 找不到角色檔案: {character_path}")
                return False
            
            info_log(f"[TTS] Chunking 已{'啟用' if self.chunking_enabled else '禁用'}, 閾值: {self.chunking_threshold} 字符")
            
            return True
            
        except Exception as e:
            error_log(f"[TTS] 初始化失敗: {str(e)}")
            import traceback
            debug_log(1, f"[TTS] 錯誤詳情:\n{traceback.format_exc()}")
            return False
    
    def get_playback_state(self) -> PlaybackState:
        """
        獲取當前音頻播放狀態
        
        Returns:
            PlaybackState: 當前播放狀態
        """
        # 檢查播放對象是否仍在播放
        if self._current_playback_obj and sa:
            if self._current_playback_obj.is_playing():
                self._playback_state = PlaybackState.PLAYING
            else:
                if self._playback_state == PlaybackState.PLAYING:
                    self._playback_state = PlaybackState.COMPLETED
        
        return self._playback_state
    
    def _get_emotion_vector_from_status(self) -> Optional[List[float]]:
        """
        從 Status Manager 獲取當前系統數值並映射為情感向量
        
        Returns:
            Optional[List[float]]: 8D 情感向量，如果沒有 Status Manager 則返回 None
        """
        if not self.status_manager:
            debug_log(3, "[TTS] Status Manager 未連接，使用預設情感")
            return self.default_emotion
        
        try:
            # 從 Status Manager 獲取數值
            status = self.status_manager.get_status()
            mood = status.get("mood", 0.0)  # type: ignore
            pride = status.get("pride", 0.5)  # type: ignore
            helpfulness = status.get("helpfulness", 0.5)  # type: ignore
            boredom = status.get("boredom", 0.0)  # type: ignore
            
            debug_log(3, f"[TTS] Status Manager 數值: mood={mood:.2f}, pride={pride:.2f}, " +
                         f"help={helpfulness:.2f}, boredom={boredom:.2f}")
            
            # 映射為情感向量
            emotion_vector = self.emotion_mapper.map_from_status_manager(
                mood, pride, helpfulness, boredom  # type: ignore
            )
            
            debug_log(3, f"[TTS] 映射情感向量: {[f'{v:.3f}' for v in emotion_vector]}")
            
            return emotion_vector
            
        except Exception as e:
            error_log(f"[TTS] 獲取情感向量失敗: {str(e)}")
            return self.default_emotion
    
    def _get_user_preferences(self) -> Dict[str, Any]:
        """
        從 Working Context Manager 獲取使用者偏好
        
        注意：Working Context Manager 管理多個上下文，這裡我們可以：
        1. 從全局共享數據獲取 TTS 偏好設置
        2. 或者從特定類型的上下文獲取（如 CONVERSATION 上下文）
        
        Returns:
            Dict[str, Any]: 使用者偏好設定
        """
        if not self.working_context_manager:
            debug_log(3, "[TTS] Working Context Manager 未連接，使用預設偏好")
            return {}
        
        try:
            # 從 Working Context Manager 的全局共享數據獲取 TTS 偏好
            # 這個區域可以存儲跨模組的用戶偏好設置
            preferences = {}
            
            # 嘗試獲取 TTS 相關偏好
            # 注意：這裡需要確認 working_context_manager 的實際 API
            # 暫時返回空字典，具體實現需要根據 Working Context 的設計來調整
            
            debug_log(3, f"[TTS] 使用者偏好: {preferences}")
            
            return preferences
            
        except Exception as e:
            error_log(f"[TTS] 獲取使用者偏好失敗: {str(e)}")
            return {}
    
    def _get_current_gs_id(self) -> str:
        """
        獲取當前 General Session ID
        從 working_context 的全局數據中讀取 (由 SystemLoop 設置)
        
        Returns:
            str: 當前 GS ID,如果無法獲取則返回 'unknown'
        """
        try:
            if self.working_context_manager:
                gs_id = self.working_context_manager.global_context_data.get('current_gs_id', 'unknown')
                return gs_id
            return 'unknown'
        except Exception as e:
            error_log(f"[TTS] 獲取 GS ID 失敗: {e}")
            return 'unknown'
    
    def _get_current_cycle_index(self) -> int:
        """
        獲取當前循環計數
        從 working_context 的全局數據中讀取 (由 Controller 在 GS 創建時設置)
        
        Returns:
            int: 當前 cycle_index,如果無法獲取則返回 0（假設為第一個 cycle）
        """
        try:
            if self.working_context_manager:
                cycle_index = self.working_context_manager.global_context_data.get('current_cycle_index', 0)
                return cycle_index
            return 0
        except Exception as e:
            error_log(f"[TTS] 獲取 cycle_index 失敗: {e}")
            return 0
    
    def _on_output_complete(self, result: Dict[str, Any]):
        """
        ✅ 事件驅動版本：TTS 輸出完成回調
        作為輸出層，TTS 完成標誌著一次完整的處理循環結束
        """
        try:
            info_log("[TTS] 輸出層完成，發布事件")
            
            # 檢查合成是否成功
            if result.get("status") != "success":
                debug_log(1, f"[TTS] 合成失敗，狀態: {result.get('status')}")
                return
            
            # 更新 Status Manager (TTS 完成會影響系統狀態)
            if self.status_manager:
                # TTS 成功完成，略微降低 boredom (系統有輸出活動)
                self.status_manager.update_boredom(-0.05, "TTS輸出完成")
                debug_log(3, "[TTS] 已更新 Status Manager")
            
            # ✅ 使用事件總線發布輸出完成事件
            try:
                from core.event_bus import event_bus, SystemEvent
                
                # 獲取當前 GS session_id 和 cycle_index (用於去重)
                session_id = self._get_current_gs_id()
                cycle_index = self._get_current_cycle_index()
                
                output_completion_data = {
                    # Flow-based 去重所需欄位
                    "session_id": session_id,
                    "cycle_index": cycle_index,
                    "layer": "OUTPUT",
                    
                    # 原有數據
                    "tts_result": result,
                    "output_path": result.get("output_path"),
                    "duration": result.get("duration"),
                    "chunk_count": result.get("chunk_count"),
                    "timestamp": time.time(),
                    "source_module": "tts",
                    "completion_type": "output_layer_finished"
                }
                
                event_bus.publish(
                    event_type=SystemEvent.OUTPUT_LAYER_COMPLETE,
                    data=output_completion_data,
                    source="tts"
                )
                
                debug_log(2, f"[TTS] 輸出層完成事件已發布 (session={session_id}, cycle={cycle_index})")
                
            except Exception as event_err:
                error_log(f"[TTS] 發布輸出完成事件失敗: {event_err}")
            
        except Exception as e:
            error_log(f"[TTS] 輸出完成回調失敗: {e}")
    
    def handle(self, data: dict) -> dict:
        """
        處理 TTS 請求 (同步介面)
        
        Args:
            data: TTSInput 字典
            
        Returns:
            dict: TTSOutput 字典
        """
        try:
            inp = TTSInput(**data)
        except Exception as e:
            return TTSOutput(
                status="error",
                success=False,
                message=f"Invalid input: {e}",
                output_path=None,
                is_chunked=False,
                chunk_count=0
            ).dict()
        
        text = inp.text
        if not text:
            return TTSOutput(
                status="error",
                success=False,
                message="Text is required",
                output_path=None,
                is_chunked=False,
                chunk_count=0
            ).dict()
        
        # 決定是否使用 chunking
        should_chunk = (
            self.chunking_enabled and 
            (len(text) > self.chunking_threshold or inp.force_chunking)
        )
        
        debug_log(2, f"[TTS] 處理文本: 長度={len(text)}, chunking={'是' if should_chunk else '否'}")
        
        # 在新的 event loop 中運行異步方法
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        if should_chunk:
            result = loop.run_until_complete(
                self._handle_streaming(text, inp.save, inp.save_name, character=inp.character, emotion_vector=inp.emotion_vector)
            )
        else:
            result = loop.run_until_complete(
                self._handle_single(text, inp.save, inp.save_name, inp.character, inp.emotion_vector)
            )
        
        # TTS 作為輸出層完成後，通知系統進行循環結束檢查
        self._on_output_complete(result)
        
        return result
    
    async def _handle_single(
        self, 
        text: str, 
        save: bool, 
        save_name: Optional[str] = None,
        character: Optional[str] = None,
        emotion_vector: Optional[List[float]] = None
    ) -> dict:
        """
        處理單段文本合成
        
        Args:
            text: 要合成的文本
            save: 是否保存到文件
            character: 角色名稱 (None 使用當前角色)
            emotion_vector: 情感向量 (None 則從 Status Manager 獲取)
            
        Returns:
            dict: TTSOutput 字典
        """
        try:
            # 檢查引擎是否已初始化
            if not self.engine:
                error_log("[TTS] 引擎未初始化")
                return TTSOutput(
                    status="error",
                    success=False,
                    message="Engine not initialized",
                    output_path=None,
                    is_chunked=False,
                    chunk_count=0
                ).model_dump()
            
            # 切換角色 (如果指定)
            if character and character != self.default_character:
                character_path = os.path.join(self.character_dir, f"{character}.pt")
                if os.path.exists(character_path):
                    self.engine.load_character(character_path)
                    debug_log(2, f"[TTS] 切換到角色: {character}")
                else:
                    error_log(f"[TTS] 找不到角色: {character}")
            
            # 獲取情感向量
            if emotion_vector is None:
                emotion_vector = self._get_emotion_vector_from_status()
            
            # 獲取使用者偏好
            preferences = self._get_user_preferences()
            
            # 準備輸出路徑
            output_path = None
            if save:
                output_path = os.path.join("outputs", "tts", f"{save_name or f'uep_{uuid.uuid4().hex[:8]}'}.wav")
            else:
                output_path = os.path.join("temp", "tts", f"temp_{uuid.uuid4().hex[:8]}.wav")
            
            info_log(f"[TTS] 合成單段文本: {len(text)} 字符")
            
            # 使用引擎合成
            async with self._playback_lock:
                self._playback_state = PlaybackState.PLAYING
                
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.engine.synthesize,
                    text,
                    output_path,
                    emotion_vector
                )
            
            if not result or not os.path.exists(output_path):
                self._playback_state = PlaybackState.ERROR
                return TTSOutput(
                    status="error",
                    success=False,
                    message="Synthesis failed",
                    output_path=None,
                    is_chunked=False,
                    chunk_count=0
                ).model_dump()
            
            # 播放音頻 (如果不保存)
            if not save and sa:
                data, sr = sf.read(output_path)
                # 轉換為 int16
                data_int16 = (data * 32767).astype(np.int16)
                self._current_playback_obj = sa.play_buffer(
                    data_int16.tobytes(),
                    num_channels=1,
                    bytes_per_sample=2,
                    sample_rate=sr
                )
                self._current_playback_obj.wait_done()
                
                # 播放完畢後刪除臨時文件
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                        debug_log(2, f"[TTS] 已刪除臨時文件: {output_path}")
                except Exception as e:
                    debug_log(1, f"[TTS] 刪除臨時文件失敗: {e}")
            
            self._playback_state = PlaybackState.COMPLETED
            
            final_path = output_path if save else None
            info_log(f"[TTS] 合成完成: {output_path}")
            
            return TTSOutput(
                status="success",
                success=True,
                message="TTS completed",
                output_path=final_path,
                is_chunked=False,
                chunk_count=1
            ).model_dump()
            
        except Exception as e:
            self._playback_state = PlaybackState.ERROR
            error_log(f"[TTS] 合成錯誤: {str(e)}")
            import traceback
            debug_log(1, f"[TTS] 錯誤詳情:\n{traceback.format_exc()}")
            return TTSOutput(
                status="error",
                success=False,
                message=f"TTS failed: {str(e)}",
                output_path=None,
                is_chunked=False,
                chunk_count=0
            ).model_dump()

    async def _handle_streaming(
        self,
        text: str,
        save: bool,
        save_name: Optional[str] = None,
        character: Optional[str] = None,
        emotion_vector: Optional[List[float]] = None
    ) -> dict:
        """
        處理串流合成 (Producer/Consumer 模式)
        
        Args:
            text: 要合成的文本
            save: 是否保存到文件
            character: 角色名稱
            emotion_vector: 情感向量
            
        Returns:
            dict: TTSOutput 字典
        """
        generated_files: List[str] = []  # 初始化臨時文件列表(用於錯誤清理)
        
        try:
            # 檢查引擎是否已初始化
            if not self.engine:
                error_log("[TTS] 引擎未初始化")
                return TTSOutput(
                    status="error",
                    success=False,
                    message="Engine not initialized",
                    output_path=None,
                    is_chunked=True,
                    chunk_count=0
                ).model_dump()
            
            # 切換角色
            if character and character != self.default_character:
                character_path = os.path.join(self.character_dir, f"{character}.pt")
                if os.path.exists(character_path):
                    self.engine.load_character(character_path)
            
            # 獲取情感向量
            if emotion_vector is None:
                emotion_vector = self._get_emotion_vector_from_status()
            
            # 使用 TTSChunker 進行智能分段
            info_log(f"[TTS] 開始 chunking: {len(text)} 字符")
            
            # 使用 TTSChunker 的智能分段算法
            chunks = self.chunker.split_text(text)
            info_log(f"[TTS] TTSChunker 分成 {len(chunks)} 段")
            
            # 使用有限緩衝隊列,最多緩衝 2 個音頻段落
            # 這樣可以保持 Producer 領先,減少播放時的停頓
            queue = asyncio.Queue(maxsize=2)
            loop = asyncio.get_event_loop()
            tmp_dir = "temp/tts"
            generated_files = []
            
            # 獲取引擎引用 (for type narrowing)
            engine = self.engine  # Type narrowing: 此時已確認 engine 不是 None
            
            # Producer: 生成音頻段落
            async def producer():
                info_log(f"[TTS] Producer 開始處理 {len(chunks)} 個段落 (緩衝: 2 段)")
                for idx, chunk in enumerate(chunks, 1):
                    try:
                        debug_log(3, f"[TTS] 處理段落 {idx}/{len(chunks)}: {chunk[:50]}...")
                        
                        fname = f"chunk_{idx}_{uuid.uuid4().hex[:8]}.wav"
                        out_path = os.path.join(tmp_dir, fname)
                        
                        # 合成段落
                        await loop.run_in_executor(
                            None,
                            engine.synthesize,
                            chunk,
                            out_path,
                            emotion_vector
                        )
                        
                        if os.path.exists(out_path):
                            generated_files.append(out_path)
                            # 如果隊列已滿,這裡會等待 Consumer 取走一個音頻
                            await queue.put(("success", out_path))
                            debug_log(3, f"[TTS] 段落 {idx} 完成並放入隊列")
                        else:
                            error_log(f"[TTS] 段落 {idx} 合成失敗")
                            await queue.put(("error", f"Chunk {idx} failed"))
                            
                    except Exception as e:
                        error_log(f"[TTS] Producer 錯誤 (段落 {idx}): {str(e)}")
                        await queue.put(("error", str(e)))
                
                # 標記結束
                await queue.put(("done", None))
                info_log(f"[TTS] Producer 完成，共生成 {len(generated_files)} 個段落")
            
            # Consumer: 播放音頻段落
            async def consumer():
                count = 0
                while True:
                    status, data = await queue.get()
                    
                    if status == "done":
                        break
                    elif status == "error":
                        error_log(f"[TTS] Consumer 收到錯誤: {data}")
                        break
                    elif status == "success":
                        count += 1
                        # 播放 (如果不保存)
                        if not save and sa:
                            try:
                                audio, sr = sf.read(data)
                                audio_int16 = (audio * 32767).astype(np.int16)
                                play_obj = sa.play_buffer(
                                    audio_int16.tobytes(), 1, 2, sr
                                )
                                play_obj.wait_done()
                                
                                # 播放完畢後刪除該段臨時文件
                                try:
                                    if os.path.exists(data):
                                        os.remove(data)
                                        debug_log(3, f"[TTS] 已刪除臨時chunk: {data}")
                                except Exception as del_err:
                                    debug_log(1, f"[TTS] 刪除臨時chunk失敗: {del_err}")
                            except Exception as e:
                                error_log(f"[TTS] 播放失敗: {str(e)}")
                
                info_log(f"[TTS] Consumer 完成，已播放 {count} 個段落")
            
            # 啟動 Producer 和 Consumer
            async with self._playback_lock:
                self._playback_state = PlaybackState.PLAYING
                
                producer_task = asyncio.create_task(producer())
                consumer_task = asyncio.create_task(consumer())
                
                await producer_task
                await consumer_task
            
            # 合併音頻 (如果需要保存)
            output_path = None
            if save and generated_files:
                info_log(f"[TTS] 合併 {len(generated_files)} 個音頻檔案")
                buffers = []
                sr = None
                
                for f in generated_files:
                    if os.path.exists(f):
                        data, _sr = sf.read(f)
                        sr = sr or _sr
                        buffers.append(data)
                
                if buffers:
                    merged = np.concatenate(buffers, axis=0)
                    output_path = os.path.join("outputs", "tts", f"{save_name or f'uep_{uuid.uuid4().hex[:8]}'}.wav")
                    sf.write(output_path, merged, sr)
                    info_log(f"[TTS] 已合併音頻到 {output_path}")
                    
                    # 合併完成後刪除原始臨時chunk文件
                    for f in generated_files:
                        try:
                            if os.path.exists(f):
                                os.remove(f)
                                debug_log(3, f"[TTS] 已刪除合併前的chunk: {f}")
                        except Exception as e:
                            debug_log(1, f"[TTS] 刪除chunk失敗: {e}")
            
            self._playback_state = PlaybackState.COMPLETED
            
            return TTSOutput(
                status="success",
                success=True,
                message=f"Streaming completed",
                output_path=output_path,
                is_chunked=True,
                chunk_count=len(chunks)
            ).model_dump()
            
        except Exception as e:
            self._playback_state = PlaybackState.ERROR
            error_log(f"[TTS] Streaming 錯誤: {str(e)}")
            import traceback
            debug_log(1, f"[TTS] 錯誤詳情:\n{traceback.format_exc()}")
            
            # 發生錯誤時清理已生成的臨時文件
            if generated_files:
                debug_log(2, f"[TTS] 清理 {len(generated_files)} 個臨時文件...")
                for f in generated_files:
                    try:
                        if os.path.exists(f):
                            os.remove(f)
                            debug_log(3, f"[TTS] 已刪除: {f}")
                    except Exception as cleanup_err:
                        debug_log(1, f"[TTS] 清理失敗: {cleanup_err}")
            
            return TTSOutput(
                status="error",
                success=False,
                message=f"Streaming failed: {str(e)}",
                output_path=None,
                is_chunked=True,
                chunk_count=0
            ).model_dump()
