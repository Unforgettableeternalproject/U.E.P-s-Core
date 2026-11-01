"""VoxCPM2 新方案延遲／資源基準（.venv-voxcpm，請設 PYTHONUTF8=1，一次只跑一個 GPU 工作）。

階段：
  all        同一行程依序 main→switch→sustained（只載入一次）
  coldstart  全新編譯快取的冷啟動時間線
  （以下為 all 內各階段）
  main       冷啟動時間線 + 前處理成本 + zh/en/ja x 短/中/長 分佈 + 串流 underrun 模擬 + ES 切句段落（turn）
             --cache cold：使用全新的 inductor/triton 快取目錄（量真正的第一次編譯）；default：沿用磁碟快取
  sustained  連續交錯生成（預設 10 分鐘），看劣化、洩漏、溫度
  switch     LoRA 開關、跨語言切換、prompt cache

定案版本：
  zh  TWLORA-TWN + ref120（ULT：加速 1.2 倍的 neutral 參考音 + 逐字稿；LoRA step_0000600 開；TWN 前處理 normalize=False）
  en  REF-EV25b（只給參考音 EV25b；LoRA 關）
  ja  REF-JV25b（假名化；只給參考音 JV25b；LoRA 關）
cfg 2.0、inference_timesteps 10、normalize=False，與 tw_blind_1101 / tw_foreign_v3 一致。
"""
import argparse
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
T_SCRIPT = time.perf_counter()


def _prep_env(cache):
    """必須在 import torch 之前設定。cold = 全新快取目錄（真正冷編譯）。"""
    import latency_bench_lib as L
    if cache == "cold":
        base = os.path.join(L.OUT, "cache_cold_" + time.strftime("%H%M%S"))
        os.environ["TORCHINDUCTOR_CACHE_DIR"] = os.path.join(base, "inductor")
        os.environ["TRITON_CACHE_DIR"] = os.path.join(base, "triton")
        return base
    return None


