"""``python -m uep.console`` 進入點。"""
from __future__ import annotations

import sys

from uep.console.app import main
from uep.log import configure_logging
from uep.memtrace import start_from_settings as start_mem_trace


def _use_utf8() -> None:
    """Windows 主控台預設 cp950 會讓管線輸入的中文變成無效字元；改為 UTF-8（輸出同）。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _use_utf8()
    configure_logging()
    start_mem_trace()  # UEP_MEM_TRACE=1 時開始記錄記憶體（診斷 commit 耗盡造成的原生崩潰）
    sys.exit(main())
