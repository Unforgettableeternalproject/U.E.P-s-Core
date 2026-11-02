"""台灣華語發音前處理（TaiwanPronunciationNormalizer）。

放在 Chunker 與 VoxCPM2 推論之間：只把需要修正的字詞替換成 VoxCPM2 官方的拼音輸入語法
`{pin1yin1}`（帶聲調數字的漢語拼音），其餘文字原樣保留，不整句轉拼音。

VoxCPM2 端注意事項（voxcpm 2.0.3 實測檢視）：
- 套件本身不解析大括號；`{zou4}` 直接進模型 tokenizer，由模型訓練時學到的拼音輸入處理。
- 呼叫 generate 時請用 `normalize=False`（預設即 False）。normalize=True 會跑 wetext 文字正規化，
  可能改寫括號內容，使用拼音輸入時不要開。
- 英文 CMUDict 音素（`{HH AH0 L OW1}`）是同一語法，本模組不碰：已存在的 `{...}` 區段原樣保護。

字典依據：教育部《國語辭典簡編本》（https://dict.concised.moe.edu.tw），於 2026-10-31 逐詞查證。
status：
  "MOE"    詞條在簡編本，注音與本表一致
  "指定"   艾斯維爾指定的讀法（與簡編本單字注音不同時以指定為準）
  "MOE-字" 詞條（專有名詞/複合詞）簡編本沒有，但單字讀音在簡編本可查證
  "待確認" 簡編本查不到或無法確認該義項，保留供人工審定
"""
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entry:
    word: str
    # {字在詞內的索引: 拼音}；只列出要替換的字，其餘字保留原樣
    fix: dict
    zhuyin: str      # 簡編本注音（整詞）
    mainland: str    # 常見的中國讀法（模型易讀成的版本），供說明用
    source: str
    status: str


def _e(word, fix, zhuyin, mainland, source, status="MOE"):
    return Entry(word, fix, zhuyin, mainland, source, status)


