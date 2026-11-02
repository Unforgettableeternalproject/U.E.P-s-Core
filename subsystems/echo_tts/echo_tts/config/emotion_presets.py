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

# ── 情緒強度區間（2.5 聽感定案 2026-08-19；2026-11-01 上緣由 0.73 收斂到 0.65）──
# preset 的 vector 定義的是「情緒配方」（各維度的相對比例），
# 實際送進模型的強度由這裡的區間決定。可用範圍 0.57 ~ 0.65：
# 低於 0.57 情緒表現不足，高於 0.65 開始蓋掉角色音色。
# STRENGTH_DEFAULT（0.65）是引擎預設總和（max_strength），不等於 user 0.5 的對應值。
STRENGTH_DEFAULT = 0.65
STRENGTH_MIN = 0.57
STRENGTH_MAX = 0.65

# 使用者面向的刻度：0.0 ~ 1.0 線性映射到上面的區間。
# 0.5 落在區間中點 0.61（0.57 與 0.65 的中點），所以預設 0.5。
USER_STRENGTH_DEFAULT = 0.5


def clamp_strength(value: float) -> float:
    """把任意強度值夾進定案區間 [0.57, 0.65]。"""
    return max(STRENGTH_MIN, min(STRENGTH_MAX, float(value)))


def user_to_strength(user_value: float | None = None) -> float:
    """把使用者輸入的 0~1 刻度映射到實際區間 [0.57, 0.65]。

    使用者不需要知道 0.57/0.65 這種數字，只要想「情緒要多強」：
        0.0 → 0.57（最弱）
        0.5 → 0.61（區間中點，預設）
        1.0 → 0.65（最強）

    超出 0~1 的值會先被夾住，所以結果保證落在區間內。
    """
    u = USER_STRENGTH_DEFAULT if user_value is None else float(user_value)
    u = max(0.0, min(1.0, u))
    return STRENGTH_MIN + u * (STRENGTH_MAX - STRENGTH_MIN)


def strength_to_user(strength: float) -> float:
    """實際強度反推回 0~1 刻度（顯示用）。"""
    span = STRENGTH_MAX - STRENGTH_MIN
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, (float(strength) - STRENGTH_MIN) / span))


def scale_to_strength(vector: list[float], strength: float | None = None) -> list[float]:
    """把情緒向量等比縮放，使總和等於指定強度（保留維度間比例）。

    這是「情緒配方 × 強度」的落實點：preset 寫什麼絕對值都不影響結果，
    只有比例有意義。全零向量（neutral）維持全零。

    Args:
        vector: 8 維情緒向量
        strength: 目標總和（實際尺度，非 0~1 使用者刻度），None 用甜蜜點；
                  會夾進 [0.57, 0.65]。使用者輸入請先過 user_to_strength()。

    Returns:
        總和等於 strength 的新向量（neutral 例外，仍為全零）
    """
    target = clamp_strength(STRENGTH_DEFAULT if strength is None else strength)
    total = sum(vector)
    if total <= 0:
        return [0.0] * 8  # neutral：不放大零向量
    return [v * target / total for v in vector]


def scale_to_user_strength(vector: list[float], user_value: float | None = None) -> list[float]:
    """scale_to_strength 的使用者刻度版：strength 用 0~1 表示。"""
    return scale_to_strength(vector, user_to_strength(user_value))

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
