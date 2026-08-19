"""IndexTTS 2.5 文本前處理輔助 — 從上游 infer_v2_5.py 移植的純函式集。

包含：發音標注轉換、語言別大小寫/正規化規則、token 預算分段。
tokenizer 由呼叫端注入（tiktoken Encoding），本模組不持有狀態。
"""
import re
from typing import Callable

PRONUNCIATION_ANNOTATION_PATTERN = re.compile(r'<([^|>\n]+)\|([^>\n]+)>')
SPLIT_PROTECTED_PATTERN = re.compile(r'<\|SPECIAL_TOKEN_\d+\|>.*?<\|SPECIAL_TOKEN_\d+\|>')
SPECIAL_TOKEN_CASE_PATTERN = re.compile(r'<\|([^|]+)\|>')
_SENTENCE_BOUNDARY = re.compile(r'(?<=[，。！？、；：,\.!\?;:\n])')


def _is_kana(s: str) -> bool:
    return bool(re.fullmatch(r'[぀-ゟ]+', s) or re.fullmatch(r'[゠-ヿ]+', s))


def apply_pronunciation_annotations(text: str) -> str:
    """處理發音標注 <文字|發音> → 特殊 token 包裹（上游 infer_v2_5 同款）。

    - 中文 → <|SPECIAL_TOKEN_2|>發音<|SPECIAL_TOKEN_2|>
    - 英文 → <|SPECIAL_TOKEN_1|>發音<|SPECIAL_TOKEN_1|>
    - 假名發音直接保留
    """
    def _replace(match: "re.Match") -> str:
        word = match.group(1)
        pronunciation = match.group(2).upper()
        if _is_kana(match.group(2)):
            return f' {match.group(2)} '
        token = 'SPECIAL_TOKEN_2' if re.search(r'[一-鿿]', word) else 'SPECIAL_TOKEN_1'
        return f'<|{token}|>{pronunciation}<|{token}|>'

    return PRONUNCIATION_ANNOTATION_PATTERN.sub(_replace, text)


def apply_lang_case_rules(text: str, lang: str) -> str:
    """語言別大小寫規則（上游：zh/en/ja 全小寫、es 全大寫），並還原特殊 token 為大寫。"""
    lang = lang.lower()
    if lang in ('ja', 'zh', 'zhen', 'en', 'zh/en', 'en/zh'):
        text = text.lower()
    elif lang == 'es':
        text = text.upper()
    return SPECIAL_TOKEN_CASE_PATTERN.sub(lambda m: f'<|{m.group(1).upper()}|>', text)


def _split_atomic_pieces(text: str) -> list:
    """切出受保護片段（特殊 token 包裹的發音區塊不可被分段截斷）。"""
    pieces, pos = [], 0
    for m in SPLIT_PROTECTED_PATTERN.finditer(text):
        if m.start() > pos:
            pieces.append((text[pos:m.start()], False))
        pieces.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        pieces.append((text[pos:], False))
    return pieces


def split_text_by_token_budget(
    text: str,
    token_len: Callable[[str], int],
    max_tokens: int,
    capacity: int,
    lang_prefix: str = "",
) -> list:
    """按 token 預算分段（上游 split_text_by_tokens 移植版）。

    Args:
        text: 已正規化的文本
        token_len: 計算文本 token 數的 callable（tiktoken encode 長度）
        max_tokens: 每段 token 上限
        capacity: GPT text position embedding 容量
        lang_prefix: 語言前綴（佔用預算）
    """
    budget = min(max_tokens, capacity - 2) - token_len(lang_prefix)
    budget = max(1, budget)
    if token_len(text) <= budget:
        return [text]

    chunks = []
    for piece, atomic in _split_atomic_pieces(text):
        if atomic:
            chunks.append(piece)
            continue
        for part in _SENTENCE_BOUNDARY.split(piece):
            if not part:
                continue
            if token_len(part) <= budget:
                chunks.append(part)
                continue
            current = ""
            for ch in part:
                if current and token_len(current + ch) > budget:
                    chunks.append(current)
                    current = ch
                else:
                    current += ch
            if current:
                chunks.append(current)

    segments, current = [], ""
    for chunk in chunks:
        if current and token_len(current + chunk) > budget:
            segments.append(current)
            current = chunk
        else:
            current += chunk
    if current:
        segments.append(current)
    return segments or [text]
