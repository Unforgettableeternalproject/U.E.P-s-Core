"""LoRA 合併 + accel 複本原地同步（CPU 極小 GPT2，不需 GPU / 真模型）。

重點：accel 模型是獨立的權重複本，合併後必須①與主模型一致 ②參數 data_ptr 不變
（已捕捉的 CUDA graph 才不會失效）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
peft = pytest.importorskip("peft")
transformers = pytest.importorskip("transformers")
if not hasattr(torch, "nn") or not hasattr(torch.nn, "Module"):   # 其他測試注入的 torch stub
    pytest.skip("torch 為 stub", allow_module_level=True)

from torch import nn  # noqa: E402
from transformers import GPT2Config, GPT2Model  # noqa: E402

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "echo_tts" / "modules" / "tts_module" / "lora_merge.py"
)
_spec = importlib.util.spec_from_file_location("_lora_merge_under_test", _MODULE_PATH)
lora_merge = importlib.util.module_from_spec(_spec)
sys.modules["_lora_merge_under_test"] = lora_merge
_spec.loader.exec_module(lora_merge)


def _config() -> GPT2Config:
    return GPT2Config(vocab_size=32, n_positions=16, n_ctx=16, n_embd=16, n_layer=2, n_head=2)


class _InferenceModel(nn.Module):
    """仿 GPT2InferenceModel：持有共用的 transformer，adapter key 以此為根。"""

    def __init__(self, transformer: nn.Module) -> None:
        super().__init__()
        self.transformer = transformer


class _FakeUnifiedVoice(nn.Module):
    def __init__(self, with_accel: bool) -> None:
        super().__init__()
        self.gpt = GPT2Model(_config()).eval()
        self.inference_model = _InferenceModel(self.gpt)
        self.accel_engine = None
        if with_accel:
            accel = GPT2Model(_config()).eval()   # 獨立複本，如同 GPT2AccelModel
            accel.load_state_dict(self.gpt.state_dict(), strict=False)
            self.accel_engine = type("E", (), {"model": accel})()


@pytest.fixture()
def adapter_dir(tmp_path):
    """用相同結構做一個假 LoRA（lora_B 隨機非零，否則合併是 no-op）。"""
    torch.manual_seed(0)
    base = _FakeUnifiedVoice(with_accel=False)
    cfg = peft.LoraConfig(
        r=2, lora_alpha=4, target_modules=["attn.c_attn", "attn.c_proj", "mlp.c_fc", "mlp.c_proj"],
        fan_in_fan_out=True, lora_dropout=0.0,
    )
    model = peft.get_peft_model(base.inference_model, cfg)
    for name, param in model.named_parameters():
        if "lora_B" in name:
            nn.init.normal_(param, std=0.1)
    out = tmp_path / "adapter"
    model.save_pretrained(str(out))
    return out


def _build(with_accel: bool, seed: int = 1) -> _FakeUnifiedVoice:
    torch.manual_seed(seed)
    return _FakeUnifiedVoice(with_accel)


def test_merge_changes_main_and_syncs_accel_in_place(adapter_dir):
    gpt = _build(with_accel=True)
    accel = gpt.accel_engine.model
    ptrs = {n: p.data_ptr() for n, p in accel.named_parameters()}
    before = accel.state_dict()["h.0.attn.c_attn.weight"].clone()

    stats = lora_merge.merge_lora_into_gpt(gpt, str(adapter_dir), scale=0.85)

    assert stats["accel_synced"] is True
    assert stats["merged_delta_norm"] > 0           # 主模型真的被改
    assert stats["accel_delta_norm"] > 0            # accel 複本也真的被改
    assert stats["accel_residual_norm"] == pytest.approx(0.0, abs=1e-7)
    # accel 複本與合併後主模型逐 key 一致
    main_sd = gpt.gpt.state_dict()
    for key, value in accel.state_dict().items():
        if key in main_sd:
            assert torch.equal(value, main_sd[key]), key
    assert not torch.equal(before, accel.state_dict()["h.0.attn.c_attn.weight"])
    # 原地更新：參數位址不變
    assert {n: p.data_ptr() for n, p in accel.named_parameters()} == ptrs
    # LoRA 層已拆回，沒有殘留的 PEFT 包裝
    assert not any("lora_" in n for n, _ in gpt.named_parameters())


def test_scale_zero_is_noop(adapter_dir):
    gpt = _build(with_accel=True)
    ref = {k: v.clone() for k, v in gpt.gpt.state_dict().items()}
    stats = lora_merge.merge_lora_into_gpt(gpt, str(adapter_dir), scale=0.0)
    assert stats["merged_delta_norm"] == pytest.approx(0.0, abs=1e-7)
    for key, value in gpt.gpt.state_dict().items():
        assert torch.allclose(value, ref[key], atol=1e-7), key


def test_scale_is_applied_linearly(adapter_dir):
    full = _build(with_accel=False)
    half = _build(with_accel=False)
    s_full = lora_merge.merge_lora_into_gpt(full, str(adapter_dir), scale=1.0)
    s_half = lora_merge.merge_lora_into_gpt(half, str(adapter_dir), scale=0.5)
    assert s_half["merged_delta_norm"] == pytest.approx(s_full["merged_delta_norm"] * 0.5, rel=1e-4)


def test_without_accel_only_main_model_changes(adapter_dir):
    gpt = _build(with_accel=False)
    stats = lora_merge.merge_lora_into_gpt(gpt, str(adapter_dir), scale=0.85)
    assert stats["accel_synced"] is False
    assert stats["merged_delta_norm"] > 0


def test_missing_adapter_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        lora_merge.merge_lora_into_gpt(_build(False), str(tmp_path / "nope"))
