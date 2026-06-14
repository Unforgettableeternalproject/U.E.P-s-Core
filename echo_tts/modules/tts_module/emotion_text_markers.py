"""
Emotion Text Markers — 文本層情緒增強（第二層）

不改模型，通過修改輸入文本來增強情緒表現：
- 插入語氣詞（嘆息、猶豫、怒音等）
- 調整標點符號（停頓、急促）
- 修改句式結構（短句vs長句）

設計原則：
- 語氣詞和停頓結構比標點符號更有效（實測驚嘆號對語調影響很小）
- 每種情緒有獨立的規則集，可配置
- 強度閾值控制：情緒太弱時不修改，避免中性文本被污染

使用方式：
    markers = EmotionTextMarkers()
    text = markers.apply("我今天很開心", emotion_vector, language="zh")
"""

import random
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# 情緒維度索引（與 emotion_dynamics.py 一致）
EMO_HAPPY = 0
EMO_ANGRY = 1
EMO_SAD = 2
EMO_AFRAID = 3
EMO_DISGUSTED = 4
EMO_MELANCHOLIC = 5
EMO_SURPRISED = 6
EMO_CALM = 7

EMO_NAMES = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]


@dataclass
class EmotionMarkupRule:
    """單一情緒的文本標記規則"""
    # 句首可插入的語氣詞（隨機選一個）
    prefix_particles: List[str] = field(default_factory=list)
    # 句中可插入的語氣詞
    mid_particles: List[str] = field(default_factory=list)
    # 句末可追加的語氣詞
    suffix_particles: List[str] = field(default_factory=list)
    # 停頓標記（用於句中插入）
    pause_markers: List[str] = field(default_factory=list)
    # 是否傾向短句（True = 在逗號處斷句）
    prefer_short_sentences: bool = False
    # 句末標點偏好（None = 不修改）
    end_punctuation: Optional[str] = None


# ── 中文規則庫 ──
ZH_RULES: Dict[int, EmotionMarkupRule] = {
    EMO_HAPPY: EmotionMarkupRule(
        prefix_particles=["嘿，", "哈，"],
        suffix_particles=["呢", "嘛", "啦"],
    ),
    EMO_ANGRY: EmotionMarkupRule(
        prefix_particles=["哼，", "切，"],
        mid_particles=["，哼，"],
        suffix_particles=["！"],
        prefer_short_sentences=True,
    ),
    EMO_SAD: EmotionMarkupRule(
        prefix_particles=["唉，"],
        pause_markers=["……", "——"],
        suffix_particles=["……"],
    ),
    EMO_AFRAID: EmotionMarkupRule(
        prefix_particles=["那、那個，"],
        mid_particles=["……"],
        suffix_particles=["……吧"],
        prefer_short_sentences=True,
    ),
    EMO_DISGUSTED: EmotionMarkupRule(
        prefix_particles=["嘖，"],
        suffix_particles=["。"],
    ),
    EMO_MELANCHOLIC: EmotionMarkupRule(
        prefix_particles=["唉……", "嗯……"],
        pause_markers=["……", "——"],
        suffix_particles=["呢……"],
    ),
    EMO_SURPRISED: EmotionMarkupRule(
        prefix_particles=["欸？", "什、什麼？"],
        mid_particles=["——"],
        suffix_particles=["！？"],
        prefer_short_sentences=True,
    ),
    EMO_CALM: EmotionMarkupRule(
        # 平靜不添加語氣詞，保持自然
    ),
}

# ── 英文規則庫 ──
EN_RULES: Dict[int, EmotionMarkupRule] = {
    EMO_HAPPY: EmotionMarkupRule(
        prefix_particles=["Oh, ", "Hey, "],
        suffix_particles=[", you know?", "!"],
    ),
    EMO_ANGRY: EmotionMarkupRule(
        prefix_particles=["Tch, ", "Hmph, "],
        suffix_particles=["!"],
        prefer_short_sentences=True,
    ),
    EMO_SAD: EmotionMarkupRule(
        prefix_particles=["*sigh*, "],
        pause_markers=["... ", "— "],
        suffix_particles=["..."],
    ),
    EMO_AFRAID: EmotionMarkupRule(
        prefix_particles=["W-wait, "],
        mid_particles=["... "],
        suffix_particles=["..."],
        prefer_short_sentences=True,
    ),
    EMO_DISGUSTED: EmotionMarkupRule(
        prefix_particles=["Ugh, "],
    ),
    EMO_MELANCHOLIC: EmotionMarkupRule(
        prefix_particles=["*sigh*... "],
        pause_markers=["... ", "— "],
        suffix_particles=["..."],
    ),
    EMO_SURPRISED: EmotionMarkupRule(
        prefix_particles=["Wait— ", "Huh? "],
        mid_particles=["— "],
        suffix_particles=["?!"],
        prefer_short_sentences=True,
    ),
    EMO_CALM: EmotionMarkupRule(),
}


