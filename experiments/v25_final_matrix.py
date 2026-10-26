"""IndexTTS 2.5 最終決選矩陣（艾斯維爾 2026-08-19 定版）。

A 組 = slice_03（yapping 3:02 起 15 秒）、B 組 = slice_04（4:02 起）。
每組 10 變體：zh / zh+LoRA / zh happy .5 / zh sad .5 / en / en happy .5 /
en sad .5 / ja / ja happy .65 / ja sad .65 —— 各情緒使用專屬提示詞。

情感向量順序：[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]

用法：
    python experiments/v25_final_matrix.py
"""
import os
import sys
import time

UPSTREAM_REPO = r"C:\Users\Bernie\source\repos\Other\index-tts\index-tts"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints_v25")
SLICE_DIR = os.path.join(PROJECT_ROOT, "outputs", "yapping_slices")
OUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "v25_final")
LORA_DIR = os.path.join(PROJECT_ROOT, "experiments", "index-tts-lora", "finetune_models", "checkpoints", "gpt_best")

sys.path.insert(0, UPSTREAM_REPO)

import transformers.pytorch_utils as _pu

if not hasattr(_pu, "is_torch_greater_or_equal_than_1_13"):
    _pu.is_torch_greater_or_equal_than_1_13 = True

GROUPS = {
    "A": os.path.join(SLICE_DIR, "slice_03_at182s.wav"),
    "B": os.path.join(SLICE_DIR, "slice_04_at242s.wav"),
}

def emo(happy=0.0, sad=0.0):
    return [happy, 0, sad, 0, 0, 0, 0, 0]

# 各情緒專屬提示詞（中性=日常報告、happy=興奮分享、sad=低落傾訴）
TEXTS = {
    "zh_neutral": "今天的天气不错，我把桌面上的工作都整理完了，接下来要做什么呢？",
    "zh_happy": "哈哈，你猜怎么着？新模型的测试一次就通过了！我们赶快把它装上去试试吧！",
    "zh_sad": "我...对不起……我把之前的存档弄丢了，明明答应过你要好好保管的……",
    "en_neutral": "The weather is nice today. I finished organizing everything on my desk. What should we do next?",
    "en_happy": "Haha! Guess what? The new model passed every single test on the first try! Let's get it running right now!",
    "en_sad": "I...I'm sorry... I lost the backup files. I promised I would keep them safe, and I let you down...",
    "ja_neutral": "今日はいい天気ですね。机の上の仕事を全部片付けました。次は何をしましょうか？",
    "ja_happy": "ねえねえ、聞いて！新しいモデルのテスト、一発で全部合格したんだよ！すごくない？",
    "ja_sad": "あ...ごめんなさい……大事なデータを失くしちゃった……ちゃんと守るって約束したのに……",
}

# (變體名, lang, 文本 key, 情感向量或 None)；LoRA 案例另列
VARIANTS = [
    ("zh",          "ZH", "zh_neutral", None),
    ("zh_happy05",  "ZH", "zh_happy",   emo(happy=0.5)),
    ("zh_sad05",    "ZH", "zh_sad",     emo(sad=0.5)),
    ("en",          "EN", "en_neutral", None),
    ("en_happy05",  "EN", "en_happy",   emo(happy=0.5)),
    ("en_sad05",    "EN", "en_sad",     emo(sad=0.5)),
    ("ja",          "JA", "ja_neutral", None),
    ("ja_happy065", "JA", "ja_happy",   emo(happy=0.65)),
    ("ja_sad065",   "JA", "ja_sad",     emo(sad=0.65)),
]


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    from indextts.infer_v2_5 import IndexTTS2

    tts = IndexTTS2(
        cfg_path=os.path.join(CKPT_DIR, "config.yaml"),
        model_dir=CKPT_DIR,
        use_bf16=True,
    )
    print(">> 模型就緒", flush=True)

    def gen(name, ref, lang, text, vec):
        kwargs = {"emo_vector": vec} if vec else {}
        t0 = time.perf_counter()
        tts.infer(
            spk_audio_prompt=ref,
            text=text,
            lang=lang,
            output_path=os.path.join(OUT_DIR, f"{name}.wav"),
            **kwargs,
        )
        print(f">> {name}: {time.perf_counter() - t0:.2f}s", flush=True)

    # 無 LoRA 案例（9 變體 × 2 組）
    for group, ref in GROUPS.items():
        for variant, lang, text_key, vec in VARIANTS:
            gen(f"{group}_{variant}", ref, lang, TEXTS[text_key], vec)

    # 掛 LoRA 後跑中文中性（2 組）
    from peft import PeftModel

    tts.gpt.inference_model = PeftModel.from_pretrained(
        tts.gpt.inference_model, LORA_DIR, is_trainable=False
    )
    tts.gpt.inference_model.eval()
    if tts.use_bf16:
        tts.gpt.inference_model = tts.gpt.inference_model.bfloat16()
    print(">> LoRA mounted", flush=True)

    for group, ref in GROUPS.items():
        gen(f"{group}_zh_LORA", ref, "ZH", TEXTS["zh_neutral"], None)
        gen(f"{group}_zh_LORA_happy05", ref, "ZH", TEXTS["zh_happy"], emo(happy=0.5))
        gen(f"{group}_zh_LORA_sad05", ref, "ZH", TEXTS["zh_sad"], emo(sad=0.5))

    print(f"\n=== 最終矩陣完成（24 檔）：{OUT_DIR} ===")


if __name__ == "__main__":
    main()
