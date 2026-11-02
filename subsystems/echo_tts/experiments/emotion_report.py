"""情緒聆聽實驗步驟 3-5：評分（ASR / 韻律 / SIM 清單）、彙整指標、輸出聆聽包與 README。

階段（共用環境，PYTHONUTF8=1）：
  asr      ES ASR 轉寫 V25 + VOX 全部句子檔，算 CER（用 GPU，跑完即釋放）
  prosody  F0 / 能量 / 語速代理指標（純 CPU）
  simlist  寫出需要算 SIM 的檔案清單（交給 emotion_sim.py，.venv-voxcpm CPU）
  pack     彙整 metrics.json，輸出聆聽包到 E:/Materials/Voices/UEP/2026-11-01_emotion_check 與 README
"""
import argparse
import os
import shutil
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emotion_common as C  # noqa: E402

ASR_SENT = os.path.join(C.OUT, "asr_sentences.json")
PROS_JSON = os.path.join(C.OUT, "prosody.json")
SIM_LIST = os.path.join(C.OUT, "sim_list.json")
SIM_JSON = os.path.join(C.OUT, "sim_sentences.json")
V_ORDER = ["V25", "VOX-NEUTRAL", "A-065", "B-057", "B-065", "B-073", "B-065-L085"]


def all_sentence_files():
    """回傳 {(sid, version): (相對 OUT 的路徑, 文字)}。"""
    out = {}
    for sid, emo, idx, text, seed in C.sentence_items():
        out[(sid, "V25")] = (f"v25/{sid}__V25.wav", text)
    for r in C.jload(C.VOX_JSON)["rows"]:
        out[(r["sid"], r["version"])] = (r["file"], r["text"])
    return out


def cmd_asr(a):
    import es_asr
    out = {}
    for (sid, v), (rel, text) in all_sentence_files().items():
        hyp = es_asr.transcribe(os.path.join(C.OUT, rel))
        out[rel] = {"asr": hyp, "cer": round(C.cer(text, hyp), 4)}
    C.jdump(ASR_SENT, out)
    print("asr done", len(out), flush=True)


def cmd_prosody(a):
    out = {}
    for (sid, v), (rel, text) in all_sentence_files().items():
        out[rel] = C.prosody(os.path.join(C.OUT, rel), text)
        print(rel, out[rel].get("f0_range_st"), flush=True)
    clips = C.jload(C.REFS_JSON)["clips"]
    for emo, d in clips.items():
        for key, c in d.items():
            out["refs/" + c["file"]] = C.prosody(os.path.join(C.REFS, c["file"]), c["text"])
    C.jdump(PROS_JSON, out)


def cmd_simlist(a):
    d = {rel: os.path.join(C.OUT, rel) for (_, _), (rel, _) in all_sentence_files().items()}
    C.jdump(SIM_LIST, d)
    print("simlist", len(d))


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return float(np.mean(xs)) if xs else float("nan")


