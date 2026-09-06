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
from transcript import ORIGIN_HUMAN, file_key_overlap, file_keys  # noqa: E402

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
# 單次請求的 payload 上限（bytes）。**限制的是總大小，不是條數**——
# 實測純英數 400 條可過，但中文 256 條（約 230 KB）就噴 HTTP 400，
# 而同一批資料切成 32 條一段卻全部通過。用條數當界只是換個地方壞，所以按位元組切。
EMBED_BATCH_BYTES = 32 * 1024


def ollama_embed(texts: list[str], model: str = OLLAMA_MODEL, timeout: float = 120.0) -> list[list[float]]:
    """呼叫 ollama 取 embedding。

    **走 HTTP 而不是 sentence-transformers 是關鍵決定。** 自行載入模型的話，
    冷啟動幾秒起跳，而 `UserPromptSubmit` 只有 30 秒 timeout 且每次都是新程序——
    等於每一輪對話都要付一次模型載入。ollama 本身就是常駐服務，
    模型留在它的記憶體裡，這邊只是一次 HTTP 往返，也**不需要自己做 daemon**。

    只用標準庫，spike 的零依賴前提保住了（hook 跑在系統 Python，不保證有 numpy）。

    **分批送是必要的，不是優化。** 初版一次把整池丟給 ollama，池子 73 條時沒事；
    全語料蒸餾後池子長到 775 條，同一支指令直接噴 HTTP 400——實測上限落在
    400 與 793 之間。這種「資料一多就整個壞掉」的失敗只會在規模長上來時出現，
    而那正是它最不該壞的時候。
    """
    import urllib.error
    import urllib.request

    # 按累積位元組切批。單條就超過上限的情況也要能送出去（自己一批），
    # 否則會切出空批次，靜默少掉一條 embedding 而讓後續索引整個錯位
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for text in texts:
        cost = len(text.encode("utf-8")) + 8  # 8 是 JSON 引號與逗號的粗估開銷
        if current and size + cost > EMBED_BATCH_BYTES:
            chunks.append(current)
            current, size = [], 0
        current.append(text)
        size += cost
    if current:
        chunks.append(current)

    embeddings: list[list[float]] = []
    start = 0
    for chunk in chunks:
        payload = json.dumps({"model": model, "input": chunk}).encode("utf-8")
        request = urllib.request.Request(OLLAMA_URL, data=payload,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                embeddings.extend(json.loads(response.read().decode("utf-8"))["embeddings"])
        except (urllib.error.URLError, KeyError, TimeoutError) as exc:
            raise RuntimeError(f"ollama 呼叫失敗（{model}, 第 {start}~{start + len(chunk)} 條）: {exc}") from exc
        start += len(chunk)

    # 少一條就會讓 pool 與 matrix 錯位，而錯位是靜默的——排序照樣算得出來，
    # 只是每條記憶配到別人的向量。寧可在這裡炸掉
    if len(embeddings) != len(texts):
        raise RuntimeError(f"ollama 回傳 {len(embeddings)} 條 embedding，與輸入的 {len(texts)} 條不符")
    return embeddings


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
        # 比對走 file_key：原始路徑因 nested git repo + 浮動 cwd 而有多種寫法，
        # 直接比字串會讓大量真實的「同檔案再編輯」case 建不出來
        source_files = file_keys(concept.get("source_files"))
        if not source_files:
            continue
        source_keys = {(t[0], t[1] if len(t) > 1 else 0) for t in (concept.get("source_turns") or [])}

        for episode in episodes:
            if episode.get("origin") != ORIGIN_HUMAN:
                continue
            if (episode.get("prompt_id"), episode.get("turn_index")) in source_keys:
                continue
            if not (source_files & file_keys(episode.get("files_edited"))):
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
                "symbols_edited": episode.get("symbols_edited") or [],
                "assistant_text": episode.get("assistant_text") or "",
            })
    return cases


