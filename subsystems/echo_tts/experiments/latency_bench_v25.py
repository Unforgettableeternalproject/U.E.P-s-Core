"""IndexTTS 2.5（V25）經 Echo-Stream 實際路徑的延遲／資源基準。

core env：C:/Users/Bernie/source/repos/Unforgettableeternalproject/U.E.P-s-Core/env/Scripts/python.exe（請設 PYTHONUTF8=1）。
只 import Echo-Stream 與 TestSeparateTTSSystem（主 checkout，develop），不修改它們。

ES 實際路徑（echo_stream/adapters/speak_indextts.py，IndexTTS2SpeakStage）：
  - 引擎：TestSeparateTTSSystem 的 echo_tts IndexTTS2Backend → modules.tts_module.lite_engine.IndexTTSLite（checkpoints_v25、uep_v25.pt）
  - 加速：.env 的 ECHO_STREAM_TTS_USE_ACCEL=true（use_accel）、fp16、cuda、佇列 2
  - LoRA：現行 ES 預設 lora_scale=None（不載）；本基準依任務指定用 lora_scale=0.85（V25-L085，永久合併進 GPT 與 accel 複本）
  - 風格：echo_stream.toml 的 [tts]（override=true）→ speed 1.1、intensity 0.65、emotion happy .65 / surprised .25 / calm .15，每句都帶
  - 串流：backend.generate(on_segment_audio=...) 每段回呼一次 → AudioChunk；「可開始播放」= 第一個 AudioChunk 抵達消費端
  - 前處理：sanitize_for_tts（只剔除符號）；沒有 TWN／假名化；語言由 detect_language 自動判斷

階段：split（產生 ES 切句結果，兩個引擎共用）、main、sustained、switch。
"""
import argparse
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "Echo-Stream")
sys.path.insert(0, ES)
T_SCRIPT = time.perf_counter()
import latency_bench_lib as L  # noqa: E402


def es_defaults():
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    return tomllib.load(open(os.path.join(ES, "echo_stream.toml"), "rb"))


def split_policy():
    from echo_stream.core.splitter import SplitPolicy
    s = es_defaults()["split"]
    return SplitPolicy(first_min_weight=float(s["first_min"]), first_max_weight=float(s["first_max"]),
                       min_weight=float(s["min"]), max_weight=float(s["max"]))


async def cmd_split(a):
    from echo_stream.core.splitter import FLUSH, SentenceSplitter
    out = {}
    for lang, paras in L.PARAS.items():
        out[lang] = []
        for p in paras:
            async def toks():
                yield p
                yield FLUSH
            sp = SentenceSplitter(split_policy())
            out[lang].append([s.text for s in [x async for x in sp.split(toks(), "t")] if s.text.strip()])
    L.jdump(os.path.join(L.OUT, "paras_split.json"), out)
    print(json.dumps(out, ensure_ascii=False, indent=1))


