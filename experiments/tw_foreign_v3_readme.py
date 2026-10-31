"""外語 v3 README 產生器（讀 score.json / prompts.json / cand_report.json / compile_smoke.json），寫 DEST/README.md。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402

import tw_foreign_v3_lib as L  # noqa: E402


def J(n):
    return json.load(open(os.path.join(L.W, n), encoding="utf-8"))


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(float(np.median(xs)), 3) if xs else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(float(np.mean(xs)), 3) if xs else None


def agg(score, lang):
    rows = {}
    for key, r in score.items():
        k, v = key.split("__")
        if not k.startswith(lang):
            continue
        rows.setdefault(v, []).append(r)
    return rows


def metric_table(score, lang):
    rows = agg(score, lang)
    h = "| 版本 | 檔數 | SIM(R1) | CER ES | CER whisper |" + (" CER ES 假名 | CER wh 假名 |" if lang == "ja" else "") + " RTF 中位 | 首包 s 中位 |"
    L_ = [h, "|" + "---|" * (h.count("|") - 1)]
    order = sorted(rows, key=lambda v: (not v.startswith("V25"), v))
    for v in order:
        rs = rows[v]
        cells = [v, str(len(rs)), str(mean([r["sim"] for r in rs])), str(mean([r["cer_es"] for r in rs])), str(mean([r["cer_wh"] for r in rs]))]
        if lang == "ja":
            cells += [str(mean([r.get("cer_es_kana") for r in rs])), str(mean([r.get("cer_wh_kana") for r in rs]))]
        if v.startswith("V25"):
            cells += [f"{med([r.get('rtf_eager') for r in rs])}（未 compile）", "未量"]
        else:
            cells += [str(med([r.get("rtf") for r in rs])), str(med([r.get("first_s") for r in rs]))]
        L_.append("| " + " | ".join(cells) + " |")
    return L_


def main():
    from japanese_kana import JapaneseKanaNormalizer
    kana = JapaneseKanaNormalizer()
    score, prompts, cand = J("score.json"), J(os.path.join("prompts", "prompts.json")), J("cand_report.json")
    smoke = J("compile_smoke.json")
    gen_meta = J("gen_meta.json")
    t = []
    t.append("# 外語 v3（日文參考音候選、英文改善）2026-11-01\n")
    t.append("""決策前提（艾斯維爾已定案）：單一 VoxCPM2 實例；中文 TWLORA-TWN + ref120；英日 LoRA 關閉（掛載 step_0000600 後 `set_lora_enabled(False)`）、不經中文前處理；**所有生成都在 torch.compile（optimize=True）下**。
**所有音檔都沒有人工聽過**；ASR、SIM、韻律數字只是輔助，最終版本由艾斯維爾耳聽選。

## 條件

- 44.1kHz、-23 LUFS；**修靜音 top_db=30**（v2 為 40）。檔案平鋪：`en/`、`ja/` 各一層，檔名 `<句>__<版本>.wav`。
- Vox：cfg 2.0、steps 10、normalize=False、seed = 1031+70+句序（各版本同 seed）。
- RTF = 生成時間 / 音訊長度；首包延遲 = 從呼叫 `generate_streaming` 到第一個 chunk（含 prompt 編碼），皆在 compile 暖機後量（每個模式先跑短中長三句），RTX 5070 Ti，單一 GPU 工作。
- SIM = resemblyzer 對 R1（既有算法）。CER：ES ASR（Breeze2.5）與 whisper medium 並列，**對原始句（漢字）計算**；日文另附「雙方都假名化後」的 CER（排除漢字／平假名書寫差異）。
- V25 對照：`IndexTTS2Backend`（uep_v25 角色向量，即 R1 衍生）、不載 LoRA、無情緒向量，**6 句全部重生**（與 v2 不同：v2 的 ja2/ja3/en2/en3 用的是帶情緒向量的舊檔）。`V25-EMO` 為 en2/en3/ja2/ja3 另帶與 v2 相同情緒向量（happy 0.5 / sad 0.5 / happy 0.65 / sad 0.65）的版本，僅供參考。V25 的 RTF 為 eager 非 compile，且沒有量首包延遲。
- 與 v2 的差異：句子擴充為 6 句（新增 en4–6、ja4–6）；seed 依新句序重排（ja 句序號變了，所以 ja1–3 與 v2 的 seed 不同，不能逐檔對照 v2）。

## 版本定義

