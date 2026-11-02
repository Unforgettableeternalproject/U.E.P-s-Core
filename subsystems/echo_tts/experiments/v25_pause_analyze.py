"""停頓比對實驗（三）：靜音分布掃描。

對 pause_compare/ 目錄下所有 wav 做 20ms 窗 RMS 掃描，
列出內部靜音段（>150ms，排除首尾），輸出位置與時長，
用於比對「異樣停頓」在哪個變體出現、由什麼機制產生。
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IN_DIR = PROJECT_ROOT / "outputs" / "experiments" / "pause_compare"

WINDOW_MS = 20
MIN_PAUSE_MS = 150
# 靜音門檻：全檔 RMS 峰值的 2%（相對門檻，對不同響度的檔案都適用）
SILENCE_RATIO = 0.02


def analyze(path: Path) -> None:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    dur_s = len(audio) / sr

    win = int(sr * WINDOW_MS / 1000)
    n_win = len(audio) // win
    rms = np.sqrt(np.mean(
        audio[: n_win * win].reshape(n_win, win) ** 2, axis=1))
    threshold = rms.max() * SILENCE_RATIO
    quiet = rms < threshold

    # 收集連續靜音 run
    runs: list[tuple[float, float]] = []  # (start_s, dur_ms)
    start = None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            dur_ms = (i - start) * WINDOW_MS
            runs.append((start * WINDOW_MS / 1000, dur_ms))
            start = None
    if start is not None:
        runs.append((start * WINDOW_MS / 1000, (n_win - start) * WINDOW_MS))

    # 排除首尾、過短
    internal = [
        (t, d) for t, d in runs
        if d >= MIN_PAUSE_MS and t > 0.1 and (t + d / 1000) < dur_s - 0.1
    ]

    print(f"\n── {path.name}  (時長 {dur_s:.2f}s，內部停頓 {len(internal)} 個) ──")
    for t, d in internal:
        marker = " ⚠️" if d >= 500 else ""
        print(f"   @{t:6.2f}s  {d:5.0f}ms{marker}")


def main() -> int:
    files = sorted(IN_DIR.glob("*.wav"))
    if not files:
        print(f"找不到 wav：{IN_DIR}")
        return 1
    for f in files:
        analyze(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
