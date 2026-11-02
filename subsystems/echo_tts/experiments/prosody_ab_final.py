"""語調 A/B 最小版（CPU，共用環境）：由 raw 組出 A/B/C 三段與 V25 對照，量測並寫 README。

  python prosody_ab_final.py            全流程（需先跑 prosody_ab_vox.py，V25 可選 prosody_ab_v25.py）
SIM 用 .venv-voxcpm 的 resemblyzer（子行程，CPU）；ASR 用 CPU 的 Breeze2.5（int8），與 GPU fp16 轉寫可能略有差異。
"""
import json
import os
import shutil
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emotion_common as C  # noqa: E402
import prosody_ab_lib as L  # noqa: E402

TMP = os.path.join(L.OUT, "final_tmp")
VENV_VOX = os.path.join(C.ROOT, ".venv-voxcpm", "Scripts", "python.exe")
TEXT = dict(L.SENTS)
KEEP = list(L.KEEP)


def main():
    import librosa
    import soundfile as sf

    os.makedirs(TMP, exist_ok=True)
    vox = C.jload(os.path.join(L.OUT, "vox_gen.json"))["rows"]
    byname = {r["name"]: r for r in vox if r.get("name")}
    v25p = os.path.join(L.OUT, "v25_gen.json")
    v25 = C.jload(v25p)["rows"] if os.path.exists(v25p) else []

    def rd(rel, base=None):
        y, sr = sf.read(os.path.join(base or L.RAW, rel), dtype="float32")
        y = y if y.ndim == 1 else y.mean(axis=1)
        return librosa.resample(y, orig_sr=sr, target_sr=L.SR).astype(np.float32) if sr != L.SR else y

    def norm(y):
        h = 480
        n = len(y) // h
        e = np.sqrt(np.mean(y[:n * h].reshape(n, h) ** 2, axis=1))
        act = e > e.max() * 0.03  # 只算有聲框的 RMS，避免靜音墊影響增益
        z = y * (0.1 / float(np.sqrt(np.mean(e[act] ** 2))))  # 有聲 RMS 約 -20 dBFS
        pk = float(np.max(np.abs(z)))
        return z * (0.98 / pk) if pk > 0.98 else z

    base = {s: rd(byname[f"{s}__BASE"]["file"]) for s, _ in L.SENTS}
    nons = {s: rd(byname[f"{s}__NONSTREAM"]["file"]) for s, _ in L.SENTS}
    st2 = {s: rd(byname[f"{s}__STREAM2"]["file"]) for s, _ in L.SENTS}
    M = {}

    # ------------------------------------------------------------ (a)(b) 句尾／句首：串流 vs 非串流
    rows = []
    for s, _ in L.SENTS:
        a, b, c = base[s], nons[s], st2[s]
        n = min(len(a), len(b))
        r = byname[f"{s}__BASE"]
        ts, tn = L.tail_stats(a), L.tail_stats(b)
        last = r["chunk_lens"][-1]
        tl = L.tail_stats(a[-last:]) if last > 960 else None
        es = L.es_segment(a, sil_ms=0.0)
        rows.append({"sid": s, "len_stream": len(a), "len_nonstream": len(b), "len_stream2": len(c),
                     "diff_ms_stream_minus_non": round((len(a) - len(b)) / 48, 1),
                     "corr_stream_vs_non": round(float(np.corrcoef(a[:n], b[:n])[0, 1]), 3),
                     "corr_stream_vs_stream2": round(float(np.corrcoef(a[:min(len(a), len(c))], c[:min(len(a), len(c))])[0, 1]), 3),
                     "tail_stream": ts, "tail_non": tn, "tail_es_current": L.tail_stats(es),
                     "last_chunk_samples": last, "last_chunk_ms": round(last / 48, 1),
                     "head_first_abs": round(float(abs(a[0])), 5), "head_first4ms_over_peak": round(float(np.abs(a[:192]).max() / np.abs(a).max()), 4),
                     "last_sample_over_peak": ts["last_over_peak"]})
    M["tail"] = rows

    # ------------------------------------------------------------ 接縫變體（6 句，量 click）
    P = [base[s] for s, _ in L.SENTS]
    variants = {
        "current(淡出2ms+15ms靜音)": (L.concat_current(P), 0.015),
        "fadeout10": (L.concat_current(P, out_ms=10.0), 0.015),
        "fadeout20": (L.concat_current(P, out_ms=20.0), 0.015),
        "fadeout40": (L.concat_current(P, out_ms=40.0), 0.015),
        "backend-tail40": (L.concat_current([L.natural_decay(p, 40.0) for p in P]), 0.015),
        "xfade20": (L.concat_xfade(P, 20), 0.0),
        "xfade40": (L.concat_xfade(P, 40), 0.0),
        "drop40": (L.concat_drop(P, 40), 0.015),
        "drop80": (L.concat_drop(P, 80), 0.015),
        "nonstream(current)": (L.concat_current([nons[s] for s, _ in L.SENTS]), 0.015),
    }
    seam = {}
    for k, ((y, joins), sil) in variants.items():
        ends = [j - sil for j in joins]
        ce, cs = L.click_metrics(y, ends), L.click_metrics(y, joins)
        jm = L.junction_summary(L.junction_metrics(y, joins))
        seam[k] = {"end_cut": L.click_summary(ce), "start": L.click_summary(cs), "junction": jm}
    M["seam"] = seam
    cands = {k: seam[k]["end_cut"]["max_step_over_p99_mean"] + seam[k]["start"]["max_step_over_p99_mean"] for k in ("fadeout20", "backend-tail40")}
    fix = min(cands, key=cands.get)
    M["seam_choice"] = {"fix": fix, "score(end+start max_step/p99 mean)": cands}

    def make_seam(parts, fixname):
        if fixname == "fadeout20":
            return L.concat_current(parts, out_ms=20.0)[0]
        return L.concat_current([L.natural_decay(p, 40.0) for p in parts])[0]

    # ------------------------------------------------------------ A / B / C
    segs_all = []
    for s in KEEP:
        r = byname[f"{s}__SPLIT-OLD"]
        segs_all += [rd(f) for f in r["seg_files"]]
    A = L.concat_current(segs_all)[0]
    B = make_seam([base[s] for s in KEEP], fix)
    refrows = {}
    for k in ("surprised073", "disgusted073"):
        refrows[k] = [rd(byname[f"{s}__REF-{k}"]["file"]) for s in KEEP]

    # 參考音挑選：F0 範圍（5–95 百分位半音）最大，且 SIM(R1) 不低於 0.85
    sim_in = {}
    for k, ys in refrows.items():
        for s, y in zip(KEEP, ys):
            p = os.path.join(TMP, f"{s}__REF-{k}.wav")
            sf.write(p, norm(y), L.SR, subtype="PCM_16")
            sim_in[f"{s}__REF-{k}"] = p
    for s in KEEP:
        p = os.path.join(TMP, f"{s}__BASE.wav")
        sf.write(p, norm(base[s]), L.SR, subtype="PCM_16")
        sim_in[f"{s}__BASE"] = p
    for k in ("surprised073", "disgusted073"):
        sim_in[f"ref/{k}"] = os.path.join(L.OUT, "refs", f"{k}_x1.2.wav")
    sim_in["ref/neutral"] = C.NEUTRAL_REF
    sim_json = _sim(sim_in, "sim_pre")
    pre = {}
    for k in refrows:
        pr = [C.prosody(sim_in[f"{s}__REF-{k}"], TEXT[s]) for s in KEEP]
        pre[k] = {"f0_range": round(float(np.mean([p["f0_range_st"] for p in pr])), 2),
                  "f0_std": round(float(np.mean([p["f0_std_st"] for p in pr])), 2),
                  "sim": round(float(np.mean([sim_json[f"{s}__REF-{k}"] for s in KEEP])), 4)}
    prb = [C.prosody(sim_in[f"{s}__BASE"], TEXT[s]) for s in KEEP]
    pre["neutral"] = {"f0_range": round(float(np.mean([p["f0_range_st"] for p in prb])), 2),
                      "f0_std": round(float(np.mean([p["f0_std_st"] for p in prb])), 2),
                      "sim": round(float(np.mean([sim_json[f"{s}__BASE"] for s in KEEP])), 4)}
    ok = {k: v for k, v in pre.items() if k != "neutral" and v["sim"] >= 0.85}
    best = max(ok, key=lambda k: ok[k]["f0_range"]) if ok else max((k for k in pre if k != "neutral"), key=lambda k: pre[k]["sim"])
    M["ref_pre"], M["ref_choice"] = pre, best
    Cc = make_seam(refrows[best], fix)

    # ------------------------------------------------------------ 輸出
    out = L.DEST
    os.makedirs(out, exist_ok=True)
    files = {"A_現行bot.wav": A, "B_建議.wav": B, "C_建議加表現力參考音.wav": Cc}
    texts = "".join(TEXT[s] for s in KEEP)
    if v25:
        files["V25_IndexTTS對照.wav"] = L.concat_current([rd(x["file"]) for s in KEEP for x in v25 if x["sid"] == s])[0]
    paths = {}
    for fn, y in files.items():
        p = os.path.join(out, fn)
        sf.write(p, norm(y), L.SR, subtype="PCM_16")
        paths[fn] = p

    # ------------------------------------------------------------ 指標
    sims = _sim(paths, "sim_final")
    asr_model = _asr_model()
    res = {}
    for fn, p in paths.items():
        pr = C.prosody(p, texts)
        txt = _asr(asr_model, p)
        res[fn] = {"sim_r1": sims[fn], "cer": round(C.cer(texts, txt), 4), "f0_range_st": pr["f0_range_st"], "f0_std_st": pr["f0_std_st"],
                   "dur_s": pr["dur_s"], "asr": txt}
    first_a = byname[f"{KEEP[0]}__SPLIT-OLD"]["first_s"]
    first_b = byname[f"{KEEP[0]}__BASE"]["first_s"]
    rtf = lambda rs: round(sum(r["total_s"] for r in rs) / sum(r["audio_s"] for r in rs), 3)  # noqa: E731
    res["A_現行bot.wav"].update({"first_s": first_a, "rtf": rtf([byname[f"{s}__SPLIT-OLD"] for s in KEEP])})
    res["B_建議.wav"].update({"first_s": first_b, "rtf": rtf([byname[f"{s}__BASE"] for s in KEEP])})
    if "C_建議加表現力參考音.wav" in res:
        res["C_建議加表現力參考音.wav"].update({"first_s": byname[f"{KEEP[0]}__REF-{best}"]["first_s"],
                                           "rtf": rtf([byname[f"{s}__REF-{best}"] for s in KEEP])})
    if v25:
        res["V25_IndexTTS對照.wav"].update({"first_s": None, "rtf": round(float(np.mean([x["rtf"] for x in v25 if x["sid"] in KEEP])), 3)})
    M["abc"] = res

    # 括號：ASR + 停頓
    brk = []
    for sid in L.BRACKET_SIDS:
        for off in L.BRACKET_SEEDS:
            for tag in ("with", "strip"):
                r = byname[f"{sid}__BRK-{tag}-seed{off}"]
                y = rd(r["file"])
                p = os.path.join(TMP, f"{sid}_{tag}_{off}.wav")
                sf.write(p, norm(y), L.SR, subtype="PCM_16")
                txt = _asr(asr_model, p)
                brk.append({"sid": sid, "tag": tag, "off": off, "audio_s": r["audio_s"], "asr": txt,
                            "cer": round(C.cer(r["text"], txt), 4), "pauses": L.pauses(p)})
    M["brk"] = brk
    C.jdump(os.path.join(L.OUT, "final_metrics.json"), M)
    _readme(M, v25)
    print(json.dumps({"fix": fix, "ref": best, "abc": {k: {kk: vv for kk, vv in v.items() if kk != "asr"} for k, v in res.items()}},
                     ensure_ascii=False, indent=1))


