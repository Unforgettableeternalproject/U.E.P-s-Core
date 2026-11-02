"""``python -m uep.console`` 進入點。"""
from __future__ import annotations

import sys

from uep.console.app import main


def _use_utf8() -> None:
    """Windows 主控台預設 cp950 會讓管線輸入的中文變成無效字元；改為 UTF-8（輸出同）。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _use_utf8()
    sys.exit(main())
