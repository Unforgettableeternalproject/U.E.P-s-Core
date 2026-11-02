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
    assert snap_speed(None, "zh") == 1.05
    assert snap_speed(None, "en") == 1.0
    assert snap_speed(None, "ja") == 1.0
    assert snap_speed(1.0, "zh") == 1.0
    assert snap_speed(1.02, "zh") == 1.0
    assert snap_speed(1.03, "zh") == 1.05
    assert snap_speed(1.04, "zh") == 1.05
    assert snap_speed(1.08, "zh") == 1.1
    assert snap_speed(1.19, "zh", (1.0, 1.05, 1.1)) == 1.1
    assert snap_speed(3.0, "zh", (1.0, 1.05, 1.1)) == 1.1
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
        ("模糊", "{mo2}{hu2}"),
        ("謝謝", "{xie4}{xie5}"),
        ("謝謝你", "{xie4}{xie5}你"),
        ("謝謝大家", "{xie4}{xie5}大家"),
        ("感謝", "感謝"),
        ("謝罪", "謝罪"),
        ("多謝", "多謝"),
        ("試著", "試{zhe5}"),
        ("他試著說明", "他試{zhe5}說明"),
        ("看著天空", "看{zhe5}天空"),
        ("走著瞧", "走{zhe5}瞧"),
    ],
)
def test_taiwan_pronunciation(word, expect):
    assert TaiwanPronunciationNormalizer().normalize(word) == expect


@pytest.mark.parametrize("text", ["著急", "著作", "睡著", "睡著了", "著名", "他著急地說", "衣著", "穿著"])
def test_taiwan_zhe_not_applied_to_other_zhe(text):
    # 只有「動詞＋著」詞條讀 zhe5；單字「著」其他讀法不得被改
    assert "zhe5" not in TaiwanPronunciationNormalizer().normalize(text)


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
    assert m["zh"]["mode"] == "ult" and sorted(m["zh"]["speeds"]) == [1.0, 1.05, 1.1]
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
        self._tiers = {"zh": (1.0, 1.05, 1.1), "en": (1.0,), "ja": (1.0,)}
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
    assert text == "步{zou4}" and cache == {"lang": "zh", "tier": 1.05}
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


# ---------------------------------------------------------------- 情緒（組合模式）
from echo_tts.config import emotion_presets as ep  # noqa: E402
from echo_tts.runtime.vox_backend import (  # noqa: E402
    EMOTION_LORA_SCALE,
    load_emotion_clips,
    pick_emotion_tier,
    resolve_emotion,
)


def test_taiwan_xi2():
    assert TaiwanPronunciationNormalizer().normalize("把燈熄掉") == "把燈{xi2}掉"


def test_strength_band_is_057_to_065():
    assert (ep.STRENGTH_MIN, ep.STRENGTH_MAX) == (0.57, 0.65)
    assert ep.user_to_strength(0.0) == pytest.approx(0.57)
    assert ep.user_to_strength(0.5) == pytest.approx(0.61)
    assert ep.user_to_strength(None) == pytest.approx(0.61)
    assert ep.user_to_strength(1.0) == pytest.approx(0.65)
    assert ep.strength_to_user(0.61) == pytest.approx(0.5)


def _vec(i, v=1.0):
    out = [0.0] * 8
    out[i] = v
    return out


@pytest.mark.parametrize(
    "value,expected",
    [
        (_vec(0), "happy"),
        (_vec(7, 0.3), "calm"),
        (tuple(_vec(2)), "sad"),
        ([0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 0.5], "surprised"),  # 同值取靠前者
        ("Angry", "angry"),
        (" sad ", "sad"),
        (None, None),
        ([0.0] * 8, None),
        ("neutral", None),
        ("", None),
        ("furious", None),  # 未知標籤
        ([0.1, 0.2], None),  # 長度不符
        (["a"] * 8, None),
        (123, None),
    ],
)
def test_resolve_emotion(value, expected):
    assert resolve_emotion(value) == expected