- `V25`：現行 IndexTTS 2.5（見上）。
- `REF-<P>`：只給參考音 P（無逐字稿）；`ULT-<P>`：參考音與 prompt 音檔同一支 P，加 P 的逐字稿（ultimate cloning）。P 見下方 prompt 表。
- `REF-R1`（英文）：未處理的 R1 原檔，v2 對照。
- `ULT-NEUTRAL`（日文）：假名化文字 + 中文 neutral.wav ULT，v2 表現最好的 KANA_ULT-NEUTRAL 路徑，對照用。
- 日文所有版本：句子與 ULT 逐字稿都先過 JapaneseKanaNormalizer。
- prompt 命名：英文 `R1c`/`R2c`（v2 乾淨片段，top_db=30 重修）、`R1n`/`R2n`（新挑的平穩段）、`EV25a/b`（V25 合成的中性英文）、`EVOXa/b`（Vox 自舉）；`...DN` 為降噪版。日文 `JV25a/b`（(a) V25 合成）、`JVOXa/b`（(b) Vox 自舉）、`R4c`（(c) ITA 原聲）。
- prompt 處理：top_db=30 修靜音、70Hz 高通（4 階）、20ms 淡入；DN = 另加輕度頻譜閘（以各頻帶最安靜 10% 幀估噪聲底，低於 2 倍噪聲底的 bin 衰減 12dB），只對 R1c、R2n、EV25a 做前後比較。

## 送進模型的實際文字
""")
    t.append("### 英文（原樣送入）\n")
    for k, s in L.SENT.items():
        if k.startswith("en"):
            t.append(f"- {k}：{s}")
    t.append("\n### 日文（原文 → 假名化後送入）\n")
    t.append("| 句 | 原文 → 送入文字 | 漢字詞讀音（人工請審多音字） |\n|---|---|---|")
    for k, s in L.SENT.items():
        if k.startswith("ja"):
            o, log = kana.explain(s)
            t.append(f"| {k} | {s} → `{o}` | " + "、".join(f"{a}→{b}" for a, b in log) + " |")
    t.append("\nja4–ja6 為新增，涵蓋多音漢字（生物／生き物／人気／辺り／大人／何度／一日中／夜／風 等）與長句（ja6 兩句共約 90 字）。讀音由 pyopenjtalk 字典給出，已對照上表人工看過一次；未逐一聽實際發音。")

    t.append("\n## 參考音（prompt）來源、逐字稿與韻律／底噪\n")
    t.append("逐字稿：R1n/R2n 為 whisper medium 詞級時間戳切出的句界視窗轉寫，R1c/R2c/R4c 沿用 v2（whisper + ES 交叉確認），合成類 prompt 的逐字稿就是當時送進合成的文字（日文為假名化後文字），並經 ES 與 whisper 兩者轉寫核對（見下方候選驗證）。皆非人工校對。")
    t.append("\n韻律欄是 F0 標準差（半音）／RMS 起伏（dB）的**粗略代理**（pyin，可能有八度誤判），不是情緒量測；越小代表越平穩。R1 整段第一句（to rescue someone…）F0 範圍達 21.8 半音，R2 後半（What? You prefer waffles… Um…）達 23.6 半音，是原 prompt 情緒偏強的可疑來源；平穩的 R1n/R2n 約 9–10 半音以下。")
    t.append("\n| prompt | 語言 | 來源 | 長度 s | F0 均值 Hz | F0 std (st) | RMS std (dB) | 開頭 0.3s <80Hz 能量 dB（處理前→後） | 噪聲底 dB（處理前→後） | 逐字稿 |\n|---|---|---|---|---|---|---|---|---|---|")
    for n, p in prompts.items():
        hb, ha = p["head_low_before"], p["head_low_after"]
        t.append(f"| {n} | {p['lang']} | {p['desc']}（{p['src']}{'' if not p['seg'] else ' ' + str([round(x, 2) for x in p['seg']])}） | {p['dur_s']} | {p['f0_mean']} | {p['f0_std_st']} | {p['rms_std_db']} | "
                 f"{hb['head_low_db']} → {ha['head_low_db']} | {p['noise_floor_before_db']} → {p['noise_floor_after_db']} | {p['text']} |")
    t.append("\n參考：R1 整檔開頭 0.3s 的 <80Hz 能量為 -42.5 dB（比其他片段高 20 dB 以上），代表 R1 本身開頭就帶低頻底噪，REF 模式會把它複製進輸出；R1c/R1n 從 3 秒後切，已避開。降噪（DN）前後噪聲底只差 1–2 dB（prompt 本來就很乾淨），差異極小，是否值得用請耳聽判斷。")
    t.append("\n### Prompt 候選的自動驗證（合成類，兩個 ASR 轉寫核對；kana 欄為雙方假名化後 CER）\n")
    t.append("| 候選 | 長度 s | CER ES | CER wh | kana ES/wh | SIM | F0 std | 是否選用 |\n|---|---|---|---|---|---|---|---|")
    used = {"V25_en4s0": "EV25a", "V25_en3s0": "EV25b", "VOX_en1s0": "EVOXa", "VOX_en2s0": "EVOXb",
            "V25_ja3s0": "JV25a", "V25_ja2s1": "JV25b", "VOX_ja3s0": "JVOXa", "VOX_ja2s1": "JVOXb"}
    for n, c in cand.items():
        t.append(f"| {n} | {c['trim_s']} | {c['cer_es']} | {c['cer_wh']} | {c.get('cer_es_kana', '')}/{c.get('cer_wh_kana', '')} | {c['sim']} | {c['f0_std_st']} | {used.get(n, '')} |")
    t.append("\n選用規則：修靜音後 6–10 秒、兩個 ASR 轉寫與文字吻合（日文以假名化後 CER 為 0）、句尾完整，其餘依 SIM 高與韻律平穩（F0 std／範圍小）取前面者；V25 的 ja 候選中有不少 F0 範圍 21 半音以上（較起伏），已避開。")

    for lang, title in (("en", "英文"), ("ja", "日文")):
        t.append(f"\n## 指標：{title}（6 句平均）\n")
        t += metric_table(score, lang)
    t.append("\n註：同一版本的 6 句平均，SIM 是 resemblyzer 對 R1，**對 R1 越高不代表越好聽**（R1 本身是英文錄音、V25 又從 R1 衍生，V25 的 SIM 天然偏高）。")
    t.append("\n## torch.compile 煙霧測試（切換 LoRA 與重編譯）\n")
    t.append("`optimize=True` + 載入 LoRA(step_0000600) 後，中文（LoRA 開）與英文（LoRA 關）交替各 3 輪：輸出皆為有限值，同模式同 seed 的音訊長度完全一致（8.80s / 7.68s），暖機後 RTF 約 0.45–0.54、首包約 0.18–0.22s；只有第一個呼叫較慢（1.87s 首包，編譯），之後切換 LoRA 沒有再觸發重編譯。結果檔：`outputs/foreign_v3/compile_smoke.json`。")
    t.append("\n## 建議優先耳聽\n")
    t.append("""日文（先聽之前偏離的 ja2、ja3，再聽新增的多音字 ja4、ja5 與長句 ja6）：
