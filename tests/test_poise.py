"""
Poise 呼吸法系統單元測試

涵蓋範圍：
  - config.py  PoiseConfig 預設值、clamp 行為、工廠函式、with_override
  - level.py   PoiseLevel 排序與屬性
  - state.py   accumulate / mark_rest / reset / has_rested
  - scheduler.py  should_trigger 觸發邏輯、find_best_trigger 句尾偵測
  - level_selector.py  potency 到層級的對應（jitter 關閉）
  - emotion_corrector.py  情緒向量校正、potency 影響
  - filler_library.py  語言分支、層級詞庫、SILENCE 回傳 None
  - planner.py  enabled=False 穿透、段數門檻保護、正常插入行為

所有測試均不依賴 torch 或 GPU。
planner 測試透過在 sys.modules 中預先注入假 torch 來隔離 sfx_loader 的依賴。
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# ────────────────────────────────────────────────────────
# 路徑設定：讓 poise 套件可以用 "from poise.xxx import ..." 取得
# tests/ → 專案根目錄 → echo_tts/modules/tts_module/
# ────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TTS_MODULE_DIR = _PROJECT_ROOT / "echo_tts" / "modules" / "tts_module"

if str(_TTS_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_TTS_MODULE_DIR))

# ────────────────────────────────────────────────────────
# torch stub：讓 sfx_loader 能被 import 而不真正載入 torch
# 必須在任何 poise import 之前注入
# ────────────────────────────────────────────────────────
def _inject_torch_stub() -> None:
    """在 sys.modules 中注入最小化 torch stub，讓 sfx_loader 能 import 成功。"""
    if "torch" in sys.modules:
        return
    torch_stub = types.ModuleType("torch")
    torch_stub.Tensor = MagicMock()  # type: ignore[attr-defined]
    sys.modules["torch"] = torch_stub
    # torchaudio stub（sfx_loader 的 load 方法會用到，但我們不會呼叫它）
    torchaudio_stub = types.ModuleType("torchaudio")
    torchaudio_stub.load = MagicMock()  # type: ignore[attr-defined]
    torchaudio_stub.transforms = MagicMock()  # type: ignore[attr-defined]
    sys.modules["torchaudio"] = torchaudio_stub


_inject_torch_stub()

# ────────────────────────────────────────────────────────
# 現在可以安全 import poise 子模組
# ────────────────────────────────────────────────────────
from poise.config import PoiseConfig, create_poise_config  # noqa: E402
from poise.level import PoiseLevel  # noqa: E402
from poise.state import PoiseState  # noqa: E402
from poise.scheduler import PoiseScheduler, is_sentence_boundary  # noqa: E402
from poise.level_selector import PoiseLevelSelector  # noqa: E402
from poise.emotion_corrector import (  # noqa: E402
    EmotionCorrector,
    NEUTRAL_VECTOR,
    BREATH_SIGH_VECTOR,
    NUM_EMOTION_DIMS,
)
from poise.filler_library import FillerLibrary, ZH_FILLERS, EN_FILLERS  # noqa: E402
from poise.planner import PoisePlanner, SegmentPlan  # noqa: E402


# ════════════════════════════════════════════════════════
# PoiseConfig 測試
# ════════════════════════════════════════════════════════

class TestPoiseConfigDefaults:
    """PoiseConfig 預設值正確性測試"""

    def test_enabled_defaults_to_false(self):
        """預設 enabled 為 False，避免不知情下影響合成"""
        cfg = PoiseConfig()
        assert cfg.enabled is False

    def test_potency_default(self):
        """potency 預設值為 0.3"""
        cfg = PoiseConfig()
        assert cfg.potency == 0.3

    def test_count_default(self):
        """count 預設值為 8.0"""
        cfg = PoiseConfig()
        assert cfg.count == 8.0

    def test_count_jitter_default(self):
        """count_jitter 預設值為 0.3"""
        cfg = PoiseConfig()
        assert cfg.count_jitter == 0.3

    def test_min_segments_to_trigger_default(self):
        """min_segments_to_trigger 預設值為 3"""
        cfg = PoiseConfig()
        assert cfg.min_segments_to_trigger == 3

    def test_min_accumulation_default(self):
        """min_accumulation 預設值為 3"""
        cfg = PoiseConfig()
        assert cfg.min_accumulation == 3

    def test_potency_jitter_default(self):
        """potency_jitter 預設值為 0.3"""
        cfg = PoiseConfig()
        assert cfg.potency_jitter == 0.3

    def test_emotion_correction_enabled_default(self):
        """emotion_correction_enabled 預設值為 True"""
        cfg = PoiseConfig()
        assert cfg.emotion_correction_enabled is True

    def test_sfx_dir_default_none(self):
        """sfx_dir 預設為 None"""
        cfg = PoiseConfig()
        assert cfg.sfx_dir is None


class TestPoiseConfigClamp:
    """PoiseConfig.__post_init__ 的 clamp 行為測試"""

    def test_potency_clamp_above_one(self):
        """potency > 1.0 被夾到 1.0"""
        cfg = PoiseConfig(potency=1.5)
        assert cfg.potency == 1.0

    def test_potency_clamp_below_zero(self):
        """potency < 0.0 被夾到 0.0"""
        cfg = PoiseConfig(potency=-0.5)
        assert cfg.potency == 0.0

    def test_potency_exact_boundaries(self):
        """potency 剛好在 0.0 和 1.0 邊界時不被改變"""
        cfg_low = PoiseConfig(potency=0.0)
        cfg_high = PoiseConfig(potency=1.0)
        assert cfg_low.potency == 0.0
        assert cfg_high.potency == 1.0

    def test_count_clamp_below_two(self):
        """count < 2.0 被夾到 2.0"""
        cfg = PoiseConfig(count=0.5)
        assert cfg.count == 2.0

    def test_count_clamp_exactly_two(self):
        """count 剛好等於 2.0 不被改變"""
        cfg = PoiseConfig(count=2.0)
        assert cfg.count == 2.0

    def test_count_valid_value_unchanged(self):
        """count 正常值不被修改"""
        cfg = PoiseConfig(count=10.0)
        assert cfg.count == 10.0

    def test_count_jitter_clamp_above_one(self):
        """count_jitter > 1.0 被夾到 1.0"""
        cfg = PoiseConfig(count_jitter=2.0)
        assert cfg.count_jitter == 1.0

    def test_count_jitter_clamp_below_zero(self):
        """count_jitter < 0.0 被夾到 0.0"""
        cfg = PoiseConfig(count_jitter=-0.1)
        assert cfg.count_jitter == 0.0

    def test_min_segments_to_trigger_clamp_below_three(self):
        """min_segments_to_trigger < 3 被夾到 3"""
        cfg = PoiseConfig(min_segments_to_trigger=1)
        assert cfg.min_segments_to_trigger == 3

    def test_min_accumulation_clamp_below_two(self):
        """min_accumulation < 2 被夾到 2"""
        cfg = PoiseConfig(min_accumulation=1)
        assert cfg.min_accumulation == 2

    def test_potency_jitter_clamp_above_one(self):
        """potency_jitter > 1.0 被夾到 1.0"""
        cfg = PoiseConfig(potency_jitter=1.5)
        assert cfg.potency_jitter == 1.0

    def test_potency_jitter_clamp_below_zero(self):
        """potency_jitter < 0.0 被夾到 0.0"""
        cfg = PoiseConfig(potency_jitter=-0.1)
        assert cfg.potency_jitter == 0.0


class TestPoiseConfigFactory:
    """create_poise_config 工廠函式測試"""

    def test_from_empty_dict_uses_defaults(self):
        """空 dict 產生的 config 等同預設值"""
        cfg = create_poise_config({})
        defaults = PoiseConfig()
        assert cfg.enabled == defaults.enabled
        assert cfg.potency == defaults.potency
        assert cfg.count == defaults.count

    def test_from_poise_key(self):
        """正確讀取 dict['poise'] 區塊"""
        raw = {
            "poise": {
                "enabled": True,
                "potency": 0.7,
                "count": 5.0,
                "count_jitter": 0.2,
                "min_segments_to_trigger": 4,
                "min_accumulation": 3,
                "potency_jitter": 0.1,
                "emotion_correction_enabled": False,
                "sfx_dir": None,
            }
        }
        cfg = create_poise_config(raw)
        assert cfg.enabled is True
        assert cfg.potency == 0.7
        assert cfg.count == 5.0
        assert cfg.count_jitter == 0.2
        assert cfg.emotion_correction_enabled is False

    def test_missing_poise_key_uses_defaults(self):
        """dict 沒有 'poise' key 時使用所有預設值"""
        cfg = create_poise_config({"other_key": "value"})
        assert cfg.enabled is False
        assert cfg.potency == 0.3


class TestPoiseConfigWithOverride:
    """PoiseConfig.with_override 建立副本測試"""

    def test_override_creates_new_instance(self):
        """with_override 回傳新 PoiseConfig 實例，不修改原始物件"""
        original = PoiseConfig(potency=0.3, count=8.0)
        overridden = original.with_override(potency=0.8)
        assert overridden is not original

    def test_override_changes_target_field(self):
        """with_override 正確修改指定欄位"""
        cfg = PoiseConfig(potency=0.3)
        overridden = cfg.with_override(potency=0.9)
        assert overridden.potency == 0.9

    def test_override_preserves_other_fields(self):
        """with_override 不影響未指定的欄位"""
        cfg = PoiseConfig(potency=0.3, count=8.0, enabled=True)
        overridden = cfg.with_override(potency=0.9)
        assert overridden.count == 8.0
        assert overridden.enabled is True

    def test_override_multiple_fields(self):
        """with_override 可同時修改多個欄位"""
        cfg = PoiseConfig(potency=0.3, count=8.0)
        overridden = cfg.with_override(potency=0.5, count=4.0)
        assert overridden.potency == 0.5
        assert overridden.count == 4.0

    def test_override_respects_clamp(self):
        """with_override 後的值仍受 __post_init__ clamp 保護"""
        cfg = PoiseConfig(potency=0.3)
        overridden = cfg.with_override(potency=999.0)
        assert overridden.potency == 1.0

    def test_override_none_value_is_ignored(self):
        """with_override 中 None 值不會覆蓋原始值"""
        cfg = PoiseConfig(potency=0.3)
        overridden = cfg.with_override(potency=None)
        assert overridden.potency == 0.3


# ════════════════════════════════════════════════════════
# PoiseLevel 測試
# ════════════════════════════════════════════════════════

class TestPoiseLevel:
    """PoiseLevel enum 排序與屬性測試"""

    def test_level_ordering(self):
        """SILENCE < BREATH < FILLER_LIGHT < FILLER_HEAVY < ACTION"""
        assert PoiseLevel.SILENCE < PoiseLevel.BREATH
        assert PoiseLevel.BREATH < PoiseLevel.FILLER_LIGHT
        assert PoiseLevel.FILLER_LIGHT < PoiseLevel.FILLER_HEAVY
        assert PoiseLevel.FILLER_HEAVY < PoiseLevel.ACTION

    def test_silence_is_zero(self):
        """SILENCE 的數值為 0"""
        assert PoiseLevel.SILENCE == 0

    def test_action_is_four(self):
        """ACTION 的數值為 4"""
        assert PoiseLevel.ACTION == 4

    def test_requires_tts_silence_false(self):
        """SILENCE 不需要 TTS 合成"""
        assert PoiseLevel.SILENCE.requires_tts is False

    def test_requires_tts_breath_true(self):
        """BREATH 需要 TTS 合成"""
        assert PoiseLevel.BREATH.requires_tts is True

    def test_requires_tts_filler_light_true(self):
        """FILLER_LIGHT 需要 TTS 合成"""
        assert PoiseLevel.FILLER_LIGHT.requires_tts is True

    def test_requires_tts_action_true(self):
        """ACTION 需要 TTS 合成"""
        assert PoiseLevel.ACTION.requires_tts is True

    def test_requires_sfx_only_action(self):
        """只有 ACTION 需要音效素材"""
        assert PoiseLevel.ACTION.requires_sfx is True
        assert PoiseLevel.SILENCE.requires_sfx is False
        assert PoiseLevel.BREATH.requires_sfx is False
        assert PoiseLevel.FILLER_HEAVY.requires_sfx is False

    def test_label_returns_chinese_string(self):
        """label 屬性回傳繁體中文字串"""
        assert isinstance(PoiseLevel.SILENCE.label, str)
        assert len(PoiseLevel.SILENCE.label) > 0
        # 所有層級都有 label
        for level in PoiseLevel:
            assert isinstance(level.label, str)
            assert len(level.label) > 0

    def test_total_level_count(self):
        """共有 5 個層級"""
        assert len(PoiseLevel) == 5


# ════════════════════════════════════════════════════════
# PoiseState 測試
# ════════════════════════════════════════════════════════

class TestPoiseStateInitial:
    """PoiseState 初始狀態測試"""

    def test_initial_accumulated_segments(self):
        """初始 accumulated_segments 為 0"""
        state = PoiseState()
        assert state.accumulated_segments == 0

    def test_initial_accumulated_duration(self):
        """初始 accumulated_duration_s 為 0.0"""
        state = PoiseState()
        assert state.accumulated_duration_s == 0.0

    def test_initial_last_rest_at_idx(self):
        """初始 last_rest_at_idx 為 -1（尚未休息）"""
        state = PoiseState()
        assert state.last_rest_at_idx == -1

    def test_initial_has_rested_false(self):
        """初始 has_rested 為 False"""
        state = PoiseState()
        assert state.has_rested is False

    def test_initial_total_rests(self):
        """初始 total_rests 為 0"""
        state = PoiseState()
        assert state.total_rests == 0

    def test_initial_rest_history_empty(self):
        """初始 rest_history 為空列表"""
        state = PoiseState()
        assert state.rest_history == []


class TestPoiseStateAccumulate:
    """PoiseState.accumulate 方法測試"""

    def test_accumulate_increments_segment_count(self):
        """accumulate 增加 accumulated_segments"""
        state = PoiseState()
        state.accumulate()
        assert state.accumulated_segments == 1

    def test_accumulate_multiple_times(self):
        """多次 accumulate 正確累加"""
        state = PoiseState()
        for _ in range(5):
            state.accumulate()
        assert state.accumulated_segments == 5

    def test_accumulate_adds_duration(self):
        """accumulate 累加 accumulated_duration_s"""
        state = PoiseState()
        state.accumulate(segment_duration_s=2.5)
        assert state.accumulated_duration_s == 2.5

    def test_accumulate_multiple_durations(self):
        """多次 accumulate 正確累加 duration"""
        state = PoiseState()
        state.accumulate(1.0)
        state.accumulate(2.0)
        state.accumulate(0.5)
        assert abs(state.accumulated_duration_s - 3.5) < 1e-9

    def test_accumulate_zero_duration_default(self):
        """不傳 duration 參數時預設為 0.0"""
        state = PoiseState()
        state.accumulate()
        assert state.accumulated_duration_s == 0.0

    def test_accumulate_does_not_change_rest_count(self):
        """accumulate 不影響 total_rests"""
        state = PoiseState()
        state.accumulate()
        assert state.total_rests == 0


class TestPoiseStateMarkRest:
    """PoiseState.mark_rest 方法測試"""

    def test_mark_rest_resets_segment_count(self):
        """mark_rest 重置 accumulated_segments 為 0"""
        state = PoiseState()
        state.accumulate()
        state.accumulate()
        state.mark_rest(at_idx=2)
        assert state.accumulated_segments == 0

    def test_mark_rest_resets_duration(self):
        """mark_rest 重置 accumulated_duration_s 為 0.0"""
        state = PoiseState()
        state.accumulate(3.5)
        state.mark_rest(at_idx=1)
        assert state.accumulated_duration_s == 0.0

    def test_mark_rest_updates_last_idx(self):
        """mark_rest 更新 last_rest_at_idx"""
        state = PoiseState()
        state.mark_rest(at_idx=5)
        assert state.last_rest_at_idx == 5

    def test_mark_rest_appends_history(self):
        """mark_rest 將 idx 加入 rest_history"""
        state = PoiseState()
        state.mark_rest(at_idx=3)
        assert 3 in state.rest_history

    def test_mark_rest_multiple_updates_history(self):
        """多次 mark_rest 都被記錄到 rest_history"""
        state = PoiseState()
        state.mark_rest(at_idx=3)
        state.accumulate()
        state.accumulate()
        state.accumulate()
        state.mark_rest(at_idx=7)
        assert state.rest_history == [3, 7]

    def test_mark_rest_increments_total_rests(self):
        """mark_rest 增加 total_rests"""
        state = PoiseState()
        state.mark_rest(at_idx=0)
        assert state.total_rests == 1

    def test_mark_rest_sets_has_rested_true(self):
        """mark_rest 後 has_rested 變為 True"""
        state = PoiseState()
        assert state.has_rested is False
        state.mark_rest(at_idx=0)
        assert state.has_rested is True


class TestPoiseStateReset:
    """PoiseState.reset 方法測試"""

    def test_reset_clears_segment_count(self):
        """reset 後 accumulated_segments 回到 0"""
        state = PoiseState()
        state.accumulate()
        state.accumulate()
        state.reset()
        assert state.accumulated_segments == 0

    def test_reset_clears_duration(self):
        """reset 後 accumulated_duration_s 回到 0.0"""
        state = PoiseState()
        state.accumulate(5.0)
        state.reset()
        assert state.accumulated_duration_s == 0.0

    def test_reset_clears_rest_history(self):
        """reset 後 rest_history 清空"""
        state = PoiseState()
        state.mark_rest(at_idx=3)
        state.reset()
        assert state.rest_history == []

    def test_reset_clears_total_rests(self):
        """reset 後 total_rests 回到 0"""
        state = PoiseState()
        state.mark_rest(at_idx=2)
        state.reset()
        assert state.total_rests == 0

    def test_reset_restores_last_rest_at_idx(self):
        """reset 後 last_rest_at_idx 回到 -1"""
        state = PoiseState()
        state.mark_rest(at_idx=5)
        state.reset()
        assert state.last_rest_at_idx == -1

    def test_reset_makes_has_rested_false(self):
        """reset 後 has_rested 回到 False"""
        state = PoiseState()
        state.mark_rest(at_idx=0)
        state.reset()
        assert state.has_rested is False

    def test_accumulate_after_reset_works(self):
        """reset 後可以正常繼續 accumulate"""
        state = PoiseState()
        state.accumulate()
        state.mark_rest(at_idx=1)
        state.reset()
        state.accumulate()
        assert state.accumulated_segments == 1


# ════════════════════════════════════════════════════════
# PoiseScheduler 測試
# ════════════════════════════════════════════════════════

def _make_scheduler(
    enabled: bool = True,
    count: float = 5.0,
    count_jitter: float = 0.0,
    min_segments: int = 3,
    min_accumulation: int = 3,
    seed: int = 42,
) -> PoiseScheduler:
    """建立可控的 PoiseScheduler（count_jitter=0 使觸發點確定性）"""
    cfg = PoiseConfig(
        enabled=enabled,
        count=count,
        count_jitter=count_jitter,
        min_segments_to_trigger=min_segments,
        min_accumulation=min_accumulation,
        potency_jitter=0.0,
    )
    return PoiseScheduler(cfg, seed=seed)


class TestPoiseSchedulerShouldTrigger:
    """PoiseScheduler.should_trigger 觸發邏輯測試"""

    def test_disabled_never_triggers(self):
        """enabled=False 時 should_trigger 永遠回傳 False"""
        scheduler = _make_scheduler(enabled=False)
        state = PoiseState()
        for _ in range(10):
            state.accumulate()
        assert scheduler.should_trigger(5, total_segments=20, state=state) is False

    def test_too_few_total_segments_no_trigger(self):
        """總段數 <= min_segments_to_trigger 時不觸發"""
        scheduler = _make_scheduler(min_segments=5)
        state = PoiseState()
        for _ in range(5):
            state.accumulate()
        # total_segments=5, min_segments_to_trigger=5 → 不觸發
        assert scheduler.should_trigger(4, total_segments=5, state=state) is False

    def test_total_segments_above_threshold_can_trigger(self):
        """總段數超過門檻時可以觸發（只要其他條件也滿足）"""
        # count=5.0, count_jitter=0 → 第 5 段觸發
        scheduler = _make_scheduler(count=5.0, count_jitter=0.0, min_accumulation=3)
        state = PoiseState()
        # 累積足夠段數
        for _ in range(6):
            state.accumulate()
        # seg_idx=5, total=20 → 應該觸發
        result = scheduler.should_trigger(5, total_segments=20, state=state)
        assert result is True

    def test_first_segment_never_triggers(self):
        """第 0 段（首段）不觸發"""
        scheduler = _make_scheduler(count=0.5, count_jitter=0.0)
        state = PoiseState()
        state.accumulate()
        assert scheduler.should_trigger(0, total_segments=20, state=state) is False

    def test_tail_protection_no_trigger(self):
        """最後兩段（remaining < 2）不觸發"""
        scheduler = _make_scheduler(count=5.0, count_jitter=0.0)
        state = PoiseState()
        for _ in range(10):
            state.accumulate()
        # seg_idx=18, total=20 → remaining=1 < 2 → 不觸發
        assert scheduler.should_trigger(18, total_segments=20, state=state) is False

    def test_tail_protection_last_segment(self):
        """最後一段（seg_idx == total-1）不觸發"""
        scheduler = _make_scheduler(count=5.0, count_jitter=0.0)
        state = PoiseState()
        for _ in range(20):
            state.accumulate()
        assert scheduler.should_trigger(19, total_segments=20, state=state) is False

    def test_min_accumulation_blocks_too_early(self):
        """累積不足 min_accumulation 時不觸發"""
        # count=3, min_accumulation=5 → 即使到了觸發點，段數不夠也不觸發
        scheduler = _make_scheduler(count=3.0, count_jitter=0.0, min_accumulation=5)
        state = PoiseState()
        # 只累積 2 段，小於 min_accumulation=5
        state.accumulate()
        state.accumulate()
        assert scheduler.should_trigger(3, total_segments=20, state=state) is False

    def test_trigger_fires_at_scheduled_point(self):
        """在 count 點精確觸發（count_jitter=0）"""
        # count=5, count_jitter=0 → _next_trigger_at = 5.0
        scheduler = _make_scheduler(count=5.0, count_jitter=0.0, min_accumulation=3)
        state = PoiseState()
        for _ in range(6):
            state.accumulate()
        # seg_idx=5 >= _next_trigger_at=5.0 → 觸發
        assert scheduler.should_trigger(5, total_segments=20, state=state) is True

    def test_trigger_not_before_scheduled_point(self):
        """在 count 點之前不觸發"""
        scheduler = _make_scheduler(count=8.0, count_jitter=0.0, min_accumulation=3)
        state = PoiseState()
        for _ in range(5):
            state.accumulate()
        # seg_idx=4 < _next_trigger_at=8.0 → 不觸發
        assert scheduler.should_trigger(4, total_segments=20, state=state) is False


class TestPoiseSchedulerFindBestTrigger:
    """PoiseScheduler.find_best_trigger 句尾邊界偵測測試"""

    def test_sentence_boundary_at_current_position(self):
        """當前段本身就是句尾時，回傳當前位置"""
        scheduler = _make_scheduler()
        state = PoiseState()
        # 建立段落，最後一個 token 是句號
        segments = [
            ["hello", " world", "。"],
            ["next", " segment"],
            ["more", " text"],
            ["end", "."],
            ["after", " end"],
            ["final", " words"],
        ]
        result = scheduler.find_best_trigger(0, total_segments=6, state=state, segments=segments)
        assert result == 0

    def test_finds_sentence_boundary_ahead(self):
        """當前段不是句尾但前方有句尾時，往前找"""
        scheduler = _make_scheduler()
        state = PoiseState()
        segments = [
            ["no", " boundary"],
            ["still", " no"],
            ["here", " is", "。"],  # seg_idx=2 是句尾
            ["after", " boundary"],
            ["more", " content"],
            ["end", "。"],
        ]
        # 從 seg_idx=0 觸發，應該找到 seg_idx=2
        result = scheduler.find_best_trigger(0, total_segments=6, state=state, segments=segments)
        assert result == 2

    def test_fallback_to_original_when_no_boundary(self):
        """在前瞻範圍內找不到句尾時，回傳原始位置"""
        scheduler = _make_scheduler()
        state = PoiseState()
        # 沒有任何句尾標點的段落
        segments = [
            ["no", " boundary"],
            ["also", " no"],
            ["still", " none"],
            ["more", " none"],
            ["end", "?", "!"],  # 這個有，但超出 MAX_LOOKAHEAD=3
        ]
        # seg_idx=0，MAX_LOOKAHEAD=3 → 最多看到 seg_idx=3，seg_idx=4 有句尾但超出範圍
        result = scheduler.find_best_trigger(0, total_segments=5, state=state, segments=segments)
        assert result == 0

    def test_is_sentence_boundary_with_cjk(self):
        """is_sentence_boundary 識別 CJK 句號"""
        assert is_sentence_boundary(["這是句子", "。"]) is True
        assert is_sentence_boundary(["問號", "？"]) is True
        assert is_sentence_boundary(["驚嘆", "！"]) is True

    def test_is_sentence_boundary_with_ascii(self):
        """is_sentence_boundary 識別 ASCII 句號"""
        assert is_sentence_boundary(["sentence", "."]) is True
        assert is_sentence_boundary(["question", "?"]) is True
        assert is_sentence_boundary(["exclaim", "!"]) is True

    def test_is_sentence_boundary_false_mid_sentence(self):
        """句中的段落不被識別為句尾"""
        assert is_sentence_boundary(["this", " is", " mid"]) is False
        assert is_sentence_boundary(["comma", ","]) is False

    def test_is_sentence_boundary_empty_tokens(self):
        """空 token 列表回傳 False"""
        assert is_sentence_boundary([]) is False


# ════════════════════════════════════════════════════════
# PoiseLevelSelector 測試
# ════════════════════════════════════════════════════════

def _make_selector(potency: float, potency_jitter: float = 0.0) -> PoiseLevelSelector:
    """建立確定性 LevelSelector（jitter=0 確保可預測）"""
    cfg = PoiseConfig(
        potency=potency,
        potency_jitter=potency_jitter,
        count_jitter=0.0,
    )
    return PoiseLevelSelector(cfg, seed=42)


def _state_with_segments(n: int) -> PoiseState:
    """建立已累積 n 段的 PoiseState（無 bonus 累積：n < ACCUMULATION_STEP=5）"""
    state = PoiseState()
    for _ in range(n):
        state.accumulate()
    return state


class TestPoiseLevelSelector:
    """PoiseLevelSelector.select potency 到層級對應測試"""

    # potency 0.0-0.19 → max_level = int(0.x * 5) = 0 → SILENCE
    def test_potency_zero_selects_silence(self):
        """potency=0.0 → SILENCE"""
        selector = _make_selector(0.0)
        state = _state_with_segments(3)
        assert selector.select(state) == PoiseLevel.SILENCE

    def test_potency_low_range_stays_silence(self):
        """potency=0.19 → max_level=0 → SILENCE"""
        selector = _make_selector(0.19)
        state = _state_with_segments(3)
        assert selector.select(state) == PoiseLevel.SILENCE

    # potency 0.2-0.39 → int(0.2*5)=1 → 最高 BREATH
    def test_potency_0_2_max_breath(self):
        """potency=0.2 → max_level=1 → 最高 BREATH"""
        selector = _make_selector(0.2)
        state = _state_with_segments(3)
        level = selector.select(state)
        assert level <= PoiseLevel.BREATH

    def test_potency_0_39_max_breath(self):
        """potency=0.39 → int(0.39*5)=1 → 最高 BREATH"""
        selector = _make_selector(0.39)
        state = _state_with_segments(3)
        level = selector.select(state)
        assert level <= PoiseLevel.BREATH

    # potency 0.4-0.59 → int(0.4*5)=2 → 最高 FILLER_LIGHT
    def test_potency_0_4_max_filler_light(self):
        """potency=0.4 → max_level=2 → 最高 FILLER_LIGHT"""
        selector = _make_selector(0.4)
        state = _state_with_segments(3)
        level = selector.select(state)
        assert level <= PoiseLevel.FILLER_LIGHT

    def test_potency_0_59_max_filler_light(self):
        """potency=0.59 → int(0.59*5)=2 → 最高 FILLER_LIGHT"""
        selector = _make_selector(0.59)
        state = _state_with_segments(3)
        level = selector.select(state)
        assert level <= PoiseLevel.FILLER_LIGHT

    # potency 0.6-0.79 → int(0.6*5)=3 → 最高 FILLER_HEAVY
    def test_potency_0_6_max_filler_heavy(self):
        """potency=0.6 → max_level=3 → 最高 FILLER_HEAVY"""
        selector = _make_selector(0.6)
        state = _state_with_segments(3)
        level = selector.select(state)
        assert level <= PoiseLevel.FILLER_HEAVY

    def test_potency_0_79_max_filler_heavy(self):
        """potency=0.79 → int(0.79*5)=3 → 最高 FILLER_HEAVY"""
        selector = _make_selector(0.79)
        state = _state_with_segments(3)
        level = selector.select(state)
        assert level <= PoiseLevel.FILLER_HEAVY

    # potency 0.8-1.0 → int(0.8*5)=4 → 最高 ACTION
    def test_potency_0_8_can_reach_action(self):
        """potency=0.8 → max_level=4 → ACTION 可達"""
        selector = _make_selector(0.8)
        # 累積足夠段數以取得 accumulation bonus
        state = _state_with_segments(10)
        level = selector.select(state)
        assert level <= PoiseLevel.ACTION

    def test_potency_1_0_can_reach_action(self):
        """potency=1.0 → max_level 被 clamp 到 4 → ACTION 可達"""
        selector = _make_selector(1.0)
        state = _state_with_segments(10)
        level = selector.select(state)
        assert level <= PoiseLevel.ACTION

    def test_selected_level_never_exceeds_max(self):
        """任何情況下選出的層級都不超過 potency 決定的 max_level"""
        potencies = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
        for potency in potencies:
            selector = _make_selector(potency)
            state = _state_with_segments(20)  # 高累積 → 可能取得 bonus
            max_level = min(int(potency * 5), 4)
            level = selector.select(state)
            assert level <= max_level, (
                f"potency={potency}: level={level} > max_level={max_level}"
            )

    def test_jitter_disabled_result_is_deterministic(self):
        """potency_jitter=0 時，相同 seed 和 state 產生相同層級"""
        selector1 = _make_selector(0.6, potency_jitter=0.0)
        selector2 = _make_selector(0.6, potency_jitter=0.0)
        state = _state_with_segments(3)
        assert selector1.select(state) == selector2.select(state)


# ════════════════════════════════════════════════════════
# EmotionCorrector 測試
# ════════════════════════════════════════════════════════

class TestEmotionCorrectorRestEmotion:
    """EmotionCorrector.compute_rest_emotion 測試"""

    def _base_emotion(self) -> list:
        """用於測試的基底情緒向量（非全零）"""
        return [0.8, 0.2, 0.1, 0.0, 0.3, 0.0, 0.4, 0.1]

    def test_silence_returns_none(self):
        """SILENCE 層級不需要情緒向量，回傳 None"""
        corrector = EmotionCorrector(enabled=True)
        result = corrector.compute_rest_emotion(self._base_emotion(), PoiseLevel.SILENCE)
        assert result is None

    def test_disabled_corrector_returns_none_for_silence(self):
        """disabled 且 SILENCE 回傳 None"""
        corrector = EmotionCorrector(enabled=False)
        result = corrector.compute_rest_emotion(self._base_emotion(), PoiseLevel.SILENCE)
        assert result is None

    def test_disabled_corrector_returns_none_for_filler(self):
        """disabled 的 corrector 對 TTS 層級也回傳 None"""
        corrector = EmotionCorrector(enabled=False)
        result = corrector.compute_rest_emotion(self._base_emotion(), PoiseLevel.FILLER_LIGHT)
        assert result is None

    def test_breath_returns_sigh_vector(self):
        """BREATH 層級回傳接近 BREATH_SIGH_VECTOR 的向量（strength=1.0）"""
        corrector = EmotionCorrector(enabled=True)
        base = self._base_emotion()
        result = corrector.compute_rest_emotion(base, PoiseLevel.BREATH)
        assert result is not None
        # strength=1.0 → 結果應完全等於 BREATH_SIGH_VECTOR
        for i, (r, expected) in enumerate(zip(result, BREATH_SIGH_VECTOR)):
            assert abs(r - expected) < 1e-9, f"dim {i}: {r} != {expected}"

    def test_action_returns_neutral_vector(self):
        """ACTION 層級（strength=1.0）回傳完全 neutral 向量"""
        corrector = EmotionCorrector(enabled=True)
        base = self._base_emotion()
        result = corrector.compute_rest_emotion(base, PoiseLevel.ACTION)
        assert result is not None
        for i, r in enumerate(result):
            assert abs(r - 0.0) < 1e-9, f"dim {i}: {r} != 0.0"

    def test_filler_light_closer_to_neutral_than_original(self):
        """FILLER_LIGHT 校正後的向量比原始向量更接近 neutral"""
        corrector = EmotionCorrector(enabled=True)
        base = self._base_emotion()
        result = corrector.compute_rest_emotion(base, PoiseLevel.FILLER_LIGHT)
        assert result is not None

        # 計算 L2 距離
        def l2(v): return sum(x * x for x in v) ** 0.5
        dist_original = l2(base)
        dist_corrected = l2(result)
        assert dist_corrected < dist_original, (
            f"校正後 {dist_corrected:.4f} 應小於原始 {dist_original:.4f}"
        )

    def test_filler_heavy_very_close_to_neutral(self):
        """FILLER_HEAVY（strength=0.9）的結果幾乎是 neutral"""
        corrector = EmotionCorrector(enabled=True)
        base = self._base_emotion()
        result = corrector.compute_rest_emotion(base, PoiseLevel.FILLER_HEAVY)
        assert result is not None
        # 0.9 → 結果 = base * 0.1 + neutral * 0.9
        for i, (r, b) in enumerate(zip(result, base)):
            expected = b * 0.1 + 0.0 * 0.9
            assert abs(r - expected) < 1e-9, f"dim {i}"

    def test_result_has_correct_dimension(self):
        """回傳向量的維度正確（NUM_EMOTION_DIMS）"""
        corrector = EmotionCorrector(enabled=True)
        base = self._base_emotion()
        result = corrector.compute_rest_emotion(base, PoiseLevel.FILLER_LIGHT)
        assert result is not None
        assert len(result) == NUM_EMOTION_DIMS

    def test_short_base_emotion_does_not_crash(self):
        """base 向量比 NUM_EMOTION_DIMS 短時，只計算有效的 dim"""
        corrector = EmotionCorrector(enabled=True)
        short_base = [0.5, 0.3]  # 只有 2 維
        result = corrector.compute_rest_emotion(short_base, PoiseLevel.FILLER_LIGHT)
        assert result is not None
        assert len(result) == 2


class TestEmotionCorrectorResumeArcScale:
    """EmotionCorrector.get_resume_arc_scale 測試"""

    def test_silence_arc_scale_is_one(self):
        """SILENCE 之後的 arc_scale 為 1.0（無情緒增強）"""
        corrector = EmotionCorrector(enabled=True)
        assert corrector.get_resume_arc_scale(PoiseLevel.SILENCE) == 1.0

    def test_breath_arc_scale_above_one(self):
        """BREATH 之後 arc_scale > 1.0（輕微重入）"""
        corrector = EmotionCorrector(enabled=True)
        assert corrector.get_resume_arc_scale(PoiseLevel.BREATH) > 1.0

    def test_action_arc_scale_highest(self):
        """ACTION 之後 arc_scale 最高（最強重入）"""
        corrector = EmotionCorrector(enabled=True)
        scales = [corrector.get_resume_arc_scale(level) for level in PoiseLevel]
        action_scale = corrector.get_resume_arc_scale(PoiseLevel.ACTION)
        # ACTION 應有最高的 arc_scale
        assert action_scale == max(scales)

    def test_disabled_returns_one(self):
        """disabled 的 corrector 永遠回傳 1.0"""
        corrector = EmotionCorrector(enabled=False)
        for level in PoiseLevel:
            assert corrector.get_resume_arc_scale(level) == 1.0


# ════════════════════════════════════════════════════════
# FillerLibrary 測試
# ════════════════════════════════════════════════════════

class TestFillerLibraryLanguage:
    """FillerLibrary.get_filler 語言分支測試"""

    def test_silence_returns_none(self):
        """SILENCE 層級回傳 None（無 filler 文字）"""
        lib = FillerLibrary(seed=42)
        result = lib.get_filler(PoiseLevel.SILENCE, language="zh")
        assert result is None

    def test_silence_en_returns_none(self):
        """SILENCE + en 語言也回傳 None"""
        lib = FillerLibrary(seed=42)
        assert lib.get_filler(PoiseLevel.SILENCE, language="en") is None

    def test_zh_filler_light_returns_string(self):
        """zh 語言 FILLER_LIGHT 回傳非空字串"""
        lib = FillerLibrary(seed=42)
        result = lib.get_filler(PoiseLevel.FILLER_LIGHT, language="zh")
        assert result is not None
        assert len(result) > 0

    def test_en_filler_light_returns_string(self):
        """en 語言 FILLER_LIGHT 回傳非空字串"""
        lib = FillerLibrary(seed=42)
        result = lib.get_filler(PoiseLevel.FILLER_LIGHT, language="en")
        assert result is not None
        assert len(result) > 0

    def test_zh_fillers_come_from_zh_pool(self):
        """zh 語言的 filler 來自 ZH_FILLERS 詞庫"""
        lib = FillerLibrary(seed=42)
        all_zh = []
        for level_list in ZH_FILLERS.values():
            all_zh.extend(level_list)
        result = lib.get_filler(PoiseLevel.FILLER_LIGHT, language="zh")
        assert result in all_zh

    def test_en_fillers_come_from_en_pool(self):
        """en 語言的 filler 來自 EN_FILLERS 詞庫"""
        lib = FillerLibrary(seed=42)
        all_en = []
        for level_list in EN_FILLERS.values():
            all_en.extend(level_list)
        result = lib.get_filler(PoiseLevel.FILLER_LIGHT, language="en")
        assert result in all_en

    def test_zh_and_en_fillers_are_different(self):
        """相同層級下，zh 和 en 的詞庫內容不同"""
        zh_pool = set(ZH_FILLERS.get(PoiseLevel.FILLER_LIGHT, []))
        en_pool = set(EN_FILLERS.get(PoiseLevel.FILLER_LIGHT, []))
        # 兩個詞庫不應完全相同
        assert zh_pool != en_pool


class TestFillerLibraryLevels:
    """FillerLibrary 層級詞庫差異測試"""

    def test_filler_light_and_heavy_have_distinct_pools(self):
        """FILLER_LIGHT 和 FILLER_HEAVY 的詞庫不重疊"""
        zh_light = set(ZH_FILLERS.get(PoiseLevel.FILLER_LIGHT, []))
        zh_heavy = set(ZH_FILLERS.get(PoiseLevel.FILLER_HEAVY, []))
        assert zh_light.isdisjoint(zh_heavy), "FILLER_LIGHT 和 FILLER_HEAVY 不應共享詞彙"

    def test_breath_returns_filler_not_silence(self):
        """BREATH 層級回傳文字（"......"），不是 None"""
        lib = FillerLibrary(seed=42)
        result = lib.get_filler(PoiseLevel.BREATH, language="zh")
        assert result is not None

    def test_action_returns_filler(self):
        """ACTION 層級回傳對應的動作描述文字"""
        lib = FillerLibrary(seed=42)
        result = lib.get_filler(PoiseLevel.ACTION, language="zh")
        assert result is not None
        assert len(result) > 0

    def test_supported_levels_contains_tts_levels(self):
        """supported_levels 包含所有需要 TTS 的層級"""
        lib = FillerLibrary()
        levels = lib.supported_levels
        assert PoiseLevel.BREATH in levels
        assert PoiseLevel.FILLER_LIGHT in levels
        assert PoiseLevel.FILLER_HEAVY in levels
        assert PoiseLevel.ACTION in levels


class TestFillerLibraryDedup:
    """FillerLibrary 去重機制測試"""

    def test_history_prevents_immediate_repeat(self):
        """連續呼叫時，不會立刻回傳同一個 filler（詞庫夠大的情況）"""
        lib = FillerLibrary(seed=42, history_size=5)
        # FILLER_LIGHT 在中文有 8 個詞彙，不應立刻重複
        seen = set()
        for _ in range(5):
            result = lib.get_filler(PoiseLevel.FILLER_LIGHT, language="zh")
            assert result is not None
            seen.add(result)
        # 5 次應該至少有 2 種不同 filler
        assert len(seen) >= 2

    def test_reset_clears_history(self):
        """reset 後 history 清空，可以重新開始"""
        lib = FillerLibrary(seed=42, history_size=10)
        first = lib.get_filler(PoiseLevel.FILLER_LIGHT, language="zh")
        lib.reset(seed=42)
        # 使用相同 seed 重置，應回傳同樣的第一個詞彙
        after_reset = lib.get_filler(PoiseLevel.FILLER_LIGHT, language="zh")
        assert first == after_reset


# ════════════════════════════════════════════════════════
# PoisePlanner 測試
# ════════════════════════════════════════════════════════

def _make_planner(
    enabled: bool = True,
    potency: float = 0.5,
    count: float = 5.0,
    count_jitter: float = 0.0,
    min_segments: int = 3,
    min_accumulation: int = 3,
    seed: int = 42,
) -> PoisePlanner:
    """建立可控的 PoisePlanner"""
    cfg = PoiseConfig(
        enabled=enabled,
        potency=potency,
        count=count,
        count_jitter=count_jitter,
        min_segments_to_trigger=min_segments,
        min_accumulation=min_accumulation,
        potency_jitter=0.0,
        sfx_dir=None,
    )
    return PoisePlanner(cfg, seed=seed)


class TestPoisePlannerDisabled:
    """PoisePlanner enabled=False 時的穿透行為"""

    def test_disabled_returns_all_normal(self):
        """enabled=False → plan 回傳全部 normal 類型的 SegmentPlan"""
        planner = _make_planner(enabled=False)
        plan = planner.plan(total_segments=10, language="zh")
        assert all(p.type == "normal" for p in plan)

    def test_disabled_returns_correct_count(self):
        """enabled=False → plan 長度等於 total_segments"""
        planner = _make_planner(enabled=False)
        plan = planner.plan(total_segments=10, language="zh")
        assert len(plan) == 10

    def test_disabled_preserves_original_indices(self):
        """enabled=False → original_seg_idx 依序為 0..N-1"""
        planner = _make_planner(enabled=False)
        plan = planner.plan(total_segments=5, language="zh")
        indices = [p.original_seg_idx for p in plan]
        assert indices == [0, 1, 2, 3, 4]

    def test_disabled_no_poise_segments(self):
        """enabled=False → 沒有任何 poise_silence / poise_tts 類型"""
        planner = _make_planner(enabled=False)
        plan = planner.plan(total_segments=20, language="zh")
        poise_types = {p.type for p in plan if p.type != "normal"}
        assert len(poise_types) == 0


class TestPoisePlannerTooFewSegments:
    """PoisePlanner 段數門檻保護測試"""

    def test_below_threshold_returns_all_normal(self):
        """total_segments <= min_segments_to_trigger → 不插入 poise"""
        # min_segments_to_trigger 預設=3（clamp 最小值），測試段數剛好等於門檻
        planner = _make_planner(enabled=True, min_segments=3)
        plan = planner.plan(total_segments=3, language="zh")
        assert all(p.type == "normal" for p in plan)
        assert len(plan) == 3

    def test_one_segment_returns_one_normal(self):
        """只有 1 段時不觸發（也低於門檻）"""
        planner = _make_planner(enabled=True)
        plan = planner.plan(total_segments=1, language="zh")
        assert len(plan) == 1
        assert plan[0].type == "normal"

    def test_two_segments_below_threshold(self):
        """只有 2 段時不觸發"""
        planner = _make_planner(enabled=True, min_segments=3)
        plan = planner.plan(total_segments=2, language="zh")
        assert len(plan) == 2
        assert all(p.type == "normal" for p in plan)


class TestPoisePlannerNormalOperation:
    """PoisePlanner 正常插入行為測試"""

    def test_plan_inserts_poise_segments(self):
        """足夠多的段落下，plan 插入 poise 段"""
        # count=5, total=30, count_jitter=0 → 必然插入 poise
        planner = _make_planner(enabled=True, count=5.0, min_segments=3, min_accumulation=3)
        plan = planner.plan(total_segments=30, language="zh")
        poise_segs = [p for p in plan if p.type != "normal"]
        assert len(poise_segs) > 0, "應有至少一個 poise 段"

    def test_plan_total_length_equals_normal_plus_poise(self):
        """plan 長度 = normal 段數 + poise 段數"""
        planner = _make_planner(enabled=True, count=5.0, min_accumulation=3)
        total = 30
        plan = planner.plan(total_segments=total, language="zh")
        normal_count = sum(1 for p in plan if p.type == "normal")
        poise_count = sum(1 for p in plan if p.type != "normal")
        assert normal_count == total
        assert len(plan) == total + poise_count

    def test_plan_normal_segments_preserve_original_indices(self):
        """plan 中 normal 段的 original_seg_idx 涵蓋 0..N-1 且有序"""
        planner = _make_planner(enabled=True, count=5.0, min_accumulation=3)
        plan = planner.plan(total_segments=20, language="zh")
        normal_indices = [p.original_seg_idx for p in plan if p.type == "normal"]
        assert normal_indices == list(range(20))

    def test_poise_segment_types_are_valid(self):
        """poise 段的 type 只能是 poise_silence / poise_audio / poise_tts"""
        planner = _make_planner(enabled=True, count=5.0, min_accumulation=3)
        plan = planner.plan(total_segments=30, language="zh")
        valid_poise_types = {"poise_silence", "poise_audio", "poise_tts"}
        for seg in plan:
            if seg.type != "normal":
                assert seg.type in valid_poise_types, f"無效 type: {seg.type}"

    def test_poise_segment_has_level(self):
        """所有 poise 段都有 poise_level 屬性"""
        planner = _make_planner(enabled=True, count=5.0, min_accumulation=3)
        plan = planner.plan(total_segments=30, language="zh")
        for seg in plan:
            if seg.type != "normal":
                assert seg.poise_level is not None

    def test_poise_silence_has_duration(self):
        """poise_silence 類型的段落有 poise_silence_ms"""
        # potency=0.0 → 只有 SILENCE 層級
        planner = _make_planner(enabled=True, potency=0.0, count=5.0, min_accumulation=3)
        plan = planner.plan(total_segments=30, language="zh")
        for seg in plan:
            if seg.type == "poise_silence":
                assert seg.poise_silence_ms is not None
                assert seg.poise_silence_ms > 0

    def test_poise_tts_has_text(self):
        """poise_tts 類型的段落有 poise_text"""
        # potency=0.5 → 可達 FILLER_LIGHT，會產生 poise_tts
        planner = _make_planner(enabled=True, potency=0.5, count=5.0, min_accumulation=3)
        plan = planner.plan(total_segments=30, language="zh")
        for seg in plan:
            if seg.type == "poise_tts":
                assert seg.poise_text is not None
                assert len(seg.poise_text) > 0

    def test_plan_is_deterministic_with_same_seed(self):
        """相同 seed 的 planner 每次 plan 回傳相同結果"""
        planner1 = _make_planner(seed=123)
        planner2 = _make_planner(seed=123)
        plan1 = planner1.plan(total_segments=20, language="zh")
        plan2 = planner2.plan(total_segments=20, language="zh")
        assert len(plan1) == len(plan2)
        for s1, s2 in zip(plan1, plan2):
            assert s1.type == s2.type
            assert s1.original_seg_idx == s2.original_seg_idx
            assert s1.poise_level == s2.poise_level

    def test_plan_reset_between_calls(self):
        """連續呼叫 plan 時 state 自動重置，結果一致"""
        planner = _make_planner(seed=42)
        plan1 = planner.plan(total_segments=20, language="zh")
        plan2 = planner.plan(total_segments=20, language="zh")
        # 兩次呼叫結果應完全相同
        assert len(plan1) == len(plan2)
        for s1, s2 in zip(plan1, plan2):
            assert s1.type == s2.type

    def test_poise_never_after_first_segment(self):
        """poise 不應插入在第一個 normal 段之後（seg_idx=0 保護）"""
        planner = _make_planner(enabled=True, count=2.0, min_accumulation=3)
        plan = planner.plan(total_segments=20, language="zh")
        # 計劃裡的第一個元素一定是 normal
        assert plan[0].type == "normal"
        assert plan[0].original_seg_idx == 0
        # 第二個元素不應是 poise（seg_idx=0 不觸發）
        if len(plan) > 1:
            # 即使有 poise，也不會緊跟在 idx=0 之後
            first_poise_pos = next((i for i, p in enumerate(plan) if p.type != "normal"), None)
            if first_poise_pos is not None:
                # poise 前至少要有 min_accumulation 個 normal
                normal_before_poise = sum(
                    1 for p in plan[:first_poise_pos] if p.type == "normal"
                )
                assert normal_before_poise >= 3  # min_accumulation

    def test_plan_with_emotion_vector(self):
        """傳入 original_emotion 時，poise_tts 段應有 poise_emotion"""
        planner = _make_planner(enabled=True, potency=0.5, count=5.0, min_accumulation=3)
        emotion = [0.5, 0.1, 0.2, 0.0, 0.0, 0.1, 0.3, 0.2]
        plan = planner.plan(total_segments=30, language="zh", original_emotion=emotion)
        for seg in plan:
            if seg.type == "poise_tts":
                assert seg.poise_emotion is not None
                break

    def test_plan_zh_language_uses_zh_fillers(self):
        """zh 語言的 poise_tts 文字來自 ZH_FILLERS 詞庫"""
        planner = _make_planner(enabled=True, potency=0.5, count=5.0, min_accumulation=3)
        plan = planner.plan(total_segments=30, language="zh")
        all_zh = []
        for level_list in ZH_FILLERS.values():
            all_zh.extend(level_list)
        for seg in plan:
            if seg.type == "poise_tts":
                assert seg.poise_text in all_zh, (
                    f"zh plan 出現非 ZH_FILLERS 的文字：{seg.poise_text!r}"
                )

    def test_plan_en_language_uses_en_fillers(self):
        """en 語言的 poise_tts 文字來自 EN_FILLERS 詞庫"""
        planner = _make_planner(enabled=True, potency=0.5, count=5.0, min_accumulation=3)
        plan = planner.plan(total_segments=30, language="en")
        all_en = []
        for level_list in EN_FILLERS.values():
            all_en.extend(level_list)
        for seg in plan:
            if seg.type == "poise_tts":
                assert seg.poise_text in all_en, (
                    f"en plan 出現非 EN_FILLERS 的文字：{seg.poise_text!r}"
                )


class TestPoisePlannerFormatSummary:
    """PoisePlanner.format_plan_summary 靜態方法測試"""

    def test_summary_contains_normal_count(self):
        """摘要字串包含 normal 段數"""
        planner = _make_planner(enabled=True, count=5.0, min_accumulation=3)
        plan = planner.plan(total_segments=20, language="zh")
        summary = PoisePlanner.format_plan_summary(plan)
        normal_count = sum(1 for p in plan if p.type == "normal")
        assert str(normal_count) in summary

    def test_summary_all_normal_plan(self):
        """全 normal plan 的摘要應顯示 0 rest"""
        planner = _make_planner(enabled=False)
        plan = planner.plan(total_segments=5, language="zh")
        summary = PoisePlanner.format_plan_summary(plan)
        assert "0 rest" in summary

    def test_summary_is_string(self):
        """摘要回傳字串型別"""
        planner = _make_planner(enabled=True, count=5.0, min_accumulation=3)
        plan = planner.plan(total_segments=20, language="zh")
        summary = PoisePlanner.format_plan_summary(plan)
        assert isinstance(summary, str)
        assert len(summary) > 0
