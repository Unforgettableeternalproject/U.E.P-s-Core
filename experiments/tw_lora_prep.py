"""步驟 2 資料整理：Common Voice zh-TW（OKHand 清洗版，CC0）-> VoxCPM2 LoRA manifest。

- 多語者：每位語者最多 PER_SPK 句，round-robin 取樣，總長取到 TARGET_S 秒
- 驗證集：另外留 VAL_SPK 位語者整個不進訓練
- ref_audio：REF_RATIO 的樣本，ref_audio 取「同一語者、不同句」的另一筆（官方要求同語者）
- 句子過濾：只留純中文字（含常用標點），排除數字與拉丁字母（避免逐字稿與讀音不一致）
- 輸出在 outputs/tw_lora_data/（gitignore）。Common Voice 條款：不重新散布、不識別語者；
  client_id 在 manifest 內不出現，語者只以匿名編號 spkNNN 表示。

使用 .venv-voxcpm：
  python experiments/tw_lora_prep.py
"""
import json
import os
import random
import re

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "outputs", "tw_lora_data")
RAW = os.path.join(BASE, "raw", "data")
WAV = os.path.join(BASE, "wav")
SEED = 1031
TARGET_S = 9000.0       # 訓練總長約 2.5 小時
PER_SPK = 36
VAL_SPK = 8
VAL_PER_SPK = 12
REF_RATIO = 0.4
MIN_DUR, MAX_DUR = 2.5, 15.0
MIN_CHARS = 8
MIN_MOS = 2.5
OK_TEXT = re.compile(r"^[一-鿿，。、？！；：「」『』（）…—\s]+$")


def main():
    rng = random.Random(SEED)
    files = sorted(f for f in os.listdir(RAW) if f.endswith(".parquet"))
    metas = []
    for f in files:
        pf = pq.ParquetFile(os.path.join(RAW, f))
        for rg in range(pf.metadata.num_row_groups):
            t = pf.read_row_group(rg, columns=["client_id", "gender", "file_name", "sentence", "predicted_mos"]).to_pylist()
            for i, r in enumerate(t):
                r.update(file=f, rg=rg)
                metas.append(r)
    print("total", len(metas))
    metas = [m for m in metas if m["predicted_mos"] >= MIN_MOS and len(m["sentence"].strip()) >= MIN_CHARS and OK_TEXT.match(m["sentence"].strip())]
    print("text ok", len(metas))
    by = {}
    for m in metas:
        by.setdefault(m["client_id"], []).append(m)
    for v in by.values():
        v.sort(key=lambda m: -m["predicted_mos"])
    spks = [k for k, v in by.items() if len(v) >= 8]
    rng.shuffle(spks)
    print("speakers >=8 clips", len(spks))
    val_spk, tr_spk = spks[:VAL_SPK], spks[VAL_SPK:]
    sid = {k: f"spk{i:03d}" for i, k in enumerate(spks)}

    # 訓練 round-robin
    order = []
    for r in range(PER_SPK):
        for k in tr_spk:
            if r < len(by[k]):
                order.append(by[k][r])
    order = order[:int(TARGET_S / 3.0 * 1.8)]  # 以平均約 3 秒估，多取 30%，之後依真實時長截斷
    val = [m for k in val_spk for m in by[k][:VAL_PER_SPK]]
    want = {(m["file"], m["rg"], m["file_name"]): m for m in order + val}

    os.makedirs(WAV, exist_ok=True)
    got = {}
    for f in files:
        pf = pq.ParquetFile(os.path.join(RAW, f))
        for rg in range(pf.metadata.num_row_groups):
            if not any(k[0] == f and k[1] == rg for k in want):
                continue
            t = pf.read_row_group(rg, columns=["file_name", "audio"]).to_pylist()
            for r in t:
                key = (f, rg, r["file_name"])
                if key in want:
                    p = os.path.join(WAV, r["file_name"])
                    with open(p, "wb") as fh:
                        fh.write(r["audio"]["bytes"])
                    info = sf.info(p)
                    got[key] = info.frames / info.samplerate
    print("extracted", len(got))

    def build(items, budget=None):
        rows, tot = [], 0.0
        for m in items:
            key = (m["file"], m["rg"], m["file_name"])
            d = got.get(key)
            if d is None or not (MIN_DUR <= d <= MAX_DUR):
                continue
            if budget and tot + d > budget:
                break
            tot += d
            rows.append({"audio": os.path.join(WAV, m["file_name"]).replace("\\", "/"), "text": m["sentence"].strip(),
                         "duration": round(d, 3), "spk": sid[m["client_id"]], "gender": m["gender"], "mos": m["predicted_mos"]})
        return rows, tot

    train, tt = build(order, TARGET_S)
    valr, vt = build(val)

    def add_ref(rows):
        by_spk = {}
        for r in rows:
            by_spk.setdefault(r["spk"], []).append(r)
        for r in rows:
            if rng.random() < REF_RATIO:
                cands = [c for c in by_spk[r["spk"]] if c is not r and 3.0 <= c["duration"] <= 10.0]
                if cands:
                    r["ref_audio"] = rng.choice(cands)["audio"]

    add_ref(train)
    add_ref(valr)
    for name, rows in (("train", train), ("val", valr)):
        with open(os.path.join(BASE, f"{name}.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                o = {k: r[k] for k in ("audio", "text", "duration") if k in r}
                if "ref_audio" in r:
                    o["ref_audio"] = r["ref_audio"]
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
    stat = {
        "train_clips": len(train), "train_hours": round(tt / 3600, 2), "train_speakers": len({r["spk"] for r in train}),
        "train_gender": {g: sum(r["gender"] == g for r in train) for g in {r["gender"] for r in train}},
        "train_with_ref": sum("ref_audio" in r for r in train),
        "train_dur_mean": round(float(np.mean([r["duration"] for r in train])), 2),
        "val_clips": len(valr), "val_hours": round(vt / 3600, 3), "val_speakers": len({r["spk"] for r in valr}),
        "mos_mean": round(float(np.mean([r["mos"] for r in train])), 3),
    }
    json.dump(stat, open(os.path.join(BASE, "stats.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(stat, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