class V25Rig:
    def __init__(self):
        self.tl = {"proc_start_epoch": L.process_start_epoch()}
        self.mem = {}
        t = time.perf_counter()
        import numpy as np
        import torch
        self.np, self.torch = np, torch
        self.tl["import_torch_s"] = time.perf_counter() - t
        t = time.perf_counter()
        from echo_stream.adapters import speak_indextts as sp
        from echo_stream.contracts.cancellation import CancellationToken
        from echo_stream.contracts.style import SpeechStyle
        from echo_stream.contracts.types import Sentence
        self.sp, self.CT, self.Sentence = sp, CancellationToken, Sentence
        cfg = es_defaults()
        tts = cfg["tts"]
        self.style = SpeechStyle(emotion={k: float(v) for k, v in (tts.get("emotion") or {}).items()}, intensity=float(tts["intensity"]),
                                 speed=float(tts["speed"]))
        self.tl["import_es_s"] = time.perf_counter() - t
        t = time.perf_counter()
        torch.cuda.init()
        torch.zeros(1, device="cuda")
        torch.cuda.synchronize()
        self.tl["cuda_init_s"] = time.perf_counter() - t
        self.mon = L.Monitor(torch_mod=torch).start()
        self.tl['mon_t0'] = self.mon.t0
        self.snap("after_cuda_init")

    def snap(self, label):
        t = self.torch
        ws, priv = L.proc_mem()
        s = {"t": time.time() - self.tl["proc_start_epoch"], "alloc_mb": t.cuda.memory_allocated() / 2 ** 20,
             "reserved_mb": t.cuda.memory_reserved() / 2 ** 20, "max_alloc_mb": t.cuda.max_memory_allocated() / 2 ** 20,
             "rss_mb": ws, "private_mb": priv}
        s.update({k: v for k, v in (self.mon._smi | self.mon._pc).items() if isinstance(v, (int, float))})
        self.mem[label] = s

    async def prepare(self):
        """依 ES 的建構方式載入；把 _build_backend / load / merge_lora / set_speed 拆開計時。"""
        sp, tl = self.sp, self.tl
        # 與 ES web server 相同：IndexTTS2SpeakStage() 讀 .env；僅額外指定 lora_scale=0.85
        self.stage = sp.IndexTTS2SpeakStage(lora_scale=0.85)
        tl["cfg_use_accel"], tl["cfg_fp16"], tl["cfg_queue"] = self.stage.use_accel, self.stage.use_fp16, self.stage.queue_size
        loop = asyncio.get_running_loop()
        t = time.perf_counter()
        backend = await loop.run_in_executor(None, self.stage._build_backend)
        tl["build_backend_s"] = time.perf_counter() - t
        wrapped = {}
        for name in ("load", "merge_lora", "set_speed"):
            fn = getattr(backend, name)

            def mk(fn, name):
                def w(*aa, **kk):
                    t0 = time.perf_counter()
                    try:
                        r = fn(*aa, **kk)
                        if name == "merge_lora":
                            tl["lora_merge_stats"] = r
                        return r
                    finally:
                        wrapped.setdefault(name, time.perf_counter() - t0)
                return w
            setattr(backend, name, mk(fn, name))
        self.stage._backend = backend
        t = time.perf_counter()
        await self.stage.prepare()
        tl["prepare_total_s"] = time.perf_counter() - t
        tl["engine_load_s"], tl["merge_lora_s"], tl["set_speed_s"] = wrapped.get("load"), wrapped.get("merge_lora"), wrapped.get("set_speed")
        tl["engine_init_timings_ms"] = dict(getattr(backend.engine, "_init_timings", {}))
        tl.setdefault("lora_merge_stats", None)
        tl["ready_s"] = time.time() - tl["proc_start_epoch"]
        self.backend = backend
        self.sr = self.stage.sample_rate
        self.snap("after_prepare")

    async def run(self, sents, idx_seed=0, style="es", tid="b", keep=False):
        """把一串句子丟進 stage.stream（與 ES 相同的呼叫方式），回傳 chunk 抵達時間線。sents 為字串清單。"""
        np, torch = self.np, self.torch
        st = self.style if style == "es" else None

        async def gen():
            for i, s in enumerate(sents):
                yield self.Sentence(text=s, turn_id=tid, index=i, is_first=(i == 0), is_last=(i == len(sents) - 1), style=st)
        torch.cuda.reset_peak_memory_stats()
        cpu0 = L.proc_cpu_s()
        t0 = time.perf_counter()
        chunks, pcms, sent_idx = [], [], []
        async for ch in self.stage.stream(gen(), self.CT()):
            tt = time.perf_counter() - t0
            d = len(ch.pcm) / 2 / ch.sample_rate
            chunks.append((tt, d))
            sent_idx.append(ch.sentence_index)
            if keep:
                pcms.append(ch.pcm)
        el = time.perf_counter() - t0
        audio = sum(d for _, d in chunks)
        sim = L.playback_sim(chunks)
        row = {"ttfa_e2e_s": chunks[0][0] if chunks else None, "gen_s": el, "audio_s": audio, "rtf": el / audio if audio else None,
               "n_chunks": len(chunks), "chunk_audio_ms_first": chunks[0][1] * 1e3 if chunks else None,
               "cpu_pct_proc": 100 * (L.proc_cpu_s() - cpu0) / max(1e-6, time.perf_counter() - t0) / (os.cpu_count() or 1),
               "peak_alloc_mb": torch.cuda.max_memory_allocated() / 2 ** 20, "underruns": sim["underruns"], "stall_s": sim["stall_s"],
               "safe_start_s": sim["safe_start_s"], "t_wall": time.perf_counter(), "chunks": [(round(a, 4), round(d, 4)) for a, d in chunks],
               "chunk_sent": sent_idx}
        wav = None
        if keep and pcms:
            wav = np.frombuffer(b"".join(pcms), dtype="<i2")
        return row, wav

    def save_wav(self, wav, name):
        import soundfile as sf
        d = os.path.join(L.WAV, "v25")
        os.makedirs(d, exist_ok=True)
        sf.write(os.path.join(d, name + ".wav"), wav, self.sr, subtype="PCM_16")

    async def warm(self):
        out = []
        for lang in L.LANGS:
            for b in ("short", "mid", "long"):
                r, _ = await self.run([L.TEXTS[lang][b][0]])
                out.append({"lang": lang, "bucket": b, "ttfa_e2e_s": r["ttfa_e2e_s"], "gen_s": r["gen_s"], "audio_s": r["audio_s"], "rtf": r["rtf"]})
        return out

    def close(self):
        self.mon.stop()


