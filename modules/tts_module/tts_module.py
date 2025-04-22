import os
import uuid
from core.module_base import BaseModule
from configs.config_loader import load_module_config
from utils.debug_helper import info_log, error_log
from .schemas import TTSInput, TTSOutput
from .infer_core import run_tts, get_config

class TTSModule(BaseModule):    
    def __init__(self, config=None):
        self.config = config or load_module_config("tts_module")
        self.model_name = self.config.get("model_name", "U.E.P_normal")
        self.model_path = self.config.get("model_path", "./models/tts")
        self.index_file = self.config.get("index_file")
        self.speaker_id = self.config.get("speaker_id", 0)
        self.default_mood = self.config.get("default_mood", "neutral")
        self.pitch_map = self.config.get("pitch_map", {
            "happy": 2,
            "excited": 3,
            "sad": -2,
            "angry": 1,
            "calm": -1,
            "neutral": 0
        })
        
        os.makedirs(os.path.join("temp", "tts"), exist_ok=True)
        
    def initialize(self):
        """Initialize the TTS module and load configurations"""
        try:
            # Initialize the config from infer_core
            core_config = get_config()
            
            # Log initialization
            info_log(f"[TTS] Module initialized with model: {self.model_name}")
            info_log(f"[TTS] Using device: {core_config.device}, half precision: {core_config.is_half}")
            
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
        mood = input_data.mood or self.default_mood
        save = input_data.save
        
        # Convert mood to pitch
        f0_up_key = self.mood_to_pitch(mood)
        
        # Generate filename and output path
        filename = f"uep_{uuid.uuid4().hex[:8]}.wav"
        out_dir = "outputs/tts" if save else "temp/tts"
        out_path = os.path.join(out_dir, filename)
        
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
                index_rate=0.5,  # Default index rate, could be configurable
                protect=0.33,    # Default protection, could be configurable
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
    
    def mood_to_pitch(self, mood: str) -> int:
        """
        Convert mood to pitch value
        
        Args:
            mood: Mood string
            
        Returns:
            Pitch value for the mood
        """
        return self.pitch_map.get(mood.lower(), 0)
    
    def shutdown(self):
        """Clean up resources when shutting down"""
        info_log("[TTS] Module shutting down")
        
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