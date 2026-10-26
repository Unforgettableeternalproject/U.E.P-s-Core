"""停頓比對實驗（二）：上游 infer_v2_5 端生成。

與 v25_pause_compare_ours.py 相同文本，參考音使用與 uep_v25.pt 同源的
uep_reference_v25.wav，輸出至同一比對目錄。
"""
from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

UPSTREAM_REPO = r"C:\Users\Bernie\source\repos\Other\index-tts\index-tts"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints_v25")
SPK_PROMPT = os.path.join(PROJECT_ROOT, "echo_tts", "assets", "reference", "uep_reference_v25.wav")
OUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "experiments", "pause_compare")

sys.path.insert(0, UPSTREAM_REPO)

# transformers 4.48.1 已移除此符號，上游 vendored GPT2 仍會 import（同 v25_smoke_test）
import transformers.pytorch_utils as _pu

if not hasattr(_pu, "is_torch_greater_or_equal_than_1_13"):
    _pu.is_torch_greater_or_equal_than_1_13 = True

TEXTS = {
    "ja": ("JA", "また雨が降ってる。こういう日は、昔のことを思い出すんだ。"),
    "zh": ("ZH", (
        "今天的天氣真的很不錯，我們決定一起去山上走走。"
        "沿途的風景美得讓人說不出話來，遠方的雲海在陽光下泛著金色的光。"
        "你說，下次還要再來一次，我笑著點了點頭。"
        "這樣的日子，真希望能一直持續下去。"
    )),
}


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    from indextts.infer_v2_5 import IndexTTS2

    tts = IndexTTS2(
        cfg_path=os.path.join(CKPT_DIR, "config.yaml"),
        model_dir=CKPT_DIR,
        use_bf16=True,
    )
    for key, (lang, text) in TEXTS.items():
        tts.infer(
            spk_audio_prompt=SPK_PROMPT,
            text=text,
            lang=lang,
            output_path=os.path.join(OUT_DIR, f"upstream_{key}.wav"),
        )
    print("UPSTREAM DONE")


if __name__ == "__main__":
    main()
