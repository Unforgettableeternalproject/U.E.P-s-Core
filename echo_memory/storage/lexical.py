"""
LexicalIndex - Episode 的 lexical（BM25）檢索路

這是「檢索排序」的第二條路，與向量路以 RRF 融合；不參與任何
「要不要存／salience／抽取」的決策（CLAUDE.md 架構禁令）。

切詞（不依賴 jieba）：
  - CJK 連續字元：單字 + 相鄰字元 bigram
  - 英數連續字元：整段小寫詞（NFKC 正規化後）
  - 其餘視為分隔符
單字與單一英數字元屬於「弱詞」：權重較低，且命中者必須同時命中至少一個
「強詞」（bigram／多字元英數詞）才算有效，避免常用單字造成雜訊。

索引只存記憶體，可隨時由 episodes 重建；以每篇文件的內容雜湊差異增量同步，
因此不需要與每個寫入點掛鉤，寫入後的下一次查詢只重算變動的文件。
"""

from __future__ import annotations

import math
import sys
import threading
import unicodedata
from array import array
from typing import Iterable

# BM25 參數（Lucene 風格 idf，不會出現負值）
BM25_K1 = 1.2
BM25_B = 0.75
# 弱詞（單字）權重
WEAK_TERM_WEIGHT = 0.3


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3400 <= cp <= 0x4DBF      # CJK Ext A
        or 0x4E00 <= cp <= 0x9FFF   # CJK Unified
        or 0xF900 <= cp <= 0xFAFF   # Compatibility
        or 0x20000 <= cp <= 0x2FFFF  # Ext B+
        or 0x3040 <= cp <= 0x30FF   # 假名
        or 0xAC00 <= cp <= 0xD7AF   # 韓文
    )


def tokenize(text: str) -> list[str]:
    """切成詞列表（含重複，供詞頻計算）。"""
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    cjk_run: list[str] = []
    word: list[str] = []

    def flush_cjk() -> None:
        if cjk_run:
            tokens.extend(cjk_run)
            tokens.extend(cjk_run[i] + cjk_run[i + 1] for i in range(len(cjk_run) - 1))
            cjk_run.clear()

    def flush_word() -> None:
        if word:
            tokens.append("".join(word))
            word.clear()

    for ch in text:
        if _is_cjk(ch):
            flush_word()
            cjk_run.append(ch)
        elif ch.isalnum():
            flush_cjk()
            word.append(ch)
        else:
            flush_cjk()
            flush_word()
    flush_cjk()
    flush_word()
    return tokens


def is_weak_term(term: str) -> bool:
    return len(term) == 1


def episode_lexical_text(ep) -> str:
    """Episode 進入 lexical 索引的文字：content_summary + user_input + agent_response。

    agent_response 依引擎契約只含已交付內容；content_summary 若只是
    user/agent 前綴的截斷則省略，避免重複計次。
    """
    user = ep.user_input or ""
    agent = ep.agent_response or ""
    summary = (ep.content_summary or "").strip()
    combined = f"{user} {agent}".strip()
    if summary and not combined.startswith(summary):
        return f"{summary}\n{user}\n{agent}"
    return f"{user}\n{agent}"


def has_term_overlap(query: str, text: str) -> bool:
    """query 的強詞是否出現在 text（無強詞時退回弱詞）。供未交付內容的二次檢查。"""
    q_terms = set(tokenize(query))
    if not q_terms:
        return False
    strong = {t for t in q_terms if not is_weak_term(t)}
    wanted = strong or q_terms
    return bool(wanted & set(tokenize(text)))


# 語料太小時 df 統計不可靠，不套用高頻詞剔除
MIN_DOCS_FOR_DF_FILTER = 30
_TF_CAP = 0xFFFF


