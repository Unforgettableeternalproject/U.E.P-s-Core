#!/usr/bin/env python3
"""Phase 2 第一塊：檢索。先驗證輕量檢索夠不夠用。

## 為什麼從 BM25 開始，而不是直接上向量檢索

檢索時機決定了可用的技術：

- `SessionStart`：此時**還沒有 query**（使用者還沒開口），只能靠 repo/branch 粗召回
- `UserPromptSubmit`：有 query，但 timeout 只有 **30 秒**，而且每次都是新程序

sentence-transformers 的冷啟動在 30 秒的預算裡是致命的——除非改成常駐 daemon，
而那是 Phase 4 的工程。所以順序應該是：先量輕量檢索的實際效果，
**夠用的話整個依賴與 daemon 的問題都不存在**；不夠用才有理由付那個代價。

## 中文怎麼斷詞

沒有詞典可用（零依賴），所以中文走 **bigram**，英數走單字。
這是無詞典中文檢索的標準做法，召回略鬆但不需要任何模型。

## ground truth 從語料來，不另外出題

每條 concept 都記著自己來自哪兩輪。**那一輪的使用者輸入就是天然的 query**，
正確答案就是那條 concept——這正是「同樣的任務再來一次，記憶該不該浮出來」。

刻意不用 `probe` 當 query：probe 是蒸餾階段為了測那條記憶量身寫的，
題目與記憶高度對齊，拿它測檢索會得到一個漂亮但沒有意義的數字
（`phase15-evaluation.md` 已經吃過這個虧）。

## 用法

    python retrieve.py --eval          # 量 recall@k 與 MRR
    python retrieve.py --query "..."   # 手動查一筆看看
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from hook_stop import DEFAULT_EPISODE_DIR, load_deduped  # noqa: E402
from transcript import ORIGIN_HUMAN  # noqa: E402

WORK_DIR = DEFAULT_EPISODE_DIR.parent
DEFAULT_CONCEPT_PATH = WORK_DIR / "concepts.json"
CONTROL_CONCEPT_PATH = WORK_DIR / "control_concepts.json"

# query 短於這個長度就不列入評測——「這個可以，換下一個」本來就不該召回任何東西，
# 拿它當測試案例只會壓低分數而不說明任何事
MIN_QUERY_CHARS = 20

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*|\d+")


def tokenize(text: str) -> list[str]:
    """中文 bigram + 英數單字。

    英文一併保留小寫原形：程式碼識別字（`useZoneRouter`、`companyid`）是這個場景
    最強的訊號，它們在 query 與 concept 裡會原樣出現。
    """
    if not text:
        return []
    tokens = [match.group(0).lower() for match in _WORD.finditer(text)]

    # 連續的中文字元切 bigram；單獨一個中文字也保留，否則單字詞會整個消失
    for run in re.findall(r"[一-鿿]+", text):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


class BM25:
    """標準 BM25。純標準庫，冷啟動成本只有讀檔與建索引。"""

    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents = documents
        self.doc_count = len(documents)
        self.doc_lengths = [len(d) for d in documents]
        self.avg_length = (sum(self.doc_lengths) / self.doc_count) if self.doc_count else 0.0
        self.term_frequencies = [Counter(d) for d in documents]

        document_frequency: Counter[str] = Counter()
        for doc in documents:
            document_frequency.update(set(doc))
        # 加 0.5 的平滑；max(..., 1e-9) 避免出現在過半文件的詞拿到負分。
        # 在 73 條的小索引裡「專案」這種詞很容易超過半數，不夾住的話會反過來扣分
        self.idf = {
            term: max(math.log((self.doc_count - freq + 0.5) / (freq + 0.5) + 1.0), 1e-9)
            for term, freq in document_frequency.items()
        }

    def score(self, query_tokens: list[str], index: int) -> float:
        frequencies = self.term_frequencies[index]
        length = self.doc_lengths[index]
        total = 0.0
        for term in query_tokens:
            if term not in frequencies:
                continue
            freq = frequencies[term]
            denominator = freq + self.k1 * (1 - self.b + self.b * length / (self.avg_length or 1))
            total += self.idf.get(term, 0.0) * freq * (self.k1 + 1) / denominator
        return total

    def rank(self, query: str) -> list[tuple[int, float]]:
        tokens = tokenize(query)
        scored = [(i, self.score(tokens, i)) for i in range(self.doc_count)]
        scored.sort(key=lambda pair: -pair[1])
        return scored


OLLAMA_URL = "http://localhost:11434/api/embed"
OLLAMA_MODEL = "bge-m3"  # 多語言，適合這份中英混合語料


def ollama_embed(texts: list[str], model: str = OLLAMA_MODEL, timeout: float = 120.0) -> list[list[float]]:
    """呼叫 ollama 取 embedding。

    **走 HTTP 而不是 sentence-transformers 是關鍵決定。** 自行載入模型的話，
    冷啟動幾秒起跳，而 `UserPromptSubmit` 只有 30 秒 timeout 且每次都是新程序——
    等於每一輪對話都要付一次模型載入。ollama 本身就是常駐服務，
    模型留在它的記憶體裡，這邊只是一次 HTTP 往返，也**不需要自己做 daemon**。

    只用標準庫，spike 的零依賴前提保住了（hook 跑在系統 Python，不保證有 numpy）。
    """
    import urllib.error
    import urllib.request

    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    request = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))["embeddings"]
    except (urllib.error.URLError, KeyError, TimeoutError) as exc:
        raise RuntimeError(f"ollama 呼叫失敗（{model}）: {exc}") from exc


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


class VectorIndex:
    """語意檢索，用來量 lexical gap 到底有多大。

    BM25 完全召不到的那些案例（分數 0），失敗原因是 query 與 statement 詞彙零重疊，
    但語意上有關聯——「反查操作者名稱」對「反查 users 要帶 companyid」。
    那是詞彙比對的天花板，不是調參能解決的。
    """

    # query embedding 跨 index 實例共用：評測時同一批 query 會被多個變體重複查詢，
    # 不快取的話光是重編碼就佔掉大部分時間（62 query × 323 ms × 變體數）
    _query_cache: dict[tuple[str, str], list[float]] = {}

    def __init__(self, documents: list[str], model: str = OLLAMA_MODEL):
        self.model = model
        self.matrix = [_normalize(v) for v in ollama_embed(documents, model)]

    def embed_query(self, query: str) -> list[float]:
        key = (self.model, query)
        cached = VectorIndex._query_cache.get(key)
        if cached is None:
            cached = _normalize(ollama_embed([query], self.model)[0])
            VectorIndex._query_cache[key] = cached
        return cached

    def rank(self, query: str) -> list[tuple[int, float]]:
        vector = self.embed_query(query)
        # 都正規化過，內積就是 cosine。池子只有幾十條，純 Python 足夠快
        scores = [sum(a * b for a, b in zip(row, vector)) for row in self.matrix]
        return sorted(enumerate(scores), key=lambda pair: -pair[1])


def load_pool(paths: list[Path]) -> list[dict[str, Any]]:
    """把所有 concept 併成一個檢索池。

    對照組的 18 條也放進來：池子越小 recall 越虛高，
    22 條裡取 3 條的隨機基線就有 14%，那個數字說明不了任何事。
    """
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        for concept in json.loads(path.read_text(encoding="utf-8")):
            key = " ".join((concept.get("statement") or "").split()).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            pool.append(concept)
    return pool


def document_text(concept: dict[str, Any], *, with_cue: bool) -> str:
    """要拿去索引的文字。

    ``with_cue`` 決定索引「記憶的內容」還是「記憶的提取線索」。

    這是整個檢索設計的核心分歧。statement 是**答案**的措辭
    （「多租戶鐵律：反查 users 要帶 companyid」），但 query 是**問題**的措辭
    （「建立者顯示 ID，我想看到操作者名稱」）。拿問題去比對答案是
    asymmetric retrieval，連向量都吃癟——實測這一題 BM25 與向量雙雙落榜。

    這正是 Echo Memory 那個名字的意思：**Ecphory 是提取線索**。
    記憶靠 cue 被喚起，不是靠內容本身被搜到。

    這裡先用 ``probe`` 當 cue 的代理來驗證方向。probe 是蒸餾階段為了測試而寫的
    「什麼開發情境會需要這條知識」，形狀正確但目的不同——
    方向對的話，蒸餾階段應該正式產一個 ``cue`` 欄位，而不是借用 probe。

    注意這**不是**拿 probe 當 query 作弊：query 一律來自真實語料，
    probe 只出現在索引側。
    """
    parts = [concept.get("statement", ""), concept.get("scope") or ""]
    if with_cue:
        parts.append(concept.get("probe") or "")
    return " ".join(p for p in parts if p)


def build_index(pool: list[dict[str, Any]], *, with_cue: bool = False) -> BM25:
    return BM25([tokenize(document_text(c, with_cue=with_cue)) for c in pool])


def build_ground_truth(pool: list[dict[str, Any]], episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每條 concept 配一個來自真實語料的 query。"""
    by_key = {(e.get("prompt_id"), e.get("turn_index")): e for e in episodes}

    cases: list[dict[str, Any]] = []
    for index, concept in enumerate(pool):
        source_turns = concept.get("source_turns") or []
        if not source_turns:
            continue
        # 取第一輪：那是引發整件事的任務描述。第二輪往往只是「這個可以，換下一個」
        first = source_turns[0]
        episode = by_key.get((first[0], first[1] if len(first) > 1 else 0))
        if episode is None or episode.get("origin") != ORIGIN_HUMAN:
            continue
        query = (episode.get("user_text") or "").strip()
        if len(query) < MIN_QUERY_CHARS:
            continue
        cases.append({
            "concept_index": index,
            "concept_id": concept.get("id"),
            "scope": concept.get("scope"),
            "query": query,
            "statement": concept.get("statement"),
        })
    return cases


