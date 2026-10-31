"""產生 2026-10-31_polyphone_check/README.md：字典、待裁決清單、差字全用例、修正前後句子與 ES ASR 轉寫。"""
import json, os, sys, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tw_accent_check as T, tw_lora_eval as E, es_asr
from tw_polyphone_check import SHORT, DEST
from taiwan_pronunciation import dictionary_markdown

def main():
    info = json.load(open(f"{T.ROOT}/outputs/tw_lora_eval/polyphone_check.json", encoding="utf-8"))
    for r in info:  # 檔案可能被重跑覆寫，這裡以現有檔重新轉寫
        for tag in ("before", "after"):
            r[tag]["asr"] = es_asr.transcribe(f"{DEST}/{r['id']}/{r['id']}__TWLORA-TWN-{tag}.wav")
    texts = [(sid, t) for sid, t, _, _ in T.SENTENCES] + [(k, t) for k, kind, t in E.eval_items() if kind == "tm"] + [(p, t) for p, t, _ in SHORT]
    cha = []
    for sid, t in texts:
        for m in re.finditer("差", t):
            ctx = t[max(0, m.start() - 1): m.start() + 3]
            cha.append((sid, ctx))
    L = ["# 破音詞逐詞聽測 2026-10-31", "",
         "目的：逐詞聽「修正前」與「修正後」的 TWLORA-TWN（LoRA=step_0000600，ULT，neutral.wav 參考）。每句資料夾 pNN 內：",
         "- `pNN__TWLORA-TWN-before.wav`：套用修正前字典（commit 5dd1b1a）的前處理",
         "- `pNN__TWLORA-TWN-after.wav`：套用目前字典", "",
         "轉寫用 Echo-Stream 的 ASR（Breeze2.5 ct2；路徑 TestSeparateSTTSystem/models/stt/whisper/whisper-breeze25-ct2-fp16，"
         "Echo-Stream 的 model_path 由 bot 設定提供、不在 repo，這是該 repo 唯一的 Breeze 模型）。ASR 聽不出聲調與同音字（例：調校會被轉成調教），只是輔助；以耳聽為準，**以下皆未經人工聆聽**。", "",
         "## 這次發現的實際原因（比字典查證更重要）", "",
         "- **s08 開頭「休默」＝「週」字被模型唸成 xiu**：base 與 LoRA、ULT 與 ref-only、3 個 seed、換不同前後文全部重現（orig 9/9 讀錯）；改成「周」或 `{zhou1}` 後 18/18 正確。與 ULT 逐字稿、LoRA、前處理無關（V25 唸得正常）。同樣原因讓「週刊」變「修刊」。",
         "- **s08「一鍋熱湯」**：問題在「鍋」字（被唸成杯/袋/類/奈/道…），不是「一」的變調：`{yi4}` 沒有改善、`{guo1}` 單獨加上就 6/6 正確（base 與 LoRA 皆然）；「湯」單獨加拼音也不夠。",
         "- **s03「這批貨」**（以下探針是用舊預設 LoRA step_0000300 做的，之後預設改為 step_0000600，本資料夾音檔已全部用 600 重做）：用 12 種條件（base/LoRA × ULT/ref × 3 seed × 4 種文字）全部讀對，無法重現；最終 8 句檔裡那一個 TWLORA 版（seed 1033）ES ASR 仍轉成「這 P4 的質量」，屬單次隨機生成失誤，不是前處理（前處理沒動到「批」「貨」）或 prompt 造成。",
         "- 共通推測：模型對少數「只在繁體出現」的字（週、鍋）token/讀音學得不穩。這類字無法靠詞典窮舉，建議之後做一輪「常用繁體字逐字唸讀掃描」（逐字合成 + ASR 比對）。", "",
         "## 字典（目前版本，含這次新增）", "", dictionary_markdown(), "",
         "新增項目的依據逐詞查教育部《國語辭典簡編本》：週末 ㄓㄡ ㄇㄛˋ、順便 ㄕㄨㄣˋ ㄅㄧㄢˋ（與預設一致，不需覆寫）、這裡 ㄓㄜˋ ㄌㄧˇ、市場 ㄕˋ ㄔㄤˇ、廁所 ㄘㄜˋ ㄙㄨㄛˇ、知識 ㄓ ㄕˋ、這些 ㄓㄜˋ ㄒㄧㄝ、力量 ㄌㄧˋ ㄌㄧㄤˋ、重新 ㄔㄨㄥˊ ㄒㄧㄣ、週刊 ㄓㄡ ㄎㄢ、週（單字）ㄓㄡ、鍋 ㄍㄨㄛ。「重構」簡編本我沒有查到詞條，標指定、未查。", "",
         "## 義項／讀法有疑義，未套用，請裁決", "",
         "| 詞 | 簡編本查到的 | 疑義 |", "|---|---|---|",
         "| 調校 | ㄉㄧㄠˋ ㄒㄧㄠˋ「離開原校到他校任職」 | 義項不同，已改採指定 ㄊㄧㄠˊ ㄐㄧㄠˋ（調整校正）|",
         "| 一會兒 | ㄧ ㄏㄨㄟˇ ㄦ，變 ㄧˋ ㄏㄨㄜˇ ㄦ（含兒化） | 簡編本是兒化讀法，與先前 s06 預期「不兒化」衝突，需裁決 |",
         "| 東西 | ㄉㄨㄥ ㄒㄧ（方位）／ㄉㄨㄥ ˙ㄒㄧ（物品，輕聲） | 兩條義項；s07「這些東西」應為物品義，是否採輕聲？拼音輸入無法表示輕聲，需裁決 |",
         "| 意思 | ㄧˋ ㄙ／ㄧˋ ˙ㄙ | 兩條讀法，頁面未顯示義項，未判定 |",
         "| 看看 | ㄎㄢˋ ˙ㄎㄢ／ㄎㄢˋ ㄎㄢˋ | 兩種都收錄，不覆寫 |",
         "| 早上、晚上 | ㄗㄠˇ ㄕㄤˋ（變 ˙ㄕㄤ）／ㄨㄢˇ ㄕㄤˋ（變 ˙ㄕㄤ） | 本調與輕聲皆收，不覆寫 |",
         "| 大家 | ㄉㄚˋ ㄍㄨ／ㄉㄚˋ ㄐㄧㄚ | 兩義項（大戶人家 vs 眾人），句中是「眾人」＝ㄐㄧㄚ，與預設一致，不覆寫 |",
         "| 差 | 單字 ㄔㄚ／ㄔㄞ／ㄘ／ㄘㄨㄛ；差別 ㄔㄚ ㄅㄧㄝˊ；差不多 ㄔㄚ ㄅㄨˋ ㄉㄨㄛ；出差 ㄔㄨ ㄔㄞ | 「很差」依指定 cha1 已套用；其他用例見下表，簡編本都是 ㄔㄚ／ㄔㄞ，未覆寫 |", "",
         "### 「差」字全用例（8 句、TaiMECS 8 句、本組短句）", "", "| 句 | 用例（前後文） | 目前處理 |", "|---|---|---|"]
    for sid, ctx in cha:
        L.append(f"| {sid} | …{ctx}… | {'指定 cha1（已套用）' if '很差' in ctx else '未套用，待裁決'} |")
    L += ["", "## 句子（修正前／後送進模型的文字與 ES ASR）", ""]
    for r in info:
        L += [f"### {r['id']}　{r['text']}", f"- 重點：{r['focus']}",
              f"- before 送入：`{r['before']['sent']}`\n  - ASR：{r['before']['asr']}",
              f"- after 送入：`{r['after']['sent']}`\n  - ASR：{r['after']['asr']}", ""]
    open(f"{DEST}/README.md", "w", encoding="utf-8", newline="\n").write("\n".join(L) + "\n")
    json.dump(info, open(f"{T.ROOT}/outputs/tw_lora_eval/polyphone_check.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("ok")
if __name__ == "__main__":
    main()
