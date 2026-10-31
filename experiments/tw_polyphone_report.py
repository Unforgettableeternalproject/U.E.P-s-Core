"""破音字／審音字清單：8 句鑑別句 + TaiMECS 真人句子集，各版本 Whisper 轉寫對位。

Whisper 是語言模型式 ASR，會把發音偏差補正回常見字，也聽不出聲調與輕聲，所以這裡只提供「線索」：
- 目標詞原樣出現在轉寫 -> 標「字面對」（讀音大致落在合理範圍，聲調仍須人工聽）
- 目標詞對位區段被轉成別的字 -> 標「轉寫不同」並列出轉成什麼（較可能讀偏）
- 轉成簡體 -> 標「簡體化」（Whisper 傾向把該段當中國腔/簡體語料）
預期讀音以教育部《國語辭典簡編本》為準（見 taiwan_pronunciation.py 的來源欄），查不到標待確認。

用法（.venv-voxcpm，需 pypinyin）：python experiments/tw_polyphone_report.py
"""
import difflib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tw_accent_check as T  # noqa: E402
import tw_lora_eval as E  # noqa: E402
from taiwan_pronunciation import ENTRIES  # noqa: E402

# (句, 目標詞, 預期讀音, 依據)
TARGETS = [
    ("s01", "步驟", "ㄅㄨˋ ㄗㄡˋ (bù zòu)", "MOE"), ("s01", "垃圾", "ㄌㄜˋ ㄙㄜˋ (lè sè)", "MOE"),
    ("s01", "星期", "ㄒㄧㄥ ㄑㄧˊ (xīng qí)", "MOE"), ("s01", "和你", "和=ㄏㄢˋ (hàn)", "待確認（連詞義項）"),
    ("s02", "企業", "ㄑㄧˋ ㄧㄝˋ (qì yè)", "MOE"), ("s02", "研究", "ㄧㄢˊ ㄐㄧㄡˋ (yán jiù)", "MOE"),
    ("s02", "頭髮", "ㄊㄡˊ ㄈㄚˇ (tóu fǎ)", "MOE"), ("s02", "綜合", "ㄗㄨㄥˋ ㄏㄜˊ (zòng hé)", "MOE"),
    ("s02", "亞洲", "ㄧㄚˇ ㄓㄡ (yǎ zhōu)", "艾斯維爾指定"),
    ("s03", "質量", "ㄓˊ ㄌㄧㄤˋ (zhí liàng)", "MOE"), ("s03", "星期", "ㄒㄧㄥ ㄑㄧˊ (xīng qí)", "MOE"),
    ("s03", "研究", "ㄧㄢˊ ㄐㄧㄡˋ (yán jiù)", "MOE"),
    ("s06", "一會兒", "ㄧˋ ㄏㄨㄟˇ ㄦˊ，不兒化（變調／兒化）", "待確認（未查簡編本）"),
    ("s07", "東西", "ㄉㄨㄥ ㄒㄧ（台灣偏本調／弱輕聲）", "待確認（輕聲未查簡編本）"),
    ("s07", "意思", "ㄧˋ ˙ㄙ（輕聲）", "待確認"),
]
VERS = ["VOX-ULT-L085", "VOX-ULT-TWN", "VOX-ULT-TWLORA", "VOX-ULT-TWLORA-TWN"]
VLABEL = {"VOX-ULT-L085": "無前處理", "VOX-ULT-TWN": "TWN", "VOX-ULT-TWLORA": "LoRA", "VOX-ULT-TWLORA-TWN": "LoRA+TWN"}


def align(ref, hyp, target):
    """回傳 ref 中 target 位置對應到 hyp 的區段（逐字對位；equal 區段精確對應）。"""
    i = ref.find(target)
    if i < 0:
        return None
    j = i + len(target)
    sm = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)
    lo, hi = None, None
    for tag, a0, a1, b0, b1 in sm.get_opcodes():
        s0, s1 = max(a0, i), min(a1, j)
        if s0 >= s1 and not (tag == "insert" and i <= a0 <= j):
            continue
        if tag == "equal":
            m0, m1 = b0 + (s0 - a0), b0 + (s1 - a0)
        else:
            m0, m1 = b0, b1
        lo = m0 if lo is None else min(lo, m0)
        hi = m1 if hi is None else max(hi, m1)
    return hyp[lo:hi] if lo is not None else ""


