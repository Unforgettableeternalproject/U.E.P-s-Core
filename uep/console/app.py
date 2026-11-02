"""``python -m uep.console`` 的啟動、參數與 REPL。"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from uep.console.commands import Console
from uep.console.rig import MODES, THINKS, TTS_CHOICES, ConsoleError, build_runtime, connect_database

DEFAULT_SESSION_DIR = Path("outputs") / "e2e"
PROMPT = "uep> "


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m uep.console", description="U.E.P Core 端到端驗收 Console（v0）")
    parser.add_argument("--mode", choices=MODES, default="fake", help="fake（預設）或 real（Think 與 TTS 真實；真實 TTS 需 UEP_E2E_ALLOW_GPU=1）")
    parser.add_argument("--think", choices=THINKS, default=None, help="覆寫 Think：real＝真實 LLM（需 UEP_LLM_API_KEY），例如 --think real＝Think 真實、STT／TTS fake")
    parser.add_argument("--tts", choices=TTS_CHOICES, default=None, help="覆寫 Speak：real＝真實 TTS（需 GPU 與 UEP_E2E_ALLOW_GPU=1），例如 --mode real --tts fake＝Think 真實、TTS fake")
    parser.add_argument("--fake", action="store_true", help="等同 --mode fake")
    parser.add_argument("--no-tts", action="store_true", help="不落地音訊（丟棄 TTS 輸出）")
    parser.add_argument("--no-stt", action="store_true", help="停用 STT 輸入（v0 fake 模式本就不經真 STT，保留旗標）")
    parser.add_argument("--session-dir", type=Path, default=DEFAULT_SESSION_DIR, help="音訊與 host epoch 的落地目錄")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None, *, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print,
) -> int:
    args = parse_args(argv)
    mode = "fake" if args.fake else args.mode
    try:
        session_factory = connect_database()
        runtime = build_runtime(mode, session_factory, args.session_dir, think=args.think, tts=args.tts, with_tts=not args.no_tts)
    except ConsoleError as exc:
        out(f"啟動失敗：{exc}")
        return 2
    console = Console(runtime, out=out)
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
