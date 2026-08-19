"""sm_120 S2Mel torch.compile 探測實驗。

背景：lite_engine 的 sm_120 防護基於早期實測（inductor 對 S2Mel 產生錯誤結果、
BigVGAN 大幅變慢）。本腳本在目前工具鏈（torch 2.7.0+cu128 + triton-windows
3.3.1.post21）下重測 S2Mel CFM estimator 的 torch.compile：

1. baseline：accel ON、無 compile，warm 一次後量測
2. compiled：呼叫 s2mel.enable_torch_compile() 後，首次（含編譯）與 warm 各量一次
3. 音訊健全性：NaN / RMS / 時長比對

用法：
    python experiments/v25_compile_s2mel_probe.py
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from echo_tts.runtime.indextts_generate import default_generation_paths, validate_generation_paths

TEXT = "遠方的雲海在陽光下泛著金色的光，我們決定一起去山上走走。"
OUT_DIR = PROJECT_ROOT / "outputs" / "experiments"


def wav_stats(path: Path) -> dict:
    """讀取 wav 的時長與 RMS，作為健全性 proxy。"""
    import audioop
    with wave.open(str(path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        duration = wf.getnframes() / wf.getframerate()
        rms = audioop.rms(frames, wf.getsampwidth())
    return {"duration_s": round(duration, 2), "rms": rms}


def timed_synth(engine, tag: str) -> dict:
    out = OUT_DIR / f"compile_probe_{tag}.wav"
    t0 = time.perf_counter()
    ok, timings = engine.synthesize_with_profile(text=TEXT, output_path=str(out))
    wall = time.perf_counter() - t0
    if not ok or not out.exists():
        return {"tag": tag, "ok": False, "wall_s": round(wall, 2)}
    stats = wav_stats(out)
    return {
        "tag": tag,
        "ok": True,
        "wall_s": round(wall, 2),
        "gpt_ms": round(timings.get("gpt_semantic_ms", 0)),
        "s2mel_ms": round(timings.get("s2mel_cfm_ms", 0)),
        "vocoder_ms": round(timings.get("vocoder_ms", 0)),
        **stats,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = default_generation_paths(PROJECT_ROOT)
    validate_generation_paths(paths)
    sys.path.insert(0, str(paths.engine_root))

    from modules.tts_module.lite_engine import IndexTTSLite

    engine = IndexTTSLite(
        cfg_path=str(paths.cfg_path),
        model_dir=str(paths.model_dir),
        use_fp16=True,
        device="cuda",
        use_accel=True,
    )
    engine.load_character(paths.character_path)

    results = []
    # baseline：第一輪當 warmup 丟棄，第二輪為 warm 基準
    timed_synth(engine, "warmup")
    results.append(timed_synth(engine, "baseline_warm"))

    # 繞過 sm_120 防護，直接對 CFM estimator 開 compile
    s2mel = engine.s2mel
    if not hasattr(s2mel, "enable_torch_compile"):
        print("RESULT: s2mel 無 enable_torch_compile 方法，實驗中止")
        return 1
    try:
        s2mel.enable_torch_compile()
    except Exception as exc:  # noqa: BLE001
        print(f"RESULT: enable_torch_compile 失敗 → {exc!r}")
        return 1

    try:
        results.append(timed_synth(engine, "compiled_first"))
        results.append(timed_synth(engine, "compiled_warm"))
    except Exception as exc:  # noqa: BLE001
        print(f"RESULT: compile 後合成崩潰 → {exc!r}")
        for r in results:
            print(r)
        return 1

    print("=" * 60)
    for r in results:
        print(r)
    base = next(r for r in results if r["tag"] == "baseline_warm")
    warm = next((r for r in results if r["tag"] == "compiled_warm"), None)
    if warm and warm["ok"] and base["ok"]:
        ratio = base["s2mel_ms"] / max(warm["s2mel_ms"], 1)
        rms_ratio = warm["rms"] / max(base["rms"], 1)
        print(f"S2Mel warm 加速比: {ratio:.2f}x；RMS 比值: {rms_ratio:.2f}"
              f"（遠離 1.0 或趨近 0 代表音訊可能壞掉，需人耳複核）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