_S = "教育部《國語辭典簡編本》"
ENTRIES = [
    _e("步驟", {1: "zou4"}, "ㄅㄨˋ ㄗㄡˋ", "bù zhòu", f"{_S}「步驟」bù zòu"),
    _e("垃圾", {0: "le4", 1: "se4"}, "ㄌㄜˋ ㄙㄜˋ", "lā jī", f"{_S}「垃圾」及其衍生詞（垃圾桶、垃圾車等）皆 ㄌㄜˋ ㄙㄜˋ"),
    _e("星期", {1: "qi2"}, "ㄒㄧㄥ ㄑㄧˊ", "xīng qī", f"{_S}「星期」xīng qí，無又音"),
    _e("企業", {0: "qi4"}, "ㄑㄧˋ ㄧㄝˋ", "qǐ yè", f"{_S}「企業」ㄑㄧˋ ㄧㄝˋ"),
    _e("頭髮", {1: "fa3"}, "ㄊㄡˊ ㄈㄚˇ", "tóu fa", f"{_S}「頭髮」ㄊㄡˊ ㄈㄚˇ；「髮」單字僅 ㄈㄚˇ"),
    _e("研究", {1: "jiu4"}, "ㄧㄢˊ ㄐㄧㄡˋ", "yán jiū", f"{_S}「研究」ㄧㄢˊ ㄐㄧㄡˋ"),
    _e("綜合", {0: "zong4"}, "ㄗㄨㄥˋ ㄏㄜˊ", "zōng hé", f"{_S}「綜合」ㄗㄨㄥˋ ㄏㄜˊ"),
    _e("質量", {0: "zhi2"}, "ㄓˊ ㄌㄧㄤˋ", "zhì liàng", f"{_S}「質量」zhí liàng（物理名詞，無又音）"),
    _e("品質", {1: "zhi2"}, "ㄆㄧㄣˇ ㄓˊ", "pǐn zhì", f"{_S}「品質」ㄆㄧㄣˇ ㄓˊ"),
    _e("本質", {1: "zhi2"}, "ㄅㄣˇ ㄓˊ", "běn zhì", f"{_S}「本質」ㄅㄣˇ ㄓˊ"),
    _e("危險", {0: "wei2"}, "ㄨㄟˊ ㄒㄧㄢˇ", "wēi xiǎn", f"{_S}「危險」wéi xiǎn"),
    _e("危機", {0: "wei2"}, "ㄨㄟˊ ㄐㄧ", "wēi jī", f"{_S}「危機」ㄨㄟˊ ㄐㄧ"),
    _e("亞洲", {0: "ya3"}, "ㄧㄚˇ ㄓㄡ", "yà zhōu", "艾斯維爾指定（台灣讀法 ㄧㄚˇ）；簡編本無「亞洲」詞條，「亞」單字收 ㄧㄚˋ，與指定讀法不同", "指定"),
    _e("法國", {0: "fa3"}, "ㄈㄚˇ ㄍㄨㄛˊ", "fà guó", f"簡編本無「法國」詞條；「法」ㄈㄚˊ/ㄈㄚˇ，國名取 ㄈㄚˇ（依艾斯維爾審音清單）", "MOE-字"),
    _e("週", {0: "zhou1"}, "ㄓㄡ", "zhōu（讀音相同）",
       f"{_S}「週」ㄓㄡ（及週末、週刊、週年…皆 ㄓㄡ）；實測 base 模型把「週」唸成 xiu（見 tw_s08_probe），以拼音強制", "MOE"),
    _e("熄", {0: "xi2"}, "ㄒㄧˊ", "xī",
       f"{_S}艾斯維爾指定「熄」讀 ㄒㄧˊ（xi2），例如「熄掉」「熄燈」，以拼音強制", "指定"),
    _e("鍋", {0: "guo1"}, "ㄍㄨㄛ", "guō（讀音相同）",
       f"{_S}「鍋」ㄍㄨㄛ（及鍋巴、鍋爐…皆 ㄍㄨㄛ）；實測 base 模型把「鍋」唸成杯/袋/道等別的字（tw_probe s08b：加 {{guo1}} 後 6/6 正確），以拼音強制", "MOE"),
    _e("捨", {0: "she3"}, "ㄕㄜˇ", "shè（模型易把「捨」當簡體「舍」讀成四聲）",
       f"{_S}「捨」ㄕㄜˇ（捨不得、捨棄、取捨、施捨、割捨、不捨、捨得皆 ㄕㄜˇ）；單字規則，以拼音強制"),
    _e("調校", {0: "tiao2", 1: "jiao4"}, "ㄊㄧㄠˊ ㄐㄧㄠˋ", "diào xiào",
       f"艾斯維爾指定（調整、校正義）；簡編本「調校」收的是 ㄉㄧㄠˋ ㄒㄧㄠˋ「離開原校到他校任職」，義項不同，不採用", "指定"),
    _e("重構", {0: "chong2", 1: "gou4"}, "ㄔㄨㄥˊ ㄍㄡˋ", "zhòng gòu", "艾斯維爾指定（重新構築義；簡編本查無「重構」詞條）", "指定"),
    _e("很差", {1: "cha1"}, "ㄏㄣˇ ㄔㄚ", "hěn chà", "艾斯維爾指定（差=ㄔㄚ）；「差」其他詞義（差別、出差、差不多）未套用，待裁決", "指定"),
    _e("衣服", {1: "fu2"}, "ㄧ ㄈㄨˊ", "yī fu（輕聲）", f"{_S}「衣服」ㄧ ㄈㄨˊ（本調，兩岸差異：大陸輕聲）"),
    _e("先生", {1: "sheng1"}, "ㄒㄧㄢ ㄕㄥ", "xiān sheng（輕聲）", f"{_S}「先生」ㄒㄧㄢ ㄕㄥ（本調，兩岸差異：大陸輕聲）"),
    _e("知道", {1: "dao4"}, "ㄓ ㄉㄠˋ", "zhī dao（輕聲）", f"{_S}「知道」ㄓ ㄉㄠˋ（本調，兩岸差異：大陸輕聲）"),
    _e("逛逛", {1: "guang4"}, "ㄍㄨㄤˋ ㄍㄨㄤˋ", "guàng guang（輕聲）", f"{_S}「逛逛」ㄍㄨㄤˋ ㄍㄨㄤˋ（本調，兩岸差異：大陸輕聲）"),
    _e("角色", {0: "jue2"}, "ㄐㄩㄝˊ ㄙㄜˋ", "jué sè（模型易誤讀 jiǎo sè）",
       f"{_S}「角色」jué sè；另列語音 ㄐㄧㄠˇ ㄙㄜˋ（jiǎo sè），不採用"),
]

