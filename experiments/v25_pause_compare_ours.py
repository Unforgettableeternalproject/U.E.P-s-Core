"""停頓比對實驗（一）：lite_engine 端生成。

同文本生成兩個變體：
  - default：shell 現況設定（poise 開、interval_silence 200ms）
  - nopoise：poise 關閉（隔離 poise 靜音/呼吸段的影響）

搭配 v25_pause_compare_upstream.py（上游 infer_v2_5）與
v25_pause_analyze.py（靜音分布掃描）使用。
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from echo_tts.runtime.indextts_backend import IndexTTS2Backend
from echo_tts.runtime.indextts_generate import default_generation_paths

TEXTS = {
    "ja": "また雨が降ってる。こういう日は、昔のことを思い出すんだ。",
    "zh": (
        "今天的天氣真的很不錯，我們決定一起去山上走走。"
        "沿途的風景美得讓人說不出話來，遠方的雲海在陽光下泛著金色的光。"
        "你說，下次還要再來一次，我笑著點了點頭。"
        "這樣的日子，真希望能一直持續下去。"
    ),
}

OUT_DIR = PROJECT_ROOT / "outputs" / "experiments" / "pause_compare"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    backend = IndexTTS2Backend(paths=default_generation_paths(PROJECT_ROOT), use_accel=True)
    backend.load()

    for lang, text in TEXTS.items():
        backend.generate(text, OUT_DIR / f"ours_default_{lang}.wav", language=lang)

    # 關閉 poise 後重生成（PoiseConfig 為執行期物件，直接改屬性）
    backend.engine.poise_config.enabled = False
    for lang, text in TEXTS.items():
        backend.generate(text, OUT_DIR / f"ours_nopoise_{lang}.wav", language=lang)

    print("OURS DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
