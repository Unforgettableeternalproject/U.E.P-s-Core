"""
Taiwan Accent Preprocessor for IndexTTS2

將輸入文本轉換為更接近台灣國語發音的形式，
利用 IndexTTS2 已有的拼音注入能力來控制腔調。

策略：
  1. 去兒化音
  2. 台灣詞彙替換
  3. 聲調/輕聲調整（台灣 vs 大陸的輕聲使用差異）
  4. 捲舌音軟化標記（透過拼音注入）

注意：IndexTTS2 的 BPE model 是在簡體中文上訓練的，
     所以我們的修改要在 trad→simp 轉換之前或與之配合。
     拼音注入格式：直接在文字中寫 `pin1yin1`，normalizer 會保留。

用法：
  preprocessor = TaiwanAccentPreprocessor()
  modified_text = preprocessor.process("你知道嗎？那個小孩兒真的很厲害")
  # → 可能輸出帶拼音標註的文字，交給 IndexTTS2 的 TextNormalizer 處理
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaiwanPreprocessConfig:
    """台灣腔前處理配置"""
    enable_erhua_removal: bool = True       # 去兒化音
    enable_vocab_replace: bool = True       # 台灣詞彙替換
    enable_tone_adjust: bool = True         # 聲調微調
    enable_retroflexion_soften: bool = True # 捲舌音軟化
    retroflexion_mode: str = "annotate"     # "annotate" = 拼音注入, "replace" = 直接替換字
    verbose: bool = False


class TaiwanAccentPreprocessor:
    """
    台灣國語腔調前處理器。

    設計原則：
    - 在 IndexTTS2 的 TextNormalizer 之前運行
    - 輸出仍然是合法的中文文字（可能帶拼音標註）
    - 利用 IndexTTS2 BPE vocab 中的拼音 token 來控制發音
    - 不改變語意，只改變發音指導
    """

    def __init__(self, config: Optional[TaiwanPreprocessConfig] = None):
        self.config = config or TaiwanPreprocessConfig()
        self._init_erhua_patterns()
        self._init_vocab_map()
        self._init_retroflexion_map()
        self._init_tone_adjustments()

    # ═══════════════════════════════════════════
    #  1. 兒化音去除
    # ═══════════════════════════════════════════

    def _init_erhua_patterns(self):
        """
        兒化音規則：
        - 台灣國語幾乎不使用兒化音
        - 「兒」作為獨立字時保留（如「兒子」「女兒」）
        - 「兒」作為後綴時去除（如「小孩兒」→「小孩」）
        """
        # 「兒」作為獨立詞的情況（不去除）
        self.erhua_keep_words = {
            "兒子", "女兒", "兒女", "兒童", "兒時", "兒歌",
            "幼兒", "嬰兒", "少兒", "育兒", "託兒", "孤兒",
            "兒媳", "兒戲", "兒科", "兒輩",
            # 簡體
            "儿子", "女儿", "儿女", "儿童", "儿时", "儿歌",
            "幼儿", "婴儿", "少儿", "育儿", "托儿", "孤儿",
            "儿媳", "儿戏", "儿科", "儿辈",
        }

        # 兒化音後綴模式（要去除的）
        # 例如：花兒、鳥兒、小孩兒、一會兒、哪兒、這兒
        self.erhua_suffix_pattern = re.compile(
            r'(?<=[一-鿿])([兒儿])(?=[^子女童時歌媳戲科輩]|$)'
        )

    def remove_erhua(self, text: str) -> str:
        """去除兒化音後綴"""
        if not self.config.enable_erhua_removal:
            return text

        # 先保護獨立詞
        protected = {}
        for word in self.erhua_keep_words:
            if word in text:
                placeholder = f"<ERH_{len(protected)}>"
                protected[placeholder] = word
                text = text.replace(word, placeholder)

        # 去除兒化音後綴
        text = self.erhua_suffix_pattern.sub("", text)

        # 恢復保護的詞
        for placeholder, word in protected.items():
            text = text.replace(placeholder, word)

        return text

    # ═══════════════════════════════════════════
    #  2. 台灣詞彙替換
    # ═══════════════════════════════════════════

    def _init_vocab_map(self):
        """
        大陸用語 → 台灣用語 對照表。
        只替換在 TTS 語境下會影響聽感的詞彙。
        注意：這些替換在語意上是等價的。
        """
        self.vocab_map = {
            # 交通
            "出租车": "計程車",
            "出租車": "計程車",
            "地铁": "捷運",
            "地鐵": "捷運",
            "公交车": "公車",
            "公交車": "公車",

            # 食物
            "土豆": "馬鈴薯",
            "西红柿": "番茄",
            "西紅柿": "番茄",
            "菠萝": "鳳梨",
            "菠蘿": "鳳梨",
            "猕猴桃": "奇異果",
            "獼猴桃": "奇異果",

            # 日常
            "信息": "資訊",
            "软件": "軟體",
            "軟件": "軟體",
            "硬件": "硬體",
            "硬件": "硬體",
            "视频": "影片",
            "視頻": "影片",
            "网络": "網路",
            "網絡": "網路",
            "鼠标": "滑鼠",
            "鼠標": "滑鼠",
            "优盘": "隨身碟",
            "優盤": "隨身碟",
            "U盘": "隨身碟",
            "U盤": "隨身碟",

            # 量詞/稱呼
            "质量": "品質",
            "質量": "品質",

            # 口語
            "特别": "特別",  # 已經一樣，但確保
            "挺好的": "蠻好的",
            "贼好": "超好",
            "賊好": "超好",
        }

    def replace_vocab(self, text: str) -> str:
        """台灣詞彙替換"""
        if not self.config.enable_vocab_replace:
            return text

        for mainland, taiwan in self.vocab_map.items():
            text = text.replace(mainland, taiwan)

        return text

    # ═══════════════════════════════════════════
    #  3. 捲舌音處理
    # ═══════════════════════════════════════════

    def _init_retroflexion_map(self):
        """
        捲舌音軟化策略。

        大陸普通話的捲舌音（zh, ch, sh, r）在台灣國語中弱化：
        - 不是完全變成平舌（z, c, s），而是介於之間
        - 某些字的捲舌/平舌在台灣是約定俗成的

        策略：
        - 某些常用字直接標註平舌拼音
        - 其他保留，讓模型自行處理（過度修改反而不自然）

        格式：字 → (台灣拼音標註)
        IndexTTS2 支援行內拼音：在文字後面寫 pin1yin1 格式
        """
        # 台灣國語中明確不捲舌（或弱捲舌）的常用字
        # 格式：{字: 台灣常用拼音}
        # 注意：這些是「台灣人普遍這樣發音」的字，不是全部
        self.retroflexion_softened = {
            # ── zh → z 類 ──
            # 「知」「只」「之」等在台灣口語中常弱化
            # 但 IndexTTS2 的拼音注入是 pinyin+tone 格式

            # ── sh → s 類 ──
            "什": "shen2",   # 什麼 → 台灣常聽到偏 sénme
            "是": "shi4",    # 「是」保留，太基本了不改

            # ── r → l/日 類 ──
            # 台灣「日」常讀偏 li4（但不完全）
            # 「人」在台灣偏 lén（很口語）
            # 但這些太激進了，先不改

            # ── 兒化相關 ──
            # 「哪兒」→「哪裡」已由詞彙替換處理
            # 「這兒」→「這裡」已由詞彙替換處理
        }

        # 更安全的做法：針對特定搭配做替換
        self.retroflexion_word_map = {
            "哪兒": "哪裡",
            "哪儿": "哪裡",
            "這兒": "這裡",
            "这儿": "這裡",
            "那兒": "那裡",
            "那儿": "那裡",
            "一會兒": "一下",
            "一会儿": "一下",
            "好玩兒": "好玩",
            "好玩儿": "好玩",
            "沒事兒": "沒事",
            "没事儿": "沒事",
            "有點兒": "有點",
            "有点儿": "有點",
        }

    def soften_retroflexion(self, text: str) -> str:
        """捲舌音軟化處理"""
        if not self.config.enable_retroflexion_soften:
            return text

        # 詞級替換（最安全）
        for mainland, taiwan in self.retroflexion_word_map.items():
            text = text.replace(mainland, taiwan)

        # 字級拼音注入（更激進，目前保守不啟用大部分）
        if self.config.retroflexion_mode == "annotate":
            for char, pinyin in self.retroflexion_softened.items():
                # 只在字獨立出現時標註（避免影響已有拼音標註）
                # IndexTTS2 pinyin 格式：直接寫 pin1yin1
                pass  # 目前跳過字級標註，太激進

        return text

    # ═══════════════════════════════════════════
    #  4. 聲調微調
    # ═══════════════════════════════════════════

    def _init_tone_adjustments(self):
        """
        台灣 vs 大陸的聲調使用差異：

        1. 輕聲使用頻率不同
           - 大陸普通話大量使用輕聲（第5聲）
           - 台灣國語較少用輕聲，很多大陸讀輕聲的字在台灣讀原調
           例如：「東西」dōngxi(大陸) vs dōngxī(台灣)
                「時候」shíhou(大陸) vs shíhòu(台灣)

        2. 部分字的聲調不同
           例如：「垃圾」lājī(大陸) vs lèsè(台灣)
                「企業」qǐyè(大陸) vs qìyè(台灣)

        策略：透過拼音注入來調整特定字的聲調
        """
        # 台灣常見的不同讀音（字 → 台灣拼音）
        # 這些是明確的台灣標準讀音差異
        self.tone_overrides = {
            # 格式：(前後文匹配, 字, 台灣拼音)
            # 用詞組匹配避免誤傷

            # 垃圾：台灣讀 lèsè，大陸讀 lājī
            "垃圾": ("le4", "se4"),  # 整個詞替換成拼音
            "垃": "le4",
            "圾": "se4",

            # 企業：台灣 qì，大陸 qǐ
            # "企": "qi4",  # 先不改，爭議較大

            # 液體：台灣 yì，大陸 yè
            # "液": "yi4",  # 先不改

            # 和：台灣常讀 hàn（文讀）
            # "和": "han4",  # 太激進，不改
        }

        # 輕聲恢復表：大陸讀輕聲但台灣讀原調的詞
        # 格式：{詞: [(字, 台灣拼音), ...]}
        self.neutral_tone_restore = {
            "東西": [("西", "xi1")],     # dōngxī 不是 dōngxi
            "东西": [("西", "xi1")],
            "時候": [("候", "hou4")],    # shíhòu 不是 shíhou
            "时候": [("候", "hou4")],
            "地方": [("方", "fang1")],   # dìfāng 不是 dìfang
            "漂亮": [("亮", "liang4")],  # piàoliang 不是 piàoliang5
            "先生": [("生", "sheng1")],  # xiānshēng 不是 xiānsheng
            "衣服": [("服", "fu2")],     # yīfú 不是 yīfu
            "客气": [("气", "qi4")],     # kèqì 不是 kèqi
            "客氣": [("氣", "qi4")],
            "喜欢": [("欢", "huan1")],   # xǐhuān 不是 xǐhuan
            "喜歡": [("歡", "huan1")],
            "明白": [("白", "bai2")],    # míngbái 不是 míngbai
            "便宜": [("宜", "yi2")],     # piányí 不是 piányi
        }

    def adjust_tones(self, text: str) -> str:
        """聲調微調：恢復台灣讀法的聲調"""
        if not self.config.enable_tone_adjust:
            return text

        # 1. 特殊讀音替換（整詞）
        if "垃圾" in text:
            text = text.replace("垃圾", "le4se4")
        if "垃圾" in text:  # 繁體
            text = text.replace("垃圾", "le4se4")

        # 2. 輕聲恢復
        for word, adjustments in self.neutral_tone_restore.items():
            if word in text:
                for char, pinyin in adjustments:
                    # 找到詞中的目標字，在其後面注入拼音
                    # IndexTTS2 格式：直接把字替換成拼音
                    # 但要小心——不能破壞其他部分
                    # 策略：把整個詞中的目標字替換成拼音
                    word_modified = word.replace(char, pinyin)
                    text = text.replace(word, word_modified)

        return text

    # ═══════════════════════════════════════════
    #  主處理流程
    # ═══════════════════════════════════════════

    def process(self, text: str) -> str:
        """
        完整的台灣腔前處理流程。

        處理順序很重要：
        1. 詞級替換（兒化詞、捲舌詞）→ 最安全
        2. 兒化音去除 → 去除剩餘的兒化後綴
        3. 詞彙替換 → 台灣用語
        4. 聲調微調 → 拼音注入
        """
        original = text

        # Step 1: 捲舌詞級替換
        text = self.soften_retroflexion(text)

        # Step 2: 去兒化音
        text = self.remove_erhua(text)

        # Step 3: 台灣詞彙替換
        text = self.replace_vocab(text)

        # Step 4: 聲調微調
        text = self.adjust_tones(text)

        if self.config.verbose and text != original:
            print(f"  [TW] '{original}'")
            print(f"     → '{text}'")

        return text


# ═══════════════════════════════════════════
#  測試
# ═══════════════════════════════════════════

def run_tests():
    """測試各個處理模組"""
    preprocessor = TaiwanAccentPreprocessor(
        TaiwanPreprocessConfig(verbose=True)
    )

    test_cases = [
        # 兒化音
        ("那個小孩兒真的很厲害", "去兒化"),
        ("花兒開了", "去兒化"),
        ("女兒很開心", "保留「女兒」"),
        ("兒子回來了", "保留「兒子」"),
        ("鳥兒在唱歌", "去兒化"),

        # 捲舌詞替換
        ("你在哪兒？", "哪兒→哪裡"),
        ("這兒很好玩兒", "這兒→這裡，好玩兒→好玩"),
        ("等一會兒", "一會兒→一下"),

        # 台灣詞彙
        ("我搭出租車去地鐵站", "詞彙替換"),
        ("這個軟件需要更新", "軟件→軟體"),
        ("請觀看這個視頻", "視頻→影片"),

        # 聲調微調
        ("這個東西多少錢", "東西 輕聲恢復"),
        ("什麼時候回來", "時候 輕聲恢復"),
        ("請把垃圾丟掉", "垃圾 讀音替換"),
        ("她很漂亮", "漂亮 輕聲恢復"),
        ("你喜歡嗎", "喜歡 輕聲恢復"),

        # 混合
        ("你在哪兒呢？那個小孩兒搭出租車去買東西了", "混合測試"),
        ("知道為什麼鴨嘴獸泰瑞都不會被認出來嗎", "BreezyVoice 預設測試句"),
    ]

    print("=" * 60)
    print("  Taiwan Accent Preprocessor 測試")
    print("=" * 60)
    print()

    for text, description in test_cases:
        print(f"[{description}]")
        result = preprocessor.process(text)
        print()

    print("=" * 60)
    print("  測試完成")
    print("=" * 60)


if __name__ == "__main__":
    run_tests()