# 連詞「和」：簡編本「和」有 ㄏㄜˊ/ㄏㄜˋ/ㄏㄢˋ/ㄏㄨˊ/˙ㄏㄨㄛ/ㄏㄨㄛˋ 六讀，ㄏㄢˋ 為語音讀法（詞條 ID 19725）；
# 「ㄏㄢˋ 對應連詞義項」簡編本頁面無法確認，標待確認。只在「人稱代名詞＋和」或「和＋人稱代名詞」時套用。
_PRON = "我你妳他她它祢您咱"
HAN_RULE = {
    "pattern": re.compile(rf"(?<=[{_PRON}])和|和(?=[{_PRON}])"),
    "pinyin": "han4",
    "source": f"{_S}「和」ㄏㄢˋ（語音）；連詞義項待確認",
    "status": "待確認",
}
# 排除：「和」緊接在這些字後多半是「和平／和氣」等詞，不是連詞
_HAN_BLOCK = re.compile(r"和(?=[平氣諧解睦藹善談約聲弦])")

_PROTECT = re.compile(r"\{[^{}]*\}")


class TaiwanPronunciationNormalizer:
    """normalize(text) -> 帶 {pinyin} 的文字；explain(text) 回傳逐項替換紀錄。"""

    def __init__(self, entries=ENTRIES, enable_han=True):
        self.entries = sorted(entries, key=lambda e: -len(e.word))  # 最長詞優先
        self.by_first = {}
        for e in self.entries:
            self.by_first.setdefault(e.word[0], []).append(e)
        self.enable_han = enable_han

    def _apply(self, seg, log):
        out, i = [], 0
        while i < len(seg):
            hit = None
            for e in self.by_first.get(seg[i], ()):
                if seg.startswith(e.word, i):
                    hit = e
                    break
            if hit:
                piece = "".join("{%s}" % hit.fix[k] if k in hit.fix else ch for k, ch in enumerate(hit.word))
                out.append(piece)
                log.append({"word": hit.word, "to": piece, "status": hit.status})
                i += len(hit.word)
            else:
                out.append(seg[i])
                i += 1
        text = "".join(out)
        if self.enable_han and "和" in text:
            def sub(m):
                if _HAN_BLOCK.match(text, m.start()):
                    return m.group(0)
                log.append({"word": "和(連詞)", "to": "{han4}", "status": HAN_RULE["status"]})
                return "{han4}"
            text = HAN_RULE["pattern"].sub(sub, text)
        return text

    def explain(self, text):
        log, parts, last = [], [], 0
        for m in _PROTECT.finditer(text):  # 已有的 {…} 不動
            parts.append(self._apply(text[last:m.start()], log))
            parts.append(m.group(0))
            last = m.end()
        parts.append(self._apply(text[last:], log))
        return "".join(parts), log

    def normalize(self, text):
        return self.explain(text)[0]


def dictionary_markdown():
    """產生字典說明表（README 用）。"""
    L = ["| 詞 | 送入模型 | 簡編本注音 | 常見中國讀法 | 依據 | 狀態 |", "|---|---|---|---|---|---|"]
    n = TaiwanPronunciationNormalizer(enable_han=False)
    for e in ENTRIES:
        piece = "".join("{%s}" % e.fix[k] if k in e.fix else ch for k, ch in enumerate(e.word))
        L.append(f"| {e.word} | `{piece}` | {e.zhuyin} | {e.mainland} | {e.source} | {e.status} |")
    L.append(f"| 和（連詞，人稱代名詞前後） | `{{han4}}` | ㄏㄢˋ | hé | {HAN_RULE['source']} | {HAN_RULE['status']} |")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    n = TaiwanPronunciationNormalizer()
    for t in sys.argv[1:] or ["步驟很簡單，垃圾要分類，星期五我和你一起去。"]:
        print(n.normalize(t))
