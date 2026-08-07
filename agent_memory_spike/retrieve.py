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

    方向驗證過之後，蒸餾階段已改為正式產出 ``cue`` 欄位。
    舊語料沒有 cue，退回 ``probe``——那是當初驗證這個方向時用的代理，
    形狀相近（同樣是「什麼情境會需要這條知識」），但它是為**測試**而寫的，
    專為**觸發**而寫的 cue 應該更好。

    注意這**不是**拿 probe 當 query 作弊：query 一律來自真實語料，
    cue/probe 只出現在索引側。
    """
    parts = [concept.get("statement", ""), concept.get("scope") or ""]
    if with_cue:
        parts.append(concept.get("cue") or concept.get("probe") or "")
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


def build_file_cases(pool: list[dict[str, Any]], episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """另一種 ground truth：**同一個檔案後來又被編輯**的輪次。

    前一種 ground truth 問的是「同樣的任務再來一次」，query 是使用者的話。
    這一種問的是「再次碰到這個檔案時，關於它的記憶會不會浮出來」——
    也就是 `PreToolUse` 觸發的場景，而那是 coding agent 特有的、對話系統沒有的訊號。

    刻意**排除該 concept 自己的來源輪次**：那些輪次的檔案必然重疊（concept 就是從那裡抽的），
    算進去等於拿答案當題目。

    假設的邊界：「編輯同一個檔案 → 該召回關於那個檔案的記憶」在大檔案上不一定成立，
    改的可能是完全不相干的區塊。所以這組 case 的上限也不是 100%。
    """
    cases: list[dict[str, Any]] = []
    for index, concept in enumerate(pool):
        source_files = set(concept.get("source_files") or [])
        if not source_files:
            continue
        source_keys = {(t[0], t[1] if len(t) > 1 else 0) for t in (concept.get("source_turns") or [])}

        for episode in episodes:
            if episode.get("origin") != ORIGIN_HUMAN:
                continue
            if (episode.get("prompt_id"), episode.get("turn_index")) in source_keys:
                continue
            if not (source_files & set(episode.get("files_edited") or [])):
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
                "files_edited": episode.get("files_edited") or [],
                "assistant_text": episode.get("assistant_text") or "",
            })
    return cases


def dump_precision_tasks(pool: list[dict[str, Any]], episodes: list[dict[str, Any]],
                         path: Path, sample_size: int, seed: int, top_k: int) -> int:
    """產出 precision 評估任務：每個情境配上檔案訊號召回的 top-k。

    recall 已經測過了（而且那個 ground truth 對檔案訊號有利）。**precision 才是
    還沒被驗證、也是決定 `PreToolUse` 能不能用的那一面**：碰到某個檔案時召回了幾條、
    其中幾條跟當下這輪真的在做的事有關。

    以「情境」而非「case」抽樣：同一輪會為多條 concept 各產生一個 case，
    照 case 抽會讓關聯多的輪次被重複抽到，precision 也就失真。
    """
    import random

    cases = build_file_cases(pool, episodes)
    # 同一輪（同一批 files_edited + 同一句 query）只留一個代表
    scenarios: dict[tuple[str, str], dict[str, Any]] = {}
    for case in cases:
        scenarios.setdefault((case["query"], "|".join(sorted(case["files_edited"]))), case)

    picked = list(scenarios.values())
    random.Random(seed).shuffle(picked)
    picked = picked[:sample_size]

    tasks = []
    for i, case in enumerate(picked):
        ranked = file_overlap_ranker(pool, case)
        retrieved = [
            {"id": pool[idx].get("id"), "statement": pool[idx].get("statement"),
             "overlap": int(score)}
            for idx, score in ranked[:top_k] if score > 0
        ]
        tasks.append({
            "id": f"prec-{i:03d}",
            "repo": case.get("scope"),
            "files_edited": case["files_edited"],
            "user_text": case["query"][:800],
            "assistant_text": case["assistant_text"][:1500],
            "retrieved": retrieved,
        })

    path.write_text(json.dumps({"count": len(tasks), "tasks": tasks},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(t["retrieved"]) for t in tasks)
    print(f"[retrieve] {len(tasks)} 個情境（去重自 {len(cases)} 個 case），"
          f"共召回 {total} 條，平均 {total / len(tasks):.2f} 條/次 → {path}", file=sys.stderr)
    return 0


def file_overlap_ranker(pool: list[dict[str, Any]], case: dict[str, Any]) -> list[tuple[int, float]]:
    """檔案訊號：重疊的檔案數就是分數。

    優先用 ``anchors``（這條記憶真正談論的對象），沒有才退回 ``source_files``。

    兩者的差別實測很大：``source_files`` 是「產生這條記憶那一輪碰過的所有檔案」，
    裡面多半是順手碰到的，用它做召回的 precision 只有 30%——
    抓到的常是「同一個檔案裡另一段邏輯的舊筆記」。
    真正有用的召回，共同特徵是錨點與當下要改的東西同名同源。
    """
    touched = set(case.get("files_edited") or [])
    scored = []
    for i, concept in enumerate(pool):
        anchors = concept.get("anchors") or concept.get("source_files") or []
        scored.append((i, float(len(set(anchors) & touched))))
    return sorted(scored, key=lambda pair: -pair[1])


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
        # 檔案訊號需要整個 case（要看這輪碰了哪些檔案），文字檢索只需要 query
        ranked = ranker(case) if getattr(ranker, "needs_case", False) else ranker(case["query"])
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
             *, use_vector: bool = True, dump_misses: Path | None = None) -> int:
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

    if dump_misses is not None:
        # 給診斷用：目前無法區分「檢索沒抓到」與「本來就沒有可辨識的關聯」，
        # 而這個比例決定了 recall 的天花板在哪、還值不值得繼續調
        dump_misses.write_text(
            json.dumps({"setting": best, "count": len(misses), "cases": misses},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n  未命中案例已寫出 → {dump_misses}", file=out)
    return 0


def query_once(pool: list[dict[str, Any]], text: str, top_k: int) -> int:
    index = build_index(pool)
    for rank, (doc_index, score) in enumerate(index.rank(text)[:top_k], 1):
        if score <= 0:
            break
        concept = pool[doc_index]
        print(f"{rank}. [{score:.2f}] ({concept.get('scope')}) {concept.get('statement', '')[:110]}")
    return 0


def evaluate_files(pool: list[dict[str, Any]], episodes: list[dict[str, Any]],
                   ks: tuple[int, ...], *, use_vector: bool = True) -> int:
    """在「同檔案再次被編輯」的情境下，比較文字訊號與檔案訊號。"""
    out = sys.stderr
    cases = build_file_cases(pool, episodes)
    if not cases:
        print("[retrieve] 建不出檔案情境的 case", file=out)
        return 1

    concepts_covered = len({c["concept_id"] for c in cases})
    print(f"[retrieve] 檔案情境：{len(cases)} 個 case，涵蓋 {concepts_covered}/{len(pool)} 條 concept",
          file=out)

    bm25_cue = build_index(pool, with_cue=True)

    def by_files(case: dict[str, Any]) -> list[tuple[int, float]]:
        return file_overlap_ranker(pool, case)
    by_files.needs_case = True

    def by_files_and_text(case: dict[str, Any]) -> list[tuple[int, float]]:
        return reciprocal_rank_fusion([file_overlap_ranker(pool, case), bm25_cue.rank(case["query"])])
    by_files_and_text.needs_case = True

    variants = [
        ("文字（BM25+cue）", bm25_cue.rank, True),
        ("檔案重疊", by_files, True),
        ("檔案 + 文字（RRF）", by_files_and_text, True),
    ]

    if use_vector:
        vector_cue = VectorIndex([document_text(c, with_cue=True) for c in pool])

        def full(case: dict[str, Any]) -> list[tuple[int, float]]:
            return reciprocal_rank_fusion([
                file_overlap_ranker(pool, case),
                bm25_cue.rank(case["query"]),
                vector_cue.rank(case["query"]),
            ])
        full.needs_case = True
        variants.append(("檔案 + 文字 + 向量（RRF）", full, True))

    for name, ranker, scoped in variants:
        ranks, avg_pool = evaluate_variant(pool, cases, ks, ranker=ranker, scope_filter=scoped)
        _report(name, ranks, len(cases), ks, avg_pool)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 檢索驗證")
    parser.add_argument("--eval", action="store_true", help="量 recall@k 與 MRR")
    parser.add_argument("--eval-files", action="store_true", help="在「同檔案再次編輯」情境下比較檔案訊號")
    parser.add_argument("--dump-precision", type=Path, help="產出檔案訊號的 precision 評估任務")
    parser.add_argument("--sample", type=int, default=30, help="precision 抽樣情境數")
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument("--query", type=str, help="手動查一筆")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--no-vector", action="store_true", help="只跑 BM25，不呼叫 ollama")
    parser.add_argument("--dump-misses", type=Path, help="把最佳設定的未命中案例寫成 JSON，供診斷")
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
    if args.dump_precision:
        return dump_precision_tasks(pool, episodes, args.dump_precision,
                                    args.sample, args.seed, args.top_k)

    if args.eval_files:
        return evaluate_files(pool, episodes, ks=(1, 3, 5, 10), use_vector=not args.no_vector)

    cases = build_ground_truth(pool, episodes)
    if not cases:
        print("[retrieve] 建不出 ground truth", file=sys.stderr)
        return 1
    return evaluate(pool, cases, ks=(1, 3, 5, 10), use_vector=not args.no_vector,
                    dump_misses=args.dump_misses)


if __name__ == "__main__":
    sys.exit(main())