def test_pick_emotion_tier():
    full = (0.57, 0.61, 0.65)
    assert pick_emotion_tier(None, full) == 0.61
    assert pick_emotion_tier(0.0, full) == 0.57
    assert pick_emotion_tier(0.2, full) == 0.57
    assert pick_emotion_tier(0.5, full) == 0.61
    assert pick_emotion_tier(1.0, full) == 0.65
    # 缺 0.61：0.5 平手取較強者，其餘取最近
    two = (0.57, 0.65)
    assert pick_emotion_tier(0.5, two) == 0.65
    assert pick_emotion_tier(0.3, two) == 0.57
    assert pick_emotion_tier(0.9, two) == 0.65
    assert pick_emotion_tier(0.5, (0.57,)) == 0.57
    assert pick_emotion_tier(0.5, ()) is None


def test_load_emotion_clips_real_assets():
    clips = load_emotion_clips(vb.DEFAULT_ASSETS_DIR)
    assert set(clips) == set(ep.EMOTION_LABELS)
    for label, tiers in clips.items():
        assert sorted(tiers) == [0.57, 0.61, 0.65], label  # 0.73 不放
        assert all(c["transcript"] for c in tiers.values())


class EmoBackend(FakeBackend):
    """假模型：記錄 LoRA scale 與送進 generate 的 reference / prompt。"""

    def __init__(self, tiers=(0.57, 0.65), **kw):
        super().__init__(**kw)
        self.scale_calls = []
        self._kana = type("K", (), {"normalize": staticmethod(lambda t: t)})()  # 假名化不是這裡要測的
        self._zh_refs = {t: {"wav": f"ref{t}.wav", "transcript": "中性"} for t in (1.0, 1.05, 1.1)}
        self._emotion_clips = {
            e: {t: {"wav": f"{e}_{t}.wav", "transcript": f"{e}逐字稿{t}"} for t in tiers} for e in ("happy", "sad")
        }

    def _set_lora_scale(self, scale):
        self.scale_calls.append(scale)
        self._lora_scale = scale

    def _build_emotion_cache(self, clip, ref):
        return {"reference": ref["wav"], "prompt": clip["wav"], "prompt_text": clip["transcript"]}


def test_emotion_uses_reference_and_prompt_and_lora_scale():
    b = EmoBackend()
    list(b.stream("你好", emotion="happy", intensity=1.0, speed=1.1))
    cache = b.opened[-1][1]
    assert cache == {"reference": "ref1.1.wav", "prompt": "happy_0.65.wav", "prompt_text": "happy逐字稿0.65"}
    assert b.scale_calls == [EMOTION_LORA_SCALE, 1.0] and EMOTION_LORA_SCALE == 0.85
    assert b._lora_scale == 1.0
    # 向量 + 預設 intensity（0.5 → 0.61 缺檔，平手取 0.65）+ 預設語速 1.05
    list(b.stream("你好", emotion=_vec(2, 0.4)))
    assert b.opened[-1][1] == {"reference": "ref1.05.wav", "prompt": "sad_0.65.wav", "prompt_text": "sad逐字稿0.65"}
    list(b.stream("你好", emotion="sad", intensity=0.0))
    assert b.opened[-1][1]["prompt"] == "sad_0.57.wav"


def test_emotion_falls_back_to_nearest_available_tier():
    b = EmoBackend(tiers=(0.57,))
    list(b.stream("你好", emotion="happy", intensity=1.0))
    assert b.opened[-1][1]["prompt"] == "happy_0.57.wav"


def test_neutral_paths_keep_existing_behavior():
    b = EmoBackend()
    for emo in (None, "neutral", [0.0] * 8, "furious"):
        list(b.stream("你好", emotion=emo))
        assert b.opened[-1][1] == {"lang": "zh", "tier": 1.05}
    assert b.scale_calls == []
    # 情緒沒有任何片段（例如 calm）：退回中性，不套 0.85
    list(b.stream("你好", emotion="calm"))
    assert b.opened[-1][1] == {"lang": "zh", "tier": 1.05} and b.scale_calls == []
    b._emotion_clips = {}
    list(b.stream("你好", emotion="happy"))
    assert b.opened[-1][1] == {"lang": "zh", "tier": 1.05} and b.scale_calls == []


def test_lora_scale_returns_to_neutral_after_emotion():
    b = EmoBackend()
    list(b.stream("你好", emotion="happy"))
    list(b.stream("你好"))
    assert b._lora_scale == 1.0 and b.scale_calls == [0.85, 1.0]


