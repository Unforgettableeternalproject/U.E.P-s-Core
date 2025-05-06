import os
import edge_tts
import torch
import torch.backends.cudnn as cudnn
import asyncio
import librosa
import time
import numpy as np
import tempfile 
from typing import Tuple, Optional, Dict, Any, Union
from fairseq import checkpoint_utils
from fairseq.data.dictionary import Dictionary
import simpleaudio as sa
from utils.debug_helper import info_log, error_log
from .lib.infer_pack.models import (
    SynthesizerTrnMs256NSFsid,
    SynthesizerTrnMs256NSFsid_nono,
    SynthesizerTrnMs768NSFsid,
    SynthesizerTrnMs768NSFsid_nono,
)
from .rmvpe import RMVPE
from .vc_infer_pipeline import VC
from .config import Config
import torch.serialization
from utils.debug_helper import debug_log, debug_log_e
import soundfile as sf

cudnn.benchmark = True

# Singleton instances for models to avoid reloading
_hubert_model = None
_rmvpe_model = None
_config = None

def safe_load(path, device):
    return torch.load(path, map_location=device, weights_only=False)

def get_config() -> Config:
    """Get or initialize the config singleton"""
    global _config
    if _config is None:
        _config = Config()
    return _config

def get_hubert_model(device: str, is_half: bool):
    """Get or initialize the hubert model singleton"""
    global _hubert_model
    if _hubert_model is None:
        info_log("[TTS] Loading Hubert model...")
        try:
            with torch.serialization.safe_globals([Dictionary]):
                models, _, _ = checkpoint_utils.load_model_ensemble_and_task(
                    ["./models/tts/hubert_base.pt"],
                    arg_overrides=None,
                    strict=False
                )
            hubert_model = models[0].to(device)
            _hubert_model = hubert_model.half() if is_half else hubert_model.float()
            info_log("[TTS] Hubert model loaded successfully")
        except Exception as e:
            error_log(f"[TTS] Failed to load Hubert model: {e}")
            raise
    return _hubert_model

def get_rmvpe_model(model_path: str, device: str, is_half: bool):
    """Get or initialize the RMVPE model singleton"""
    global _rmvpe_model
    if _rmvpe_model is None:
        info_log("[TTS] Loading RMVPE model...")
        try:
            _rmvpe_model = RMVPE(model_path, is_half, device)
            info_log("[TTS] RMVPE model loaded successfully")
        except Exception as e:
            error_log(f"[TTS] Failed to load RMVPE model: {e}")
            raise
    return _rmvpe_model

def load_model_data(model_name: str, model_path: str, index_file: Optional[str] = None) -> Tuple:
    """
    Load RVC model and its configurations
    
    Args:
        model_name: Name of the model
        model_path: Path to the model directory
        index_file: Optional path to the index file
        
    Returns:
        Tuple of (tgt_sr, net_g, vc, version, index_file, if_f0)
    """
    config = get_config()
    info_log(f"[TTS] Loading model: {model_name}")
    
    try:
        #model_dir = os.path.join(model_path, model_name)
        model_dir = model_path
        pth_files = [
            os.path.join(model_dir, f)
            for f in os.listdir(model_dir)
            if f.endswith(".pth")
        ]
        
        if len(pth_files) == 0:
            error_log(f"[TTS] No PTH file found in {model_dir}")
            raise ValueError(f"No PTH file found in {model_dir}")
            
        pth_path = pth_files[0]
        info_log(f"[TTS] Loading {pth_path}")
        
        cpt = torch.load(pth_path, map_location="cpu", weights_only=True)
        tgt_sr = cpt["config"][-1]
        cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]  # n_spk
        if_f0 = cpt.get("f0", 1)
        version = cpt.get("version", "v1")
        
        # Initialize the appropriate network
        if version == "v1":
            if if_f0 == 1:
                net_g = SynthesizerTrnMs256NSFsid(*cpt["config"], is_half=config.is_half)
            else:
                net_g = SynthesizerTrnMs256NSFsid_nono(*cpt["config"])
        elif version == "v2":
            if if_f0 == 1:
                net_g = SynthesizerTrnMs768NSFsid(*cpt["config"], is_half=config.is_half)
            else:
                net_g = SynthesizerTrnMs768NSFsid_nono(*cpt["config"])
        else:
            error_log(f"[TTS] Unknown version: {version}")
            raise ValueError(f"Unknown version: {version}")
            
        # Remove encoder query
        del net_g.enc_q
        
        # Load weights
        net_g.load_state_dict(cpt["weight"], strict=False)
        net_g.eval().to(config.device)
        
        if config.is_half:
            net_g = net_g.half()
        else:
            net_g = net_g.float()
            
        # Initialize VC
        vc = VC(tgt_sr, config)
        
        # Use provided index file or search for one
        if index_file:
            info_log(f"[TTS] Using provided index file: {index_file}")
        else:
            index_files = [
                os.path.join(model_dir, f)
                for f in os.listdir(model_dir)
                if f.endswith(".index")
            ]
            
            if len(index_files) == 0:
                info_log("[TTS] No index file found, proceeding without index")
                index_file = ""
            else:
                index_file = index_files[0]
                info_log(f"[TTS] Found index file: {index_file}")
                
        info_log(f"[TTS] Model loaded successfully: {model_name}")
        return tgt_sr, net_g, vc, version, index_file, if_f0
            
    except Exception as e:
        error_log(f"[TTS] Error loading model: {str(e)}")
        raise

