"""VoxBackend 單元測試（不需 GPU / 模型）：語言判斷、檔位、前處理、manifest、鎖的釋放。"""
import threading
import time

import numpy as np
import pytest

from echo_tts.runtime import vox_backend as vb
from echo_tts.runtime.vox_backend import VoxBackend, detect_lang, load_manifest, normalize_lang, snap_speed
from echo_tts.text.taiwan_pronunciation import TaiwanPronunciationNormalizer


@pytest.mark.parametrize(
    "text,expected",
    [
        ("今天天氣很好。", "zh"),
        ("The weather is nice today.", "en"),
        ("こんにちは、元気ですか？", "ja"),
        ("机の上の仕事を全部片付けました。", "ja"),  # 含漢字的日文，先看假名
        ("一発で全部合格した", "ja"),
        ("我今天用 Python 寫了一個 API。", "zh"),  # 中英混雜
        ("OK, 好的", "zh"),
        ("123 ...", "en"),
    ],
)
def test_detect_lang(text, expected):
    assert detect_lang(text) == expected


def test_normalize_lang():
    assert normalize_lang(None, "hello") == "en"
    assert normalize_lang("ZH-TW", "hello") == "zh"
    assert normalize_lang("ja_JP", "x") == "ja"
    with pytest.raises(ValueError):
        normalize_lang("fr", "x")


def test_snap_speed():
    assert snap_speed(None, "zh") == 1.2
    assert snap_speed(None, "en") == 1.0
    assert snap_speed(None, "ja") == 1.0
    assert snap_speed(1.0, "zh") == 1.0
    assert snap_speed(1.04, "zh") == 1.0
    assert snap_speed(1.06, "zh") == 1.1
    assert snap_speed(1.19, "zh") == 1.2
    assert snap_speed(3.0, "zh") == 1.2
    assert snap_speed(0.5, "zh") == 1.0
    assert snap_speed(1.2, "en", (1.0,)) == 1.0  # 該語言只有 1.0 檔
    with pytest.raises(ValueError):
        snap_speed("fast", "zh")


@pytest.mark.parametrize(
    "word,expect",
    [
        ("步驟", "步{zou4}"),
        ("週末", "{zhou1}末"),
        ("鍋", "{guo1}"),
        ("調校", "{tiao2}{jiao4}"),
        ("重構", "{chong2}{gou4}"),
        ("很差", "很{cha1}"),
        ("亞洲", "{ya3}洲"),
    ],
)
def test_taiwan_pronunciation(word, expect):
    assert TaiwanPronunciationNormalizer().normalize(word) == expect


def test_taiwan_protects_existing_braces():
    assert TaiwanPronunciationNormalizer().normalize("{zou4}步驟") == "{zou4}步{zou4}"


def test_kana_normalizer():
    pytest.importorskip("pyopenjtalk")
    from echo_tts.text.japanese_kana import JapaneseKanaNormalizer

    n = JapaneseKanaNormalizer()
    assert n.normalize("机") == "つくえ"
    assert n.normalize("一発") == "いっぱつ"
    assert n.normalize("こんにちは。") == "こんにちは。"  # 無漢字原樣
    assert "..." in n.normalize("あ...ごめん")  # 省略號維持半形


def test_manifest_loads_and_files_exist():
    m = load_manifest(vb.DEFAULT_ASSETS_DIR)
    assert set(m) == {"zh", "en", "ja"}
    assert m["zh"]["mode"] == "ult" and sorted(m["zh"]["speeds"]) == [1.0, 1.1, 1.2]
    assert all(v["transcript"] for v in m["zh"]["speeds"].values())
    assert m["en"]["mode"] == "ref" and m["ja"]["mode"] == "ref"


def test_manifest_missing_file(tmp_path):
    (tmp_path / "manifest.json").write_text(
        '{"languages": {"zh": {"mode": "ref", "speeds": {"1.0": {"wav": "nope.wav"}}}}}', encoding="utf-8"
    )
    with pytest.raises(FileNotFoundError):
        load_manifest(tmp_path)


def test_config_requires_paths():
    with pytest.raises(ValueError):
        VoxBackend({"lora_dir": "x"})
    with pytest.raises(ValueError):
        VoxBackend({"model_dir": "x"})
    b = VoxBackend({"model_dir": "m", "lora_dir": "l"})
    assert b.device == "cuda" and b.optimize is True and b.assets_dir == vb.DEFAULT_ASSETS_DIR
    assert VoxBackend.sample_rate == 48000


