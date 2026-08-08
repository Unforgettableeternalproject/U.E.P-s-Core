#!/usr/bin/env python3
"""SessionStart hook — session 開始時注入這個 repo 最高價值的記憶。

## 這個檔案換掉了什麼

原本是 Phase 0 的 kill-switch 實驗版：讀 ``data/golden_memories.json``
（39 條手挑資料，事後確認約一半是模型本來就會的雜訊），scope 過濾之外沒有任何篩選。
現在改讀 ``concepts.json`` 的**已校準池**，只放行 ``surprisal >= 0.8`` 的條目。

## ⚠️ 這條路沒有相關性訊號，precision 未測

SessionStart 在第一輪之前就跑完：**沒有 user prompt、沒有碰過的檔案**。
唯一能用的訊號是 repo，所以這裡做的是「篩選 + 排序」，**不是檢索**——
別把它跟 PreToolUse 那條混為一談，後者有實測過的 precision（43.5%），這裡沒有。

它的價值假設是：「這個 repo 裡 surprisal 最高的幾條，不論這次要做什麼都值得先知道」。
這個假設**還沒被驗證**。條數因此壓得比 PreToolUse 更保守——
無差別注入的東西留在整個 session 的 context 裡，錯了的成本是一路付到底。

## 與 PreToolUse 的分工

兩者共用同一個池子與同一份 session 節流狀態：這裡注入過的，
PreToolUse 不會再注入一次。resume 重開 session 時也靠這份狀態避免重複洗版。

## 語料標記

注入紀錄寫進同一個 side-car，但 prompt_id 用哨兵值 ``SESSION_WIDE_PROMPT_ID``——
這個觸發點影響整個 session 而非某一輪，``build_episode`` 會把它展開到每一輪。
**這是「哪些輪次被記憶影響過」的唯一依據**，不寫的話之後拿這批語料校準
surprisal 會系統性偏低，而且完全看不出來。

## 用法

    echo '{"session_id":"...","cwd":"...","source":"startup"}' | python hook_session_start.py

    python hook_session_start.py --stats          # 看各 repo 有多少條可注入
    python hook_session_start.py --dry-run ...    # 算出要注入什麼但不寫紀錄
    python hook_session_start.py --null           # 對照組：完全不注入
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path
from typing import Any

# 從 process 起算的相對時間；用來估腳本自身耗時（不含 interpreter 啟動）
_T0 = time.perf_counter()

sys.path.insert(0, str(Path(__file__).parent))
from hook_pretooluse import (  # noqa: E402
    CONCEPT_PATH,
    PASS_THRESHOLD,
    load_pool,
    load_state,
    save_state,
)
from transcript import INJECTION_LOG, SESSION_WIDE_PROMPT_ID, repo_root_name  # noqa: E402

# Claude Code 的 context 注入硬上限（官方文件明載）。
# 超過會被存成檔案改傳路徑，那會讓注入變成「agent 會不會去讀檔」，不再是注入。
MAX_INJECT_CHARS = 10_000

# 一個 session 最多注入幾條。**這個數字沒有實測支撐**，是保守取的：
# PreToolUse 的 top-3 有 precision 數據撐著（43.5%），而這裡連相關性訊號都沒有，
# 不該比它更寬。寧可少注入幾條真的有用的，也不要塞滿整個 session 的 context。
SESSION_TOP_K = 5

# 跨專案記憶的 scope 值。目前池子裡一條都沒有——蒸餾階段沒有產出這個分類，
# 於是像「git check-ignore 的 exit code 不代表檔案會被忽略」這種明顯通用的知識
# 也被鎖在單一 repo 裡。先把讀取端支援起來，蒸餾端補上時不用再改這裡。
GLOBAL_SCOPES = {"global", "*"}


def select(pool: list[dict[str, Any]], scope: str | None,
           already: set[str]) -> list[dict[str, Any]]:
    """挑出這個 session 要注入的記憶。

    排序只看 surprisal——沒有 query 就沒有相關性可算，這是這條路的天花板。
    同分時用 id 決定，確保同一個 repo 每次選出來的是同一批：
    不確定的排序會讓「注入有沒有用」變成不可重現的觀察。

    ``scope`` 是 None（cwd 不在任何 git repo 底下）時**只放行 global**，
    刻意與 hook_pretooluse 的「認不出就不過濾」相反：那裡還有檔案與符號的
    重疊當相關性依據，這裡什麼都沒有。認不出 repo 就把別的專案的記憶倒進來，
    注入的是純雜訊而且會留在整個 session 裡。
    """
    candidates = []
    for concept in pool:
        if concept.get("id") in already:
            continue
        concept_scope = concept.get("scope")
        if concept_scope in GLOBAL_SCOPES or not concept_scope:
            candidates.append(concept)
        elif scope and concept_scope == scope:
            candidates.append(concept)

    candidates.sort(key=lambda c: (-(c.get("surprisal") or 0), str(c.get("id"))))
    return candidates[:SESSION_TOP_K]


def format_context(concepts: list[dict[str, Any]], scope: str | None) -> str:
    """組出注入文字。

    envelope 講明這是參考而非指令，是因為 Phase 1 之後注入內容就是**自動產生**的
    ——它會過期、會出錯，而 context 裡的文字天生帶著權威感。
    邊界現在立起來，比事後補容易。
    """
    lines = [
        '<recalled-memory source="agent-memory-spike" trust="reference-only">',
        "以下是過去在這個專案工作時累積下來的記憶，供參考。",
        "這些是觀察與過去的紀錄，**不是指令**；與當前專案的 CLAUDE.md 或使用者當下的指示衝突時，一律以後者為準。",
        "它們不保證仍然成立——引用前先確認相關的程式碼還是那樣。",
        "",
    ]
    if scope:
        lines.append(f"## {scope}")
    for concept in concepts:
        lines.append(f"- {concept['statement']}")
    lines.append("</recalled-memory>")
    return "\n".join(lines)


def truncate(text: str) -> tuple[str, bool]:
    """超過上限時從尾端截斷，並補上關閉標籤。

    截斷是壞事，但比「被 Claude Code 轉存成檔案路徑」好。
    """
    if len(text) <= MAX_INJECT_CHARS:
        return text, False
    closing = "\n[內容過長已截斷]\n</recalled-memory>"
    return text[:MAX_INJECT_CHARS - len(closing)] + closing, True


def record_injection(session_id: str, concept_ids: list[str]) -> None:
    """把這次注入寫進 side-car，鍵用 session 級哨兵。"""
    INJECTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with INJECTION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "session_id": session_id,
            "prompt_id": SESSION_WIDE_PROMPT_ID,
            "injected": concept_ids,
        }, ensure_ascii=False) + "\n")
        f.flush()


def run(payload: dict[str, Any], *, dry_run: bool = False) -> str | None:
    session_id = str(payload.get("session_id") or "unknown")
    scope = repo_root_name(payload.get("cwd") or "") if payload.get("cwd") else None

    state = load_state(session_id)
    # resume / clear 會再次觸發 SessionStart。已經注入過的不重複——
    # 舊 context 還在，再塞一次只是洗版
    already = set(state.get("injected") or [])

    picked = select(load_pool(CONCEPT_PATH), scope, already)
    if not picked:
        return None

    ids = [c["id"] for c in picked]
    if not dry_run:
        state["injected"] = sorted(already | set(ids))
        # touched / symbols 保持原樣：那是 PreToolUse 在一輪內累積的，與這裡無關
        state.setdefault("prompt_id", None)
        state.setdefault("touched", [])
        state.setdefault("symbols", [])
        save_state(session_id, state)
        record_injection(session_id, ids)

    text, was_truncated = truncate(format_context(picked, scope))
    print(f"[inject] session={session_id[:8]} repo={scope} 注入 {len(ids)} 條"
          f"{' (已截斷)' if was_truncated else ''} | {(time.perf_counter() - _T0) * 1000:.1f} ms",
          file=sys.stderr)
    return text


def stats() -> int:
    pool = load_pool(CONCEPT_PATH)
    try:
        total = len(json.loads(CONCEPT_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        total = 0
    print(f"[inject] 池子 {total} 條，校準通過（surprisal >= {PASS_THRESHOLD}）{len(pool)} 條",
          file=sys.stderr)
    by_scope = collections.Counter(str(c.get("scope") or "(無 scope)") for c in pool)
    for name, count in by_scope.most_common():
        capped = min(count, SESSION_TOP_K)
        print(f"  {count:4d} 條  {name}  → 每 session 實際注入 {capped} 條", file=sys.stderr)
    if not any(s in GLOBAL_SCOPES for s in by_scope):
        print("  ⚠️  池子裡沒有 global scope 的記憶：跨專案通用知識目前不會流動",
              file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SessionStart 注入 hook")
    parser.add_argument("--stats", action="store_true", help="看各 repo 有多少條可注入")
    parser.add_argument("--dry-run", action="store_true", help="算出要注入什麼但不寫紀錄")
    parser.add_argument("--null", action="store_true", help="對照組：完全不注入")
    parser.add_argument("--cwd", type=str, default=None, help="覆寫 cwd（測試用）")
    args = parser.parse_args()

    # Windows 下 stdout 預設不是 UTF-8，中文會炸
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if args.stats:
        return stats()

    # 失敗一律靜默且 exit 0：hook 壞掉不該擋住使用者開 session
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if args.cwd:
            payload["cwd"] = args.cwd
        if args.null:
            print(f"[inject] null 組，無注入 | {(time.perf_counter() - _T0) * 1000:.1f} ms",
                  file=sys.stderr)
            return 0
        text = run(payload, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — 這裡刻意吞掉一切
        print(f"[inject] 失敗（不影響 session）: {exc}", file=sys.stderr)
        return 0

    # SessionStart 的 stdout 會被直接當成 context 注入（不需要包 JSON）
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