async def generate_edge_tts_audio(text: str, voice: str = "en-US-AvaNeural", rate: float = 0, output_filename: str = None) -> str:
    """
    Generate audio using edge-tts
    
    Args:
        text: Text to synthesize
        voice: Voice to use for synthesis
        output_filename: Path to save the output file
        
    Returns:
        Path to the generated audio file
    """
    if output_filename is None:
        output_filename = os.path.join(os.getcwd(), "temp", "tts", "edge_output.mp3")
    
    # Ensure directory exists
    os.makedirs(os.path.dirname(output_filename), exist_ok=True)
    
    # Turn rate into formatted string (+/-n%)
    speed = f"+{int(rate * 10)}%" if rate > 1 else f"-{int((1 - rate) * 10)}%"

    info_log(f"[TTS] Generating pre-process audio with edge-tts...")
    try:
        await edge_tts.Communicate(text, voice, rate=speed).save(output_filename)
        info_log(f"[TTS] Edge-TTS audio saved to {output_filename}")
        return output_filename
    except Exception as e:
        error_log(f"[TTS] Error with edge-tts: {str(e)}")
        raise

async def run_tts(
    tts_text: str,
    model_name: str,
    model_path: str,
    index_file: Optional[str] = None,
    output_path: Optional[str] = None,
    f0_up_key: int = 0,
    f0_method: str = "rmvpe",
    index_rate: float = 0,
    protect: float = 0.33,
    speaker_id: int = 0,
    speed_rate: float = 0,
    voice: str = "en-US-AvaNeural",
) -> Dict[str, Any]:
    """
    Run the TTS pipeline
    
    Args:
        tts_text: Text to synthesize
        model_name: Name of the model to use
        model_path: Path to the model directory
        index_file: Path to the index file (optional)
        output_path: Path to save the output audio
        f0_up_key: Pitch shift amount
        f0_method: F0 extraction method
        index_rate: Index rate for voice conversion
        protect: Protection value for voice conversion
        speaker_id: Speaker ID to use
        voice: Voice to use for initial TTS
        speed_rate: Speed rate for initial TTS
        loop: Event loop for async operations (optional)
    Returns:
        Dictionary with status, output_path, and message
    """
    config = get_config()
    
    try:
        # Load model data
        t0 = time.time()
        tgt_sr, net_g, vc, version, model_index, if_f0 = load_model_data(model_name, model_path, index_file)
        
        # If we have an index_file from parameters, use it instead
        if index_file:
            model_index = index_file

        edge_output_filename = await generate_edge_tts_audio(tts_text, voice, speed_rate)   

        t1 = time.time()
        edge_time = t1 - t0
        
        # Load audio from edge-tts
        audio, sr = librosa.load(edge_output_filename, sr=16000, mono=True)
        duration = len(audio) / sr
        info_log(f"[TTS] Audio loaded from edge-tts, duration: {duration:.2f}s")
        
        # Load models if not already loaded
        hubert_model = get_hubert_model(config.device, config.is_half)
        
        if f0_method == "rmvpe":
            rmvpe_model = get_rmvpe_model("./models/tts/rmvpe.pt", config.device, config.is_half)
            vc.model_rmvpe = rmvpe_model
        
        # Run voice conversion pipeline
        info_log("[TTS] Starting voice conversion...")
        times = [0, 0, 0]
        with torch.inference_mode():
            audio_opt, new_sr = vc.pipeline(
                hubert_model,
                net_g,
                speaker_id,
                audio,
                edge_output_filename,
                times,
                f0_up_key,
                f0_method,
                model_index,
                index_rate,
                if_f0,
                3,  # Filter radius
                tgt_sr,
                0,  # resample_sr
                0.25,  # rms_mix_rate
                version,
                protect,
                None,
            )
       
        if output_path:
            # 儲存檔案
            sf.write(output_path, audio_opt, new_sr)
            info_log(f"[TTS] Audio saved to {output_path}")
            return {
                "status": "success",
                "output_path": output_path,
                "message": "TTS completed and file saved."
            }
        else:
            # 純記憶體播放，結束後自動捨棄
            return {
                "status": "success",
                "audio_buffer": audio_opt,  # float32 array, [-1,1]
                "sr": new_sr,
                "message": "TTS completed, buffer ready."
            }

    except Exception as e:
        error_log(f"[TTS] Error during TTS: {str(e)}")
        # Ensure temporary files are cleaned up in case of error
        if 'edge_output_filename' in locals() and os.path.exists(edge_output_filename):
            try:
                os.remove(edge_output_filename)
                info_log(f"[TTS] Removed temporary edge-tts file due to error: {edge_output_filename}")
            except:
                pass
        return {
            "status": "error",
            "output_path": None,
            "message": str(e)
        }
