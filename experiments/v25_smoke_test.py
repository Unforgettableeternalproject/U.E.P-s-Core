"""IndexTTS 2.5 smoke test — 直接使用上游 infer_v2_5 驗證 checkpoint 可用性。

用法：
    python experiments/v25_smoke_test.py [--import-only]

- 使用上游 repo（Other/index-tts）的 infer_v2_5，不動我們的 lite_engine
- 參考音檔暫用 2.0 引擎生成的 U.E.P 語音（原始 sample_prompt.wav 已佚失）
- 首次執行會自動下載輔助模型（w2v-bert-2.0、campplus）到 checkpoints_v25/hf_cache/
"""
import os
import sys
import time

UPSTREAM_REPO = r"C:\Users\Bernie\source\repos\Other\index-tts\index-tts"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints_v25")
SPK_PROMPT = os.path.join(PROJECT_ROOT, "outputs", "tts", "shell", "Faraway.wav")
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "outputs", "v25_smoke.wav")

sys.path.insert(0, UPSTREAM_REPO)

# 我們的 transformers 4.48.1 已移除此符號，上游 vendored GPT2 程式碼仍會 import；
# 2.0 移植版是直接寫死 True，這裡用 shim 墊回去，不修改上游檔案
import transformers.pytorch_utils as _pu

if not hasattr(_pu, "is_torch_greater_or_equal_than_1_13"):
    _pu.is_torch_greater_or_equal_than_1_13 = True


def main() -> None:
    t0 = time.perf_counter()
    from indextts.infer_v2_5 import IndexTTS2

    print(f">> import OK ({time.perf_counter() - t0:.2f}s)")
    if "--import-only" in sys.argv:
        return

    t0 = time.perf_counter()
    tts = IndexTTS2(
        cfg_path=os.path.join(CKPT_DIR, "config.yaml"),
        model_dir=CKPT_DIR,
        use_bf16=True,
    )
    init_s = time.perf_counter() - t0
    print(f">> IndexTTS2 v2.5 初始化完成：{init_s:.2f}s")

    text = "系統重啟完成。這是二點五版模型的第一次語音合成測試，聽起來如何？"
    t0 = time.perf_counter()
    tts.infer(
        spk_audio_prompt=SPK_PROMPT,
        text=text,
        lang="ZH",
        output_path=OUTPUT_PATH,
    )
    infer_s = time.perf_counter() - t0

    import torchaudio

    info = torchaudio.info(OUTPUT_PATH)
    audio_s = info.num_frames / info.sample_rate
    print(f">> 合成完成：{infer_s:.2f}s，音長 {audio_s:.2f}s，RTF={infer_s / audio_s:.3f}")
    print(f">> 輸出：{OUTPUT_PATH}")


if __name__ == "__main__":
    main()