class Rig:
    """載入 Vox 並提供 synth()；記錄冷啟動時間線與每個里程碑的記憶體。"""

    def __init__(self, cache="default", monitor=True, optimize=True, no_builtin_warm=False, compile_kwargs=None):
        self.logs = []
        self.cache = cache
        self.cache_dir = _prep_env(cache)
        import latency_bench_lib as L
        self.L = L
        self.tl = {"proc_start_epoch": L.process_start_epoch()}
        self.tl["python_boot_s"] = time.time() - self.tl["proc_start_epoch"] - (time.perf_counter() - T_SCRIPT)
        self.mem = {}
        t = time.perf_counter()
        import numpy as np
        import torch
        self.np, self.torch = np, torch
        self.tl["import_torch_s"] = time.perf_counter() - t
        t = time.perf_counter()
        import voxcpm  # noqa: F401
        from voxcpm.model import voxcpm2 as vm
        import tw_accent_check as T
        import tw_lora_eval as E
        self.T, self.E = T, E
        self.tl["import_voxcpm_and_exp_s"] = time.perf_counter() - t
        t = time.perf_counter()
        torch.cuda.init()
        torch.zeros(1, device="cuda")
        torch.cuda.synchronize()
        self.tl["cuda_init_s"] = time.perf_counter() - t
        self.mon = L.Monitor(torch_mod=torch).start() if monitor else None
        self.tl['mon_t0'] = self.mon.t0 if self.mon else None
        # 記錄 from_pretrained 內建暖機（VoxCPM 建構時對 tts_model.generate 的第一次呼叫）耗時
        self._first_gen = {}
        orig = vm.VoxCPM2Model.generate

        def timed(selff, *a, **k):
            if no_builtin_warm and "s" not in self._first_gen:
                self._first_gen["s"] = 0.0
                return None
            t0 = time.perf_counter()
            try:
                return orig(selff, *a, **k)
            finally:
                self._first_gen.setdefault("s", time.perf_counter() - t0)
        vm.VoxCPM2Model.generate = timed
        self.snap("after_cuda_init")
        t = time.perf_counter()
        if compile_kwargs:
            _oc = torch.compile

            def _c(*a, **k):
                k.update(compile_kwargs)
                return _oc(*a, **k)
            torch.compile = _c
        self.model = E.load_model(optimize=optimize)
        if compile_kwargs:
            torch.compile = _oc
        self.tl["from_pretrained_total_s"] = time.perf_counter() - t
        self.tl["builtin_warmup_s"] = self._first_gen.get("s")
        self.tl["weights_load_s"] = self.tl["from_pretrained_total_s"] - (self.tl["builtin_warmup_s"] or 0)
        vm.VoxCPM2Model.generate = orig
        self.sr = self.model.tts_model.sample_rate
        self.snap("after_load_warm")
        t = time.perf_counter()
        E.set_tag(self.model, E.DEFAULT_LORA_TAG)
        self.tl["lora_load_s"] = time.perf_counter() - t
        self.snap("after_lora")
        t = time.perf_counter()
        from taiwan_pronunciation import TaiwanPronunciationNormalizer
        self.norm = TaiwanPronunciationNormalizer()
        self.tl["twn_init_s"] = time.perf_counter() - t
        t = time.perf_counter()
        from japanese_kana import JapaneseKanaNormalizer
        self.kana = JapaneseKanaNormalizer()
        self.tl["kana_init_s"] = time.perf_counter() - t
        t = time.perf_counter()
        self.kana.normalize("今日はいい天気ですね。")
        self.tl["kana_first_call_s"] = time.perf_counter() - t
        n_path, n_text = T.neutral_ref()
        pdir = os.path.join(L.ROOT, "outputs", "foreign_v3", "prompts")
        import json
        pj = json.load(open(os.path.join(pdir, "prompts.json"), encoding="utf-8"))
        self.modes = {
            "zh": {"ref": os.path.join(E.EV, "neutral_ref120.wav"), "ptext": n_text, "lora": True, "pre": self.norm.normalize},
            "en": {"ref": pj["EV25b"]["wav"], "ptext": None, "lora": False, "pre": lambda s: s},
            "ja": {"ref": pj["JV25b"]["wav"], "ptext": None, "lora": False, "pre": self.kana.normalize},
        }
        self.tl["ready_s"] = time.time() - self.tl["proc_start_epoch"]  # 行程啟動到可呼叫 synth
        self.cur_lora = True
        self.sink = None

    # ------------------------------------------------------------------ 記錄
    def _attach_log(self):
        self.logs = []
        rig = self

        class H(logging.Handler):
            def emit(self, rec):
                try:
                    m = rec.getMessage()
                except Exception:  # noqa: BLE001
                    return
                low = m.lower()
                if any(k in low for k in ("recompil", "cache_size_limit", "cudagraph", "graph break", "skipping", "dynamo hit")):
                    rig.logs.append((time.perf_counter(), rec.name, m[:400]))
        h = H()
        h.setLevel(logging.DEBUG)
        logging.getLogger("torch").addHandler(h)

    def dyn_stats(self):
        try:
            c = self.torch._dynamo.utils.counters
            return {"unique_graphs": int(c["stats"].get("unique_graphs", 0)), "frames_ok": int(c["frames"].get("ok", 0)),
                    "log_n": len(self.logs)}
        except Exception:  # noqa: BLE001
            return {"unique_graphs": -1, "frames_ok": -1, "log_n": len(self.logs)}

    def snap(self, label):
        t = self.torch
        ws, priv = self.L.proc_mem()
        s = {"t": time.time() - self.tl["proc_start_epoch"], "alloc_mb": t.cuda.memory_allocated() / 2 ** 20,
             "reserved_mb": t.cuda.memory_reserved() / 2 ** 20, "max_alloc_mb": t.cuda.max_memory_allocated() / 2 ** 20,
             "rss_mb": ws, "private_mb": priv}
        if self.mon is not None:
            s.update({k: v for k, v in (self.mon._smi | self.mon._pc).items() if isinstance(v, (int, float))})
        self.mem[label] = s

    # ------------------------------------------------------------------ 合成
    def set_lora(self, on):
        if on != self.cur_lora:
            self.model.set_lora_enabled(on)
            self.cur_lora = on

    def synth(self, lang, raw, seed, prompt_cache=None):
        np, torch, L = self.np, self.torch, self.L
        cfg = self.modes[lang]
        torch.cuda.reset_peak_memory_stats()
        ds0 = self.dyn_stats()
        cpu0 = L.proc_cpu_s()
        t_a = time.perf_counter()
        core0 = L.cur_cpu()
        text = cfg["pre"](raw)
        t_pre = time.perf_counter()
        self.set_lora(cfg["lora"])
        t_lora = time.perf_counter()
        torch.manual_seed(seed)
        kw = dict(reference_wav_path=cfg["ref"])
        if cfg["ptext"]:
            kw.update(prompt_wav_path=cfg["ref"], prompt_text=cfg["ptext"])
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        ch, times, first = [], [], None
        if prompt_cache is None:
            gen = self.model.generate_streaming(text=text, cfg_value=2.0, inference_timesteps=10, normalize=False, **kw)
        else:
            gen = (w.squeeze(0).cpu().numpy() for w, _, _ in self.model.tts_model._generate_with_prompt_cache(
                target_text=text, prompt_cache=prompt_cache, min_len=2, max_len=4096, inference_timesteps=10, cfg_value=2.0,
                retry_badcase=True, retry_badcase_max_times=3, retry_badcase_ratio_threshold=6.0, streaming=True))
        for c in gen:
            tt = time.perf_counter()
            if first is None:
                first = tt - t0
            times.append(tt - t0)
            ch.append(c)
        torch.cuda.synchronize()
        el = time.perf_counter() - t0
        w = np.concatenate(ch)
        dur = len(w) / self.sr
        pre_s, lora_s = t_pre - t_a, t_lora - t_pre
        off = pre_s + lora_s
        chunks = [(off + a, len(c) / self.sr) for a, c in zip(times, ch)]
        ds1 = self.dyn_stats()
        row = {"lang": lang, "text": raw, "pre_text": text, "seed": seed, "pre_ms": pre_s * 1e3, "lora_toggle_ms": lora_s * 1e3,
               "ttfa_core_s": first, "ttfa_e2e_s": off + first, "gen_s": el, "audio_s": dur, "rtf": el / dur,
               "n_chunks": len(ch), "chunk_audio_ms_first": chunks[0][1] * 1e3,
               "cpu_pct_proc": 100 * (L.proc_cpu_s() - cpu0) / max(1e-6, time.perf_counter() - t_a) / (os.cpu_count() or 1),
               "peak_alloc_mb": torch.cuda.max_memory_allocated() / 2 ** 20,
               "new_graphs": ds1["unique_graphs"] - ds0["unique_graphs"], "new_logs": ds1["log_n"] - ds0["log_n"],
               "t_wall": time.perf_counter(), "core0": core0, "core1": L.cur_cpu()}
        sim0 = L.playback_sim(chunks)
        row["underruns"], row["stall_s"], row["safe_start_s"] = sim0["underruns"], sim0["stall_s"], sim0["safe_start_s"]
        row["_chunks"] = chunks
        return row, w

    def save_wav(self, w, name):
        import soundfile as sf
        d = os.path.join(self.L.WAV, "vox")
        os.makedirs(d, exist_ok=True)
        sf.write(os.path.join(d, name + ".wav"), w, self.sr, subtype="PCM_16")

    def warm(self, langs=("zh", "en", "ja")):
        """與 tw_blind_1101 一致的暖機：每個模式短、中、長各一句。回傳每次呼叫的耗時（冷啟動第一次呼叫）。"""
        out = []
        for lang in langs:
            for b in ("short", "mid", "long"):
                row, _ = self.synth(lang, self.L.TEXTS[lang][b][0], 1)
                out.append({k: row[k] for k in ("lang", "ttfa_e2e_s", "gen_s", "audio_s", "rtf", "new_graphs", "new_logs")} | {"bucket": b})
        return out

    def close(self):
        if self.mon:
            self.mon.stop()