# ---------------------------------------------------------------- 假模型：鎖與串流
class _FakeRaw:
    """模擬底層 generator：被 close 時記錄。"""

    def __init__(self, n, delay=0.0):
        self.closed = False
        self._it = self._run(n, delay)

    def _run(self, n, delay):
        try:
            for i in range(n):
                if delay:
                    time.sleep(delay)
                yield np.full(4, i, dtype=np.float64)
        finally:
            self.closed = True

    def __iter__(self):
        return self._it

    def close(self):
        self._it.close()


def _wrap(raw):
    try:
        yield from raw
    finally:
        raw.close()


class FakeBackend(VoxBackend):
    def __init__(self, n=5, delay=0.0):
        super().__init__({"model_dir": "m", "lora_dir": "l"})
        self.n, self.delay = n, delay
        self.opened = []
        self.lora_calls = []
        self._model = object()
        self._tiers = {"zh": (1.0, 1.1, 1.2), "en": (1.0,), "ja": (1.0,)}
        self._tw = TaiwanPronunciationNormalizer()
        self._caches = {(lg, t): {"lang": lg, "tier": t} for lg, ts in self._tiers.items() for t in ts}

    def _set_lora(self, on):
        self.lora_calls.append(on)

    def _open_stream(self, text, cache):
        raw = _FakeRaw(self.n, self.delay)
        self.opened.append((text, cache, raw))
        return _wrap(raw)


def _lock_free(b):
    got = b._lock.acquire(blocking=False)
    if got:
        b._lock.release()
    return got


def test_stream_routes_lang_speed_and_dtype():
    b = FakeBackend()
    chunks = list(b.stream("步驟", speed=None))
    assert len(chunks) == 5 and all(c.dtype == np.float32 and c.ndim == 1 for c in chunks)
    text, cache, _ = b.opened[-1]
    assert text == "步{zou4}" and cache == {"lang": "zh", "tier": 1.2}
    list(b.stream("步驟", speed=1.0))
    assert b.opened[-1][1]["tier"] == 1.0
    list(b.stream("hello world"))
    assert b.opened[-1][1] == {"lang": "en", "tier": 1.0} and b.lora_calls[-1] is False
    assert b.lora_calls[0] is True
    assert _lock_free(b)


def test_lock_released_on_close_before_start_and_midway():
    b = FakeBackend()
    g = b.stream("你好")
    g.close()  # 從未開始
    assert _lock_free(b)
    g = b.stream("你好")
    next(g)
    assert not _lock_free(b)
    g.close()
    assert _lock_free(b)
    assert b.opened[-1][2].closed  # 底層 generator 也被關
    # close 後馬上能再 stream、unload
    assert len(list(b.stream("你好"))) == 5
    b.unload()
    assert b._model is None and _lock_free(b)


def test_lock_released_on_generatorexit_and_exception():
    b = FakeBackend()
    g = b.stream("你好")
    next(g)
    with pytest.raises(GeneratorExit):
        g.throw(GeneratorExit)
    assert _lock_free(b)
    g = b.stream("你好")
    next(g)
    with pytest.raises(RuntimeError):
        g.throw(RuntimeError("boom"))
    assert _lock_free(b)


def test_stream_after_unload_raises_and_releases():
    b = FakeBackend()
    b.unload()
    with pytest.raises(RuntimeError):
        next(b.stream("你好"))
    assert _lock_free(b)


def test_concurrent_streams_are_serialized():
    b = FakeBackend(n=4, delay=0.05)
    events = []

    def run(name):
        for _ in b.stream("你好"):
            events.append(name)

    t1 = threading.Thread(target=run, args=("a",))
    t2 = threading.Thread(target=run, args=("b",))
    t1.start()
    time.sleep(0.02)
    t2.start()
    t1.join()
    t2.join()
    assert len(events) == 8
    assert events == sorted(events)  # a 全部完成才輪到 b，不交錯


def test_unload_waits_for_active_stream():
    b = FakeBackend(n=4, delay=0.05)
    g = b.stream("你好")
    next(g)
    done = threading.Event()
    threading.Thread(target=lambda: (b.unload(), done.set())).start()
    time.sleep(0.1)
    assert not done.is_set()  # 還在等
    g.close()
    assert done.wait(2)
    assert b._model is None
