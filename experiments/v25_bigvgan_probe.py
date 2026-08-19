"""BigVGAN 效能探測：cuda kernel 與精度的 A/B。

背景：warm profiling 顯示 BigVGAN 佔 ~4.1s（35%），且 lite_engine 以 FP32 呼叫
（`self.bigvgan(mel.float())`）。本腳本量測四個變體在同一 dummy mel 上的耗時：

  1. kernel OFF + FP32（疑似現況的下限）
  2. kernel ON  + FP32（現況設定；若與 1 相同代表 kernel 沒生效）
  3. kernel OFF + FP16
  4. kernel ON  + FP16（若支援）

並比對輸出健全性（NaN / 與 FP32 基準的相對誤差）。

用法：
    python experiments/v25_bigvgan_probe.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = PROJECT_ROOT / "echo_tts"
sys.path.insert(0, str(ENGINE_ROOT))

import torch

BIGVGAN_DIR = ENGINE_ROOT / "modules" / "tts_module" / "checkpoints_v25" / "hf_cache" / "bigvgan"
# 約 24 秒音訊的 mel 長度（22050Hz / hop 256）
MEL_FRAMES = 2067
WARMUP_ITERS = 2
TIMED_ITERS = 3


def bench(model, mel: torch.Tensor) -> tuple[float, torch.Tensor]:
    with torch.no_grad():
        for _ in range(WARMUP_ITERS):
            model(mel)
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        for _ in range(TIMED_ITERS):
            out = model(mel)
        torch.cuda.synchronize()
        elapsed = (time.perf_counter() - t0) / TIMED_ITERS
    return elapsed * 1000, out.float().cpu()


def main() -> int:
    from modules.tts_module.s2mel.modules.bigvgan import bigvgan as bigvgan_mod

    device = torch.device("cuda")
    torch.manual_seed(42)
    mel32 = torch.randn(1, 80, MEL_FRAMES, device=device)

    results = {}
    baseline = None
    for kernel in (False, True):
        try:
            model = bigvgan_mod.BigVGAN.from_pretrained(str(BIGVGAN_DIR), use_cuda_kernel=kernel)
        except Exception as exc:  # noqa: BLE001
            print(f"kernel={kernel}: from_pretrained 失敗 → {exc!r}")
            continue
        model = model.to(device)
        model.remove_weight_norm()
        model.eval()

        for half in (False, True):
            tag = f"kernel={'ON ' if kernel else 'OFF'} {'FP16' if half else 'FP32'}"
            try:
                m = model.half() if half else model.float()
                mel = mel32.half() if half else mel32
                ms, out = bench(m, mel)
                nan = torch.isnan(out).any().item()
                if baseline is None:
                    baseline = out
                    rel = 0.0
                else:
                    denom = baseline.abs().mean().item() or 1.0
                    rel = (out - baseline).abs().mean().item() / denom
                results[tag] = ms
                print(f"{tag}: {ms:.0f}ms/24s音訊  NaN={nan}  相對誤差={rel:.4f}")
            except Exception as exc:  # noqa: BLE001
                print(f"{tag}: 失敗 → {exc!r}")
            # half() 就地修改模型，跑完 FP16 後此 model 實例不再還原，直接換下一個 kernel 變體
        del model
        torch.cuda.empty_cache()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
