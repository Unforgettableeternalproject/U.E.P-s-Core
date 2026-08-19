"""Per-run warm profiling 工具。

`Entry.py profile --repeat N` 的 summary 只給平均值，首輪冷啟/編譯會灌水。
本腳本逐輪印出各階段耗時，方便分離 cold / warm 數據。

用法：
    python experiments/v25_warm_profile.py [runs]
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

TEXT = (
    "今天的天氣真的很不錯，我們決定一起去山上走走。"
    "沿途的風景美得讓人說不出話來，遠方的雲海在陽光下泛著金色的光。"
    "你說，下次還要再來一次，我笑著點了點頭。"
    "這樣的日子，真希望能一直持續下去。"
)


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    # use_accel=True 與 Entry.py shell/profile 的預設一致（Backend 類別預設是 False）
    backend = IndexTTS2Backend(paths=default_generation_paths(PROJECT_ROOT), use_accel=True)
    out_dir = PROJECT_ROOT / "outputs" / "experiments"
    out_dir.mkdir(parents=True, exist_ok=True)

    for i in range(runs):
        _, report = backend.profile_generate(TEXT, out_dir / f"warm_profile_{i}.wav")
        s = report.synthesis
        print(
            f"run {i + 1}/{runs}: total={s.total_wall_ms:.0f}ms "
            f"gpt={s.gpt_semantic_ms:.0f}ms s2mel={s.s2mel_cfm_ms:.0f}ms "
            f"vocoder={s.vocoder_ms:.0f}ms first_segment={s.first_segment_ms:.0f}ms "
            f"audio={s.audio_duration_ms:.0f}ms rtf={s.rtf:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
