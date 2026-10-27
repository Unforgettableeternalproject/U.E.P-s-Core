"""LoRA 合併進 GPT 權重，並原地同步到 accel 引擎的獨立複本。

## 為什麼需要

``UnifiedVoice.post_init_gpt2_config`` 在 ``use_accel`` 時會另外建一個
``GPT2AccelModel``，用 ``load_state_dict(self.gpt.state_dict())`` 複製權重，
``generate`` 有 accel_engine 時直接走 ``accel_engine.generate``，**不經過**
``inference_model``。所以 ``load_lora`` 用 PEFT 包 ``inference_model`` 的做法，
在 accel 下對實際推理路徑完全沒有作用（靜默失效）。

## 作法

1. PEFT 載入 adapter（原地把 LoRA 層注入共用的 ``gpt`` 子模組）
2. 以指定 scale 設定 PEFT scaling 後 ``merge_and_unload()``，把 delta 併進 ``gpt`` 權重
3. 有 accel 模型時，``load_state_dict(gpt.state_dict(), strict=False)`` 原地 ``copy_``，
   參數記憶體位址不變，已捕捉的 CUDA graph 才不會失效；並檢查 data_ptr 沒變
4. 回傳統計（同步前後探針層權重差的 norm），呼叫端記錄 log

合併後權重已含 LoRA，**不可再調 scale**（要換 scale 必須重新載入基底權重）。
"""

from __future__ import annotations

import os
from typing import Any


def _probe_key(model_keys, preferred: str = "h.0.attn.c_attn.weight") -> str | None:
    """挑一個一定被 LoRA 動到的層當探針。"""
    keys = list(model_keys)
    if preferred in keys:
        return preferred
    for key in keys:
        if key.endswith("attn.c_attn.weight"):
            return key
    return None


def merge_lora_into_gpt(
    gpt: Any,
    adapter_path: str,
    scale: float = 1.0,
    use_fp16: bool = False,
) -> dict:
    """把 PEFT adapter 以 ``scale`` 合併進 ``gpt``，並原地同步 accel 複本。

    Args:
        gpt: UnifiedVoice（需有 ``gpt``、``inference_model``；可選 ``accel_engine.model``）。
        adapter_path: PEFT adapter 目錄。
        scale: 合併強度 0.0~1.0（乘在 lora_alpha / r 上）。
        use_fp16: 保留給呼叫端對齊引擎設定；dtype 以基底權重為準。

    Returns:
        統計 dict：``scale``、``probe``、``merged_delta_norm``（主模型探針層）、
        ``accel_synced``、``accel_delta_norm``、``accel_residual_norm``。
    """
    adapter_path = str(adapter_path)
    if not os.path.isdir(adapter_path):
        raise FileNotFoundError(f"LoRA adapter 目錄不存在: {adapter_path}")
    try:
        from peft import PeftModel
    except ImportError as e:  # pragma: no cover
        raise ImportError("需要安裝 peft: pip install peft") from e
    import torch

    scale = max(0.0, min(1.0, float(scale)))
    base = gpt.inference_model
    # torch.compile 包裝層：PEFT 要掛在原始 module 上
    base = getattr(base, "_orig_mod", base)

    accel_model = getattr(getattr(gpt, "accel_engine", None), "model", None)
    core = gpt.gpt   # UnifiedVoice 內部的 GPT2 本體，key 與 accel 模型一致（h.N.attn...）
    main_sd = core.state_dict()
    probe = _probe_key(main_sd.keys())
    main_before = main_sd[probe].detach().clone() if probe else None
    accel_sd = accel_model.state_dict() if accel_model is not None else None
    accel_before = (
        accel_sd[probe].detach().clone() if (accel_sd is not None and probe in accel_sd) else None
    )
    accel_ptrs = (
        {n: p.data_ptr() for n, p in accel_model.named_parameters()}
        if accel_model is not None
        else {}
    )

    peft_model = PeftModel.from_pretrained(base, adapter_path, is_trainable=False)
    peft_model.eval()
    # adapter 權重轉成與基底相同的 dtype，避免半精度基底與 fp32 delta 混算
    peft_model.to(next(base.parameters()).dtype)

    adapter_name = next(iter(peft_model.peft_config))
    cfg = peft_model.peft_config[adapter_name]
    scaled = (cfg.lora_alpha / cfg.r) * scale
    for module in peft_model.modules():
        scaling = getattr(module, "scaling", None)
        if isinstance(scaling, dict) and adapter_name in scaling:
            scaling[adapter_name] = scaled

    with torch.no_grad():
        peft_model.merge_and_unload()   # 原地：LoRA 層換回已含 delta 的原 Conv1D/Linear

    stats: dict = {
        "scale": scale,
        "probe": probe,
        "merged_delta_norm": None,
        "accel_synced": False,
        "accel_delta_norm": None,
        "accel_residual_norm": None,
    }
    main_after = core.state_dict()
    if main_before is not None:
        stats["merged_delta_norm"] = float(
            (main_after[probe].float() - main_before.float()).norm()
        )

    if accel_model is not None:
        with torch.no_grad():
            # load_state_dict 內部走 param.copy_，位址不變；accel 沒有的 key（strict=False）略過
            accel_model.load_state_dict(main_after, strict=False)
        moved = [
            n for n, p in accel_model.named_parameters() if accel_ptrs.get(n) != p.data_ptr()
        ]
        if moved:
            raise RuntimeError(f"accel 參數記憶體位址改變（CUDA graph 會失效）: {moved[:3]}")
        stats["accel_synced"] = True
        if accel_before is not None:
            accel_after = accel_model.state_dict()[probe]
            stats["accel_delta_norm"] = float((accel_after.float() - accel_before.float()).norm())
            stats["accel_residual_norm"] = float(
                (accel_after.float() - main_after[probe].float().to(accel_after.device)).norm()
            )
    return stats
