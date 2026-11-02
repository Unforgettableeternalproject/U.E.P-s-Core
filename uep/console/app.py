"""``python -m uep.console`` 的啟動、參數與 REPL。"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from uep.console.commands import DEFAULT_WAIT_S, Console
from uep.console.rig import (
    MEMORY_CHOICES, MODES, STT_CHOICES, THINKS, TTS_CHOICES, ConsoleError, build_runtime, connect_database,
)

DEFAULT_SESSION_DIR = Path("outputs") / "e2e"
PROMPT = "uep> "
REAL_WAIT_S = 90.0
"""real 模式等 turn 結束的上限（首輪含 VOX 冷合成，實測可超過 15 秒）。"""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m uep.console", description="U.E.P Core 端到端驗收 Console（v0）")
    parser.add_argument("--mode", choices=MODES, default="fake", help="fake（預設）或 real（STT／Think／TTS／記憶全真實；需 UEP_E2E_ALLOW_GPU=1）")
    parser.add_argument("--think", choices=THINKS, default=None, help="覆寫 Think：real＝真實 LLM（需 UEP_LLM_API_KEY），例如 --think real＝Think 真實、STT／TTS fake")
    parser.add_argument("--tts", choices=TTS_CHOICES, default=None, help="覆寫 Speak：real＝真實 TTS（需 GPU 與 UEP_E2E_ALLOW_GPU=1），例如 --mode real --tts fake＝Think 真實、TTS fake")
    parser.add_argument("--stt", choices=STT_CHOICES, default=None, help="覆寫 STT：real＝真實 faster-whisper（/wav、/mic 走真實辨識）")
    parser.add_argument("--memory", choices=MEMORY_CHOICES, default=None, help="覆寫記憶：real＝EM engine＋bge-m3（資料根取 UEP_MEMORY_DATA_ROOT）")
    parser.add_argument("--play", action="store_true", help="TTS 音訊同步送本機播放（需 sounddevice；請戴耳機，外放會被麥克風收回而自我觸發）")
    parser.add_argument("--fake", action="store_true", help="等同 --mode fake")
    parser.add_argument("--no-tts", action="store_true", help="不落地音訊（丟棄 TTS 輸出）")
    parser.add_argument("--no-stt", action="store_true", help="等同 --stt fake")
    parser.add_argument("--session-dir", type=Path, default=DEFAULT_SESSION_DIR, help="音訊與 host epoch 的落地目錄")
    return parser.parse_args(argv)


def out_startup_plan(mode: str, args: argparse.Namespace, out: Callable[[str], None]) -> None:
    """real 元件載入前先提示（載入要數十秒到數分鐘）與耳機提醒。"""
    if mode != "real" and not (args.stt == "real" or args.tts == "real" or args.memory == "real"):
        return
    out("載入真實元件（STT／Think／Speak／記憶），完成前不接受輸入；VOX 首次載入可能超過一分鐘…")
    if args.play:
        out("提醒：--play 請戴耳機；喇叭外放會被麥克風收回而自我觸發（A4 會假失敗）。")


def report_startup(runtime, out: Callable[[str], None]) -> None:
    """印出各元件 warmup 耗時與記憶資料根。"""
    labels = {"tts": "TTS（Speak）", "think": "Think", "stt": "STT（ASR 含暖機）", "memory_embedding": "記憶 embedding（bge-m3）"}
    for key, label in labels.items():
        if key in runtime.warmups:
            out(f"  warmup {label}：{runtime.warmups[key]:.2f} 秒")
    if runtime.memory_root is not None:
        out(f"  記憶資料根：{runtime.memory_root}")


def main(
    argv: Sequence[str] | None = None, *, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print,
) -> int:
    args = parse_args(argv)
    mode = "fake" if args.fake else args.mode
    try:
        session_factory = connect_database()
        out_startup_plan(mode, args, out)
        runtime = build_runtime(
            mode, session_factory, args.session_dir, think=args.think, tts=args.tts,
            stt="fake" if args.no_stt else args.stt, memory=args.memory, play=args.play, with_tts=not args.no_tts,
        )
    except ConsoleError as exc:
        out(f"啟動失敗：{exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - 載入／啟動失敗（逾時、模型缺檔）：訊息只含類型與說明，不含連線字串
        out(f"啟動失敗（元件載入）：{type(exc).__name__}：{exc}")
        return 2
    console = Console(runtime, out=out, input_fn=input_fn, wait_s=REAL_WAIT_S if mode == "real" else DEFAULT_WAIT_S)
    report_startup(runtime, out)
    out(f"U.E.P Console v0（{mode} 模式，think={runtime.mode}）。輸入 /help 查看指令，/quit 結束。音訊目錄：{runtime.session_dir}")
    try:
        while True:
            try:
                line = input_fn(PROMPT)
            except (EOFError, KeyboardInterrupt):
                break
            if not console.execute(line):
                break
    finally:
        runtime.close()
    return 0