class EmotionTextMarkers:
    """
    文本層情緒標記器。

    根據主導情緒在文本中插入語氣詞、停頓和標點調整，
    增強 TTS 的情緒表達力。
    """

    def __init__(
        self,
        enabled: bool = True,
        activation_threshold: float = 0.10,
        prefix_probability: float = 0.7,
        mid_probability: float = 0.3,
        suffix_probability: float = 0.5,
        zh_rules: Optional[Dict[int, EmotionMarkupRule]] = None,
        en_rules: Optional[Dict[int, EmotionMarkupRule]] = None,
    ):
        """
        Args:
            enabled: 是否啟用文本標記
            activation_threshold: 主導情緒低於此值時不修改文本
            prefix_probability: 插入句首語氣詞的機率
            mid_probability: 插入句中語氣詞的機率
            suffix_probability: 插入句末語氣詞的機率
            zh_rules: 中文規則覆寫
            en_rules: 英文規則覆寫
        """
        self.enabled = enabled
        self.activation_threshold = activation_threshold
        self.prefix_probability = prefix_probability
        self.mid_probability = mid_probability
        self.suffix_probability = suffix_probability
        self.zh_rules = zh_rules or ZH_RULES
        self.en_rules = en_rules or EN_RULES

    def apply(
        self,
        text: str,
        emotion_vector: List[float],
        language: str = "zh",
    ) -> str:
        """
        根據情緒向量修改文本。

        Args:
            text: 原始文本
            emotion_vector: 8D 情緒向量（emo_bias + normalize 後）
            language: 語言 ("zh" 或 "en")

        Returns:
            修改後的文本（情緒太弱時回傳原文）
        """
        if not self.enabled or not text.strip():
            return text

        if len(emotion_vector) != 8:
            return text

        # 找主導情緒
        max_val = max(emotion_vector)
        if max_val < self.activation_threshold:
            return text

        dominant_idx = emotion_vector.index(max_val)
        rules = self.zh_rules if language == "zh" else self.en_rules
        rule = rules.get(dominant_idx)

        if rule is None:
            return text

        # 按機率應用各種標記
        result = text

        # 1. 句首語氣詞
        if rule.prefix_particles and random.random() < self.prefix_probability:
            # 只在文本不是已經以語氣詞開頭時才加
            particle = random.choice(rule.prefix_particles)
            if not result.startswith(particle):
                result = particle + result

        # 2. 句中停頓（在逗號後隨機插入）
        if rule.pause_markers and random.random() < self.mid_probability:
            result = self._insert_pause(result, rule.pause_markers, language)

        # 3. 句中語氣詞（在合適的位置插入）
        if rule.mid_particles and random.random() < self.mid_probability:
            result = self._insert_mid_particle(result, rule.mid_particles, language)

        # 4. 句末語氣詞
        if rule.suffix_particles and random.random() < self.suffix_probability:
            particle = random.choice(rule.suffix_particles)
            # 移除原本的句末標點再加新的
            result = self._replace_end_punctuation(result, particle, language)

        return result

    def _insert_pause(self, text: str, markers: List[str], language: str) -> str:
        """在句中合適的位置（逗號後）插入停頓標記"""
        if language == "zh":
            # 在中文逗號後插入
            comma_positions = [m.start() for m in re.finditer(r'，', text)]
        else:
            comma_positions = [m.start() for m in re.finditer(r',\s?', text)]

        if not comma_positions:
            return text

        # 隨機選一個逗號位置插入
        pos = random.choice(comma_positions)
        marker = random.choice(markers)

        if language == "zh":
            # 替換逗號為停頓標記
            return text[:pos] + marker + text[pos + 1:]
        else:
            # 在逗號後的空格處插入
            match = re.match(r',\s?', text[pos:])
            if match:
                end = pos + match.end()
                return text[:pos] + ", " + marker + text[end:]
        return text

    def _insert_mid_particle(self, text: str, particles: List[str], language: str) -> str:
        """在句中（約 1/3~2/3 位置）插入語氣詞"""
        length = len(text)
        if length < 10:
            return text

        # 尋找 1/3 到 2/3 之間的分割點
        search_start = length // 3
        search_end = 2 * length // 3

        if language == "zh":
            # 找中文逗號或句號
            split_chars = "，、。；"
        else:
            split_chars = ",;."

        best_pos = None
        for i in range(search_start, search_end):
            if i < len(text) and text[i] in split_chars:
                best_pos = i + 1
                break

        if best_pos is None:
            return text

        particle = random.choice(particles)
        return text[:best_pos] + particle + text[best_pos:]

    def _replace_end_punctuation(self, text: str, new_ending: str, language: str) -> str:
        """替換或追加句末標點"""
        if language == "zh":
            # 移除尾部的中文標點
            stripped = text.rstrip("。！？…～~")
        else:
            stripped = text.rstrip(".!?…~")

        return stripped + new_ending


def create_emotion_text_markers(config: dict) -> EmotionTextMarkers:
    """
    從 config dict 建立 EmotionTextMarkers。

    config.yaml 預期格式：
        emotion_text_markers:
          enabled: true
          activation_threshold: 0.10
          prefix_probability: 0.7
          mid_probability: 0.3
          suffix_probability: 0.5
    """
    cfg = config.get('emotion_text_markers', {})
    return EmotionTextMarkers(
        enabled=cfg.get('enabled', True),
        activation_threshold=float(cfg.get('activation_threshold', 0.10)),
        prefix_probability=float(cfg.get('prefix_probability', 0.7)),
        mid_probability=float(cfg.get('mid_probability', 0.3)),
        suffix_probability=float(cfg.get('suffix_probability', 0.5)),
    )
