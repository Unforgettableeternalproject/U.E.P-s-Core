"""產生 E:/Materials/Voices/UEP/2026-11-01_foreign_v2/README.md（CPU，不用 GPU）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import librosa  # noqa: E402
import numpy as np  # noqa: E402

import tw_accent_check as T  # noqa: E402
import tw_foreign_v2 as F  # noqa: E402
from japanese_kana import JapaneseKanaNormalizer  # noqa: E402
from tw_foreign_check import ITEMS  # noqa: E402


def lead(path):
    """回傳 (起音秒, 淡弱前導秒)：前導 = 首個 > -65dB 的幀到首個 > 峰值 10% 幀的間隔。"""
    y = librosa.load(path, sr=16000)[0]
    rms = librosa.feature.rms(y=y, frame_length=400, hop_length=160)[0]
    db = librosa.amplitude_to_db(rms + 1e-9)
    first = int(np.argmax(db > -65))
    on = int(np.argmax(rms > 0.1 * rms.max()))
    return round(on * 0.01, 2), round(max(0, on - first) * 0.01, 2)


def main():
    sc = json.load(open(os.path.join(F.W, "score.json"), encoding="utf-8"))
    meta = json.load(open(os.path.join(F.W, "gen_meta.json"), encoding="utf-8"))
    kana = JapaneseKanaNormalizer()
    ptxt = {k: json.load(open(os.path.join(F.REFS, k + ".json"), encoding="utf-8"))["text"] for k in F.PROMPTS}
    L = ["# 外語 v2（英文／日文）2026-11-01", "",
         "決策前提：所有語言共用同一個 VoxCPM2 實例。LoRA 預設 `step_0000600`；英日句掛載後以 `set_lora_enabled(False)` 關閉、不經 TWN；"
         "中文句開 LoRA + TWN（驗證見最後）。44.1kHz、-23 LUFS、頭尾靜音已修齊；cfg 2.0 / steps 10（除參數組）、normalize=False、seed = SEED+70+句序，各版相同。",
         "ASR：ES Breeze2.5 與原版 faster-whisper medium（language=en/ja）兩者並列；兩者對日文都能讀出漢字，Breeze 在日文大致可用，但仍輔助，**所有音檔未經人工聆聽**。",
         "RTF = 生成時間 / 音訊長度（eager，未開 torch.compile，RTX 5070 Ti；首包延遲未量）；<1 才是比即時快。主跑（en1–3、ja1–2）約 1.4–1.6；ja3 因日文假名化修正後重生，當時 GPU 被其他程式佔用，RTF 偏高（3–6），日文表的 RTF 平均因此偏高，不代表模型本身。", "",
         "## 版本定義", "",
         "- `V25`：現行 IndexTTS 2.5（v25_final 的 B 系列；happy/sad 句帶情緒向量 0.5/0.65，其餘版本無情緒提示）",
         "- `REF-R1` / `REF-R2`：只給參考音 R1（uep_reference_v25.wav）／R2（uep_pancake.wav），無逐字稿",
         "- `ULT-R1c` / `ULT-R2c`：ULT，參考音與 prompt 音檔同一支「乾淨 prompt」，在句界結束、頭尾為近靜音、逐字稿完整對得上：",
         f"  - R1c（R1 的 {F.PROMPTS['R1c'][1]}–{F.PROMPTS['R1c'][2]}s）：`{ptxt['R1c']}`",
         f"  - R2c（R2 的 0–6.0s）：`{ptxt['R2c']}`",
         f"  - R4c（R4 的 {F.PROMPTS['R4c'][1]}–{F.PROMPTS['R4c'][2]}s）：`{ptxt['R4c']}`",
         "  - 逐字稿由 whisper word timestamps 與 ES ASR 交叉確認（兩者相符），不是人工校對",
         "- `REF-R2_cfgX_tY` / `ULT-R2c_cfgX_tY`：同上，cfg=X、inference_timesteps=Y（預設為 cfg2.0 t10）",
         "- `REF-R4` / `ULT-R4c`（日文）：原文直接送入，參考音 R4（ITA 日文）／乾淨 R4c prompt",
         "- `KANA_*`（日文）：句子（含 ULT prompt 逐字稿）先經 JapaneseKanaNormalizer（pyopenjtalk-plus，漢字詞轉平假名）再送入",
         "- `KANA_ULT-NEUTRAL`：假名化文字 + 中文 neutral.wav ULT（U.E.P 中文參考音與其中文逐字稿）",
         "- `KANA_REF-R1`：假名化文字 + 英文 R1 只給參考音", "",
         "## 1. 英文怪開頭：原因與修法", "",
         "逐檔檢查 2026-10-31_foreign_check 的 en 檔（ES ASR + whisper medium 詞級時間戳，與原文對齊找開頭多出的詞）：",
         "- 多出內容只出現在 `VOX-BASE-ULT-R1`，3/3 檔：en1「biocal hotline or police emergency number as a consumer think before you shop as a」、"
         "en2「File the hotline… As a consumer, think before… As a hey hey hey」、en3「Call the hotline or police emergency number. As a consumer, think before you shop. As a」，音檔長度 13.6–15.6 秒（正常約 7 秒）。",
         "- 原因：ULT 把 prompt 逐字稿尾段（R1 的 15 秒片段切在 \"As a\"，逐字稿是 Whisper 轉的，句尾不完整）重新唸了一遍，之後才接目標句；是 prompt 在句中被切斷造成的續寫，不是 badcase 重試、也不是 ref-only 外洩。",
         "- 其餘 en 檔（REF-R1、REF-R2、ULT-NEUTRAL、TWLORA-OFF、V25）詞級轉寫開頭都與原文對上，沒有多出的詞。",
         "- 另外，部分檔在語音前有一段極弱的前導（低頻雜音，疑似呼吸/底噪，約 -55 至 -70 dB，0.25–0.3 秒），ASR 看不到；若他聽到的「怪開頭」是這個，見下方統計與建議。",
         "- 修法：用句界結束的乾淨 prompt（上表 R1c / R2c）。驗證：本資料夾所有 ULT-R1c / ULT-R2c（含參數組）en 檔轉寫開頭都沒有多出內容。", ""]
    # 前導表
    L += ["下表為本資料夾英文檔的起音與弱前導（秒）：", "", "| 檔 | 起音 | 弱前導 | 開頭多出詞（whisper） |", "|---|---|---|---|"]
    for k, r in sc.items():
        if k.startswith("en") and ("cfg" not in k):
            on, ld = lead(r["file"])
            L.append(f"| {k} | {on} | {ld} | {r.get('extra_prefix', '') or '無'} |")
    flagged = [(k, lead(r["file"])) for k, r in sc.items() if k.startswith("en") and "cfg" not in k and lead(r["file"])[1] >= 0.15]
    L += ["", f"弱前導 >= 0.15 秒的英文檔：{', '.join(k for k, _ in flagged) or '無'}（共 {len(flagged)}/{sum(1 for k in sc if k.startswith('en') and 'cfg' not in k)}）。",
          "建議：後處理的修靜音門檻從 top_db=40 收緊到 30（弱前導與語音差約 35 dB 以上）即可去掉；這次沒有套用，檔案保持原樣供比對。", "",
          "## 2. 日文假名化", "",
          "工具：`pyopenjtalk-plus`（Windows 可直接 pip 安裝，已裝在 `.venv-voxcpm`）；fugashi + unidic-lite 也試過，但「一発」會得到「いちはつ」，pyopenjtalk 得到「いっぱつ」，故採 pyopenjtalk。"
          "規則：只轉含漢字的詞，讀音取 `read`，若 `pron` 與 `read` 同長且只差促音/濁音就取 `pron`；省略號 `...` 還原為半形（frontend 會轉全形，曾讓模型唸出雜音）。", "",
          "| 原文 → 假名化後送入模型 |", "|---|"]
    for iid, lang, text, *_ in ITEMS:
        if lang == "ja":
            k, log = kana.explain(text)
            L.append(f"| {iid}：{text} → `{k}`<br>漢字詞：{'、'.join(f'{a}→{b}' for a, b in log)} |")
    L += ["", "讀音檢查（字典輸出）：机→つくえ ✓、合格→ごうかく ✓、一発→いっぱつ ✓、今日→きょう ✓、片付け→かたずけ（ズ/ヅ同音，發音正確）、失くし→なくし ✓。"
          "限制：專有名詞、數字、罕見讀法需人工審；ULT prompt R4c 逐字稿含外來語專名（ストラットフォード…），是 Whisper 轉寫，未對照 ITA 原稿。", ""]
    # 指標表
    def agg(prefix_pred):
        rows = [r for k, r in sc.items() if prefix_pred(k)]
        f = lambda xs: round(float(np.mean(xs)), 3) if xs else None  # noqa: E731
        return f([r["sim"] for r in rows]), f([r["cer_whisper"] for r in rows]), f([r["cer_es"] for r in rows]), f([r["rtf"] for r in rows if r.get("rtf")]), len(rows)
    L += ["## 3. 彙總指標（3 句平均；SIM 對 R1；CER 為 whisper / ES）", "", "### 英文", "", "| 版本 | SIM | CER whisper | CER ES | RTF | 檔數 |", "|---|---|---|---|---|---|"]
    names = ["V25", "REF-R1", "REF-R2", "ULT-R1c", "ULT-R2c"] + [f"{m}_cfg{c}_t{s}" for m in ("REF-R2", "ULT-R2c") for c, s in F.GRID]
    for n in names:
        a = agg(lambda k, n=n: k.startswith("en") and k.split("__")[1] == n)
        L.append(f"| {n} | {a[0]} | {a[1]} | {a[2]} | {a[3]} | {a[4]} |")
    L += ["", "### 日文", "", "| 版本 | SIM | CER whisper | CER ES | RTF | 檔數 |", "|---|---|---|---|---|---|"]
    for n in ["V25", "REF-R4", "KANA_REF-R4", "ULT-R4c", "KANA_ULT-R4c", "KANA_ULT-NEUTRAL", "KANA_REF-R1"]:
        a = agg(lambda k, n=n: k.startswith("ja") and k.split("__")[1] == n)
        L.append(f"| {n} | {a[0]} | {a[1]} | {a[2]} | {a[3]} | {a[4]} |")
    z = sc["zh_check__TWLORA-TWN"]
    L += ["", f"### 同一實例回中文（LoRA 重新開啟）", "",
          f"`zh_check/zh_check__TWLORA-TWN.wav`（s01、TWN、ULT neutral）：SIM {z['sim']}，ES 轉寫 `{z['es']}`（CER {z['cer_es']}），RTF {z['rtf']}。"
          "（whisper 在中文會出現簡體字，CER 偏高屬已知的弱指標。）", "",
          "## 4. 建議耳聽的檔案", "",
          "- 英文：`en*__V25`（對照）、`en*__REF-R2`、`en*__ULT-R2c`、`en*__ULT-R1c`、`en2__REF-R1`（看有沒有那 0.25 秒前導）、`en*__REF-R2_cfg1.5_t10` vs `ULT-R2c_cfg1.5_t20`（看參數）",
          "- 日文：`ja*__V25`、`ja*__REF-R4`、`ja*__KANA_REF-R4`、`ja*__KANA_ULT-R4c`、`ja*__KANA_ULT-NEUTRAL`（U.E.P 音色，SIM 約 0.9）",
          "- 重點問題：英文口音／韻律是否自然（ASR 看不出）；日文假名化後是否改善「漢字用中文讀音」。", "",
          "## 5. 句子與逐檔送入文字、轉寫", ""]
    for iid, lang, text, *_ in ITEMS:
        L += [f"### {iid}（{lang}）", f"原文：{text}"]
        if lang == "ja":
            L += [f"假名化：{kana.normalize(text)}"]
        L.append("")
        for k, r in sc.items():
            if k.split("__")[0] == iid:
                m = meta.get(k, {})
                sent = m.get("sent", "（V25 既有檔）")
                L.append(f"- `{k.split('__')[1]}`：SIM {r['sim']}　RTF {r.get('rtf')}　cfg/steps {m.get('cfg', '-')}/{m.get('steps', '-')}\n  - 送入：{sent}\n  - whisper：{r['whisper']}\n  - ES：{r['es']}")
        L.append("")
    open(os.path.join(F.DEST, "README.md"), "w", encoding="utf-8", newline="\n").write("\n".join(L) + "\n")
    print("ok", len(L))


if __name__ == "__main__":
    main()
