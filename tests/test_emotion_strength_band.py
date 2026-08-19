"""情緒強度區間（0.65 ± 0.08）的契約測試。

這些數值來自 2.5 聽感盲測定案（艾斯維爾 2026-08-19），改動 config 或
lite_engine 的套用順序時必須讓這裡通過，否則等於推翻校準結果。
"""
from pathlib import Path

import pytest
import yaml

from echo_tts.config.emotion_presets import (
    EMOTION_LABELS,
    STRENGTH_DEFAULT,
    STRENGTH_MAX,
    STRENGTH_MIN,
    USER_STRENGTH_DEFAULT,
    clamp_strength,
    get_preset_vector,
    load_presets,
    scale_to_strength,
    scale_to_user_strength,
    strength_to_user,
    user_to_strength,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "echo_tts" / "modules" / "tts_module" / "config.yaml"
CALM_INDEX = EMOTION_LABELS.index("calm")


@pytest.fixture(scope="module")
def emotion_cfg() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fp:
        return yaml.safe_load(fp)["emotion"]


@pytest.fixture(scope="module")
def presets() -> dict:
    return load_presets()


def apply_engine_pipeline(vector: list[float], cfg: dict) -> list[float]:
    """複製 lite_engine 的套用順序：normalize(上限式) → emo_bias(+cap)。

    刻意不 import lite_engine——那會拉進 torch 與模型相依。這裡驗的是
    「順序與數值契約」，順序一旦在引擎裡被改回去，這個測試就會抓到。
    """
    max_strength = float(cfg["max_strength"])
    bias = cfg.get("emo_bias")
    cap = float(cfg["emo_bias_cap"])

    out = list(vector)
    total = sum(out)
    if total > max_strength > 0:
        out = [v * max_strength / total for v in out]

    if bias is not None and any(v > 0 for v in out):
        biased = [v * b for v, b in zip(out, bias)]
        biased_sum = sum(biased)
        if biased_sum > cap:
            biased = [v * cap / biased_sum for v in biased]
        out = biased
    return out


# ── 區間常數本身 ──

def test_strength_band_is_the_tuned_one():
    assert STRENGTH_DEFAULT == pytest.approx(0.65)
    assert STRENGTH_MIN == pytest.approx(0.57)
    assert STRENGTH_MAX == pytest.approx(0.73)


@pytest.mark.parametrize("value,expected", [
    (0.0, STRENGTH_MIN),
    (0.4, STRENGTH_MIN),
    (0.57, 0.57),
    (0.65, 0.65),
    (0.73, 0.73),
    (0.9, STRENGTH_MAX),
    (5.0, STRENGTH_MAX),
])
def test_clamp_strength_maps_into_band(value, expected):
    assert clamp_strength(value) == pytest.approx(expected)


# ── 使用者 0~1 刻度的映射 ──

@pytest.mark.parametrize("user,expected", [
    (0.0, STRENGTH_MIN),
    (0.5, STRENGTH_DEFAULT),   # 甜蜜點剛好是區間中點
    (1.0, STRENGTH_MAX),
    (0.25, 0.61),
    (0.75, 0.69),
])
def test_user_scale_maps_linearly(user, expected):
    assert user_to_strength(user) == pytest.approx(expected)


def test_default_user_strength_hits_sweet_spot():
    assert user_to_strength(None) == pytest.approx(STRENGTH_DEFAULT)
    assert user_to_strength(USER_STRENGTH_DEFAULT) == pytest.approx(STRENGTH_DEFAULT)


@pytest.mark.parametrize("user", [-1.0, -0.01, 1.01, 99.0])
def test_user_scale_clamps_out_of_range(user):
    result = user_to_strength(user)
    assert STRENGTH_MIN <= result <= STRENGTH_MAX


def test_user_scale_roundtrips():
    for user in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert strength_to_user(user_to_strength(user)) == pytest.approx(user)


def test_scale_to_user_strength_lands_in_band():
    for user in (0.0, 0.5, 1.0):
        vec = scale_to_user_strength([1.0] + [0.0] * 7, user)
        assert sum(vec) == pytest.approx(user_to_strength(user))
        assert STRENGTH_MIN - 1e-9 <= sum(vec) <= STRENGTH_MAX + 1e-9


# ── scale_to_strength ──

def test_scale_preserves_ratio():
    vec = scale_to_strength([0.7, 0.0, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0])
    assert sum(vec) == pytest.approx(STRENGTH_DEFAULT)
    # 主副 70:30 的比例不因縮放而改變
    assert vec[0] / sum(vec) == pytest.approx(0.7)
    assert vec[2] / sum(vec) == pytest.approx(0.3)


def test_scale_is_independent_of_input_magnitude():
    """preset 寫的絕對值不影響結果，只有比例有意義。"""
    small = scale_to_strength([0.07, 0.0, 0.03, 0.0, 0.0, 0.0, 0.0, 0.0])
    large = scale_to_strength([7.0, 0.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    assert small == pytest.approx(large)


def test_scale_clamps_out_of_band_strength():
    assert sum(scale_to_strength([1.0] + [0.0] * 7, 0.2)) == pytest.approx(STRENGTH_MIN)
    assert sum(scale_to_strength([1.0] + [0.0] * 7, 2.0)) == pytest.approx(STRENGTH_MAX)


def test_neutral_stays_zero():
    """全零向量不能被放大——neutral 就是要沒有情緒。"""
    assert scale_to_strength([0.0] * 8) == [0.0] * 8


# ── preset 經過引擎後的實際強度 ──

def test_every_preset_lands_in_band(presets, emotion_cfg):
    for name, entry in presets.items():
        scaled = scale_to_strength(list(get_preset_vector(entry)))
        if name == "neutral":
            assert sum(scaled) == pytest.approx(0.0)
            continue
        assert sum(scaled) == pytest.approx(STRENGTH_DEFAULT), f"{name} 未落在甜蜜點"

        final = apply_engine_pipeline(scaled, emotion_cfg)
        # 引擎只會因 emo_bias 往下調，絕不該超過上緣
        assert sum(final) <= STRENGTH_MAX + 1e-9, f"{name} 超出區間上緣"


def test_calm_bias_applies_after_normalize(presets, emotion_cfg):
    """順序契約：bias 若被移回 normalize 之前，calm 會拿到 0.65 而非 0.366。"""
    calm_bias = emotion_cfg["emo_bias"][CALM_INDEX]
    scaled = scale_to_strength(list(get_preset_vector(presets["calm"])))
    final = apply_engine_pipeline(scaled, emotion_cfg)

    assert final[CALM_INDEX] == pytest.approx(STRENGTH_DEFAULT * calm_bias)
    assert sum(final) < STRENGTH_MIN, "calm 應被壓到區間之下"


def test_non_calm_dimensions_are_not_biased(emotion_cfg):
    """定案：只有 calm 保留 bias，其餘七維在同總和下齊平。"""
    bias = emotion_cfg["emo_bias"]
    for i, label in enumerate(EMOTION_LABELS):
        if label == "calm":
            assert bias[i] == pytest.approx(0.5625)
        else:
            assert bias[i] == pytest.approx(1.0), f"{label} 不該有 bias"


def test_config_max_strength_matches_band(emotion_cfg):
    assert emotion_cfg["max_strength"] == pytest.approx(STRENGTH_DEFAULT)
    assert emotion_cfg["emo_bias_cap"] == pytest.approx(STRENGTH_DEFAULT)


def test_calm_free_presets_are_equalised(presets, emotion_cfg):
    """不含 calm 分量的 preset 經引擎後都應拿到同一個強度。

    注意 YAML 裡的 happy / melancholic 等 preset 並非純單軸，本來就帶少量
    其他維度；只要沒沾到 calm，最終總和就該精確等於甜蜜點。
    """
    equalised, damped = {}, {}
    for name, entry in presets.items():
        if name == "neutral":
            continue
        scaled = scale_to_strength(list(get_preset_vector(entry)))
        total = sum(apply_engine_pipeline(scaled, emotion_cfg))
        if scaled[CALM_INDEX] > 0:
            damped[name] = total
        else:
            equalised[name] = total

    assert equalised, "找不到不含 calm 的 preset"
    assert all(v == pytest.approx(STRENGTH_DEFAULT) for v in equalised.values()), equalised
    # 含 calm 的一律被壓到甜蜜點之下，且仍在合理範圍內（不會被壓成無感）
    assert all(v < STRENGTH_DEFAULT for v in damped.values()), damped
