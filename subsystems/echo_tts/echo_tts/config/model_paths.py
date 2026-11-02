"""TTS 權重位置解析（權重不進 git）。

- ``UEP_TTS_MODELS_DIR``：VOX 權重根目錄，內含 ``VoxCPM2/`` 與 ``lora_tw/step_0000600/``。
  預設 ``%USERPROFILE%/.uep/models/vox``。
- ``UEP_TTS_INDEXTTS_DIR``：IndexTTS2 權重根目錄，版面與遷入前 TTS repo 的 ``echo_tts/`` 相同
  （``modules/tts_module/checkpoints_v25/``、``models/tts/uep_v25.pt``）。
  未設定時沿用套件內建位置（遷入前行為）；舊 TTS repo 的權重不需搬動，把此變數指到舊 ``echo_tts/`` 目錄即可。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_MODELS_DIR = "UEP_TTS_MODELS_DIR"
ENV_INDEXTTS_DIR = "UEP_TTS_INDEXTTS_DIR"

VOX_MODEL_SUBDIR = "VoxCPM2"
VOX_LORA_SUBDIR = Path("lora_tw") / "step_0000600"


def vox_models_dir() -> Path:
    raw = os.environ.get(ENV_MODELS_DIR, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".uep" / "models" / "vox"


def default_vox_paths() -> tuple[Path, Path]:
    """回傳預設的 (VoxCPM2 模型目錄, LoRA checkpoint 目錄)。"""
    root = vox_models_dir()
    return root / VOX_MODEL_SUBDIR, root / VOX_LORA_SUBDIR


def with_default_vox_paths(config: dict | None) -> dict:
    """補上 VoxBackend config 缺少的 ``model_dir``／``lora_dir``（明確給的值優先）。"""
    merged = dict(config or {})
    model_dir, lora_dir = default_vox_paths()
    if not merged.get("model_dir"):
        merged["model_dir"] = str(model_dir)
    if not merged.get("lora_dir"):
        merged["lora_dir"] = str(lora_dir)
    return merged


def indextts_weights_root(engine_root: Path) -> Path:
    """IndexTTS2 權重根目錄：``UEP_TTS_INDEXTTS_DIR`` 優先，否則為套件內建位置 ``engine_root``。"""
    raw = os.environ.get(ENV_INDEXTTS_DIR, "").strip()
    return Path(raw).expanduser() if raw else engine_root
