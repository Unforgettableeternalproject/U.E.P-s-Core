#!/usr/bin/env python3
"""Phase 3 B：PreToolUse hook — 要動某個檔案之前，把相關的記憶送進 context。

## 為什麼是 PreToolUse 而不是 UserPromptSubmit

兩個觸發點都成立，但**只有檔案訊號量過 precision**（Phase 3 A1：
top-3 + overlap>=2 → precision 43.5%、情境命中率 62%）。
文字訊號那條的 precision 至今沒測，而 precision 才是決定「召回會不會誤導」的那一面。
先走有數據的那條。

另一個理由是 query 品質：語料實測 human 輪次的 `user_text` 中位數只有 58 字元，
大量是「繼續」「可以」——`UserPromptSubmit` 天生受限於此，
而「即將編輯哪個檔案」完全不受影響。

## 三道閘門

1. **只注入校準過且通過門檻的記憶**（`surprisal >= 0.8`）。
   未校準的不是「低價值」而是「不知道價值」，兩者都不該進 context。
2. **overlap >= 2**。只重疊一項的召回實測 RELEVANT 只有 8.2%、IRRELEVANT 77%——
   「剛好碰到同一個檔案」不構成相關。
3. **同一 session 同一條記憶只注入一次**。PreToolUse 每次工具呼叫都觸發，
   一輪可能有幾十次；不節流的話同一條記憶會反覆洗版。

## 失敗一律靜默

跟 Stop hook 同樣的取捨：hook 壞掉不該擋住使用者工作。
代價是壞了不會有人通知，所以 `--doctor` 要能對帳（注入紀錄 N 筆 vs 語料對上 M 輪）。

## 用法

    echo '{"session_id":"...","prompt_id":"...","tool_name":"Edit",
           "tool_input":{"file_path":"..."},"cwd":"..."}' | python hook_pretooluse.py

    python hook_pretooluse.py --stats        # 看池子裡有多少條可注入
    python hook_pretooluse.py --dry-run ...  # 算出要注入什麼但不寫紀錄
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from transcript import (  # noqa: E402
    INJECTION_LOG,
    TOUCH_LOG,
    extract_symbols,
    file_key,
    file_keys,
    normalize_path,
    repo_root,
    repo_root_name,
)

WORK_DIR = INJECTION_LOG.parent
CONCEPT_PATH = WORK_DIR / "concepts.json"
# 每個 session 一個節流檔，理由同 episode 的每 session 一檔：
# 不同 session 落在不同檔案，天然沒有跨程序寫入衝突
STATE_DIR = WORK_DIR / "inject_state"

# 這三個常數與 retrieve.py 的實測結果一致。刻意複製而不 import：
# hook 每次工具呼叫都會跑，import retrieve 會連帶拉進 BM25、向量索引與 ollama 客戶端
PASS_THRESHOLD = 0.8
MIN_FILE_OVERLAP = 2
INJECT_TOP_K = 3

EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}

# 跨專案記憶的正典表示法是 **scope=None**（蒸餾指示要求「跨專案通用則填 null」）。
# 這組字串是為了容忍蒸餾者寫成字面值，收料端會正規化掉，池子裡不該出現
GLOBAL_SCOPES = {"global", "*"}


def is_global(scope: Any) -> bool:
    """這條記憶是不是跨專案通用。

    三條注入路徑共用這一份判斷。先前各自寫各自的，`hook_session_start.stats`
    只比對 GLOBAL_SCOPES 字串而不認 None，於是通用記憶回填後注入已經生效、
    `--stats` 卻還在印「池子裡沒有 global scope 的記憶」。
    分岔的判斷不會報錯，它會讓驗證步驟說謊。
    """
    return not scope or scope in GLOBAL_SCOPES


def load_pool(path: Path = CONCEPT_PATH) -> list[dict[str, Any]]:
    """只載入校準通過的記憶。

    未校準的條目**不能**當成低價值而放行：沒測過就是不知道模型會不會，
    而注入模型本來就會的東西是負價值——它佔掉 context 卻不改變任何行為。
    """
    try:
        concepts = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [c for c in concepts if (c.get("surprisal") or 0) >= PASS_THRESHOLD]


def split_anchors(anchors: list[str]) -> tuple[list[str], list[str]]:
    """把錨點分成檔案類與符號類（與 retrieve.py 同一套規則）。"""
    files: list[str] = []
    symbols: list[str] = []
    for anchor in anchors:
        if "/" in anchor or "\\" in anchor or ("." in anchor and not anchor.startswith("--")):
            files.append(anchor)
        else:
            symbols.append(anchor)
    return files, symbols


def select(pool: list[dict[str, Any]], touched: set[str], symbols: set[str],
           scope: str | None, already: set[str]) -> list[dict[str, Any]]:
    """挑出要注入的記憶。與 A1 量 precision 時的 ranker 逐項對齊。

    對齊有兩個要點，弄錯任何一個，A1 那組門檻就不適用：

    1. ``touched`` / ``symbols`` 是**這一輪到目前為止碰過的全部**，不只當下這一次。
       A1 的 overlap 是「記憶的錨點 ∩ 那一輪編輯的檔案與符號」，
       只看單次呼叫的話 overlap 幾乎不可能到 2。
    2. **符號要算進去**。實測差距是天與地：只比對檔案時 `overlap>=2` 的觸發率是
       **0.0%**（252/343 條記憶只有一個檔案錨點，湊不出兩個檔案重疊），
       加上符號之後是 39.9%。

    符號拿得到是因為 ``tool_input`` 就是完整的工具參數——
    Edit 的 ``old_string``/``new_string``、Write 的 ``content`` 都在裡面。
    """
    if not touched and not symbols:
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for concept in pool:
        if concept.get("id") in already:
            continue
        if scope and not is_global(concept.get("scope")) and concept.get("scope") != scope:
            continue
        anchors = concept.get("anchors") or concept.get("source_files") or []
        anchor_files, anchor_symbols = split_anchors(anchors)
        # 檔案與符號等權——實測選出來的，見 retrieve.file_overlap_ranker
        overlap = (len(file_keys(anchor_files) & touched)
                   + len({s.lower() for s in anchor_symbols} & symbols))
        if overlap >= MIN_FILE_OVERLAP:
            scored.append((overlap, concept))

    scored.sort(key=lambda pair: -pair[0])
    return [concept for _, concept in scored[:INJECT_TOP_K]]


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return STATE_DIR / f"{safe or 'unknown'}.json"


def load_state(session_id: str) -> dict[str, Any]:
    """讀 session 的注入狀態。

    ``injected`` 是整個 session 累積的（同一條記憶不重複洗版），
    ``touched`` / ``symbols`` 只算當前這一輪（換 prompt_id 就重來）——
    overlap 要對齊的是「一輪碰過什麼」，跨輪累積會讓門檻越來越鬆。
    """
    empty: dict[str, Any] = {"injected": [], "prompt_id": None, "touched": [], "symbols": []}
    try:
        data = json.loads(state_path(session_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    return data if isinstance(data, dict) else empty


def save_state(session_id: str, state: dict[str, Any]) -> None:
    path = state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def record_touch(session_id: str, prompt_id: str, key: str) -> None:
    """記下「hook 看到了這個編輯目標」。

    與注入紀錄分開，而且**不論有沒有注入都要寫**——要抓的遺漏，
    症狀正是「該累積進 touched 的檔案沒有累積到」，那種輪次多半根本沒注入，
    只看注入紀錄就永遠看不見它。
    """
    TOUCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TOUCH_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "session_id": session_id,
            "prompt_id": prompt_id,
            "file_key": key,
        }, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def record_injection(session_id: str, prompt_id: str, concept_ids: list[str]) -> None:
    """把這次注入寫進 side-car。

    **這是語料裡「哪些輪次被記憶影響過」的唯一依據。** 不寫的話，
    之後拿被影響過的語料校準 surprisal 會系統性偏低，而且完全看不出來。
    """
    INJECTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with INJECTION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "session_id": session_id,
            "prompt_id": prompt_id,
            "injected": concept_ids,
        }, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def format_context(concepts: list[dict[str, Any]], target: str) -> str:  # noqa: D401
    """組出要送進 context 的文字。

    刻意講明這是過去累積的筆記而非事實斷言——記憶會過期，
    而池子的收斂只保證「已知的矛盾被處理掉了」，不保證每條都還成立。
    """
    lines = [f"以下是過去在 `{target}` 這個檔案上累積的筆記，可能與這次修改有關："]
    for concept in concepts:
        lines.append(f"- {concept['statement']}")
    lines.append("（這些是過去的紀錄，不保證仍然成立；與你現在看到的程式碼衝突時以程式碼為準。）")
    return "\n".join(lines)


def run(payload: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any] | None:
    tool_name = payload.get("tool_name")
    key = EDIT_TOOLS.get(str(tool_name))
    if key is None:
        return None

    target = (payload.get("tool_input") or {}).get(key)
    if not isinstance(target, str) or not target:
        return None

    session_id = str(payload.get("session_id") or "unknown")
    prompt_id = str(payload.get("prompt_id") or "unknown")
    scope = repo_root_name(payload.get("cwd") or "") if payload.get("cwd") else None

    state = load_state(session_id)
    # 換一輪就把 touched / symbols 清掉：門檻要對齊的是「這一輪碰過什麼」
    same_turn = state.get("prompt_id") == prompt_id
    touched = set(state.get("touched") or []) if same_turn else set()
    symbols = set(state.get("symbols") or []) if same_turn else set()

    # **先正規化成 repo 相對路徑再取比對鍵。** `tool_input.file_path` 是絕對路徑，
    # 而 `anchors` 是蒸餾者寫的 repo 相對路徑，兩者的段數不同：
    #
    #     絕對路徑 → testseperatememorysystem/agent_memory_spike/retrieve.py
    #     錨點     →                          agent_memory_spike/retrieve.py
    #
    # `file_key` 取末 3 段，於是兩邊永遠不相等——**hook 裡的檔案錨點完全失效**，
    # 只剩符號在起作用。A1 量觸發率時比對的兩邊都是語料裡的相對路徑，
    # 所以那組數字看不到這件事（同一型的錯：同名的量不一定是同一個量）。
    key = file_key(normalize_path(target, repo_root(str(payload.get("cwd") or ""))))
    if key:
        touched.add(key)
        if not dry_run:
            record_touch(session_id, prompt_id, key)
    symbols.update(s.lower() for s in extract_symbols(payload.get("tool_input") or {}))

    already = set(state.get("injected") or [])
    # 顯式帶入路徑：預設參數在函式定義時就綁定了，那樣測試換不掉它
    picked = select(load_pool(CONCEPT_PATH), touched, symbols, scope, already)
    new_state = {"injected": sorted(already), "prompt_id": prompt_id,
                 "touched": sorted(touched), "symbols": sorted(symbols)}
    if not picked:
        # 沒挑到也要存：touched 是累積出來的，不存的話下一次又從零開始
        if not dry_run:
            save_state(session_id, new_state)
        return None

    ids = [c["id"] for c in picked]
    if not dry_run:
        new_state["injected"] = sorted(already | set(ids))
        save_state(session_id, new_state)
        record_injection(session_id, prompt_id, ids)

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": format_context(picked, target),
        }
    }


def stats() -> int:
    pool = load_pool()
    try:
        total = len(json.loads(CONCEPT_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        total = 0
    anchored = sum(1 for c in pool if split_anchors(c.get("anchors") or [])[0])
    print(f"[inject] 池子 {total} 條，校準通過 {len(pool)} 條，其中有檔案錨點 {anchored} 條",
          file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 PreToolUse 注入 hook")
    parser.add_argument("--stats", action="store_true", help="看池子裡有多少條可注入")
    parser.add_argument("--dry-run", action="store_true", help="算出要注入什麼但不寫紀錄")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if args.stats:
        return stats()

    # 失敗一律靜默且 exit 0：hook 壞掉不該擋住使用者工作
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        result = run(payload, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — 這裡刻意吞掉一切
        print(f"[inject] 失敗（不影響工具執行）: {exc}", file=sys.stderr)
        return 0

    if result:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
