"""
Emotion Presets loader — 從 YAML 載入情緒預設定義。

YAML 格式支援兩種 entry：
  1. 簡單格式（只有 vector，向後相容）：
       happy:
         vector: [0.3, 0.0, ...]

  2. 完整格式（帶 overrides）：
       flustered:
         vector: [0.05, 0.0, ...]
         overrides:
           speed_variation: 0.12
           text_markers:
             zh: { prefix_particles: [...], ... }
             en: { prefix_particles: [...], ... }
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# 8 基礎情緒維度名稱
EMOTION_LABELS = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]

# 預設 YAML 路徑（相對於此檔案所在目錄）
_DEFAULT_YAML = Path(__file__).resolve().parent / "emotion_presets.yaml"

# ── 型別別名 ──
PresetEntry = list[float] | dict[str, Any]


def load_presets(path: str | Path | None = None) -> dict[str, PresetEntry]:
    """載入 emotion presets YAML。

    Args:
        path: YAML 檔案路徑。None 則使用預設位置。

    Returns:
        dict mapping preset name → PresetEntry (list or dict with vector/overrides)

    Raises:
        FileNotFoundError: YAML 檔案不存在時
    """
    yaml_path = Path(path) if path is not None else _DEFAULT_YAML
    if not yaml_path.exists():
        raise FileNotFoundError(f"Emotion presets file not found: {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    presets: dict[str, PresetEntry] = {}
    for name, entry in raw.items():
        if isinstance(entry, dict) and "vector" in entry:
            vec = entry["vector"]
            if not isinstance(vec, list) or len(vec) != 8:
                raise ValueError(f"Preset '{name}': vector must be a list of 8 floats, got {vec}")
            overrides = entry.get("overrides")
            if overrides:
                presets[name] = {"vector": vec, "overrides": overrides}
            else:
                presets[name] = vec
        elif isinstance(entry, list) and len(entry) == 8:
            # 純 list 格式（向後相容）
            presets[name] = entry
        else:
            raise ValueError(f"Preset '{name}': invalid format. Expected list[8] or dict with 'vector' key.")

    return presets


def get_preset_vector(entry: PresetEntry) -> list[float]:
    """Extract the 8D emotion vector from a preset entry."""
    if isinstance(entry, dict):
        return entry["vector"]
    return entry


def get_preset_overrides(entry: PresetEntry) -> dict | None:
    """Extract dynamics overrides from a preset entry, or None."""
    if isinstance(entry, dict):
        return entry.get("overrides")
    return None
