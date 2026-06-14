"""
Poise — 呼吸法系統

TTS 長文生成的自然度增強機制：在段落之間插入緩衝段落
（靜音、呼吸聲、銜接詞、插入語），模擬人類的呼吸節奏。

三重功能：
1. 自然度 — 模擬人類呼吸與疲勞
2. 系統穩定 — 讓 GPT RTF 飄移回正軌
3. 情緒校正 — 將自適應語感偏移拉回原始值

核心參數：
- Potency（強度）：決定休息的「重量」和內容層級
- Count（頻率）：決定休息插入頻率，帶 jitter 隨機抖動

使用方式：
    from echo_tts.modules.tts_module.poise import PoisePlanner, PoiseConfig

    config = PoiseConfig(enabled=True, potency=0.3, count=8.0)
    planner = PoisePlanner(config)
    plan = planner.plan(segments, total_segments=len(segments), language="zh")
"""

from .config import PoiseConfig, create_poise_config
from .level import PoiseLevel
from .state import PoiseState
from .scheduler import PoiseScheduler
from .level_selector import PoiseLevelSelector
from .emotion_corrector import EmotionCorrector
from .filler_library import FillerLibrary
from .sfx_loader import SfxLoader
from .planner import PoisePlanner, SegmentPlan

__all__ = [
    "PoiseConfig",
    "create_poise_config",
    "PoiseLevel",
    "PoiseState",
    "PoiseScheduler",
    "PoiseLevelSelector",
    "EmotionCorrector",
    "FillerLibrary",
    "SfxLoader",
    "PoisePlanner",
    "SegmentPlan",
]
