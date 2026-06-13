"""
PoiseConfig — 呼吸法系統配置
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PoiseConfig:
    """
    Poise 系統的全局配置。

    可從 config.yaml 的 poise: 區塊讀取，也可在每次合成時
    透過 override 覆蓋個別參數。

    Attributes:
        enabled: 是否啟用 Poise 系統（False = 完全跳過，不影響合成結果）
        potency: 強度 0.0–1.0，決定休息的最高層級上限和持續時間
                 0.0 = 只有靜音，1.0 = 可能觸發 ACTION 層級
        count: 基礎觸發頻率，每 N 段觸發一次休息（帶 jitter）
               值越小 = 越頻繁休息
        count_jitter: 觸發間隔的隨機抖動幅度（0.0–1.0）
                      actual_interval = count * (1 ± count_jitter * random)
        min_segments_to_trigger: 最低段數門檻，段落總數 ≤ 此值時不觸發
        min_accumulation: 兩次休息之間的最小累積段數（防止過於密集）
        potency_jitter: 層級選擇的隨機抖動（±1 層的機率權重）
        emotion_correction_enabled: 是否在休息段執行情緒向量校正
        sfx_dir: 音效素材目錄路徑（None = 跳過音效，fallback 到靜音）
    """
    enabled: bool = False
    potency: float = 0.3
    count: float = 8.0
    count_jitter: float = 0.3
    min_segments_to_trigger: int = 3
    min_accumulation: int = 3
    potency_jitter: float = 0.3
    emotion_correction_enabled: bool = True
    sfx_dir: Optional[str] = None

    def __post_init__(self):
        """Validate and clamp values."""
        self.potency = max(0.0, min(1.0, self.potency))
        self.count = max(2.0, self.count)
        self.count_jitter = max(0.0, min(1.0, self.count_jitter))
        self.min_segments_to_trigger = max(3, self.min_segments_to_trigger)
        self.min_accumulation = max(2, self.min_accumulation)
        self.potency_jitter = max(0.0, min(1.0, self.potency_jitter))

    @property
    def sfx_path(self) -> Optional[Path]:
        """Resolve sfx_dir to a Path, if set."""
        if self.sfx_dir is None:
            return None
        p = Path(self.sfx_dir)
        return p if p.exists() else None

    def with_override(self, **kwargs) -> "PoiseConfig":
        """
        Create a copy with selective overrides.

        Usage:
            override = config.with_override(potency=0.8, count=5.0)
        """
        import dataclasses
        current = dataclasses.asdict(self)
        current.update({k: v for k, v in kwargs.items() if v is not None})
        return PoiseConfig(**current)


def create_poise_config(config: dict) -> PoiseConfig:
    """
    Create PoiseConfig from a config dict (e.g., from config.yaml).

    Expected YAML structure:
        poise:
          enabled: false
          potency: 0.3
          count: 8.0
          count_jitter: 0.3
          min_segments_to_trigger: 3
          min_accumulation: 3
          potency_jitter: 0.3
          emotion_correction_enabled: true
          sfx_dir: null
    """
    cfg = config.get('poise', {})
    return PoiseConfig(
        enabled=cfg.get('enabled', False),
        potency=float(cfg.get('potency', 0.3)),
        count=float(cfg.get('count', 8.0)),
        count_jitter=float(cfg.get('count_jitter', 0.3)),
        min_segments_to_trigger=int(cfg.get('min_segments_to_trigger', 3)),
        min_accumulation=int(cfg.get('min_accumulation', 3)),
        potency_jitter=float(cfg.get('potency_jitter', 0.3)),
        emotion_correction_enabled=cfg.get('emotion_correction_enabled', True),
        sfx_dir=cfg.get('sfx_dir', None),
    )