def strip(row):
    r = {k: v for k, v in row.items() if k != "_chunks"}
    r["chunks"] = [(round(a, 4), round(d, 4)) for a, d in row["_chunks"]]
    return r


# ====================================================================================== main
def do_main(rig, a):
    L, T = rig.L, rig.T
    res = {"stage": "main", "cache": rig.cache, "cache_dir": rig.cache_dir, "timeline": rig.tl, "mem": rig.mem}
    # 1) 首次呼叫（每個模式）→ 冷啟動的「第一個音訊」
    t_m0 = time.perf_counter()
    first_calls = []
    for lang in L.LANGS:
        row, w = rig.synth(lang, L.TEXTS[lang]["short"][0], 11)
        first_calls.append(strip(row) | {"since_proc_start_s": time.time() - rig.tl["proc_start_epoch"]})
        if lang == "zh":
            rig.tl["first_audio_after_proc_start_s"] = (time.time() - rig.tl["proc_start_epoch"]) - row["gen_s"] + row["ttfa_core_s"]
    res["first_calls"] = first_calls
    rig.snap("after_first_calls")
    # 2) 暖機（短中長）
    res["warm"] = rig.warm()
    rig.snap("after_warm")
    # 3) 前處理成本（CPU）
    pre = {}
    for lang, fn in (("zh", rig.norm.normalize), ("ja", rig.kana.normalize)):
        for b in ("short", "mid", "long"):
            xs = []
            for _ in range(30):
                for t in L.TEXTS[lang][b]:
                    t0 = time.perf_counter()
                    fn(t)
                    xs.append((time.perf_counter() - t0) * 1e3)
            pre[f"{lang}_{b}_ms"] = L.pct(xs)
    res["preproc"] = pre
    # 4) 分佈
    rows, win = [], {}
    for lang in L.LANGS:
        for b in ("short", "mid", "long"):
            n = L.BUCKET_N[b]
            ta = time.perf_counter()
            for i in range(n):
                row, w = rig.synth(lang, L.TEXTS[lang][b][i % 4], 5000 + i)
                row["idx"], row["bucket"] = i, b
                rows.append(strip(row))
                rig.save_wav(w, f"{lang}_{b}_{i:02d}")
            win[f"{lang}_{b}"] = rig.mon.window(ta, time.perf_counter())
            print(lang, b, "done", flush=True)
    res["rows"], res["resource_windows"] = rows, win
    rig.snap("after_buckets")
    # 5) ES 切句段落（turn）：沿用 ES 切句器的結果，逐句順序合成
    import json
    paras = json.load(open(os.path.join(L.OUT, "paras_split.json"), encoding="utf-8"))
    turns = []
    for lang in L.LANGS:
        for k in range(a.turns):
            sents = paras[lang][k % len(paras[lang])]
            time.sleep(a.idle)  # 模擬 ES 每輪 TTS 前 STT+LLM 的空檔（GPU 降頻後第一句的 TTFA）
            tstart = time.perf_counter()
            chunks, per = [], []
            for si, s in enumerate(sents):
                row, w = rig.synth(lang, s, 7000 + k * 10 + si)
                base = time.perf_counter() - row["gen_s"] - (row["pre_ms"] + row["lora_toggle_ms"]) / 1e3 - tstart
                chunks += [(base + c, d) for c, d in row["_chunks"]]
                per.append({"text": s, "ttfa_e2e_s": row["ttfa_e2e_s"], "gen_s": row["gen_s"], "audio_s": row["audio_s"], "start_s": base})
                if k == 0:
                    rig.save_wav(w, f"turn_{lang}_{si}")
            sim = L.playback_sim(chunks)
            tot_a = sum(p["audio_s"] for p in per)
            turns.append({"lang": lang, "k": k, "n_sent": len(sents), "ttfa_s": chunks[0][0], "total_gen_s": per[-1]["start_s"] + per[-1]["gen_s"],
                          "audio_s": tot_a, "turn_rtf": (per[-1]["start_s"] + per[-1]["gen_s"]) / tot_a, "underruns": sim["underruns"],
                          "stall_s": sim["stall_s"], "safe_start_s": sim["safe_start_s"], "sent": per})
        print(lang, "turns done", flush=True)
    res["turns"] = turns
    rig.snap("end")
    res["monitor_samples"] = rig.mon.samples
    res["dyn_logs"] = [(round(t - T_SCRIPT, 2), n, m) for t, n, m in rig.logs][:200]
    L.jdump(os.path.join(L.RAW, "vox_main.json"), res)
    print("main saved", flush=True)


