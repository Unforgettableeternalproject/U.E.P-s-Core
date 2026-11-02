"""日文假名前處理（JapaneseKanaNormalizer）：送進 VoxCPM2 前把含漢字的詞轉成平假名，避免模型用中文讀音唸漢字。

用 pyopenjtalk(-plus) 的 run_frontend 取得讀音（Windows 有 wheel，裝在 .venv-voxcpm）。
- 只轉含漢字的詞，假名／標點／外來語維持原樣
- 讀音取 `read`（正書法讀音，例：合格＝ごうかく、片付け＝かたづけ）；
  若 `pron` 與 `read` 長度相同、只差促音／濁半濁音（例：一＝いっ、発＝ぱつ）就改用 `pron`，
  這樣「一発」得到「いっぱつ」而不是「いちはつ」。pron 含長音符「ー」時不用（會把漢字詞改成長音符拼法）。
限制：pyopenjtalk 的字典讀音，數字、專有名詞、罕見讀法可能不準，需人工審。
"""
import re

_KANJI = re.compile(r"[一-鿿々〆ヵヶ]")


def _kata_to_hira(s):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


class JapaneseKanaNormalizer:
    def __init__(self):
        import pyopenjtalk
        self._fe = pyopenjtalk.run_frontend

    def explain(self, text):
        out, log = [], []
        for w in self._fe(text):
            s, read, pron = w["string"], w["read"], w["pron"].replace("’", "")
            if _KANJI.search(s) and read and read != "＊":
                use = pron if (len(pron) == len(read) and "ー" not in pron and pron != read) else read
                hira = _kata_to_hira(use)
                out.append(hira)
                log.append((s, hira))
            else:
                out.append(s.replace("．", "."))  # frontend 會把 '.' 轉成全形，還原以免省略號被唸成雜音
        return "".join(out), log

    def normalize(self, text):
        return self.explain(text)[0]


if __name__ == "__main__":
    import sys
    n = JapaneseKanaNormalizer()
    for t in sys.argv[1:] or ["机の上の仕事を全部片付けました。", "一発で全部合格したんだよ！"]:
        print(n.explain(t))
