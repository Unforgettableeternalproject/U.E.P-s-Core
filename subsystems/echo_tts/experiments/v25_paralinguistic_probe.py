"""IndexTTS 2.5 原生副語言音效探測 — 驗證模型能否直接「演」出笑聲/嘆氣/呼吸等效果。

動機：現有 SFX 拼接素材品質不佳，若模型能靠擬聲詞文字 + 情感向量原生生成
這些聲音，就不需要走預錄素材拼接路線（Fish S2 的 marker 精神，用我們的方式實現）。

情感向量順序：[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]

用法：
    python experiments/v25_paralinguistic_probe.py
"""
import os
import sys
import time

UPSTREAM_REPO = r"C:\Users\Bernie\source\repos\Other\index-tts\index-tts"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints_v25")
SPK_PROMPT = os.path.join(PROJECT_ROOT, "outputs", "tts", "shell", "Faraway.wav")
OUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "v25_probe")

sys.path.insert(0, UPSTREAM_REPO)

import transformers.pytorch_utils as _pu

if not hasattr(_pu, "is_torch_greater_or_equal_than_1_13"):
    _pu.is_torch_greater_or_equal_than_1_13 = True

# 2.5 tokenizer 對部分繁體詞元解析不完整，比照 2.0 front.py 先做 t2s 轉換
from opencc import OpenCC

_T2S = OpenCC("t2s")

# (檔名, 文字, 情感向量或 None)
CASES = [
    # 笑聲系：擬聲詞 + happy 推高
    ("laugh_text_only", "哈哈哈哈哈！太好笑了吧！", None),
    ("laugh_happy_08", "哈哈哈哈哈！太好笑了吧！", [0.8, 0, 0, 0, 0, 0, 0, 0]),
    ("chuckle_happy_05", "呵呵，嗯哼，有意思。", [0.5, 0, 0, 0, 0, 0, 0, 0.3]),
    # 嘆氣系：長音擬聲 + melancholic
    ("sigh_text_only", "唉……算了，就這樣吧。", None),
    ("sigh_melancholic_08", "唉……算了，就這樣吧。", [0, 0, 0.2, 0, 0, 0.8, 0, 0]),
    # 驚訝倒抽氣
    ("gasp_surprised_08", "嚇！？你、你什麼時候站在那裡的！", [0, 0, 0, 0.3, 0, 0, 0.8, 0]),
    # 呼吸/停頓感：刪節號 + calm
    ("breath_calm", "嗯……讓我想想……應該、大概是這樣沒錯。", [0, 0, 0, 0, 0, 0, 0, 0.9]),
    # 哭腔
    ("sob_sad_08", "嗚……嗚嗚……為什麼會變成這樣……", [0, 0, 0.9, 0, 0, 0.3, 0, 0]),
]


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    from indextts.infer_v2_5 import IndexTTS2

    tts = IndexTTS2(
        cfg_path=os.path.join(CKPT_DIR, "config.yaml"),
        model_dir=CKPT_DIR,
        use_bf16=True,
    )
    print(">> 模型就緒，開始探測", flush=True)

    results = []
    for name, text, emo in CASES:
        out_path = os.path.join(OUT_DIR, f"{name}.wav")
        t0 = time.perf_counter()
        kwargs = {}
        if emo is not None:
            kwargs["emo_vector"] = emo
        tts.infer(
            spk_audio_prompt=SPK_PROMPT,
            text=_T2S.convert(text),
            lang="ZH",
            output_path=out_path,
            **kwargs,
        )
        dt = time.perf_counter() - t0
        results.append((name, dt))
        print(f">> {name}: {dt:.2f}s -> {out_path}", flush=True)

    print("\n=== 探測完成 ===")
    for name, dt in results:
        print(f"  {name}: {dt:.2f}s")
    print(f"輸出目錄：{OUT_DIR}")


if __name__ == "__main__":
    main()