def main():
    import librosa
    from faster_whisper import WhisperModel
    from opencc import OpenCC
    from pypinyin import Style, lazy_pinyin
    s2t = OpenCC("s2t")
    wm = WhisperModel("medium", device="cuda", compute_type="float16")
    tr = lambda p: "".join(s.text for s in wm.transcribe(librosa.load(p, sr=16000)[0], language="zh", beam_size=5)[0]).strip()  # noqa: E731
    sents = {sid: text for sid, text, _, _ in T.SENTENCES}
    asr = {}
    for sid in sents:
        for v in VERS:
            asr[f"{sid}__{v}"] = tr(os.path.join(T.DEST, sid, f"{sid}__{v}.wav"))
    rows = ["## 破音字／審音字清單（8 句）", "",
            "格式：`字面對` = 轉寫含目標詞；否則列出對位的轉寫內容（`簡` = 含簡體字）。Whisper 聽不出聲調，「字面對」不等於讀對。", "",
            "| 句 | 詞 | 預期讀音（台灣） | 依據 | " + " | ".join(VLABEL[v] for v in VERS) + " |", "|---|---|---|---|" + "---|" * len(VERS)]
    for sid, w, exp, src in TARGETS:
        cells = []
        for v in VERS:
            h = asr[f"{sid}__{v}"]
            ht = s2t.convert(h)
            seg = align(sents[sid], ht, w)
            raw_seg = align(sents[sid], h, w) or ""
            simp = "簡" if s2t.convert(raw_seg) != raw_seg else ""
            cells.append("字面對" + (simp and "（簡）") if seg == w and not simp else f"{seg}{'（簡）' if simp else ''}")
        rows.append(f"| {sid} | {w} | {exp} | {src} | " + " | ".join(cells) + " |")
    rows += ["", "Whisper 完整轉寫：", ""]
    rows += [f"- {k}：{v}" for k, v in asr.items()]
    # TaiMECS 子集：pypinyin 找出多音字候選；預期讀音未逐字查簡編本一律待確認
    items = [(k, t) for k, kind, t in E.eval_items() if kind == "tm"]
    dict_words = {e.word: e for e in ENTRIES}
    rows += ["", "## 破音字候選（TaiMECS 子集 8 句）", "",
             "多音字候選 = 手選的常見破音字（行調校還會重差得率模了都少發乾降量間為種更和覺著長數樂）出現處，附 pypinyin 詞組讀音（大陸語料訓練，非台灣標準）；台灣標準讀法僅字典已收的詞有查證，其餘標待確認，請人工審定。"
             " 這 8 句只有 base(=TWN) 與 LoRA(step_0000300)+TWN 兩版，無「無前處理」對照。", "",
             "| 句 | 字（前後文） | pypinyin 詞組讀音 | base(TWN) 轉寫對位 | LoRA+TWN 轉寫對位 |", "|---|---|---|---|---|"]
    HET = "行調校還會重差得率模了都少發乾降量間為種更和覺著長數樂"
    for key, text in items:
        asr_b = json.load(open(os.path.join(E.EV, "metrics_base.json"), encoding="utf-8"))[key]["asr"]
        asr_l = json.load(open(os.path.join(E.EV, "metrics_step_0000300.json"), encoding="utf-8"))[key]["asr"]
        zh = "".join(c for c in text if "一" <= c <= "鿿")  # 僅漢字，與 lazy_pinyin 一一對齊
        py = lazy_pinyin(zh, style=Style.TONE3)
        cleanz = lambda t: re.sub(r"[^一-鿿]+", "", s2t.convert(t))  # noqa: E731
        for idx, ch in enumerate(zh):
            if ch not in HET:
                continue
            ctx = zh[max(0, idx - 1): idx + 2]
            ab = align(zh, cleanz(asr_b), ctx) or "-"
            al = align(zh, cleanz(asr_l), ctx) or "-"
            rows.append(f"| {key} | {ch}（{ctx}） | {py[idx]} | {'字面對' if ab == ctx else ab} | {'字面對' if al == ctx else al} |")
    out = os.path.join(E.EV, "polyphone.md")
    open(out, "w", encoding="utf-8").write("\n".join(rows) + "\n")
    print("\n".join(rows))


if __name__ == "__main__":
    main()