class LexicalIndex:
    """BM25 索引持有者：以文件內容雜湊差異增量維護 postings。

    索引只存記憶體，可隨時由 episodes 重建。記憶體結構刻意精簡：
      - term 以 sys.intern 共用；postings 為 term -> array('Q')，每筆 (ord << 16 | tf)；
      - 每篇只保存 hash、詞數與 unique term 元組（刪除時用），不保存詞頻 Counter。
    並行：_lock 只保護索引結構（毫秒級）；tokenize 在 _lock 之外完成，
    由 _build_lock（非阻塞）確保同時只有一個執行緒在建索引，其他查詢直接用舊索引。
    呼叫端傳入的 (id, text) 快照在它自己的 _mem_lock 內取得；此處不做任何 I/O。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._ord_of: dict[str, int] = {}
        self._id_of: dict[int, str] = {}
        self._hash: dict[int, int] = {}
        self._len: dict[int, int] = {}
        self._terms: dict[int, tuple[str, ...]] = {}
        self._postings: dict[str, array] = {}
        self._total_len = 0
        self._next_ord = 0

    # ── 結構維護（呼叫端須持有 _lock）──

    def _remove_locked(self, doc_id: str) -> None:
        o = self._ord_of.pop(doc_id)
        del self._id_of[o]
        for term in self._terms.pop(o):
            plist = self._postings.get(term)
            if plist is None:
                continue
            kept = array("Q", (e for e in plist if (e >> 16) != o))
            if kept:
                self._postings[term] = kept
            else:
                del self._postings[term]
        self._total_len -= self._len.pop(o)
        del self._hash[o]

    def _add_locked(self, doc_id: str, h: int, counter: dict, length: int) -> None:
        o = self._next_ord
        self._next_ord += 1
        self._ord_of[doc_id] = o
        self._id_of[o] = doc_id
        for term, tf in counter.items():
            plist = self._postings.get(term)
            if plist is None:
                plist = self._postings[term] = array("Q")
            plist.append((o << 16) | min(tf, _TF_CAP))
        self._terms[o] = tuple(counter)
        self._len[o] = length
        self._total_len += length
        self._hash[o] = h

    @staticmethod
    def _prepare(text: str) -> tuple[dict, int]:
        tokens = tokenize(text)
        counter: dict[str, int] = {}
        for t in tokens:
            t = sys.intern(t)
            counter[t] = counter.get(t, 0) + 1
        return counter, len(tokens)

    def _sync(self, docs: list[tuple[str, str]]) -> None:
        current = {doc_id: (hash(text), text) for doc_id, text in docs}
        with self._lock:
            known = {self._id_of[o]: self._hash[o] for o in self._id_of}
        removed = [d for d in known if d not in current]
        changed = [d for d, (h, _t) in current.items() if known.get(d) != h]
        if not removed and not changed:
            return
        if not self._build_lock.acquire(blocking=False):
            return  # 別的執行緒正在建索引：這次用現有（可能稍舊的）索引
        try:
            # 鎖外做最重的 tokenize；之後單次短鎖套用
            prepared = {d: (current[d][0], *self._prepare(current[d][1])) for d in changed}
            with self._lock:
                for d in removed:
                    if d in self._ord_of:
                        self._remove_locked(d)
                for d, (h, counter, length) in prepared.items():
                    if d in self._ord_of:
                        self._remove_locked(d)
                    self._add_locked(d, h, counter, length)
        finally:
            self._build_lock.release()

    def query(
        self,
        docs: list[tuple[str, str]],
        query: str,
        top_n: int,
        min_score: float = 0.0,
        rel_min: float = 0.0,
        max_df: float = 1.0,
        min_coverage: float = 0.0,
    ) -> list[tuple[str, float]]:
        """同步索引到 docs 後做 BM25 搜尋，回傳 [(doc_id, score)]（分數遞減）。

        min_score 為絕對下限；rel_min 為相對最高分的比例下限（0 關閉）。
        max_df：df 占文件數比例超過此值的詞視為無鑑別力而剔除（語料統計，非關鍵字清單；
        文件數不足 MIN_DOCS_FOR_DF_FILTER 時不套用）。
        min_coverage：文件須命中「有效強詞」（存在於索引且未被 df 剔除）的至少此比例。
        """
        self._sync(docs)
        with self._lock:
            return self._search(query, top_n, min_score, rel_min, max_df, min_coverage)

    def _search(
        self, query: str, top_n: int, min_score: float, rel_min: float,
        max_df: float, min_coverage: float,
    ) -> list[tuple[str, float]]:
        n_docs = len(self._ord_of)
        if n_docs == 0 or top_n <= 0:
            return []
        terms = set(tokenize(query))
        if not terms:
            return []
        has_strong_query = any(not is_weak_term(t) for t in terms)
        df_filter = n_docs >= MIN_DOCS_FOR_DF_FILTER and max_df < 1.0

        scores: dict[int, float] = {}
        matched_strong: dict[int, int] = {}
        n_strong_eff = 0
        avgdl = (self._total_len / n_docs) or 1.0
        for term in terms:
            plist = self._postings.get(term)
            if not plist:
                continue
            n_t = len(plist)
            if df_filter and n_t / n_docs > max_df:
                continue
            weak = is_weak_term(term)
            if not weak:
                n_strong_eff += 1
            weight = WEAK_TERM_WEIGHT if weak else 1.0
            idf = math.log(1.0 + (n_docs - n_t + 0.5) / (n_t + 0.5))
            for e in plist:
                o, tf = e >> 16, e & _TF_CAP
                norm = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * self._len[o] / avgdl)
                scores[o] = scores.get(o, 0.0) + weight * idf * tf * (BM25_K1 + 1.0) / norm
                if not weak:
                    matched_strong[o] = matched_strong.get(o, 0) + 1

        if has_strong_query and n_strong_eff == 0:
            return []  # 強詞全被剔除或不存在：只剩弱詞/高頻詞，不算有效命中
        need = math.ceil(min_coverage * n_strong_eff - 1e-9) if n_strong_eff else 0
        ranked = [
            (o, sc) for o, sc in scores.items()
            if sc >= min_score
            and (matched_strong.get(o, 0) >= max(need, 1) if has_strong_query else True)
        ]
        ranked.sort(key=lambda x: (-x[1], x[0]))
        if ranked and rel_min > 0:
            floor = ranked[0][1] * rel_min
            ranked = [r for r in ranked if r[1] >= floor]
        return [(self._id_of[o], sc) for o, sc in ranked[:top_n]]


def build_docs(episodes: Iterable) -> list[tuple[str, str]]:
    return [(ep.id, episode_lexical_text(ep)) for ep in episodes]