def _sim(items, name):
    lst = os.path.join(TMP, name + "_list.json")
    outp = os.path.join(TMP, name + ".json")
    json.dump(items, open(lst, "w", encoding="utf-8"), ensure_ascii=False)
    subprocess.run([VENV_VOX, os.path.join(HERE, "emotion_sim.py"), lst, outp], check=True, env={**os.environ, "PYTHONUTF8": "1"},
                   stdout=subprocess.DEVNULL)
    return json.load(open(outp, encoding="utf-8"))


def _asr_model():
    import es_asr
    from faster_whisper import WhisperModel
    return WhisperModel(es_asr.MODEL, device="cpu", compute_type="int8", local_files_only=True, cpu_threads=8)


def _asr(m, path):
    import librosa
    y = librosa.load(path, sr=16000, mono=True)[0]
    segs, _ = m.transcribe(y, language="zh", beam_size=5, temperature=[0.0, 0.2])
    return "".join(s.text for s in segs).strip()


def _med(xs):
    return round(float(np.median(xs)), 2)


def _readme(M, v25):
    T = M["tail"]
    sm = M["seam"]
    r = M["abc"]
    a, b, c = r["A_現行bot.wav"], r["B_建議.wav"], r.get("C_建議加表現力參考音.wav")
    cur, fo, bt = sm["current(淡出2ms+15ms靜音)"], sm["fadeout20"], sm["backend-tail40"]
    brk = M["brk"]
    lp = lambda tag: _med([max([p[1] for p in x["pauses"]] or [0]) for x in brk if x["tag"] == tag])  # noqa: E731
    same = sum(1 for x in brk if x["tag"] == "with" and x["asr"] and x["cer"] <= 0.1)
    L1 = (f"A = 舊切法（首段≤10字/其餘約20字逐段合成）+ 現行接縫（淡出實際 2ms + 15ms 靜音）+ neutral ref120；B = 整段合成 + {M['seam_choice']['fix']} + neutral；"
          f"C = B 換參考音 {M['ref_choice']}（x1.2，F0 範圍 {M['ref_pre'][M['ref_choice']]['f0_range']} vs neutral {M['ref_pre']['neutral']['f0_range']} st，原檔來源見 outputs/prosody_ab/refs）。")
    fm = lambda d: f"SIM {d['sim_r1']}｜CER {d['cer']}｜F0範圍 {d['f0_range_st']}st｜std {d['f0_std_st']}st｜首包 {d.get('first_s')}s｜RTF {d.get('rtf')}"  # noqa: E731
    L2 = f"指標：A {fm(a)}；B {fm(b)}" + (f"；C {fm(c)}" if c else "") + (f"；V25 {fm(r['V25_IndexTTS對照.wav'])}" if v25 else "；V25 對照未產生（時限）") + "。"
    L3 = (f"接縫（6 句、5 個接縫）：串流 vs 非串流長度差中位 {_med([x['diff_ms_stream_minus_non'] for x in T])}ms、串流結尾最後 20ms 相對語音 {_med([x['tail_stream']['rms_last20_rel_db'] for x in T])}dB "
          f"（非串流 {_med([x['tail_non']['rms_last20_rel_db'] for x in T])}dB）、衰減長度 {_med([x['tail_stream']['decay_ms'] or 0 for x in T])}ms；"
          f"句尾 cut click（最大樣本差/p99）現行 {cur['end_cut']['max_step_over_p99_mean']}、淡出20ms {fo['end_cut']['max_step_over_p99_mean']}、補尾40ms {bt['end_cut']['max_step_over_p99_mean']}，"
          f"句首 {cur['start']['max_step_over_p99_mean']}/{fo['start']['max_step_over_p99_mean']}/{bt['start']['max_step_over_p99_mean']}。完整數據在 outputs/prosody_ab/final_metrics.json。")
    L4 = (f"括號（s2「」、s4『』，各3 seed，CPU ASR＋停頓）：含括號與移除括號的平均最長句內停頓 {lp('with')}ms / {lp('strip')}ms，ASR 轉寫與原文接近者 {same}/6；"
          f"未人工聆聽，只能排除明顯的轉寫異常。")
    L5 = "以上全部尚未人工聆聽；指標為代理值（F0 用 pyin、SIM 用 resemblyzer、ASR 為 CPU int8），以耳聽為準。"
    with open(os.path.join(L.DEST, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n\n".join([L1, L2, L3, L4, L5]) + "\n")


if __name__ == "__main__":
    main()
