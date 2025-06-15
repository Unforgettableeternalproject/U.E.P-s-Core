import asyncio
import os
import uuid
from core.module_base import BaseModule
from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log
from .schemas import TTSInput, TTSOutput
from .infer_core import run_tts, get_config
from utils.tts_chunker import TTSChunker
import numpy as np
import soundfile as sf
import simpleaudio as sa

class TTSModule(BaseModule):    
    def __init__(self, config=None):
        self.config = config or load_module_config("tts_module")
        self.model_name = self.config.get("model_name", "U.E.P_normal")
        self.model_path = self.config.get("model_path", "./models/tts")
        self.index_file = self.config.get("index_file")
        self.speaker_id = self.config.get("speaker_id", 0)
        self.default_mood = self.config.get("default_mood", "neutral")
        self.speed_rate = self.config.get("speed_rate", -0.1)
        self.pitch_map = self.config.get("pitch_map", {
            "happy": 2,
            "excited": 3,
            "sad": -2,
            "angry": 1,
            "calm": -1,
            "neutral": 0
        })

        chunking_config = self.config.get("chunking", {})
        self.max_chars = chunking_config.get("max_chars", 150)
        self.min_chars = chunking_config.get("min_chars", 50)
        self.chunker = TTSChunker(
            max_chars=self.max_chars,
            min_chars=self.min_chars,
            respect_punctuation=chunking_config.get("respect_punctuation", True),
            pause_between_chunks=chunking_config.get("pause_between_chunks", 0.2)
        )
        
        # Thresholds for chunking
        self.chunking_threshold = chunking_config.get("chunking_threshold", 200)  # Characters
        
        os.makedirs(os.path.join("temp", "tts"), exist_ok=True)
        os.makedirs(os.path.join("outputs", "tts"), exist_ok=True)
        
        os.makedirs(os.path.join("temp", "tts"), exist_ok=True)
        os.makedirs(os.path.join("outputs", "tts"), exist_ok=True)

    def debug(self):
        # Debug level = 1
        debug_log(1, "[TTS] Debug 模式啟用")
        # Debug level = 2
        debug_log(2, f"[TTS] 模組名稱: {self.model_name}")
        debug_log(2, f"[TTS] 模型路徑: {self.model_path}")
        debug_log(2, f"[TTS] 索引檔案: {self.index_file}")
        debug_log(2, f"[TTS] 語者 ID: {self.speaker_id}")
        debug_log(2, f"[TTS] 預設情緒: {self.default_mood}")
        debug_log(2, f"[TTS] 語速調整: {f'+{int(self.speed_rate * 10)}%' if self.speed_rate > 1 else f'-{int((1 - self.speed_rate) * 10)}%'}")
        # Debug level = 3
        debug_log(3, f"[TTS] 模組設定: {self.config}")
        
    def initialize(self):
        """Initialize the TTS module and load configurations"""
        try:
            # Initialize the config from infer_core
            core_config = get_config()
            
            # Log initialization
            info_log(f"[TTS] Module initialized with model: {self.model_name}")
            info_log(f"[TTS] Using device: {core_config.device}, half precision: {core_config.is_half}")
            info_log(f"[TTS] Chunking enabled with max {self.max_chars} chars per chunk")

            # Warm up the TTS model
            info_log(f"[TTS] Warming up TTS model...")
            try:
                asyncio.run(
                    run_tts(
                        tts_text="This is for the warm-up.",
                        model_name=self.model_name,
                        model_path=self.model_path,
                        index_file=self.index_file,
                        f0_up_key=7,
                        speaker_id=self.speaker_id,
                        output_path=None,
                        speed_rate=-self.speed_rate
                    )
                )
            except Exception as e:
                error_log(f"[TTS] Warm up failed: {str(e)}")
                return False

            return True
        except Exception as e:
            error_log(f"[TTS] Initialization failed: {str(e)}")
            return False
    
    async def handle(self, data: dict) -> dict:
        try:
            inp = TTSInput(**data)
        except Exception as e:
            return TTSOutput(status="error", message=f"Invalid input: {e}").dict()

        text, mood, save, fc = inp.text, inp.mood or self.default_mood, inp.save, inp.force_chunking

        if not text:
            return TTSOutput(status="error", message="Text is required").dict()

        if len(text) > self.chunking_threshold or fc:
            return await self.handle_streaming(text, mood, save)
        else:
            return await self.handle_single(text, mood, save)
    
    async def handle_single(self, text, mood, save) -> dict:
        f0 = self.pitch_map.get(mood.lower(), 0)
        out_path = None
        try:
            if save:
                fname = f"uep_{uuid.uuid4().hex[:8]}.wav"
                out_dir = "outputs/tts"
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, fname)
        
            info_log(f"[TTS] 處理單段文字，長度={len(text)}")
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: asyncio.run(
                    run_tts(
                        tts_text=text,
                        model_name=self.model_name,
                        model_path=self.model_path,
                        index_file=self.index_file,
                        f0_up_key=f0,
                        speaker_id=self.speaker_id,
                        output_path=out_path,
                        speed_rate=-self.speed_rate,
                        # voice="zh-TW-HsiaoChenNeural" # For Chinese Voice Purpose, uncomment this line.
                        voice="ja-JP-NanamiNeural" # For Japanese Voice Purpose, uncomment this line.
                    )
                )
            )

            if result.get("status") != "success":
                return TTSOutput(
                    status="error",
                    message=result.get("message", "Unknown error")
                ).dict()

            if not save:
                 audio = result["audio_buffer"]  # float32 ndarray in [-1,1]
                 sr    = result["sr"]
                 play_obj = sa.play_buffer(
                    audio.tobytes(),
                    num_channels=1,
                    bytes_per_sample=2,
                    sample_rate=sr
                 )
                 play_obj.wait_done()

            return TTSOutput(
                status="success",
                output_path=out_path,
                message="TTS completed and played."
            ).dict()
        except Exception as e:
            error_log(f"[TTS] Synthesis error: {str(e)}")
            return TTSOutput(status="error", message=f"TTS failed: {str(e)}").dict()

    async def handle_streaming(self, text, mood, save) -> dict:
        f0 = self.pitch_map.get(mood.lower(), 0)
        chunks = self.chunker.split_text(text)

        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        tmp_dir = "temp/tts"
        generated_files = []

        # 同步執行緒中生成 chunk WAV 檔
        def gen_chunk(idx, chunk_text):
            fname = f"chunk_{idx}_{uuid.uuid4().hex[:8]}.wav"
            out_path = os.path.join(tmp_dir, fname) if save else None
            result = asyncio.run(
                run_tts(
                    tts_text=chunk_text,
                    model_name=self.model_name,
                    model_path=self.model_path,
                    index_file=self.index_file,
                    f0_up_key=f0,
                    speaker_id=self.speaker_id,
                    output_path=out_path,
                    speed_rate=-self.speed_rate,
                    # voice="zh-TW-HsiaoYuNeural"
                    voice="ja-JP-NanamiNeural"
                )
            )
            return result, fname, out_path  # 返回更多資訊以便追蹤

        async def producer():
            info_log(f"[TTS] 開始處理 {len(chunks)} 個文本段落")
            for idx, chunk in enumerate(chunks, 1):
                info_log(f"[TTS] 處理第 {idx}/{len(chunks)} 段")
                result, fname, out_path = await loop.run_in_executor(None, gen_chunk, idx, chunk)
                if result["status"] != "success":
                    await queue.put({"error": result["message"]})
                    return
                
                if save:
                    path = result["output_path"]
                    generated_files.append(path)
                    info_log(f"[TTS] 已生成檔案 {path}")
                    await queue.put(path)
                else:
                    await queue.put(result)
            # 標記結束
            info_log(f"[TTS] 所有段落處理完成")
            await queue.put(None)

        async def consumer():
            count = 0
            while True:
                item = await queue.get()
                if item is None:
                    break
                if "error" in item:
                    error_log(f"TTS 產生失敗：{item['error']}")
                    break
                count += 1
                if not save:
                    audio, sr = item["audio_buffer"], item["sr"]
                    play_obj = sa.play_buffer(audio.tobytes(), 1, 2, sr)
                    play_obj.wait_done()
            info_log(f"[TTS] 已處理 {count} 個段落")

        # 等待兩個任務都完成
        producer_task = asyncio.create_task(producer())
        consumer_task = asyncio.create_task(consumer())
        await producer_task
        await consumer_task

        # 最後如果要存檔，確保所有檔案都已生成後再合併
        output_path = None
        if save and generated_files:
            info_log(f"[TTS] 合併 {len(generated_files)} 個音訊檔案")
            buffers, sr = [], None
            for f in generated_files:
                if os.path.exists(f):  # 確認檔案存在
                    data, _sr = sf.read(f)
                    sr = sr or _sr
                    buffers.append(data)
            
            if buffers:  # 確保有檔案可合併
                merged = np.concatenate(buffers, axis=0)
                out_dir = "outputs/tts"
                os.makedirs(out_dir, exist_ok=True)
                fname = f"uep_{uuid.uuid4().hex[:8]}.wav"
                output_path = os.path.join(out_dir, fname)
                sf.write(output_path, merged, sr)
                info_log(f"[TTS] 已合併音訊到 {output_path}")

        return {
            "status": "success",
            "output_path": output_path,
            "message": f"串流處理完成，共 {len(chunks)} 段",
            "chunk_count": len(chunks),
            "is_streaming": True
        }

    
    def mood_to_pitch(self, mood: str) -> int:
        """
        Convert mood to pitch value
        
        Args:
            mood: Mood string
            
        Returns:
            Pitch value for the mood
        """
        return self.pitch_map.get(mood.lower(), 0)
    
    def get_queue_status(self) -> dict:
        """Get current status of the TTS queue"""
        status = self.chunker.get_queue_status()
        return {
            "is_playing": status["is_playing"],
            "queue_length": status["queue_length"]
        }
    
    def stop_playback(self) -> dict:
        """Stop current playback and clear the queue"""
        self.chunker.stop()
        return {
            "status": "success",
            "message": "TTS playback stopped and queue cleared"
        }
    
    def shutdown(self):
        """Clean up resources when shutting down"""
        info_log("[TTS] Module shutting down")
        
        # Stop any ongoing TTS processes
        self.chunker.stop()
        
        # Clean up temp files
        try:
            temp_dir = os.path.join("temp", "tts")
            if os.path.exists(temp_dir):
                for file in os.listdir(temp_dir):
                    if file.endswith((".wav", ".mp3")):
                        try:
                            os.remove(os.path.join(temp_dir, file))
                        except:
                            pass
        except Exception as e:
            error_log(f"[TTS] Error during cleanup: {str(e)}")