def reciprocal_rank_fusion(rankings: list[list[tuple[int, float]]], k: int = 60) -> list[tuple[int, float]]:
    """RRF 融合多組排序。

    用 RRF 而不是加權相加分數：BM25 的分數與 cosine 完全不同尺度，
    要相加就得先正規化，而正規化係數本身又是一組要調的參數。
    RRF 只看名次，沒有這個問題。
    """
    fused: dict[int, float] = {}
    for ranking in rankings:
        for position, (doc_index, score) in enumerate(ranking):
            if score <= 0:
                continue
            fused[doc_index] = fused.get(doc_index, 0.0) + 1.0 / (k + position + 1)
    return sorted(fused.items(), key=lambda pair: -pair[1])


def evaluate_variant(
    pool: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    ks: tuple[int, ...],
    *,
    ranker,
    scope_filter: bool,
) -> tuple[list[int], float]:
    """跑一種檢索設定，回傳 (每個 case 的命中排名, 平均候選池大小)。

    ``scope_filter`` 模擬真實情境：hook 一定知道當前在哪個 repo，
    跨專案的記憶不必參與排序。這是免費且確定的訊號，
    比任何相似度都可靠——不用白不用。
    """
    ranks: list[int] = []
    pool_sizes: list[int] = []

    for case in cases:
        ranked = ranker(case["query"])
        if scope_filter:
            # 同 repo，或標為跨專案通用（scope 為空）的才留下
            ranked = [
                (i, s) for i, s in ranked
                if pool[i].get("scope") in (case["scope"], None, "")
            ]
        pool_sizes.append(len(ranked))
        position = next(
            (i for i, (doc_index, score) in enumerate(ranked)
             if doc_index == case["concept_index"] and score > 0),
            None,
        )
        ranks.append(position + 1 if position is not None else -1)

    return ranks, (sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0)