def test_lora_scale_restored_on_close_and_exception():
    b = EmoBackend()
    g = b.stream("你好", emotion="happy")
    next(g)
    assert b._lora_scale == 0.85
    g.close()
    assert b._lora_scale == 1.0 and _lock_free(b)
    g = b.stream("你好", emotion="happy")
    next(g)
    with pytest.raises(RuntimeError):
        g.throw(RuntimeError("boom"))
    assert b._lora_scale == 1.0 and _lock_free(b)


def test_lora_scale_restored_when_open_stream_fails():
    b = EmoBackend()

    def boom(text, cache):
        raise RuntimeError("open failed")

    b._open_stream = boom
    with pytest.raises(RuntimeError):
        list(b.stream("你好", emotion="happy"))
    assert b._lora_scale == 1.0 and _lock_free(b)


@pytest.mark.parametrize("text", ["hello world", "こんにちは"])
def test_en_ja_ignore_emotion(text, caplog):
    b = EmoBackend()
    with caplog.at_level("DEBUG", logger=vb.logger.name):
        list(b.stream(text, emotion="happy", intensity=1.0))
        list(b.stream(text, emotion="sad"))
    lang = "en" if text == "hello world" else "ja"
    assert b.opened[-1][1] == {"lang": lang, "tier": 1.0}
    assert b.scale_calls == []
    assert len([r for r in caplog.records if "忽略 emotion" in r.getMessage()]) == 1  # 只記錄一次


# ---------------------------------------------------------------- 執行緒契約（torch.compile CUDA graph 綁定執行緒）
def _in_thread(fn):
    box = {}

    def run():
        try:
            box["ok"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return box


def test_same_thread_ok():
    b = FakeBackend()
    b._owner_thread = threading.get_ident()
    assert len(list(b.stream("你好"))) == 5
    b.unload()
    assert b._owner_thread is None


def test_cross_thread_stream_raises_and_releases_lock():
    b = FakeBackend()
    b._owner_thread = threading.get_ident()
    box = _in_thread(lambda: list(b.stream("你好")))
    assert isinstance(box.get("err"), RuntimeError) and "同一條執行緒" in str(box["err"])
    assert b.opened == [] and _lock_free(b)
    assert len(list(b.stream("你好"))) == 5  # 原執行緒仍可用


def test_cross_thread_unload_raises_and_releases_lock():
    b = FakeBackend()
    b._owner_thread = threading.get_ident()
    box = _in_thread(b.unload)
    assert isinstance(box.get("err"), RuntimeError) and "同一條執行緒" in str(box["err"])
    assert b._model is not None and _lock_free(b)
    b.unload()
    assert b._model is None and b._owner_thread is None


def test_no_thread_check_when_not_optimized():
    b = FakeBackend()
    b.optimize = False
    b._owner_thread = None  # optimize=False 時 prepare 不記錄
    assert "err" not in _in_thread(lambda: list(b.stream("你好")))
    assert "err" not in _in_thread(b.unload)


@pytest.mark.parametrize("optimize,recorded", [(True, True), (False, False)])
def test_prepare_records_owner_thread(optimize, recorded):
    """prepare 在載入模型前記錄執行緒（optimize=False 不記）；失敗 release 後重設，可換執行緒重新 prepare。"""
    owners = []

    class B(VoxBackend):
        def _load_model(self):
            owners.append(self._owner_thread)
            raise RuntimeError("stop")

    b = B({"model_dir": ".", "lora_dir": ".", "optimize": optimize})
    with pytest.raises(RuntimeError):
        b.prepare()
    assert owners == [threading.get_ident() if recorded else None]
    assert b._owner_thread is None


def test_info_log_shows_normalized_text_and_settings(caplog):
    b = EmoBackend()
    with caplog.at_level("INFO", logger=vb.logger.name):
        list(b.stream("週末步驟", emotion="happy", intensity=1.0, speed=1.1))
        list(b.stream("步驟"))
        list(b.stream("hello world"))
    msgs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("VOX 生成")]
    assert len(msgs) == 3
    assert "lang=zh mode=COMBO lora=on scale=0.85 speed=1.1 emotion=happy intensity_tier=0.65" in msgs[0]
    assert "'{zhou1}末步{zou4}'" in msgs[0]  # 正規化後（含 pinyin）的文字
    assert "mode=ULT lora=on scale=1.0 speed=1.05 emotion=None" in msgs[1] and "步{zou4}" in msgs[1]
    assert "lang=en mode=REF lora=off" in msgs[2] and "hello world" in msgs[2]
