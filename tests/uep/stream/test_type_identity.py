"""N1 型別雙身分防護：Core 與全部現存 adapter 只認 ``uep.stream`` 的契約型別（D105 Q4）。

若 adapter 仍從 ``echo_stream`` 取契約，同名 dataclass 會成為兩個不同型別，``isinstance`` 與序列化靜默失效。
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

from tests.uep.stream.conftest import FakeTTSBackend as FakeBackend
from tests.uep.stream.conftest import sentence_stream
from uep.stream.adapters.speak_indextts import IndexTTS2SpeakStage
from uep.stream.adapters.think_llm import EchoBackend, LLMThinkStage
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import AudioChunk, Sentence, Utterance

REPO_ROOT = Path(__file__).resolve().parents[3]

ADAPTER_MODULES = (
    "think_llm", "speak_indextts", "speak_vox", "input_stt", "input_multichannel",
    "memory_echo", "think_config", "output_guard",
)


async def test_think_adapter_產出的是_core_版_Sentence():
    stage = LLMThinkStage(EchoBackend(), system_prompt="人設")
    sentences = [s async for s in stage.stream(Utterance(text="嗨。"), CancellationToken())]
    assert sentences
    assert all(isinstance(s, Sentence) for s in sentences)
    assert Sentence.__module__ == "uep.stream.contracts.types"


async def test_speak_adapter_產出的是_core_版_AudioChunk():
    stage = IndexTTS2SpeakStage(backend=FakeBackend(segments_per_sentence=2))
    chunks = [c async for c in stage.stream(sentence_stream(["一。", "二。"]), CancellationToken())]
    assert chunks
    assert all(isinstance(c, AudioChunk) for c in chunks)


def test_adapter_模組引用的契約型別全部來自_uep_stream():
    from uep.stream import contracts

    for name in ADAPTER_MODULES:
        mod = importlib.import_module(f"uep.stream.adapters.{name}")
        for attr, value in vars(mod).items():
            module_name = getattr(value, "__module__", "") or ""
            assert not module_name.startswith("echo_stream"), f"{name}.{attr} 來自 {module_name}"
            if attr in {"CancellationToken", "Sentence", "Utterance", "AudioChunk", "VoiceEvent", "SpeechStyle"}:
                assert value is getattr(contracts, attr), f"{name}.{attr} 與 uep.stream.contracts 不是同一個型別"


def test_匯入_uep_stream_與_uep_echo_不會載入_echo_stream():
    """獨立行程驗證：整個 uep.stream 與 uep.echo 匯入後，sys.modules 不應出現 echo_stream。"""
    code = (
        "import importlib, pkgutil, sys\n"
        "import uep.stream, uep.echo\n"
        "for pkg in (uep.stream, uep.echo):\n"
        "    for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + '.'):\n"
        "        importlib.import_module(m.name)\n"
        "bad = sorted(n for n in sys.modules if n == 'echo_stream' or n.startswith('echo_stream.'))\n"
        "print('LEAK:' + ','.join(bad) if bad else 'CLEAN')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "CLEAN", out.stdout