# ====================================================================================== sustained
SEQ = [("zh", "short"), ("en", "mid"), ("ja", "short"), ("zh", "mid"), ("en", "short"), ("ja", "mid"),
       ("zh", "long"), ("en", "long"), ("ja", "long")]


def do_sustained(rig, a):
    L = rig.L
    rig.snap("sustained_start")
    rows, t_start = [], time.perf_counter()
    snaps, i, last_snap = [], 0, 0.0
    deadline = t_start + a.minutes * 60
    while time.perf_counter() < deadline:
        lang, b = SEQ[i % len(SEQ)]
        row, w = rig.synth(lang, L.TEXTS[lang][b][(i // len(SEQ)) % 4], 9000 + i)
        r = strip(row)
        r.update(idx=i, bucket=b, t_off=row["t_wall"] - t_start)
        rows.append(r)
        if r["t_off"] - last_snap > 60:
            rig.snap(f"t{int(r['t_off'])}")
            last_snap = r["t_off"]
        if i % 30 == 0:
            print(i, round(r["t_off"]), lang, b, round(r["rtf"], 3), flush=True)
        i += 1
    rig.snap("sustained_end")
    L.jdump(os.path.join(L.RAW, "vox_sustained.json"), {"stage": "sustained", "minutes": a.minutes, "rows": rows, "mem": rig.mem,
                                                       "monitor_samples": rig.mon.samples, "timeline": rig.tl,
                                                       "dyn_logs": [(round(t - T_SCRIPT, 2), n, m) for t, n, m in rig.logs][:200]})


# ====================================================================================== switch
def do_switch(rig, a):
    L = rig.L
    res = {"stage": "switch"}
    m = rig.model
    # (a) 純切換開銷
    xs = []
    for _ in range(200):
        t = time.perf_counter()
        m.set_lora_enabled(False)
        m.set_lora_enabled(True)
        xs.append((time.perf_counter() - t) * 1e3 / 2)
    rig.cur_lora = True
    res["toggle_ms"] = L.pct(xs)
    # (b) LoRA 開／關對同一個 en 句子的生成差異（en 模式、參考音相同）
    ab = {"on": [], "off": []}
    for rep in range(5):
        for on in (True, False):
            rig.modes["en"]["lora"] = on
            row, _ = rig.synth("en", L.TEXTS["en"]["mid"][0], 100 + rep)
            ab["on" if on else "off"].append({k: row[k] for k in ("ttfa_core_s", "gen_s", "audio_s", "rtf", "lora_toggle_ms", "new_graphs")})
    rig.modes["en"]["lora"] = False
    res["lora_ab_en_mid"] = ab
    # (c) 區塊 vs 交錯（中句）
    block, inter = [], []
    for lang in L.LANGS:
        for j in range(5):
            row, _ = rig.synth(lang, L.TEXTS[lang]["mid"][j % 4], 200 + j)
            block.append(strip(row) | {"pos": j, "mode": "block"})
    for j in range(12):
        lang = L.LANGS[j % 3]
        row, _ = rig.synth(lang, L.TEXTS[lang]["mid"][(j // 3) % 4], 300 + j)
        inter.append(strip(row) | {"pos": j, "mode": "inter"})
    res["block"], res["inter"] = block, inter
    # (d) 長短交錯（同語言內長度劇烈變化 → 看是否觸發重編譯）
    seqs = []
    for lang in L.LANGS:
        for j in range(6):
            b = ("short", "long", "mid")[j % 3]
            row, _ = rig.synth(lang, L.TEXTS[lang][b][j % 4], 400 + j)
            seqs.append(strip(row) | {"bucket": b, "pos": j})
    res["len_alternate"] = seqs
    # (e) prompt cache：建置時間與使用後 TTFA
    pc = {}
    for lang in L.LANGS:
        cfg = rig.modes[lang]
        ts = []
        for _ in range(5):
            torch = rig.torch
            torch.cuda.synchronize()
            t = time.perf_counter()
            cache = m.tts_model.build_prompt_cache(prompt_text=cfg["ptext"], prompt_wav_path=cfg["ref"] if cfg["ptext"] else None,
                                                   reference_wav_path=cfg["ref"])
            torch.cuda.synchronize()
            ts.append((time.perf_counter() - t) * 1e3)
        rig.set_lora(cfg["lora"])
        plain, cached = [], []
        for rep in range(5):
            row, _ = rig.synth(lang, L.TEXTS[lang]["mid"][rep % 4], 500 + rep)
            plain.append(row["ttfa_core_s"])
            row, _ = rig.synth(lang, L.TEXTS[lang]["mid"][rep % 4], 500 + rep, prompt_cache=cache)
            cached.append(row["ttfa_core_s"])
        pc[lang] = {"build_ms": L.pct(ts), "ttfa_plain_s": L.pct(plain), "ttfa_cached_s": L.pct(cached)}
    res["prompt_cache"] = pc
    res["dyn_logs"] = [(round(t - T_SCRIPT, 2), n, mm) for t, n, mm in rig.logs][:200]
    L.jdump(os.path.join(L.RAW, "vox_switch.json"), res)


def do_affinity(rig, a):
    """CPU 親和性因果測試：Vox 的解碼迴圈是單執行緒 CPU 派發，看 RTF 是否隨 P 核／E 核漂移（12700F：邏輯核 0–15 = P，16–19 = E）。"""
    L = rig.L
    orig = L.get_affinity()
    res = {"orig_mask": orig}
    for name, mask in (("free", orig), ("P_only", 0xFFFF & orig), ("E_only", 0xF0000 & orig), ("free_noEcoQoS", orig), ("free_again", orig)):
        if not mask:
            continue
        L.set_affinity(mask)
        if name == "free_noEcoQoS":
            res["set_power_throttling_off_ok"] = L.set_power_throttling(False)
        if name == "free_again":
            L.set_power_throttling(None)
        time.sleep(0.5)
        rs = []
        for j in range(6):
            row, _ = rig.synth("zh", L.TEXTS["zh"]["mid"][j % 4], 600 + j)
            rs.append({k: row[k] for k in ("ttfa_e2e_s", "gen_s", "audio_s", "rtf", "core0", "core1")})
        res[name] = rs
        print("affinity", name, round(sorted(r["rtf"] for r in rs)[len(rs) // 2], 3), flush=True)
    L.set_affinity(orig)
    L.jdump(os.path.join(L.RAW, "vox_affinity.json"), res)


def cmd_all(a):
    """同一個行程依序：main（含冷啟動時間線）→ switch → sustained，只載入一次。"""
    rig = Rig(cache="default")
    stages = a.only.split(",") if a.only else ["main", "switch", "affinity", "sustained"]
    if "main" in stages:
        do_main(rig, a)
    else:
        rig.warm()
    if "switch" in stages:
        do_switch(rig, a)
    if "affinity" in stages:
        do_affinity(rig, a)
    if "sustained" in stages:
        do_sustained(rig, a)
    rig.close()


def cmd_coldstart(a):
    """全新的 inductor/triton 快取目錄下的冷啟動：載入 + 每模式第一次呼叫 + 暖機。不跑分佈。"""
    rig = Rig(cache="cold")
    L = rig.L
    res = {"stage": "coldstart", "cache_dir": rig.cache_dir, "timeline": rig.tl, "mem": rig.mem}
    fc = []
    for lang in L.LANGS:
        row, _ = rig.synth(lang, L.TEXTS[lang]["short"][0], 11)
        fc.append(strip(row) | {"since_proc_start_s": time.time() - rig.tl["proc_start_epoch"]})
        if lang == "zh":
            rig.tl["first_audio_after_proc_start_s"] = (time.time() - rig.tl["proc_start_epoch"]) - row["gen_s"] + row["ttfa_core_s"]
    res["first_calls"] = fc
    res["warm"] = rig.warm()
    rig.snap("after_warm")
    L.jdump(os.path.join(L.RAW, "vox_coldstart.json"), res)
    rig.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["all", "coldstart"])
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--idle", type=float, default=3.0, help="每個 turn 前的空檔秒數")
    ap.add_argument("--only", default="", help="all 時只跑指定階段（逗號分隔）")
    a = ap.parse_args()
    {"all": cmd_all, "coldstart": cmd_coldstart}[a.stage](a)