async def do_main(rig, a):
    res = {"stage": "main", "timeline": rig.tl, "mem": rig.mem, "style": {"speed": rig.style.speed, "intensity": rig.style.intensity,
                                                                         "emotion": rig.style.emotion}}
    first = []
    for lang in L.LANGS:
        r, _ = await rig.run([L.TEXTS[lang]["short"][0]])
        first.append(r | {"lang": lang, "since_proc_start_s": time.time() - rig.tl["proc_start_epoch"]})
        if lang == "zh":
            rig.tl["first_audio_after_proc_start_s"] = (time.time() - rig.tl["proc_start_epoch"]) - r["gen_s"] + r["ttfa_e2e_s"]
    res["first_calls"] = first
    rig.snap("after_first_calls")
    res["warm"] = await rig.warm()
    rig.snap("after_warm")
    rows, win = [], {}
    for lang in L.LANGS:
        for b in ("short", "mid", "long"):
            ta = time.perf_counter()
            for i in range(L.BUCKET_N[b]):
                r, w = await rig.run([L.TEXTS[lang][b][i % 4]], keep=True)
                rig.save_wav(w, f"{lang}_{b}_{i:02d}")
                rows.append(r | {"lang": lang, "bucket": b, "idx": i, "text": L.TEXTS[lang][b][i % 4]})
            win[f"{lang}_{b}"] = rig.mon.window(ta, time.perf_counter())
            print(lang, b, "done", flush=True)
    res["rows"], res["resource_windows"] = rows, win
    rig.snap("after_buckets")
    paras = json.load(open(os.path.join(L.OUT, "paras_split.json"), encoding="utf-8"))
    turns = []
    for lang in L.LANGS:
        for k in range(a.turns):
            sents = paras[lang][k % len(paras[lang])]
            await asyncio.sleep(a.idle)  # 模擬 ES 每輪 TTS 前 STT+LLM 的空檔（GPU 降頻後第一句的 TTFA）
            r, w = await rig.run(sents, keep=(k == 0))
            if w is not None:
                rig.save_wav(w, f"turn_{lang}")
            # 每句的首包抵達時間
            first_per = {}
            for (tt, d), si in zip([(c[0], c[1]) for c in r["chunks"]], r["chunk_sent"]):
                first_per.setdefault(si, tt)
            turns.append({"lang": lang, "k": k, "n_sent": len(sents), "ttfa_s": r["ttfa_e2e_s"], "total_gen_s": r["gen_s"], "audio_s": r["audio_s"],
                          "turn_rtf": r["rtf"], "underruns": r["underruns"], "stall_s": r["stall_s"], "safe_start_s": r["safe_start_s"],
                          "first_chunk_per_sentence_s": first_per, "n_chunks": r["n_chunks"]})
        print(lang, "turns done", flush=True)
    res["turns"] = turns
    # ES adapter 橋接額外開銷：同一句直接打 backend.generate（callback 時間）對比 stage.stream
    direct, via = [], []
    txt = L.TEXTS["zh"]["short"][1]
    import tempfile
    for i in range(15):
        r, _ = await rig.run([txt])
        via.append(r["ttfa_e2e_s"])
        tt = {}
        t0 = time.perf_counter()

        def cb(tensor, seg, total, tt=tt, t0=t0):
            tt.setdefault("first", time.perf_counter() - t0)
        loop = asyncio.get_running_loop()
        path = os.path.join(tempfile.gettempdir(), "latbench_direct.wav")
        rig.backend.emotion_vector = rig.sp.scale_emotion(rig.style.emotion_vector(), rig.style.intensity)
        await loop.run_in_executor(None, lambda: rig.backend.generate(rig.sp.sanitize_for_tts(txt), path, on_segment_audio=cb))
        direct.append(tt["first"])
    res["bridge_overhead"] = {"via_stage_ttfa_s": L.pct(via), "direct_backend_ttfa_s": L.pct(direct)}
    rig.snap("end")
    res["monitor_samples"] = rig.mon.samples
    L.jdump(os.path.join(L.RAW, "v25_main.json"), res)
    print("main saved", flush=True)


SEQ = [("zh", "short"), ("en", "mid"), ("ja", "short"), ("zh", "mid"), ("en", "short"), ("ja", "mid"),
       ("zh", "long"), ("en", "long"), ("ja", "long")]


