"""把 outputs/latency_bench/raw/*.json 彙整成 markdown 表格（outputs/latency_bench/tables.md）。純 CPU，任何 python 都能跑。"""
import glob
import json
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import latency_bench_lib as L  # noqa: E402

RAW = L.RAW
out = []


def J(name):
    p = os.path.join(RAW, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def P(xs, q):
    xs = [x for x in xs if x is not None]
    return float(np.percentile(xs, q)) if xs else float("nan")


def f(x, nd=2):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.{nd}f}"


def table(head, rows):
    out.append("| " + " | ".join(head) + " |")
    out.append("|" + "|".join("---" for _ in head) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    out.append("")


def h(t, lv=3):
    out.append("#" * lv + " " + t)
    out.append("")


ENG = {"vox": J("vox_main.json"), "v25": J("v25_main.json")}
NAME = {"vox": "Vox", "v25": "V25"}


def rows_of(eng, lang, b):
    d = ENG[eng]
    return [r for r in d["rows"] if r["lang"] == lang and r["bucket"] == b] if d else []


# ------------------------------------------------------------------ 1 冷啟動
def sec_cold():
    h("1. 冷啟動")
    vc, vw, v25 = J("vox_coldstart.json"), ENG["vox"], ENG["v25"]
    def g(d, k):
        return f(d["timeline"].get(k)) if d else "-"
    cols = [("Vox 冷快取（全新 inductor/triton 目錄）", vc), ("Vox 暖快取（磁碟快取）", vw)]
    rows = []
    for label, k in (("import torch", "import_torch_s"), ("import voxcpm + 實驗模組", "import_voxcpm_and_exp_s"), ("CUDA 初始化", "cuda_init_s"),
                     ("from_pretrained 合計", "from_pretrained_total_s"), ("  其中內建暖機（torch.compile 首次編譯）", "builtin_warmup_s"),
                     ("  其中權重載入", "weights_load_s"), ("LoRA step_0000600 載入", "lora_load_s"), ("TWN 初始化", "twn_init_s"),
                     ("假名化初始化（含 pyopenjtalk 載入）", "kana_init_s"), ("假名化首次呼叫", "kana_first_call_s"),
                     ("行程啟動 → 可呼叫 synth", "ready_s"), ("行程啟動 → 第一個 zh 音訊（TTFA 到手）", "first_audio_after_proc_start_s")):
        rows.append([label] + [g(d, k) for _, d in cols])
    table(["Vox 項目（秒）"] + [c for c, _ in cols], rows)
    if v25:
        t = v25["timeline"]
        it = t.get("engine_init_timings_ms", {})
        rows = [["import torch", f(t.get("import_torch_s"))], ["import echo_stream", f(t.get("import_es_s"))], ["CUDA 初始化", f(t.get("cuda_init_s"))],
                ["_build_backend", f(t.get("build_backend_s"))], ["prepare() 合計（載入 + merge_lora + 引擎內建暖機）", f(t.get("prepare_total_s"))],
                ["  GPT 載入", f(it.get("gpt_ms", 0) / 1e3)], ["  semantic codec", f(it.get("semantic_codec_ms", 0) / 1e3)], ["  s2mel", f(it.get("s2mel_ms", 0) / 1e3)],
                ["  BigVGAN（含 CUDA kernel 載入／編譯）", f(it.get("bigvgan_ms", 0) / 1e3)], ["  引擎內建暖機", f(it.get("warmup_ms", 0) / 1e3)],
                ["merge_lora(0.85)", f(t.get("merge_lora_s"))], ["行程啟動 → 可呼叫", f(t.get("ready_s"))],
                ["行程啟動 → 第一個 zh 音訊", f(t.get("first_audio_after_proc_start_s"))]]
        table(["V25（ES 路徑，accel + LoRA 0.85）項目", "秒"], rows)
    h("首次呼叫（載入後、未額外暖機）", 4)
    rows = []
    for eng in ("vox", "v25"):
        for fc in (ENG[eng] or {}).get("first_calls", []):
            rows.append([NAME[eng], fc["lang"], f(fc.get("ttfa_e2e_s")), f(fc["gen_s"]), f(fc["audio_s"]), f(fc["rtf"])])
    if vc:
        for fc in vc.get("first_calls", []):
            rows.append(["Vox（冷快取）", fc["lang"], f(fc.get("ttfa_e2e_s")), f(fc["gen_s"]), f(fc["audio_s"]), f(fc["rtf"])])
    table(["引擎", "語言", "TTFA", "gen_s", "音長", "RTF"], rows)


# ------------------------------------------------------------------ 2/3/4 分佈
def sec_dist():
    h("2. 首包延遲（TTFA）與 RTF 分佈")
    out.append("TTFA = 呼叫到第一個可播放 chunk 到手（含前處理、LoRA 切換）；Vox 為 generate_streaming 第一個 chunk（約 160 ms 音訊），"
               "V25 為 ES 路徑 `IndexTTS2SpeakStage.stream` 的第一個 AudioChunk（= 第一個 segment 合成完成，短中句等於整句）。\n")
    rows = []
    for lang in L.LANGS:
        for b in ("short", "mid", "long"):
            for eng in ("vox", "v25"):
                rs = rows_of(eng, lang, b)
                if not rs:
                    continue
                t = [r["ttfa_e2e_s"] for r in rs]
                rows.append([lang, b, NAME[eng], len(rs), f(np.mean([r["audio_s"] for r in rs]), 1)] + [f(P(t, q), 3) for q in (50, 90, 95, 99)] + [f(max(t), 3)])
    table(["語言", "桶", "引擎", "n", "音長(s)", "TTFA P50", "P90", "P95", "P99", "max"], rows)
    h("RTF 與總生成時間", 4)
    rows = []
    for lang in L.LANGS:
        for b in ("short", "mid", "long"):
            for eng in ("vox", "v25"):
                rs = rows_of(eng, lang, b)
                if not rs:
                    continue
                r_ = [r["rtf"] for r in rs]
                g_ = [r["gen_s"] for r in rs]
                rows.append([lang, b, NAME[eng], len(rs), f(P(g_, 50)), f(P(g_, 95)), f(max(g_))] + [f(P(r_, q), 3) for q in (50, 90, 95, 99)] + [f(max(r_), 3)])
    table(["語言", "桶", "引擎", "n", "gen P50(s)", "gen P95", "gen max", "RTF P50", "P90", "P95", "P99", "max"], rows)


def sec_outliers():
    h("3. 離群值")
    out.append("離群 = TTFA 或 RTF 超過「中位數 + max(3×MAD, 15%)」。欄位：外部 CPU = 系統 CPU% − 本行程 CPU%（該呼叫前後 ±1 秒取樣平均）；"
               "核 = 呼叫起訖所在邏輯處理器（0–15 P 核、16–19 E 核）；GPU 他人 = 其他行程 GPU 引擎使用率合計。\n")
    for eng in ("vox", "v25"):
        d = ENG[eng]
        if not d:
            continue
        mon0 = d["timeline"].get("mon_t0")
        samples = d.get("monitor_samples", [])
        rows = []
        for lang in L.LANGS:
            for b in ("short", "mid", "long"):
                rs = rows_of(eng, lang, b)
                if len(rs) < 5:
                    continue
                for key in ("ttfa_e2e_s", "rtf"):
                    for i in L.outliers(rs, key):
                        r = rs[i]
                        ctx = ""
                        if mon0 is not None:
                            t1 = r["t_wall"] - mon0
                            win = [s for s in samples if t1 - r["gen_s"] - 1 <= s["t"] <= t1 + 1]
                            if win:
                                oc = np.mean([s["sys_cpu_pct"] - s["proc_cpu_pct"] for s in win])
                                ctx = f"外部CPU {oc:.0f}% / SM {np.mean([s.get('sm_mhz', 0) for s in win]):.0f}MHz / {np.max([s.get('temp_c', 0) for s in win]):.0f}°C / GPU他人 {np.mean([s.get('gpu_eng_other_sum', 0) for s in win]):.0f}%"
                        rows.append([lang, b, r["idx"], key, f(r[key], 3), f(float(np.median([x[key] for x in rs])), 3), f(r["audio_s"], 1), r.get("core0", "-"),
                                     ctx])
        out.append(f"**{NAME[eng]}**")
        out.append("")
        table(["語言", "桶", "idx", "指標", "值(s)", "同桶中位", "音長", "核", "當時環境"], rows or [["（無）", "", "", "", "", "", "", "", ""]])


# ------------------------------------------------------------------ 6 串流穩定性
def sec_stream():
    h("4. 串流穩定性（即時播放模擬）")
    out.append("播放從第一個 chunk 到手即開始（prebuffer 0），後續 chunk 若晚於播放進度即記一次 underrun。safe-start = 能做到零 underrun 的最早起播時刻（含預緩衝）。\n")
    rows = []
    for lang in L.LANGS:
        for b in ("short", "mid", "long"):
            for eng in ("vox", "v25"):
                rs = rows_of(eng, lang, b)
                if not rs:
                    continue
                gaps = []
                for r in rs:
                    ch = r["chunks"]
                    gaps += [ch[i + 1][0] - ch[i][0] for i in range(len(ch) - 1)]
                ub = [L.playback_sim([tuple(c) for c in r["chunks"]], pb)["underruns"] for r in rs for pb in (0.0,)]
                ub200 = [L.playback_sim([tuple(c) for c in r["chunks"]], 0.2)["underruns"] for r in rs]
                ub500 = [L.playback_sim([tuple(c) for c in r["chunks"]], 0.5)["underruns"] for r in rs]
                ss = [r["safe_start_s"] for r in rs]
                tt = [r["ttfa_e2e_s"] for r in rs]
                rows.append([lang, b, NAME[eng], f(np.mean([r["n_chunks"] for r in rs]), 1), f(P(gaps, 50) * 1e3, 0) if gaps else "-",
                             f(P(gaps, 95) * 1e3, 0) if gaps else "-", f(max(gaps) * 1e3, 0) if gaps else "-",
                             f"{sum(1 for u in ub if u)}/{len(rs)}", f"{sum(1 for u in ub200 if u)}/{len(rs)}", f"{sum(1 for u in ub500 if u)}/{len(rs)}",
                             f(P(ss, 50), 3), f(P(ss, 95), 3), f(P(ss, 95) - P(tt, 95), 3)])
    table(["語言", "桶", "引擎", "chunk 數", "間隔 P50(ms)", "P95", "max", "有 underrun 的呼叫(prebuf 0)", "(prebuf 200ms)", "(prebuf 500ms)",
           "safe-start P50(s)", "P95", "P95 − TTFA P95"], rows)


def sec_turn():
    h("5. ES 切句段落（turn）：句間銜接與 underrun")
    out.append("段落經 ES 切句器（echo_stream.toml 的 split 參數）切成 2–3 句後逐句順序合成；每輪前先靜置 3 秒（模擬 STT/LLM 空檔，GPU 降頻後第一句）。\n")
    rows = []
    for lang in L.LANGS:
        for eng in ("vox", "v25"):
            d = ENG[eng]
            if not d:
                continue
            ts = [t for t in d["turns"] if t["lang"] == lang]
            if not ts:
                continue
            rows.append([lang, NAME[eng], len(ts), f(np.mean([t["n_sent"] for t in ts]), 1), f(np.mean([t["audio_s"] for t in ts]), 1),
                         f(P([t["ttfa_s"] for t in ts], 50), 3), f(P([t["ttfa_s"] for t in ts], 95), 3), f(max(t["ttfa_s"] for t in ts), 3),
                         f(P([t["turn_rtf"] for t in ts], 50), 3), f(P([t["turn_rtf"] for t in ts], 95), 3),
                         f"{sum(1 for t in ts if t['underruns'])}/{len(ts)}", f(sum(t["stall_s"] for t in ts) / len(ts), 2),
                         f(P([t["safe_start_s"] for t in ts], 50), 3), f(P([t["safe_start_s"] for t in ts], 95), 3)])
    table(["語言", "引擎", "n", "句數", "音長(s)", "TTFA P50", "P95", "max", "turn RTF P50", "P95", "有 underrun 的 turn", "平均停頓(s)", "safe-start P50", "P95"], rows)


# ------------------------------------------------------------------ 7 切換
def sec_switch():
    h("6. 切換成本")
    vs, vv = J("vox_switch.json"), J("v25_switch.json")
    if vs:
        t = vs["toggle_ms"]
        out.append(f"- Vox `set_lora_enabled` 單次：P50 {t['p50']:.3f} ms、P99 {t['p99']:.3f} ms、max {t['max']:.3f} ms（200 次）。")
        ab = vs["lora_ab_en_mid"]
        for k in ("on", "off"):
            out.append(f"- Vox en 中句 LoRA {k}：gen P50 {P([x['gen_s'] for x in ab[k]], 50):.3f}s、RTF P50 {P([x['rtf'] for x in ab[k]], 50):.3f}、TTFA(core) P50 {P([x['ttfa_core_s'] for x in ab[k]], 50):.3f}s。")
        out.append("")
    def blk(d, label):
        rows = []
        for lang in L.LANGS:
            b_all = [r for r in d["block"] if r["lang"] == lang]
            b_first, b_rest = [r for r in b_all if r["pos"] == 0], [r for r in b_all if r["pos"] > 0]
            inter = [r for r in d["inter"] if r["lang"] == lang]
            i_first = [r for r in inter if r["pos"] < 3]
            rows.append([label, lang, f(np.median([r["ttfa_e2e_s"] for r in b_first]), 3), f(np.median([r["ttfa_e2e_s"] for r in b_rest]), 3),
                         f(np.median([r["ttfa_e2e_s"] for r in inter]), 3), f(max(r["ttfa_e2e_s"] for r in inter), 3),
                         f(np.median([r["rtf"] for r in b_rest]), 3), f(np.median([r["rtf"] for r in inter]), 3), f(max(r["rtf"] for r in inter), 3)])
        return rows
    rows = []
    for lab, d in (("Vox", vs), ("V25", vv)):
        if d:
            rows += blk(d, lab)
    h("中句：區塊連續 vs 三語交錯", 4)
    table(["引擎", "語言", "區塊內第 1 筆(切換後) TTFA", "區塊內第 2+ 筆 TTFA P50", "交錯 TTFA P50", "交錯 TTFA max", "區塊 RTF P50", "交錯 RTF P50", "交錯 RTF max"], rows)
    h("同語言內長短劇烈交替（短→長→中循環）", 4)
    rows = []
    for lab, d in (("Vox", vs), ("V25", vv)):
        if not d:
            continue
        for lang in L.LANGS:
            for b in ("short", "mid", "long"):
                rs = [r for r in d["len_alternate"] if r["lang"] == lang and r["bucket"] == b]
                rows.append([lab, lang, b, len(rs), f(P([r["ttfa_e2e_s"] for r in rs], 50), 3), f(max(r["ttfa_e2e_s"] for r in rs), 3), f(P([r["rtf"] for r in rs], 50), 3), f(max(r["rtf"] for r in rs), 3)])
    table(["引擎", "語言", "桶", "n", "TTFA P50", "TTFA max", "RTF P50", "RTF max"], rows)
    if vs:
        h("Vox prompt cache（每次呼叫都重建參考音 prompt 的成本）", 4)
        rows = [[lang, f(v["build_ms"]["p50"], 1), f(v["ttfa_plain_s"]["p50"], 3), f(v["ttfa_cached_s"]["p50"], 3)] for lang, v in vs["prompt_cache"].items()]
        table(["語言", "build_prompt_cache P50(ms)", "TTFA(core) 一般 P50(s)", "TTFA(core) 預建 cache P50(s)"], rows)
    if vv:
        ab = vv["style_ab_zh_mid"]
        out.append(f"- V25 ES 風格（speed 1.1 + 情緒向量）vs 無 style，zh 中句：gen P50 {P([x['gen_s'] for x in ab['style']], 50):.2f}s vs {P([x['gen_s'] for x in ab['none']], 50):.2f}s；"
                   f"音長 {P([x['audio_s'] for x in ab['style']], 50):.2f}s vs {P([x['audio_s'] for x in ab['none']], 50):.2f}s。")
        out.append("")


def sec_pre():
    h("7. 前處理成本（單句，ms）")
    d = ENG["vox"]
    if not d:
        return
    rows = []
    for k, v in d["preproc"].items():
        rows.append([k.replace("_ms", ""), f(v["p50"], 3), f(v["p95"], 3), f(v["max"], 3)])
    table(["項目", "P50", "P95", "max"], rows)


# ------------------------------------------------------------------ 9 資源
def sec_res():
    h("8. 資源")
    base = None
    rows = []
    for eng in ("vox", "v25"):
        d = ENG[eng]
        if not d:
            continue
        m = d["mem"]
        for k, v in m.items():
            rows.append([NAME[eng], k, f(v["alloc_mb"], 0), f(v["reserved_mb"], 0), f(v.get("max_alloc_mb"), 0), f(v.get("vram_used_mb"), 0), f(v["rss_mb"], 0), f(v.get("proc_shared_mb"), 0),
                         f(v.get("proc_dedicated_mb"), 0), f(v.get("adapter_shared_mb"), 0)])
    table(["引擎", "里程碑", "torch allocated MB", "reserved MB", "peak alloc MB", "nvidia-smi 全卡 used MB", "行程 RSS MB", "WDDM 行程共享 MB", "行程專用 MB", "全轉接卡共享 MB"], rows)
    h("各條件的 GPU／CPU 使用（取樣視窗平均／最大）", 4)
    rows = []
    for eng in ("vox", "v25"):
        d = ENG[eng]
        if not d:
            continue
        for k, w in d["resource_windows"].items():
            if not w:
                continue
            g = lambda n, s="mean": f(w[n][s], 0) if n in w else "-"  # noqa: E731
            rows.append([NAME[eng], k, w["n"], g("gpu_util"), g("gpu_util", "max"), g("temp_c", "max"), g("sm_mhz"), g("power_w"), g("proc_cpu_pct"), g("sys_cpu_pct"), g("proc_shared_mb", "max"),
                         ",".join(w.get("throttle_codes", []))])
    table(["引擎", "條件", "樣本", "GPU util 平均%", "最大%", "溫度最大°C", "SM 時脈平均MHz", "功耗W", "本行程 CPU%（佔全機）", "系統 CPU%", "共享記憶體最大MB", "降頻原因碼"], rows)


def sec_sustained():
    h("9. 持續負載（約 10 分鐘三語交錯）")
    for eng in ("vox", "v25"):
        d = J(f"{eng}_sustained.json")
        if not d:
            continue
        rows_ = d["rows"]
        n = len(rows_)
        T = max(r["t_off"] for r in rows_)
        thirds = [rows_[: n // 3], rows_[n // 3: 2 * n // 3], rows_[2 * n // 3:]]
        out.append(f"**{NAME[eng]}**：{n} 次呼叫、{T / 60:.1f} 分鐘。")
        out.append("")
        h("三等分比較（同樣的語言／長度組合輪替）", 4)
        table(["段", "n", "RTF P50", "RTF P95", "RTF max", "TTFA P50", "TTFA P95", "TTFA max", "underrun 呼叫"],
              [[i + 1, len(t), f(P([r["rtf"] for r in t], 50), 3), f(P([r["rtf"] for r in t], 95), 3), f(max(r["rtf"] for r in t), 3), f(P([r["ttfa_e2e_s"] for r in t], 50), 3),
                f(P([r["ttfa_e2e_s"] for r in t], 95), 3), f(max(r["ttfa_e2e_s"] for r in t), 3), sum(1 for r in t if r["underruns"])] for i, t in enumerate(thirds)])
        # 每分鐘 RTF 中位數
        mins = {}
        for r in rows_:
            mins.setdefault(int(r["t_off"] // 60), []).append(r["rtf"])
        out.append("每分鐘 RTF 中位數：" + "、".join(f"{k}分 {np.median(v):.3f}" for k, v in sorted(mins.items())))
        out.append("")
        x = np.array([r["t_off"] for r in rows_])
        y = np.array([r["rtf"] for r in rows_])
        slope = np.polyfit(x / 60, y, 1)[0]
        out.append(f"RTF 對時間線性斜率：{slope:+.4f} / 分鐘（整體 RTF 中位 {np.median(y):.3f}）。")
        out.append("")
        # 慢相位分析：與同 (語言,桶) 的 P25 RTF 相比超過 1.25 倍者
        base = {}
        for r in rows_:
            base.setdefault((r["lang"], r["bucket"]), []).append(r["rtf"])
        base = {k: float(np.percentile(v, 25)) for k, v in base.items()}
        slow = [r for r in rows_ if r["rtf"] > 1.25 * base[(r["lang"], r["bucket"])]]
        out.append(f"慢相位呼叫（RTF > 1.25 × 同桶 P25）：{len(slow)}/{n}（{100 * len(slow) / n:.0f}%）；發生在第 " +
                   ("、".join(f"{k}" for k in sorted({int(r['t_off'] // 60) for r in slow})) + " 分鐘。") if slow else "（無）")
        out.append("")
        s_all = d["monitor_samples"]
        mon0 = d["timeline"].get("mon_t0")
        s = s_all
        if mon0 is not None:
            a0 = rows_[0]["t_wall"] - rows_[0]["gen_s"] - mon0
            s = [dict(q, t=q["t"] - a0) for q in s_all if a0 <= q["t"] <= a0 + T + 2]
        if s:
            def span(key, a, b):
                v = [q[key] for q in s if key in q and a <= q["t"] <= b]
                return np.mean(v) if v else float("nan")
            tt = s[-1]["t"]
            rows = []
            for key, lab in (("temp_c", "GPU 溫度 °C"), ("sm_mhz", "SM 時脈 MHz"), ("power_w", "功耗 W"), ("gpu_util", "GPU util %"), ("t_alloc_mb", "torch allocated MB"),
                             ("t_reserved_mb", "torch reserved MB"), ("rss_mb", "行程 RSS MB"), ("proc_shared_mb", "WDDM 行程共享 MB"), ("proc_cpu_pct", "本行程 CPU%（佔全機）"),
                             ("sys_cpu_pct", "系統 CPU%"), ("gpu_eng_other_sum", "其他行程 GPU%")):
                rows.append([lab, f(span(key, 0, 60), 0), f(span(key, tt / 2 - 30, tt / 2 + 30), 0), f(span(key, tt - 60, tt), 0), f(max((q.get(key, 0) for q in s), default=0), 0)])
            table(["指標", "前 60 秒", "中間 60 秒", "最後 60 秒", "全程最大"], rows)
            codes = {}
            for q in s:
                codes[q.get("throttle")] = codes.get(q.get("throttle"), 0) + 1
            out.append("降頻原因碼分佈（樣本數）：" + "、".join(f"{k}: {v}" for k, v in codes.items()))
            out.append("")


def sec_quality():
    q = J("quality.json")
    if not q:
        return
    h("10. 品質健全性（ES Breeze ASR CER；Vox 另算 SIM 對 R1）")
    rows = []
    for eng in ("vox", "v25"):
        for lang in L.LANGS:
            for b in ("short", "mid", "long", "turn"):
                rs = [r for r in q[eng] if r["lang"] == lang and r["bucket"] == b]
                if not rs:
                    continue
                cer = [r["cer"] for r in rs]
                ck = [r["cer_kana"] for r in rs if "cer_kana" in r]
                sim = [r["sim_r1"] for r in rs if "sim_r1" in r]
                rows.append([NAME[eng], lang, b, len(rs), f(np.mean(cer), 3), f(max(cer), 3), f(np.mean(ck), 3) if ck else "-", f(np.mean(sim), 3) if sim else "-"])
    table(["引擎", "語言", "桶", "n", "CER 平均", "CER 最大", "ja kana-CER 平均", "SIM(R1) 平均"], rows)


def sec_affinity():
    h("12. CPU 排程對延遲的影響（親和性／EcoQoS 因果測試，zh 中句各 6 次，RTF P50）")
    rows = []
    for eng, nm in (("vox", "Vox"), ("v25", "V25")):
        d = J(f"{eng}_affinity.json")
        if not d:
            continue
        rows.append([nm] + [f(float(np.median([x["rtf"] for x in d[k]])), 3) for k in ("free", "P_only", "E_only", "free_noEcoQoS", "free_again")])
    table(["引擎", "預設排程", "僅 P 核(0–15)", "僅 E 核(16–19)", "預設親和性 + 關閉 EcoQoS", "還原預設"], rows)


def sec_recompile():
    h("11. 重編譯診斷與緩解")
    d = J("vox_recompile_diag.json")
    if d:
        ab = d["monitor_ab"]
        out.append(f"- 監控開銷 A/B（zh 中句各 6 次）：監控開 gen P50 {np.median([r['gen_s'] for r in ab['monitor_on']]):.3f}s / RTF {np.median([r['rtf'] for r in ab['monitor_on']]):.3f}；"
                   f"監控關 gen P50 {np.median([r['gen_s'] for r in ab['monitor_off']]):.3f}s / RTF {np.median([r['rtf'] for r in ab['monitor_off']]):.3f}。")
        for k, lab in (("ladder_asc", "長度階梯升序"), ("ladder_rnd", "長度階梯隨機序"), ("alternate_long_short", "長短交替")):
            rs = d[k]
            out.append(f"- {lab}（{len(rs)} 次）：RTF P50 {np.median([r['rtf'] for r in rs]):.3f}、max {max(r['rtf'] for r in rs):.3f}；TTFA P50 {np.median([r['ttfa_e2e_s'] for r in rs]):.3f}、max {max(r['ttfa_e2e_s'] for r in rs):.3f}。")
        out.append("")
    rows = []
    for name in ("baseline", "nowarm", "dynamic"):
        v = J(f"vox_variant_{name}.json")
        if not v:
            continue
        err = os.path.join(RAW, f"vox_variant_{name}.err")
        n_rc = n_cg = n_gb = 0
        if os.path.exists(err):
            txt = open(err, encoding="utf-8", errors="ignore").read()
            after = txt.split("### FIRST", 1)[-1]
            n_rc = len(re.findall(r"Recompiling function", after))
            n_cg = len(re.findall(r"Recording function", after))
            n_gb = len(re.findall(r"Graph break", after))
        f1 = v["first"]
        rnd = v["ladder_rnd"]
        rows.append([name, f(v["timeline"].get("from_pretrained_total_s"), 1), f(v["timeline"].get("builtin_warmup_s"), 1), f(f1[0]["ttfa_e2e_s"], 2), f(f1[0]["gen_s"], 2),
                     f(max(r["ttfa_e2e_s"] for r in f1), 2), f(max(r["gen_s"] / max(r["audio_s"], 1e-6) for r in f1), 2),
                     f(np.median([r["rtf"] for r in rnd]), 3), f(max(r["rtf"] for r in rnd), 3), f(max(r["ttfa_e2e_s"] for r in rnd), 2), n_rc, n_cg, n_gb])
    if rows:
        table(["變體", "load 合計(s)", "內建暖機(s)", "第 1 次呼叫 TTFA", "第 1 次 gen_s", "前 12 次 TTFA max", "前 12 次 RTF max", "長度亂序 27 次 RTF P50", "RTF max", "TTFA max", "呼叫期間 recompile 數", "cudagraph 重錄數", "graph break 數"], rows)
    b = J("vox_variant_baseline.json")
    if b and "first_after_multiwarm" in b:
        out.append("multiwarm（用 27 種長度預熱一輪後再跑同樣的前 12 次）：TTFA max "
                   f"{max(r['ttfa_e2e_s'] for r in b['first_after_multiwarm']):.2f}s（預熱前 {max(r['ttfa_e2e_s'] for r in b['first']):.2f}s）；RTF max "
                   f"{max(r['rtf'] for r in b['first_after_multiwarm']):.2f}（預熱前 {max(r['rtf'] for r in b['first']):.2f}）。")
        out.append("")


if __name__ == "__main__":
    for fn in (sec_cold, sec_dist, sec_outliers, sec_stream, sec_turn, sec_switch, sec_pre, sec_res, sec_sustained, sec_quality, sec_recompile, sec_affinity):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            out.append(f"（{fn.__name__} 失敗：{type(e).__name__}: {e}）")
            out.append("")
    open(os.path.join(L.OUT, "tables.md"), "w", encoding="utf-8").write("\n".join(out))
    print("\n".join(out))
