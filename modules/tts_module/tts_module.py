import asyncio
import os
import uuid
from core.module_base import BaseModule
from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log
from .schemas import TTSInput, TTSOutput
from .infer_core import run_tts, get_config
from utils.tts_chunker import TTSChunker

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

            return True
        except Exception as e:
            error_log(f"[TTS] Initialization failed: {str(e)}")
            return False
    
    def handle(self, data: dict) -> dict:
        """
        Handle TTS request
        
        Args:
            data: Dictionary containing request data
        
        Returns:
            Dictionary with response data
        """
        # Parse input through Pydantic model for validation
        try:
            input_data = TTSInput(**data)
        except Exception as e:
            error_log(f"[TTS] Input validation failed: {str(e)}")
            return TTSOutput(status="error", message=f"Invalid input: {str(e)}").dict()
        
        # Extract parameters
        text = input_data.text
        
        # Check if we need to use chunking
        if len(text) > self.chunking_threshold:
            info_log(f"[TTS] Text length ({len(text)}) exceeds chunking threshold, using chunking")
            return self.handle_chunked(input_data)
        
        # For shorter text, use the standard processing
        return self.handle_single(input_data)
    
    def handle_single(self, input_data) -> dict:
        """Process a single chunk of text"""
        text = input_data.text
        mood = input_data.mood or self.default_mood
        save = input_data.save
        
        # Convert mood to pitch
        f0_up_key = self.mood_to_pitch(mood)
        
        # Generate filename and output path
        filename = f"uep_{uuid.uuid4().hex[:8]}.wav"
        out_dir = "outputs/tts"
        out_path = os.path.join(out_dir, filename) if save else None
        
        # Log the request
        info_log(f"[TTS] Processing text: '{text[:30]}{'...' if len(text) > 30 else ''}' with mood: {mood}")
        
        try:
            # Run TTS inference
            result = run_tts(
                tts_text=text,
                model_name=self.model_name,
                model_path=self.model_path,
                index_file=self.index_file,
                f0_up_key=f0_up_key,
                speaker_id=self.speaker_id,
                output_path=out_path,
                index_rate=0,    # Default index rate, could be configurable
                protect=0.33,    # Default protection, could be configurable
                speed_rate=-self.speed_rate,    # Default speed rate, could be configurable
            )
            
            # Return the result
            return TTSOutput(
                status=result["status"],
                output_path=result["output_path"],
                message=result["message"]
            ).dict()
            
        except Exception as e:
            error_log(f"[TTS] Synthesis error: {str(e)}")
            return TTSOutput(status="error", message=f"TTS failed: {str(e)}").dict()

    async def handle_chunked(self, input_data) -> dict:
        """Process text by splitting into chunks and processing sequentially"""
        text = input_data.text
        mood = input_data.mood or self.default_mood
        save = input_data.save
        
        # Convert mood to pitch
        f0_up_key = self.mood_to_pitch(mood)
        
        # Prepare arguments for TTS processor
        tts_args = {
            "mood": mood,
            "save": save  # Only the final chunk will be saved if requested
        }
        
        # Create a wrapper function to handle individual chunks
        def process_chunk(text, mood, save):
            return self._process_tts_chunk(text, mood, save)
        
        try:
            # Create a new event loop if one doesn't exist
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                # No event loop exists in this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            result = await self.chunker.process_text(text, process_chunk, tts_args)
            
            # Generate a filename for the whole text if saving was requested
            output_path = None
            if save:
                filename = f"uep_{uuid.uuid4().hex[:8]}.wav"
                out_dir = "outputs/tts"
                output_path = os.path.join(out_dir, filename)
                # Note: In a real implementation, you might want to save the final result
                # by concatenating all the audio chunks
            
            return TTSOutput(
                status="processing",
                output_path=output_path,
                message=f"Processing {result.get('chunk_count', 0)} chunks sequentially",
                is_chunked=True,
                chunk_count=result.get('chunk_count', 0)
            ).dict()
            
        except Exception as e:
            error_log(f"[TTS] Chunked processing error: {str(e)}")
            return TTSOutput(status="error", message=f"Chunked TTS failed: {str(e)}").dict()
    
    async def _process_tts_chunk(self, text, mood, save):
        """Process a single chunk for the chunking system"""
        f0_up_key = self.mood_to_pitch(mood)
    
        # We don't save intermediate chunks
        out_path = None
    
        try:
            result = run_tts(
                tts_text=text,
                model_name=self.model_name,
                model_path=self.model_path,
                index_file=self.index_file,
                f0_up_key=f0_up_key,
                speaker_id=self.speaker_id,
                output_path=out_path,
                index_rate=0,
                protect=0.33,
                speed_rate=-self.speed_rate,
            )
            return result
        
        except Exception as e:
            error_log(f"[TTS] Chunk processing error: {str(e)}")
            return {
                "status": "error",
                "output_path": None,
                "message": str(e)
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