def _report(name: str, ranks: list[int], cases: int, ks: tuple[int, ...], avg_pool: float) -> None:
    out = sys.stderr
    print(f"\n=== {name} ===（平均候選池 {avg_pool:.0f} 條）", file=out)
    print(f"  完全召不到: {ranks.count(-1)}/{cases}", file=out)
    for k in ks:
        hits = sum(1 for r in ranks if 0 < r <= k)
        baseline = min(k / avg_pool, 1.0) if avg_pool else 0
        lift = (hits / cases) / baseline if baseline else 0
        print(f"  recall@{k:<2d}: {hits:3d}/{cases} = {hits / cases:6.1%}"
              f"   （隨機基線 {baseline:.1%}，提升 {lift:.1f}x）", file=out)
    found = [r for r in ranks if r > 0]
    if found:
        mrr = sum(1 / r for r in found) / cases
        print(f"  MRR      : {mrr:.3f}", file=out)


def evaluate(pool: list[dict[str, Any]], cases: list[dict[str, Any]], ks: tuple[int, ...],
             *, use_vector: bool = True) -> int:
    import time

    out = sys.stderr
    print(f"[retrieve] 檢索池 {len(pool)} 條，評測 {len(cases)} 個 query", file=out)

    bm25 = build_index(pool)
    variants: list[tuple[str, Any, bool]] = [
        ("BM25", bm25.rank, False),
        ("BM25 + scope", bm25.rank, True),
    ]

    bm25_cue = build_index(pool, with_cue=True)
    variants.append(("BM25 + scope + cue", bm25_cue.rank, True))

    if use_vector:
        started = time.perf_counter()
        vector = VectorIndex([document_text(c, with_cue=False) for c in pool])
        index_seconds = time.perf_counter() - started

        started = time.perf_counter()
        vector.rank(cases[0]["query"])
        query_ms = (time.perf_counter() - started) * 1000
        print(f"  向量索引：{len(pool)} 條建索引 {index_seconds:.1f}s、"
              f"單次查詢 {query_ms:.0f} ms（ollama {OLLAMA_MODEL}）", file=out)

        vector_cue = VectorIndex([document_text(c, with_cue=True) for c in pool])

        def hybrid(query: str) -> list[tuple[int, float]]:
            return reciprocal_rank_fusion([bm25.rank(query), vector.rank(query)])

        def hybrid_cue(query: str) -> list[tuple[int, float]]:
            return reciprocal_rank_fusion([bm25_cue.rank(query), vector_cue.rank(query)])

        variants += [
            ("向量", vector.rank, False),
            ("向量 + scope", vector.rank, True),
            ("向量 + scope + cue", vector_cue.rank, True),
            ("Hybrid(RRF) + scope", hybrid, True),
            ("Hybrid(RRF) + scope + cue", hybrid_cue, True),
        ]

    results: dict[str, list[int]] = {}
    for name, ranker, scoped in variants:
        ranks, avg_pool = evaluate_variant(pool, cases, ks, ranker=ranker, scope_filter=scoped)
        results[name] = ranks
        _report(name, ranks, len(cases), ks, avg_pool)

    best = max(results, key=lambda n: sum(1 for r in results[n] if 0 < r <= 5))
    misses = [c for c, r in zip(cases, results[best]) if not (0 < r <= max(ks))]
    if misses:
        print(f"\n  最佳設定（{best}）仍未進 top-{max(ks)} 的 {len(misses)} 例（前 5）：", file=out)
        for case in misses[:5]:
            print(f"    - [{case['scope']}] query: {case['query'][:70]}", file=out)
            print(f"      應召回: {case['statement'][:70]}", file=out)
    return 0