# 注入時的檔案訊號設定。**這兩個數字是實測選的**，50 個情境、236 條判定：
#
#   設定                  注入條數  precision  情境命中率  平均條/次
#   top-3 / overlap>=1       144      38.2%       66%       2.88
#   top-3 / overlap>=2       115      43.5%       62%       2.30   ← 採用
#   top-3 / overlap>=3        53      49.1%       32%       1.06
#   top-5 / overlap>=2       175      38.3%       68%       3.50
#
# 只重疊一項的召回 RELEVANT 只有 8.2%、IRRELEVANT 77%——那是「剛好碰到同一個檔案」，
# 濾掉它換來 5.3pp 的 precision，只損失 4pp 的命中率。
# 再往上收到 3 就崩了：命中率腰斬到 32%，因為多數真正有用的召回本來就只重疊兩項。
MIN_FILE_OVERLAP = 2
INJECT_TOP_K = 3

PRECISION_JUDGE_INSTRUCTIONS = """\
你在評估一個 coding agent 的記憶召回**準不準**。

每一題會給你：那一輪使用者說的話、助手實際做了什麼、改了哪些檔案，
以及系統在那個當下召回的幾條記憶。逐條判斷這條記憶對**當下這輪**有沒有用。

- `RELEVANT`：這條記憶講的正是這輪要處理的東西，事先看到它會讓這輪做得更對或更快
- `MARGINAL`：沾得上邊（同一個檔案、同一個模組），但跟這輪真正在做的事沒有交集
- `IRRELEVANT`：完全用不上

**判斷紀律**：召回到「同一個檔案的另一段邏輯的舊筆記」算 `MARGINAL`，不算 `RELEVANT`——
共用一個檔案不代表相關。要判 `RELEVANT`，得說得出這條記憶會影響這輪的哪個決定。

只輸出 JSON：

```json
{"verdicts": [
  {"task_id": "prec-000", "concept_id": "c-019", "verdict": "MARGINAL",
   "reason": "一句話說明"}
]}
```
"""


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

    path.write_text(json.dumps({"instructions": PRECISION_JUDGE_INSTRUCTIONS,
                                "count": len(tasks), "tasks": tasks},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(t["retrieved"]) for t in tasks)
    print(f"[retrieve] {len(tasks)} 個情境（去重自 {len(cases)} 個 case），"
          f"共召回 {total} 條，平均 {total / len(tasks):.2f} 條/次 → {path}", file=sys.stderr)
    return 0


def show_precision(path: Path, spec: str) -> int:
    """印出指定範圍的 precision 判定材料。

    跟其他工具同一個介面（``--show`` 取一批、agent 判、``--ingest`` 收回），
    因為 C 階段要把這條串進自動化，一次性的手工分析串不進去。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    start, _, end = spec.partition("-")
    tasks = payload["tasks"][int(start):int(end or start) + 1]

    print(payload.get("instructions", PRECISION_JUDGE_INSTRUCTIONS))
    for task in tasks:
        if not task["retrieved"]:
            continue
        print(f"\n{'=' * 70}\n### {task['id']}  (repo: {task['repo']})")
        print(f"\n[改到的檔案]\n{', '.join(task['files_edited']) or '（無）'}")
        print(f"\n[使用者說的話]\n{task['user_text']}")
        print(f"\n[助手實際做了什麼]\n{task['assistant_text']}")
        print("\n[當下召回的記憶]")
        for item in task["retrieved"]:
            print(f"  - {item['id']}（重疊 {item['overlap']}）：{item['statement']}")
    return 0


def format_precision_task(task: dict[str, Any]) -> str:
    """判卷者看到的單一情境。與 ``--show-precision`` 的排版刻意一致：
    人工判與 headless 判必須看到同一份材料，否則兩者的數字不可比。
    """
    lines = [f"### {task['id']}  (repo: {task['repo']})",
             f"\n[改到的檔案]\n{', '.join(task['files_edited']) or '（無）'}",
             f"\n[使用者說的話]\n{task['user_text']}",
             f"\n[助手實際做了什麼]\n{task['assistant_text']}",
             "\n[當下召回的記憶]"]
    for item in task["retrieved"]:
        lines.append(f"  - {item['id']}（重疊 {item['overlap']}）：{item['statement']}")
    return "\n".join(lines)


def judge_precision(task_path: Path, out_dir: Path, batch_size: int) -> int:
    """分批交給 headless `claude -p` 判卷。

    先前只有 `--show-precision`（人工判），於是三條注入路徑裡唯一掛載的這條
    反而是唯一不能重跑量測的。與另外兩條同一個介面、同一個判卷後端。
    """
    from pipeline import adjudicate_to_file

    payload = json.loads(task_path.read_text(encoding="utf-8"))
    tasks = [t for t in payload["tasks"] if t["retrieved"]]
    out_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for start in range(0, len(tasks), batch_size):
        batch = tasks[start:start + batch_size]
        target = out_dir / f"verdicts-{start // batch_size:02d}.json"
        if target.exists():
            print(f"  [{target.name}] 已存在，略過", file=sys.stderr)
            continue
        prompt = (payload.get("instructions", PRECISION_JUDGE_INSTRUCTIONS)
                  + "\n\n" + "\n\n".join(format_precision_task(t) for t in batch))
        ok, summary = adjudicate_to_file(prompt, target)
        print(f"  [{target.name}] {'OK' if ok else '失敗'}: {summary}", file=sys.stderr)
        failures += 0 if ok else 1
    if failures:
        print(f"[precision] {failures} 批失敗，重跑同一個指令會續判（已完成的會略過）",
              file=sys.stderr)
    return 1 if failures else 0


def ingest_precision(task_path: Path, verdict_path: Path) -> int:
    """收回判定並算數字。

    報兩個指標，因為它們回答不同的問題：

    - **逐條 precision**：召回的東西裡有多少是有用的，決定注入會不會稀釋 context
    - **情境命中率**：有多少次召回裡「至少有一條 RELEVANT」，
      這才是決定 ``PreToolUse`` 值不值得掛的指標——只要有一條真的有用，
      這次召回就賺到了，旁邊幾條無關的代價只是多佔一點篇幅
    """
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    tasks = {t["id"]: t for t in payload["tasks"]}

    sources = sorted(verdict_path.glob("*.json")) if verdict_path.is_dir() else [verdict_path]
    verdicts: list[dict[str, Any]] = []
    for source in sources:
        data = json.loads(source.read_text(encoding="utf-8"))
        verdicts.extend(data if isinstance(data, list) else data.get("verdicts", []))

    counts: dict[str, int] = {}
    by_task: dict[str, list[str]] = {}
    for verdict in verdicts:
        label = verdict.get("verdict") or "?"
        counts[label] = counts.get(label, 0) + 1
        by_task.setdefault(verdict.get("task_id"), []).append(label)

    total = sum(counts.values())
    relevant = counts.get("RELEVANT", 0)
    marginal = counts.get("MARGINAL", 0)
    judged_tasks = [t for t in tasks.values() if t["retrieved"] and t["id"] in by_task]
    hit = sum(1 for t in judged_tasks if "RELEVANT" in by_task[t["id"]])

    out = sys.stderr
    print(f"[precision] {len(judged_tasks)} 個有召回的情境、{total} 條判定: {counts}", file=out)
    if total:
        print(f"  逐條 precision（嚴格）: {relevant}/{total} = {relevant / total * 100:.1f}%", file=out)
        print(f"  逐條 precision（含 MARGINAL）: {(relevant + marginal)}/{total} "
              f"= {(relevant + marginal) / total * 100:.1f}%", file=out)
    if judged_tasks:
        print(f"  情境命中率（至少一條 RELEVANT）: {hit}/{len(judged_tasks)} "
              f"= {hit / len(judged_tasks) * 100:.1f}%", file=out)
    return 0


def split_anchors(anchors: list[str]) -> tuple[list[str], list[str]]:
    """把錨點分成檔案類與符號類。

    兩者要比對的東西不同：檔案類對 ``files_edited``、符號類對 ``symbols_edited``。
    混在一起用集合交集的話，符號永遠不可能命中檔案路徑——
    這正是先前「anchors 粒度做細反而扣分」的原因。

    判斷依據是路徑分隔符與副檔名。CSS 自訂屬性（``--uep-island-z``）
    帶連字號但不帶點，會落在符號側，那是對的。
    """
    files: list[str] = []
    symbols: list[str] = []
    for anchor in anchors:
        if "/" in anchor or "\\" in anchor or ("." in anchor and not anchor.startswith("--")):
            files.append(anchor)
        else:
            symbols.append(anchor)
    return files, symbols


def file_overlap_ranker(pool: list[dict[str, Any]], case: dict[str, Any], *,
                        use_symbols: bool = True) -> list[tuple[int, float]]:
    """檔案訊號：重疊的檔案數就是分數。

    優先用 ``anchors``（這條記憶真正談論的對象），沒有才退回 ``source_files``。

    兩者的差別實測很大：``source_files`` 是「產生這條記憶那一輪碰過的所有檔案」，
    裡面多半是順手碰到的，用它做召回的 precision 只有 30%——
    抓到的常是「同一個檔案裡另一段邏輯的舊筆記」。
    真正有用的召回，共同特徵是錨點與當下要改的東西同名同源。

    **只重疊一項的召回幾乎全是雜訊**（實測 RELEVANT 8.2%、IRRELEVANT 77%），
    所以要注入時應該用 ``MIN_FILE_OVERLAP`` 過濾——見那個常數的說明。
    評測時不過濾：門檻要能被量測，寫死在 ranker 裡就比較不出來了。
    """
    touched_files = file_keys(case.get("files_edited"))
    # use_symbols=False 是對照組：同一個池子、同一批 case，只差有沒有用符號級錨點。
    # 沒有這個對照就只能拿「改版前的數字」比，而那跨了池子與 case 的變動，說明不了什麼
    touched_symbols = ({s.lower() for s in (case.get("symbols_edited") or [])}
                       if use_symbols else set())

    scored = []
    for i, concept in enumerate(pool):
        anchors = concept.get("anchors") or concept.get("source_files") or []
        files, symbols = split_anchors(anchors)
        # 段界尾段吻合，與 hook_pretooluse.select 共用同一個函式——
        # 裸檔名錨點在全等比對下永遠比不中，兩邊必須一起換，不能分岔
        file_hits = file_key_overlap(file_keys(files), touched_files)

        # 符號與檔案**等權**。這個權重是實測選出來的，不是拍腦袋：
        #   權重 2      → recall@1 5.1% / recall@5 19.7% / MRR 0.114
        #   等權（1）   → recall@1 8.5% / recall@5 23.6% / MRR 0.152  ← 最好
        #   稀有度加權  → recall@1 7.7% / recall@5 21.5% / MRR 0.136
        # 「罕見符號應該更有鑑別力」聽起來很合理，實測反而更差，所以不留那段複雜度。
        symbol_hits = len({s.lower() for s in symbols} & touched_symbols)
        scored.append((i, float(file_hits + symbol_hits)))
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

    def by_files_only(case: dict[str, Any]) -> list[tuple[int, float]]:
        return file_overlap_ranker(pool, case, use_symbols=False)
    by_files_only.needs_case = True

    def by_files_and_text(case: dict[str, Any]) -> list[tuple[int, float]]:
        return reciprocal_rank_fusion([file_overlap_ranker(pool, case), bm25_cue.rank(case["query"])])
    by_files_and_text.needs_case = True

    variants = [
        ("文字（BM25+cue）", bm25_cue.rank, True),
        ("錨點：只用檔案", by_files_only, True),
        ("錨點：檔案 + 符號", by_files, True),
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
    parser.add_argument("--show-precision", type=str, help="印出 precision 判定材料，例如 0-9")
    parser.add_argument("--judge-precision", type=Path, metavar="OUT_DIR",
                        help="分批交給 headless claude 判卷（與另外兩條注入路徑同一個介面）")
    parser.add_argument("--ingest-precision", type=Path, help="收回 precision 判定並算數字")
    parser.add_argument("--batch-size", type=int, default=10, help="判卷每批幾題")
    parser.add_argument("--precision-path", type=Path, default=WORK_DIR / "precision_tasks.json")
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

    if args.show_precision:
        return show_precision(args.precision_path, args.show_precision)
    if args.judge_precision:
        return judge_precision(args.precision_path, args.judge_precision, args.batch_size)
    if args.ingest_precision:
        return ingest_precision(args.precision_path, args.ingest_precision)

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
