"""回歸：Laya 載入路徑與記憶 embedding 暖機路徑並行第一次 import transformers 不得 ImportError。

兩條路徑在組裝期同時發生（Laya 背景執行緒 vs 主執行緒的記憶暖機）。每輪用全新子行程，避免本行程已 import 過
transformers 而測不出來；只 import，不載任何模型權重。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROUNDS = 4

_SCRIPT = r"""
import sys, threading
sys.path.insert(0, {core!r})
sys.path.insert(0, {em!r})
from uep.echo import import_guard, laya_wiring, memory_wiring

if {unguarded!r}:  # 對照組：拿掉鎖，重現修正前的行為
    class _NoLock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    for mod in (laya_wiring, memory_wiring):
        mod.transformers_import_lock = _NoLock()

errors = []
barrier = threading.Barrier(2)

def run(fn):
    barrier.wait()
    try:
        fn()
    except BaseException as exc:
        errors.append(f"{{type(exc).__name__}}: {{exc}}")

threads = [
    threading.Thread(target=run, args=(laya_wiring.preimport_laya_deps,)),
    threading.Thread(target=run, args=(memory_wiring.preimport_embedding_deps,)),
]
for t in threads: t.start()
for t in threads: t.join()
print("\n".join(errors))
sys.exit(1 if errors else 0)
"""


def _run_once(unguarded: bool) -> subprocess.CompletedProcess[str]:
    script = _SCRIPT.format(
        core=str(ROOT), em=str(ROOT / "subsystems" / "echo_memory"), unguarded=unguarded,
    )
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=300, cwd=ROOT, check=False)


def _deps_available() -> bool:
    probe = subprocess.run(
        [sys.executable, "-c", "import laya, transformers, sentence_transformers"], capture_output=True, timeout=120, check=False,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _deps_available(), reason="需要 laya／transformers／sentence-transformers")


@pytest.mark.parametrize("round_no", range(ROUNDS))
def test_laya_and_embedding_imports_do_not_race(round_no: int) -> None:
    result = _run_once(unguarded=False)
    assert result.returncode == 0, result.stdout + result.stderr