def query_once(pool: list[dict[str, Any]], text: str, top_k: int) -> int:
    index = build_index(pool)
    for rank, (doc_index, score) in enumerate(index.rank(text)[:top_k], 1):
        if score <= 0:
            break
        concept = pool[doc_index]
        print(f"{rank}. [{score:.2f}] ({concept.get('scope')}) {concept.get('statement', '')[:110]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 檢索驗證")
    parser.add_argument("--eval", action="store_true", help="量 recall@k 與 MRR")
    parser.add_argument("--query", type=str, help="手動查一筆")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--no-vector", action="store_true", help="只跑 BM25，不呼叫 ollama")
    parser.add_argument("--episode-dir", type=Path, default=DEFAULT_EPISODE_DIR)
    parser.add_argument("--concept-path", type=Path, default=DEFAULT_CONCEPT_PATH)
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    pool = load_pool([args.concept_path, CONTROL_CONCEPT_PATH])
    if not pool:
        print("[retrieve] 檢索池是空的", file=sys.stderr)
        return 1

    if args.query:
        return query_once(pool, args.query, args.top_k)

    episodes, _ = load_deduped(args.episode_dir)
    cases = build_ground_truth(pool, episodes)
    if not cases:
        print("[retrieve] 建不出 ground truth", file=sys.stderr)
        return 1
    return evaluate(pool, cases, ks=(1, 3, 5, 10), use_vector=not args.no_vector)


if __name__ == "__main__":
    sys.exit(main())
