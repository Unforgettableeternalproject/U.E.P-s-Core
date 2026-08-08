#!/usr/bin/env python3
"""UserPromptSubmit hook — 使用者開口之後，把主題相關的記憶送進 context。

## 三條注入路徑裡的哪一條

| 觸發點 | 訊號 | precision |
|---|---|---|
| SessionStart | 只有 repo | 無（沒有相關性訊號可言） |
| PreToolUse | 即將編輯的檔案 + 符號 | 43.5%（實測） |
| **UserPromptSubmit（這裡）** | **使用者輸入的文字** | **未測** |

Phase 2 的結論是「兩個觸發點互補，不是二選一」：這裡管使用者**明確問到**的主題，
PreToolUse 管**即將碰到**的檔案。兩者的失效模式不同，補不了對方。

## ⚠️ 天花板已知，而且不高

語料實測 human 輪次的 ``user_text`` 中位數只有 58 字元，大量是「繼續」「可以」——
**這條路天生受限於此**。Phase 2 的未命中診斷：63% 屬於 UNRETRIEVABLE
（query 完全沒有主題訊號），真實天花板 recall 約 80.6%。
短於 ``MIN_QUERY_CHARS`` 的直接不查，那不是放棄，是那種輸入本來就不該召回任何東西。

## 為什麼是純 BM25

Phase 2 實測：BM25 → 向量檢索 recall@5 **±0.0pp**，而 statement → cue **+8.1pp**。
**索引提取線索遠比換演算法有效**，加 cue 後純 BM25 的 MRR（0.436）甚至略高於
hybrid（0.431）。所以這裡零依賴、不碰 ollama——沒有理由付那個代價。

## 門檻怎麼來的

分數除以「這個 query 在這個索引上的理論最高分」（見 ``score_ceiling``），
落在 [0, 1] 且與池子大小無關。**只除以 token 數是不夠的**——
那只消掉 query 長度，沒消掉 idf 的尺度，小 scope 會永遠不觸發（實測見該函式）。

564 個真實 query 的觸發率：

    門檻   0.08   0.12   0.20   0.30   最小 scope（2 條）在 0.30 時
    總觸發 94.9%  83.7%  46.1%  20.7%  53%

取 **0.30**。**這個數字沒有 precision 驗證**，只是「沒有數據時寧可吝嗇」——
注入錯的東西比不注入更糟，它會佔著 context 並把模型往錯的方向帶。
要調之前先照 Phase 3 A1 的做法量 precision，別憑感覺動它。

## 用法

    echo '{"session_id":"...","prompt":"...","cwd":"..."}' | python hook_userpromptsubmit.py

    python hook_userpromptsubmit.py --eval          # 看各門檻下的觸發率
    python hook_userpromptsubmit.py --dry-run ...   # 算出要注入什麼但不寫紀錄
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_T0 = time.perf_counter()

sys.path.insert(0, str(Path(__file__).parent))
from hook_pretooluse import (  # noqa: E402
    CONCEPT_PATH,
    load_pool,
    load_state,
    save_state,
)
from retrieve import BM25, document_text, tokenize  # noqa: E402
from transcript import (  # noqa: E402
    INJECTION_LOG,
    prompt_fingerprint,
    repo_root_name,
)

# 短於這個長度就不查。「繼續」「可以」本來就不該召回任何東西，
# 硬查只會拿雜訊填滿 context——與 retrieve.py 評測時的門檻一致。
MIN_QUERY_CHARS = 20

# 正規化分數（score / score_ceiling）的下限。推導見模組 docstring。
# **未經 precision 驗證**，是刻意取的保守值。
MIN_NORMALISED_SCORE = 0.30

# 一輪最多注入幾條。與 PreToolUse 一致——那個數字有實測撐著（top-3 / overlap>=2）。
INJECT_TOP_K = 3


def score_ceiling(index: BM25, tokens: list[str]) -> float:
    """這個 query 在這個索引上的理論最高分。

    **不能只除以 token 數。** BM25 的 idf 依賴池子大小：87 條的池子裡稀有詞
    idf 約 4.5，而只有 2 條時上限是 ``log(2)=0.69``——同一個 query 在小池子的
    分數天生低六倍，拿混合各 scope 量出來的門檻去套小 scope，結果是**永遠不觸發**。
    實測抓到的：本 repo 只有 2 條記憶，一句直接問到那條記憶主題的 query
    正規化後只有 0.126，遠低於混合分布量出來的 0.35。

    除以理論最高分之後分數落在 [0, 1]，且與池子大小無關——
    它的意思變成「這條記憶覆蓋了這句話多少可比對的資訊」。
    BM25 的單項上限在詞頻趨近無限時是 ``idf * (k1 + 1)``。
    """
    return sum(index.idf.get(term, 0.0) for term in set(tokens)) * (index.k1 + 1)


def select(pool: list[dict[str, Any]], query: str, scope: str | None,
           already: set[str]) -> list[dict[str, Any]]:
    """用 BM25 挑出與這句話相關的記憶。

    scope 過濾比照 SessionStart：認不出 repo 就只放行 global。
    這裡雖然有文字訊號，但別的專案的記憶配上剛好撞詞的 query 是典型的假陽性，
    而假陽性正是這條路最該防的東西（它的 precision 還沒測過）。
    """
    if len(query.strip()) < MIN_QUERY_CHARS:
        return []

    candidates = [
        c for c in pool
        if c.get("id") not in already
        and (not c.get("scope") or c.get("scope") == scope)
    ]
    if not candidates:
        return []

    tokens = tokenize(query)
    if not tokens:
        return []

    index = BM25([tokenize(document_text(c, with_cue=True)) for c in candidates])
    ceiling = score_ceiling(index, tokens)
    if ceiling <= 0:
        # query 的詞一個都不在索引裡，沒有任何東西可比
        return []

    picked = []
    for position, score in index.rank(query)[:INJECT_TOP_K]:
        if score / ceiling < MIN_NORMALISED_SCORE:
            # ranked 已按分數排序，第一個不過關的之後都不會過
            break
        picked.append(candidates[position])
    return picked


def format_context(concepts: list[dict[str, Any]]) -> str:
    lines = ["以下是過去累積的筆記，可能與你這次要處理的事情有關："]
    for concept in concepts:
        lines.append(f"- {concept['statement']}")
    lines.append("（這些是過去的紀錄，不保證仍然成立；與你現在看到的程式碼或使用者當下的指示衝突時，以後者為準。）")
    return "\n".join(lines)


def record_injection(session_id: str, prompt_id: str | None, fingerprint: str,
                     concept_ids: list[str]) -> None:
    """把這次注入寫進 side-car。

    ``prompt_id`` 在這個觸發點不保證存在，缺席時靠指紋歸屬——
    「注入了卻標記不到」會讓之後的校準系統性偏低且完全看不出來，
    所以寧可用比較弱的鍵，也不要沒有鍵。
    """
    INJECTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with INJECTION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "session_id": session_id,
            "prompt_id": prompt_id,
            "prompt_fingerprint": fingerprint,
            "injected": concept_ids,
        }, ensure_ascii=False) + "\n")
        f.flush()


def run(payload: dict[str, Any], *, dry_run: bool = False) -> str | None:
    query = str(payload.get("prompt") or "")
    if not query.strip():
        return None

    session_id = str(payload.get("session_id") or "unknown")
    prompt_id = payload.get("prompt_id")
    scope = repo_root_name(payload.get("cwd") or "") if payload.get("cwd") else None

    state = load_state(session_id)
    already = set(state.get("injected") or [])

    picked = select(load_pool(CONCEPT_PATH), query, scope, already)
    if not picked:
        return None

    ids = [c["id"] for c in picked]
    if not dry_run:
        state["injected"] = sorted(already | set(ids))
        state.setdefault("prompt_id", None)
        state.setdefault("touched", [])
        state.setdefault("symbols", [])
        save_state(session_id, state)
        record_injection(session_id, str(prompt_id) if prompt_id else None,
                         prompt_fingerprint(query), ids)

    print(f"[inject] session={session_id[:8]} repo={scope} 注入 {len(ids)} 條 "
          f"| {(time.perf_counter() - _T0) * 1000:.1f} ms", file=sys.stderr)
    return format_context(picked)


def evaluate() -> int:
    """在真實語料上看各門檻的觸發率。

    這**不是** precision——只說明「多常會注入」，不說明「注入的對不對」。
    後者要照 Phase 3 A1 的做法出題給評審判，那還沒做。
    """
    from hook_stop import DEFAULT_EPISODE_DIR, load_deduped
    from transcript import ORIGIN_HUMAN

    pool = load_pool(CONCEPT_PATH)
    episodes, _ = load_deduped(DEFAULT_EPISODE_DIR)
    queries = [
        (e.get("user_text") or "", e.get("repo"))
        for e in episodes
        if e.get("origin") == ORIGIN_HUMAN and len((e.get("user_text") or "").strip()) >= MIN_QUERY_CHARS
    ]
    print(f"[eval] 池子 {len(pool)} 條 | 夠長的 human query {len(queries)} 個 "
          f"（全部 {sum(1 for e in episodes if e.get('origin') == ORIGIN_HUMAN)} 個）",
          file=sys.stderr)

    original = globals()["MIN_NORMALISED_SCORE"]
    try:
        for threshold in (0.0, 0.15, 0.25, 0.35, 0.45, 0.60):
            globals()["MIN_NORMALISED_SCORE"] = threshold
            hits = [len(select(pool, q, scope, set())) for q, scope in queries]
            fired = sum(1 for h in hits if h)
            mark = "  ← 目前設定" if threshold == original else ""
            print(f"  門檻 {threshold:.2f}  觸發 {fired:4d}/{len(queries)} "
                  f"({100 * fired / max(len(queries), 1):5.1f}%)  "
                  f"平均 {sum(hits) / max(len(queries), 1):.2f} 條/次{mark}", file=sys.stderr)
    finally:
        globals()["MIN_NORMALISED_SCORE"] = original
    print("\n  ⚠️  觸發率不是 precision。要知道注入的對不對，得照 A1 出題給評審判。",
          file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="UserPromptSubmit 注入 hook")
    parser.add_argument("--eval", action="store_true", help="看各門檻下的觸發率")
    parser.add_argument("--dry-run", action="store_true", help="算出要注入什麼但不寫紀錄")
    parser.add_argument("--cwd", type=str, default=None, help="覆寫 cwd（測試用）")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if args.eval:
        return evaluate()

    # 失敗一律靜默且 exit 0：hook 壞掉不該擋住使用者送出訊息
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if args.cwd:
            payload["cwd"] = args.cwd
        text = run(payload, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — 這裡刻意吞掉一切
        print(f"[inject] 失敗（不影響送出）: {exc}", file=sys.stderr)
        return 0

    # UserPromptSubmit 的 stdout 會被直接當成 context 注入
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