- 每句各取：`V25`、`ULT-JV25a`、`ULT-JVOXa`、`ULT-R4c`、`ULT-NEUTRAL`（v2 對照），再加同候選的 `REF-` 版（`REF-JV25a`、`REF-JVOXa`、`REF-R4c`）看有沒有「ULT 比 REF 好」的情況。
- (a) 兩段與 (b) 兩段的差別可聽 `ULT-JV25b`、`ULT-JVOXb`。

英文（先聽 en1、en2、en3，再聽新增的長句 en4、en6）：
- 每句各取：`V25`、`ULT-R1c`（v2 版對照）、`ULT-R1n`、`ULT-R2n`、`ULT-EV25a`、`ULT-EVOXa`，以及它們的 `REF-` 版。
- 雜訊：`ULT-R1c` vs `ULT-R1cDN`、`ULT-R2n` vs `ULT-R2nDN`、`ULT-EV25a` vs `ULT-EV25aDN`；`REF-R1`（未處理 R1 原檔，開頭有底噪）對比 `REF-R1c`／`REF-R1n`。
- 情緒：en2（興奮）、en3（抱歉）本來就是帶情緒的句子，prompt 是中性時輸出情緒會偏平，是預期行為；若 `V25-EMO` 有情緒，可和它對照。

這份只是排序依據，不是推薦。""")
    t.append("\n## 限制與未驗證\n")
    t.append("""- 全部音檔未人工聆聽；ASR 輔助判斷，ES 對日文不一定穩。
- 「情緒太極端」沒有客觀指標，只提供 F0/RMS 變異的粗略代理。
- 降噪是自製頻譜閘（venv 沒有 noisereduce，ZipEnhancer 未下載），強度保守，效果小。
- 「Vox 自舉」用的 neutral.wav 本身是 V25 生成，所以長期不依賴 IndexTTS 只是相對的：它仍是 IndexTTS 產出的中文參考音。
- V25 的 RTF 為 eager、無首包數字，不能和 compile 後的 Vox 直接比較。
- prompt 的 ULT 對 prompt 逐字稿的「句尾完整」是由合成句本身的句尾與修靜音保證，沒有逐一聽。""")
    open(os.path.join(L.DEST, "README.md"), "w", encoding="utf-8", newline="\n").write("\n".join(t) + "\n")
    print("readme ok")


if __name__ == "__main__":
    main()
