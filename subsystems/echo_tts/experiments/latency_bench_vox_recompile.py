"""Vox 重編譯診斷與緩解比較（.venv-voxcpm，PYTHONUTF8=1）。

  diag     基準模型：量監控開銷（A/B）、長度階梯（每語言 9 種長度，升序 + 隨機序）、記錄每次呼叫是否變慢
           建議搭配 TORCH_LOGS=recompiles,cudagraphs,graph_breaks 並把 stderr 導到檔案（呼叫前會寫 "### call ..." 標記）
  variant  緩解方案比較（--name）：
             baseline      官方 optimize=True，只做 tw_blind_1101 式暖機（短中長）
             multiwarm     載入後用多種長度預熱
             dynamic       torch.compile 改 dynamic=True（在 wrapper 內重新包裝 compile，不動 site-packages）
             pad           (視診斷結果) 固定 padding 形狀
所有改動只在本腳本內 monkeypatch，不修改 site-packages。
"""
import argparse
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import latency_bench_lib as L  # noqa: E402
import latency_bench_vox as V  # noqa: E402


def mark(msg):
    print(f"### {msg}", file=sys.stderr, flush=True)


def ladder(lang):
    """每語言 9 種長度（從短到長），由長句前綴取出。"""
    base = L.TEXTS[lang]["long"][0] + L.TEXTS[lang]["long"][1]
    if lang == "en":
        w = base.split()
        sizes = [2, 4, 6, 10, 14, 20, 28, 36, 44]
        return [" ".join(w[:n]).rstrip(",.") + "." for n in sizes]
    sizes = {"zh": [4, 8, 12, 16, 20, 26, 32, 40, 52], "ja": [5, 8, 12, 16, 22, 30, 40, 55, 70]}[lang]
    return [base[:n].rstrip("，、。") + "。" for n in sizes]


def run_calls(rig, items, tag, seed0=0, verbose=True):
    out = []
    for i, (lang, text) in enumerate(items):
        mark(f"call {tag} {i} {lang} len={len(text)}")
        row, w = rig.synth(lang, text, 700 + seed0 + i)
        r = V.strip(row)
        r.update(tag=tag, i=i, nchar=len(text))
        out.append(r)
        if verbose:
            print(tag, i, lang, len(text), f"ttfa={row['ttfa_e2e_s']:.2f} gen={row['gen_s']:.2f} aud={row['audio_s']:.2f} rtf={row['rtf']:.2f} g+{row['new_graphs']}", flush=True)
    return out


def cmd_diag(a):
    rig = V.Rig(cache="default")
    res = {"stage": "diag", "timeline": rig.tl, "mem": rig.mem}
    mark("warm")
    res["warm"] = rig.warm(("en", "ja", "zh"))  # 刻意先 en、ja 後 zh：分辨「第一次呼叫」vs「第一次 zh/ULT/LoRA 路徑」
    # A/B：監控開銷（監控停掉後 nvidia-smi/typeperf 子行程終止）
    ab = {}
    items = [("zh", L.TEXTS["zh"]["mid"][i % 4]) for i in range(6)]
    ab["monitor_on"] = run_calls(rig, items, "ab_on")
    rig.mon.stop()
    time.sleep(1.5)
    ab["monitor_off"] = run_calls(rig, items, "ab_off")
    res["monitor_ab"] = ab
    lad = {lang: ladder(lang) for lang in L.LANGS}
    asc = [(lang, t) for lang in L.LANGS for t in lad[lang]]
    res["ladder_asc"] = run_calls(rig, asc, "asc")
    rnd = list(asc)
    random.Random(7).shuffle(rnd)
    res["ladder_rnd"] = run_calls(rig, rnd, "rnd", seed0=100)
    # 同一個語言內長短劇烈交替
    zz = []
    for j in range(9):
        for lang in L.LANGS:
            zz.append((lang, lad[lang][8 if j % 2 == 0 else 0]))
    res["alternate_long_short"] = run_calls(rig, zz, "alt", seed0=200)
    res["dyn_stats"] = rig.dyn_stats()
    res["mem_end"] = rig.mem
    L.jdump(os.path.join(L.RAW, "vox_recompile_diag.json"), res)


FIRST = [("en", "short"), ("en", "mid"), ("en", "long"), ("ja", "short"), ("ja", "mid"), ("ja", "long"),
         ("zh", "short"), ("zh", "mid"), ("zh", "long"), ("zh", "long"), ("en", "short"), ("ja", "short")]


def first_items():
    return [(lang, L.TEXTS[lang][b][i % 4]) for i, (lang, b) in enumerate(FIRST)]


def cmd_variant(a):
    """--name: baseline（含 multiwarm 對照）| nowarm（不做內建暖機）| dynamic（torch.compile dynamic=True）。"""
    kw = {}
    if a.name == "nowarm":
        kw["no_builtin_warm"] = True
    elif a.name == "dynamic":
        kw["compile_kwargs"] = {"dynamic": True}
    t0 = time.time()
    rig = V.Rig(cache="default", **kw)
    res = {"stage": "variant", "name": a.name, "timeline": rig.tl, "mem": rig.mem}
    # prompt cache 首次 / 第二次建置（區分「首次呼叫延遲」是不是出在參考音前處理）
    cfg = rig.modes["zh"]
    pcs = []
    for _ in range(3):
        rig.torch.cuda.synchronize()
        t = time.perf_counter()
        rig.model.tts_model.build_prompt_cache(prompt_text=cfg["ptext"], prompt_wav_path=cfg["ref"], reference_wav_path=cfg["ref"])
        rig.torch.cuda.synchronize()
        pcs.append((time.perf_counter() - t) * 1e3)
    res["prompt_cache_build_ms"] = pcs
    mark("FIRST")
    res["first"] = run_calls(rig, first_items(), "first")
    lad = {lang: ladder(lang) for lang in L.LANGS}
    asc = [(lang, t) for lang in L.LANGS for t in lad[lang]]
    rnd = list(asc)
    random.Random(7).shuffle(rnd)
    if a.name == "baseline":
        # multiwarm：用 27 種長度預熱一輪，再量一次 FIRST
        mark("MULTIWARM")
        res["multiwarm_pass"] = run_calls(rig, asc, "mw")
        mark("FIRST2")
        res["first_after_multiwarm"] = run_calls(rig, first_items(), "first2", seed0=50)
    mark("LADDER_RND")
    res["ladder_rnd"] = run_calls(rig, rnd, "rnd", seed0=100)
    res["dyn_stats"] = rig.dyn_stats()
    res["mem_end"] = dict(rig.mem)
    rig.snap("end")
    res["mem"] = rig.mem
    res["monitor_samples"] = rig.mon.samples
    L.jdump(os.path.join(L.RAW, f"vox_variant_{a.name}.json"), res)
    rig.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["diag", "variant"])
    ap.add_argument("--name", default="baseline")
    a = ap.parse_args()
    {"diag": cmd_diag, "variant": cmd_variant}[a.stage](a)