async def do_sustained(rig, a):
    rig.snap("sustained_start")
    rows, i, last = [], 0, 0.0
    t_start = time.perf_counter()
    deadline = t_start + a.minutes * 60
    while time.perf_counter() < deadline:
        lang, b = SEQ[i % len(SEQ)]
        r, _ = await rig.run([L.TEXTS[lang][b][(i // len(SEQ)) % 4]])
        r.update(lang=lang, bucket=b, idx=i, t_off=r["t_wall"] - t_start)
        rows.append(r)
        if r["t_off"] - last > 60:
            rig.snap(f"t{int(r['t_off'])}")
            last = r["t_off"]
        if i % 30 == 0:
            print(i, round(r["t_off"]), lang, b, round(r["rtf"], 3), flush=True)
        i += 1
    rig.snap("sustained_end")
    L.jdump(os.path.join(L.RAW, "v25_sustained.json"), {"stage": "sustained", "minutes": a.minutes, "rows": rows, "mem": rig.mem,
                                                       "monitor_samples": rig.mon.samples, "timeline": rig.tl})


async def do_switch(rig, a):
    res = {"stage": "switch"}
    block, inter = [], []
    for lang in L.LANGS:
        for j in range(5):
            r, _ = await rig.run([L.TEXTS[lang]["mid"][j % 4]])
            block.append(r | {"lang": lang, "pos": j, "mode": "block"})
    for j in range(12):
        lang = L.LANGS[j % 3]
        r, _ = await rig.run([L.TEXTS[lang]["mid"][(j // 3) % 4]])
        inter.append(r | {"lang": lang, "pos": j, "mode": "inter"})
    res["block"], res["inter"] = block, inter
    seqs = []
    for lang in L.LANGS:
        for j in range(6):
            b = ("short", "long", "mid")[j % 3]
            r, _ = await rig.run([L.TEXTS[lang][b][j % 4]])
            seqs.append(r | {"lang": lang, "bucket": b, "pos": j})
    res["len_alternate"] = seqs
    # 風格（語速 1.1 + 情緒向量）對比無 style：ES 每句換 style 的代價
    ab = {"style": [], "none": []}
    for rep in range(6):
        for st in ("es", "none"):
            r, _ = await rig.run([L.TEXTS["zh"]["mid"][rep % 4]], style=st)
            ab["style" if st == "es" else "none"].append({k: r[k] for k in ("ttfa_e2e_s", "gen_s", "audio_s", "rtf")})
    res["style_ab_zh_mid"] = ab
    L.jdump(os.path.join(L.RAW, "v25_switch.json"), res)


async def do_affinity(rig, a):
    """CPU 親和性／EcoQoS 因果測試（同 Vox 版）：zh 中句各 8 次。"""
    orig = L.get_affinity()
    res = {"orig_mask": orig}
    for name, mask in (("free", orig), ("P_only", 0xFFFF & orig), ("E_only", 0xF0000 & orig), ("free_noEcoQoS", orig), ("free_again", orig)):
        L.set_affinity(mask)
        if name == "free_noEcoQoS":
            res["set_power_throttling_off_ok"] = L.set_power_throttling(False)
        if name == "free_again":
            L.set_power_throttling(None)
        await asyncio.sleep(0.5)
        rs = []
        for j in range(6):
            r, _ = await rig.run([L.TEXTS["zh"]["mid"][j % 4]])
            rs.append({k: r[k] for k in ("ttfa_e2e_s", "gen_s", "audio_s", "rtf")})
        res[name] = rs
        print("affinity", name, round(sorted(x["rtf"] for x in rs)[4], 3), flush=True)
    L.set_affinity(orig)
    L.jdump(os.path.join(L.RAW, "v25_affinity.json"), res)


async def cmd_all(a):
    """同一個行程依序：main（含冷啟動時間線）→ switch → sustained，只載入一次。"""
    rig = V25Rig()
    await rig.prepare()
    stages = a.only.split(",") if a.only else ["main", "switch", "affinity", "sustained"]
    if "main" in stages:
        await do_main(rig, a)
    else:
        await rig.warm()
    if "switch" in stages:
        await do_switch(rig, a)
    if "affinity" in stages:
        await do_affinity(rig, a)
    if "sustained" in stages:
        await do_sustained(rig, a)
    rig.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["split", "all"])
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--idle", type=float, default=3.0, help="每個 turn 前的空檔秒數")
    ap.add_argument("--only", default="", help="all 時只跑指定階段（逗號分隔）")
    a = ap.parse_args()
    asyncio.run({"split": cmd_split, "all": cmd_all}[a.stage](a))