def cmd_pack(a):
    files = all_sentence_files()
    asr, pros, sim = C.jload(ASR_SENT), C.jload(PROS_JSON), C.jload(SIM_JSON)
    vox = {(r["sid"], r["version"]): r for r in C.jload(C.VOX_JSON)["rows"]}
    refs = C.jload(C.REFS_JSON)
    clips = refs["clips"]
    metrics = {}
    for (sid, v), (rel, text) in files.items():
        m = {"sid": sid, "version": v, "file": rel, "text": text, "sim_r1": sim[rel], **asr[rel], **pros[rel]}
        if (sid, v) in vox:
            r = vox[(sid, v)]
            m.update({k: r[k] for k in ("first_s", "total_s", "rtf", "prompt_feat_len", "ref_feat_len")})
        metrics[f"{sid}__{v}"] = m
    C.jdump(C.METRICS_JSON, metrics)

    emo_of = {sid: emo for sid, emo, _, _, _ in C.sentence_items()}
    sids = [s for s, *_ in C.sentence_items()]

    # ---- 聆聽包檔案
    if os.path.isdir(C.PACK):
        shutil.rmtree(C.PACK)
    for emo in C.EMOTIONS:
        os.makedirs(os.path.join(C.PACK, emo), exist_ok=True)
    for (sid, v), (rel, _) in files.items():
        shutil.copy2(os.path.join(C.OUT, rel), os.path.join(C.PACK, emo_of[sid], f"{sid}__{v}.wav"))
    cd = os.path.join(C.PACK, "_emotion_clips")
    os.makedirs(cd, exist_ok=True)
    for emo in C.EMOTIONS:
        for key in C.STRENGTHS:
            shutil.copy2(os.path.join(C.REFS, clips[emo][key]["file"]), os.path.join(cd, f"{emo}_{key}.wav"))
    shutil.copy2(C.R1_PATH, os.path.join(cd, "_R1.wav"))
    shutil.copy2(C.NEUTRAL_REF, os.path.join(cd, "_neutral_ref120.wav"))

    # ---- README
    L = []
    w = L.append
    w("# VOX 情緒表現聆聽包（2026-11-01）\n")
    w("本包只列實驗數據，不含結論；是否可用請以人耳判斷。檔名 `<句>__<版本>.wav`，依情緒分資料夾；"
      "`_emotion_clips/` 為情緒參考片段（含 R1 與 ref120）。\n")
    w("**未經人工試聽**：所有指標皆為機器代理（SIM、ASR CER、F0／能量／語速），無人耳評分。\n")
    w("## 版本定義\n")
    w("| 版本 | 內容 |\n|---|---|")
    w("| V25 | IndexTTS 2.5（uep_v25 + LoRA 0.85）直接用情緒向量，strength 0.65（情緒預設配方縮放到向量總和 0.65） |")
    w("| VOX-NEUTRAL | 正式後端 zh 設定：ref = prompt = `zh_neutral_x1.2.wav`（ref120）+ 逐字稿，LoRA step_0000600 開（scale 1.0）、台灣讀音前處理 |")
    w("| A-065 | ULT-EMO：ref 與 prompt 都用情緒片段（strength 0.65）+ 該片段逐字稿 |")
    w("| B-057 / B-065 / B-073 | COMBO：ref = ref120（鎖音色），prompt = 情緒片段（strength 0.57／0.65／0.73）+ 逐字稿，走 voxcpm2 的 ref+continuation 組合模式 |")
    w("| B-065-L085 | 同 B-065，僅 LoRA scaling 改 0.85（用 `LoRALinear.scaling` buffer 調整） |")
    w("\n註：**B-065 的 LoRA scale 就是正式後端的 1.0**（alpha/r = 32/32），所以沒有另做 `B-065-L100`；LoRA 對照是 B-065（1.0）對 B-065-L085（0.85）。\n")
    w("共同設定：cfg 2.0、inference_timesteps 10、normalize 不使用、streaming、不重試、`optimize=True`；同一句各版本使用同一個 seed。\n")
    w("## 強度對應\n")
    w("ES user 刻度 0／0.5／1 對應 raw（IndexTTS 情緒向量總和）0.57／0.65／0.73。VOX 沒有情緒向量，強度以「不同 strength 生成的情緒參考片段」表達，"
      "只使用 0.57、0.65、0.73 三檔。\n")
    w("## 情緒片段來源與逐字稿\n")
    w("來源：IndexTTS 2.5（uep_v25 角色 + LoRA 0.85，中文，`use_accel=False`），每個情緒 x 強度生成 2 個 take（seed 1001／1002），"
      "挑選規則：長度 8–12 秒內優先 → ES ASR CER（差 0.03 內視為同級）→ SIM（resemblyzer 對 R1）最高。\n")
    w("**偏離說明**：未沿用既有 `v25_emotion_refs` 的 0.65 片段（約 10–19 秒）再裁切，而是以縮短過的文本、同 seed 重新生成 0.57／0.65／0.73 三檔，"
      "讓三個強度使用完全相同的文本與裁切規則；片段是整段生成，句尾完整，不是音訊裁切。\n")
    w("| 情緒 | 強度 | take | 秒數 | CER | SIM | 向量總和 | 長度合格 |\n|---|---|---|---|---|---|---|---|")
    for emo in C.EMOTIONS:
        for key in C.STRENGTHS:
            c = clips[emo][key]
            w(f"| {emo} | {c['strength']} | t{c['take']} | {c['audio_s']} | {c['cer']:.3f} | {c['sim_r1']:.3f} | {c['vector_sum']} | {'是' if c['in_range'] else '**否**'} |")
    w("\n逐字稿（三個強度、兩個 take 相同）：\n")
    for emo in C.EMOTIONS:
        w(f"- **{emo}**：{C.CLIP_TEXTS[emo]}")
    w("\n## 測試句\n")
    for sid, emo, idx, text, seed in C.sentence_items():
        w(f"- `{sid}`（seed {seed}）：{text}")

    # ---- 指標表
    def agg(v, key, sel=None):
        return _mean([metrics[f"{s}__{v}"].get(key) for s in sids if (sel is None or sel(s)) and f"{s}__{v}" in metrics])

    w("\n## 指標：各版本平均（16 句）\n")
    w("| 版本 | SIM(R1) | CER | F0 均值 Hz | F0 範圍 半音(p5-p95) | F0 std 半音 | 能量 dB(活動段均值) | 能量 std dB | 語速 字/秒 | 時長 s | 首包 s | RTF |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for v in V_ORDER:
        fmt = lambda k, d=2: ("-" if np.isnan(agg(v, k)) else f"{agg(v, k):.{d}f}")  # noqa: E731
        w(f"| {v} | {fmt('sim_r1', 3)} | {fmt('cer', 3)} | {fmt('f0_mean_hz', 1)} | {fmt('f0_range_st')} | {fmt('f0_std_st')} | "
          f"{fmt('rms_db_mean', 1)} | {fmt('rms_db_std', 1)} | {fmt('rate_cps')} | {fmt('dur_s')} | {fmt('first_s', 3)} | {fmt('rtf', 3)} |")
    w("\n注意：VOX-NEUTRAL 的 ref120 為 1.2 倍速參考音，B／A 的情緒片段為原速，語速與 RTF 的差異有一部分來自這個差異，不全是情緒造成。"
      "V25 沒有首包與 RTF（非串流量測）。F0 由 librosa pyin（130–700 Hz，已排除貼邊界的誤判幀與八度離群幀）估計，僅為粗略代理，不同句子間的範圍差異不一定等於聽感差異。\n")

    w("## 指標：各情緒的 F0 範圍（半音）與 F0 均值（Hz），每格為該情緒 2 句平均\n")
    w("| 情緒 | " + " | ".join(V_ORDER) + " |\n|---|" + "---|" * len(V_ORDER))
    for emo in C.EMOTIONS:
        sel = lambda s, e=emo: emo_of[s] == e  # noqa: E731
        cells = [f"{agg(v, 'f0_range_st', sel):.1f} st / {agg(v, 'f0_mean_hz', sel):.0f} Hz" for v in V_ORDER]
        w(f"| {emo} | " + " | ".join(cells) + " |")
    w("\n## 指標：各情緒的能量均值（dB）與語速（字/秒），每格為該情緒 2 句平均\n")
    w("| 情緒 | " + " | ".join(V_ORDER) + " |\n|---|" + "---|" * len(V_ORDER))
    for emo in C.EMOTIONS:
        sel = lambda s, e=emo: emo_of[s] == e  # noqa: E731
        cells = [f"{agg(v, 'rms_db_mean', sel):.1f} dB / {agg(v, 'rate_cps', sel):.2f}" for v in V_ORDER]
        w(f"| {emo} | " + " | ".join(cells) + " |")
    w("\n## 指標：各情緒 SIM(R1) ／ CER，每格為該情緒 2 句平均\n")
    w("| 情緒 | " + " | ".join(V_ORDER) + " |\n|---|" + "---|" * len(V_ORDER))
    for emo in C.EMOTIONS:
        sel = lambda s, e=emo: emo_of[s] == e  # noqa: E731
        cells = [f"{agg(v, 'sim_r1', sel):.3f} / {agg(v, 'cer', sel):.3f}" for v in V_ORDER]
        w(f"| {emo} | " + " | ".join(cells) + " |")

    # ---- 延遲
    w("\n## 延遲：B 相對 VOX-NEUTRAL 的增量（compile 下，streaming，同句同 seed，16 句成對平均）\n")
    w("首包 = 呼叫起算（含台灣讀音前處理）到第一個 chunk；RTF = 總生成時間 / 音訊秒數。")
    w("\n| 版本 | 首包 s | Δ首包 s | RTF | ΔRTF | 總生成 s | prompt 長度(feat 數) | ref 長度(feat 數) |\n|---|---|---|---|---|---|---|---|")
    base_first = {s: metrics[f"{s}__VOX-NEUTRAL"]["first_s"] for s in sids}
    base_rtf = {s: metrics[f"{s}__VOX-NEUTRAL"]["rtf"] for s in sids}
    for v in ["VOX-NEUTRAL", "A-065", "B-057", "B-065", "B-073", "B-065-L085"]:
        df = _mean([metrics[f"{s}__{v}"]["first_s"] - base_first[s] for s in sids])
        dr = _mean([metrics[f"{s}__{v}"]["rtf"] - base_rtf[s] for s in sids])
        w(f"| {v} | {agg(v, 'first_s'):.3f} | {df:+.3f} | {agg(v, 'rtf'):.3f} | {dr:+.3f} | {agg(v, 'total_s'):.2f} | "
          f"{agg(v, 'prompt_feat_len'):.0f} | {agg(v, 'ref_feat_len'):.0f} |")
    w("\n同一個 (情緒, 版本) 的第 1 句與第 2 句首包／RTF 平均（檢查是否有 recompile 暖機偏差）：\n")
    w("| 版本 | 第 1 句 首包 / RTF | 第 2 句 首包 / RTF |\n|---|---|---|")
    for v in ["VOX-NEUTRAL", "A-065", "B-057", "B-065", "B-073", "B-065-L085"]:
        s1 = [s for s in sids if s.endswith("1")]
        s2 = [s for s in sids if s.endswith("2")]
        w(f"| {v} | {_mean([metrics[f'{s}__{v}']['first_s'] for s in s1]):.3f} / {_mean([metrics[f'{s}__{v}']['rtf'] for s in s1]):.3f} | "
          f"{_mean([metrics[f'{s}__{v}']['first_s'] for s in s2]):.3f} / {_mean([metrics[f'{s}__{v}']['rtf'] for s in s2]):.3f} |")

    # ---- 建議優先耳聽
    w("\n## 建議優先耳聽（依檔案類型，非結論）\n")
    w("1. 每種情緒的第 1 句，四版對照：`<情緒>1__V25`、`__VOX-NEUTRAL`、`__B-065`、`__A-065`。")
    w("2. 強度階梯：`<情緒>1__B-057`／`B-065`／`B-073`，建議先聽 happy、angry、sad、afraid（原 IndexTTS 向量差異較明顯的情緒）。")
    w("3. LoRA 影響：`<情緒>1__B-065` 對 `__B-065-L085`。")
    flagged = sorted(metrics.values(), key=lambda m: (-m["cer"], m["sim_r1"]))[:8]
    w("4. 機器指標顯示可能有問題、值得確認的檔案（CER 高／SIM 低的前 8 名）：")
    for m in flagged:
        w(f"   - `{emo_of[m['sid']]}/{m['sid']}__{m['version']}.wav`：CER {m['cer']:.3f}、SIM {m['sim_r1']:.3f}")
    w("\n## 逐檔指標\n")
    w("| 檔案 | SIM | CER | F0 均值 | F0 範圍 st | 能量 dB | 語速 | 時長 | 首包 | RTF |\n|---|---|---|---|---|---|---|---|---|---|")
    for s in sids:
        for v in V_ORDER:
            m = metrics[f"{s}__{v}"]
            g = lambda k, d=2: ("-" if m.get(k) is None else f"{m[k]:.{d}f}")  # noqa: E731
            w(f"| {emo_of[s]}/{s}__{v} | {m['sim_r1']:.3f} | {m['cer']:.3f} | {g('f0_mean_hz', 1)} | {g('f0_range_st')} | "
              f"{g('rms_db_mean', 1)} | {g('rate_cps')} | {g('dur_s')} | {g('first_s', 3)} | {g('rtf', 3)} |")
    with open(os.path.join(C.PACK, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("pack done", C.PACK)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["asr", "prosody", "simlist", "pack"])
    a = ap.parse_args()
    {"asr": cmd_asr, "prosody": cmd_prosody, "simlist": cmd_simlist, "pack": cmd_pack}[a.stage](a)
