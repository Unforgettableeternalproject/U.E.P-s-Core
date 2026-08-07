#!/usr/bin/env python3
"""Phase 1.5：從語料蒸餾出 concept 候選。

骨架抄自 ``echo_memory/dream/distillation.py``（溯源、去重、空結果不標記），
但**判準整個換掉**，因為 Echo 的蒸餾在 coding agent 情境下有三處會直接出錯：

1. ``_collect_candidates`` 用 ``salience_score >= 0.5`` 篩候選。
   Phase 0 已經推翻這根軸——記憶價值取決於 surprisal（模型不知道的程度），
   不是 salience（重要性）。而且 spike 的 episode 根本沒有 salience 欄位。
2. prompt 是對話取向的（``entity（人/地/物）``、「跨多個對話出現的模式」），
   抽出來會是對話式概念，不是「這個 repo 的 X 慣例與模型預設相反」。
3. 沒有 surprisal 校準環節——蒸餾完直接寫進 SemanticMemory，
   沒有任何機制驗證模型本來就知不知道。校準在 ``calibrate.py``。

## 為什麼不直接 import echo_memory

刻意複製而非依賴，維持 spike 完全獨立（艾斯維爾裁決）。
代價是 Echo 那邊的修復不會同步過來，兩份會分岔。

## 為什麼 LLM 是外部注入的

spike 維持零第三方依賴。蒸餾用 ``--emit`` 產出任務檔、``--ingest`` 收結果，
中間那步交給 coding agent 自己做——它本來就是 LLM，不必再接一個 provider。

## 用法

    python distill.py --stats                # 只看粗篩結果的分布
    python distill.py --emit                 # 產出蒸餾任務檔
    python distill.py --ingest <concepts.json>   # 收回蒸餾結果

輸出一律寫在 repo 外（理由同 hook_stop.py 的 DEFAULT_EPISODE_DIR：
語料含商業專案原文，放 repo 內遲早外洩）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from hook_stop import DEFAULT_EPISODE_DIR, load_deduped  # noqa: E402
from transcript import ORIGIN_HUMAN  # noqa: E402

WORK_DIR = DEFAULT_EPISODE_DIR.parent
DEFAULT_TASK_PATH = WORK_DIR / "distill_tasks.json"
DEFAULT_CONCEPT_PATH = WORK_DIR / "concepts.json"

# 一輪使用者輸入超過這個長度就不算「短」。
# 語料實測 human 輪的 user_text 中位數只有 58 字元、p75 是 150——
# 艾斯維爾本來就講得短，所以「短輸入」單獨用完全沒有鑑別力
# （1374 輪裡命中 304 輪）。它只能當附加條件，不能當主訊號。
SHORT_INPUT_CHARS = 200


def load_episodes(episode_dir: Path) -> list[dict[str, Any]]:
    """讀出去重後的語料，按 session 內順序排好。"""
    episodes, _ = load_deduped(episode_dir)
    episodes.sort(key=lambda e: (e.get("session_id") or "", e.get("turn_index") or 0))
    return episodes


def find_candidates(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """粗篩：找出「同一個檔案在相鄰的 human 輪被重複修改」的輪次對。

    這是結構訊號，不是 keyword matching——不看說了什麼詞，只看做了什麼事。
    （Echo 的架構禁令同樣適用於這裡：用關鍵字清單決定什麼值得記住，
    是對記憶形成的廉價模仿。）

    **為什麼是「重複修改」**：一個檔案在前一輪剛改完、下一輪又被碰，
    代表第一次沒改對。而「模型第一次會做錯的地方」正是 surprisal 高的地方——
    它按自己的預設信念做了，然後被修正。

    **只當粗篩，不當判準**。實測這個訊號會撈到大量「同意繼續」的輪次
    （「這個可以，換下一個」「那你繼續做吧」），那不是糾正。
    篩不掉它們是預期的——判斷交給後面看得到內容的 LLM，
    這裡的職責只是把 1374 輪縮到可負擔的數量。
    """
    by_session: dict[str, list[dict[str, Any]]] = {}
    for episode in episodes:
        by_session.setdefault(episode.get("session_id") or "", []).append(episode)

    candidates: list[dict[str, Any]] = []
    for session_id, sequence in by_session.items():
        human_turns = [e for e in sequence if e.get("origin") == ORIGIN_HUMAN]
        for index in range(1, len(human_turns)):
            previous, current = human_turns[index - 1], human_turns[index]
            overlap = set(previous.get("files_edited") or []) & set(current.get("files_edited") or [])
            if not overlap:
                continue
            candidates.append({
                "session_id": session_id,
                "repo": current.get("repo"),
                "overlap_files": sorted(overlap),
                "short_followup": len(current.get("user_text") or "") < SHORT_INPUT_CHARS,
                "turns": [previous, current],
            })
    return candidates


def find_control_pairs(episodes: list[dict[str, Any]], sample_size: int, seed: int) -> list[dict[str, Any]]:
    """對照組：相鄰的 human 輪對，但**沒有**檔案重疊。

    存在的理由是量粗篩訊號的召回率。目前只知道 78 組候選裡蒸餾出 55 條，
    但不知道剩下的一千多輪裡漏掉多少——**精確率有數字，召回率完全未知**。

    如果對照組的產出率跟候選組差不多，那這個訊號等於隨機抽樣，
    整個粗篩步驟只是在省 token，沒有在做篩選。

    固定 seed 是為了可重現：這個數字會被拿來做決策，換一次抽樣就變一次結論不行。
    """
    import random

    by_session: dict[str, list[dict[str, Any]]] = {}
    for episode in episodes:
        by_session.setdefault(episode.get("session_id") or "", []).append(episode)

    pool: list[dict[str, Any]] = []
    for session_id, sequence in by_session.items():
        human_turns = [e for e in sequence if e.get("origin") == ORIGIN_HUMAN]
        for index in range(1, len(human_turns)):
            previous, current = human_turns[index - 1], human_turns[index]
            if set(previous.get("files_edited") or []) & set(current.get("files_edited") or []):
                continue  # 這是候選，不是對照
            # 兩輪都毫無內容的話連人也蒸餾不出東西，放進對照組只會虛低產出率，
            # 讓訊號看起來比實際更有鑑別力
            if not (current.get("assistant_text") or previous.get("assistant_text")):
                continue
            pool.append({
                "session_id": session_id,
                "repo": current.get("repo"),
                "overlap_files": [],
                "short_followup": len(current.get("user_text") or "") < SHORT_INPUT_CHARS,
                "turns": [previous, current],
            })

    random.Random(seed).shuffle(pool)
    print(f"[distill] 對照組母體 {len(pool)} 組，抽樣 {min(sample_size, len(pool))} 組",
          file=sys.stderr)
    return pool[:sample_size]


def _turn_key(episode: dict[str, Any]) -> list[Any]:
    return [episode.get("prompt_id"), episode.get("turn_index")]


def _trim(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（截斷，原長 {len(text)} 字元）"


def build_task(candidate: dict[str, Any], index: int) -> dict[str, Any]:
    """把一組候選整理成蒸餾任務。

    **assistant_text 給的額度比 user_text 大。** 初版反過來，理由是「使用者說的話
    才是糾正的證據」——實測是錯的。使用者的糾正往往只有一句（「這個可以，換下一個」），
    真正可蒸餾的事實幾乎都在助手的回覆裡：它踩到專案特有的陷阱、講出根因，
    那才是模型下次不會再犯所需要的東西。實測 cand-000 的關鍵事實
    （既有 profile 走 cache-only 路徑，新欄位不會回填）落在第 400 字元之後，
    原本的 800 字元上限差點把它截掉。
    """
    previous, current = candidate["turns"]
    return {
        "id": f"cand-{index:03d}",
        "repo": candidate["repo"],
        "overlap_files": candidate["overlap_files"],
        "source_turns": [_turn_key(previous), _turn_key(current)],
        "before": {
            "user": _trim(previous.get("user_text"), 1200),
            "assistant": _trim(previous.get("assistant_text"), 2500),
            "files_edited": previous.get("files_edited") or [],
        },
        "after": {
            "user": _trim(current.get("user_text"), 1200),
            "assistant": _trim(current.get("assistant_text"), 2500),
            "files_edited": current.get("files_edited") or [],
        },
    }


# 蒸餾指示。與 Echo 的 _DISTILLATION_PROMPT 差在判準：
# 那邊問「可複用的抽象概念」，這邊問「模型本來不會知道的事」。
DISTILL_INSTRUCTIONS = """\
你在為 coding agent 的記憶層做知識蒸餾。

輸入是一組「同一個檔案在相鄰兩輪被重複修改」的對話片段。這個結構通常代表
第一次沒做對、第二次被修正——但**不一定**，也可能只是使用者同意繼續做下一項。

## 你要判斷的唯一問題

這組片段裡，有沒有一件**一個沒讀過這個專案的強力模型不會知道、或會猜錯**的事？

有價值的三類：
- `project-fact`：專案特有事實，尤其是文件或慣例會把人導向錯誤答案的那種
- `belief-correction`：模型的預設信念是錯的，這裡有反例
- `user-stance`：使用者的立場與模型預設相反

沒有價值的（**必須輸出空陣列**）：
- 通用最佳實踐——模型本來就會，注入只是浪費 context
- 一次性的除錯過程、這次改了什麼
- 使用者只是說「繼續」「可以」「換下一個」——那不是糾正
- 你不確定的時候。**寧可空手，不要湊數**。

## 輸出格式

只輸出 JSON，不要其他文字：

```json
{
  "concepts": [
    {
      "statement": "一句話陳述這件事。必須具體到可以被驗證對錯。",
      "kind": "project-fact | belief-correction | user-stance",
      "scope": "repo 名稱，跨專案通用則填 null",
      "probe": "一個開發問題，用來測試模型會不會主動講出這一點",
      "why": "為什麼你認為模型不會知道這件事"
    }
  ]
}
```

`probe` 是關鍵，出題規則：
- 問的是**開發任務**，不是問「你知不知道 X」——後者會觸發後見之明，模型看到
  答案幾乎都會說知道
- 不可在題目裡洩漏答案
- 題目要自然到「一個模型如果真的知道這件事，回答時就會自己提到它」

如果這組片段沒有這種東西，輸出 `{"concepts": []}`。這是常態，不是失敗。
"""


def emit(episodes: list[dict[str, Any]], task_path: Path, *, control: int = 0, seed: int = 20260807) -> int:
    if control:
        candidates = find_control_pairs(episodes, control, seed)
    else:
        candidates = find_candidates(episodes)
    tasks = [build_task(c, i) for i, c in enumerate(candidates)]
    payload = {
        "instructions": DISTILL_INSTRUCTIONS,
        "task_count": len(tasks),
        "tasks": tasks,
    }
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[distill] 從 {len(episodes)} 輪語料粗篩出 {len(tasks)} 組候選 → {task_path}",
          file=sys.stderr)
    return 0


def ingest(result_path: Path, concept_path: Path, task_path: Path) -> int:
    """收回蒸餾結果，加上溯源後存檔。

    溯源（哪些輪次生出這條）對應 Echo 的 DERIVED_FROM 邊。這裡不建圖，
    但欄位先留著——Phase 2 接檢索時需要它回頭驗證記憶是否仍然成立。
    """
    # 蒸餾是分批平行跑的，結果散在多個檔案裡，所以也接受目錄
    sources = sorted(result_path.glob("*.json")) if result_path.is_dir() else [result_path]
    entries: list[dict[str, Any]] = []
    for source in sources:
        payload = json.loads(source.read_text(encoding="utf-8"))
        entries.extend(payload if isinstance(payload, list) else payload.get("results", []))

    tasks = {t["id"]: t for t in json.loads(task_path.read_text(encoding="utf-8"))["tasks"]}
    print(f"[distill] 讀入 {len(sources)} 個結果檔、{len(entries)} 組判定", file=sys.stderr)

    concepts: list[dict[str, Any]] = []
    seen_statements: set[str] = set()
    for entry in entries:
        task = tasks.get(entry.get("id"))
        if task is None:
            continue
        for concept in entry.get("concepts") or []:
            statement = (concept.get("statement") or "").strip()
            # 去重比對正規化過的陳述——同一件事會從多組候選被抽出來
            key = " ".join(statement.split()).lower()
            if not statement or key in seen_statements:
                continue
            seen_statements.add(key)
            concepts.append({
                "id": f"c-{len(concepts):03d}",
                "statement": statement,
                "kind": concept.get("kind"),
                "scope": concept.get("scope") or task.get("repo"),
                "probe": concept.get("probe"),
                "why": concept.get("why"),
                # 溯源：Phase 2 要靠它判斷記憶是否已經失效
                "source_candidate": task["id"],
                "source_turns": task["source_turns"],
                "source_files": task["overlap_files"],
                # 行為測試填這兩欄，蒸餾階段一律留空。
                # LLM 自評不可靠（實測準確率 60-70%，且在最有價值的條目上系統性失準），
                # 所以這裡沒有任何自評欄位可以先填。
                "surprisal": None,
                "probe_result": None,
            })

    concept_path.write_text(json.dumps(concepts, ensure_ascii=False, indent=2), encoding="utf-8")
    kinds: dict[str, int] = {}
    for c in concepts:
        kinds[c.get("kind") or "?"] = kinds.get(c.get("kind") or "?", 0) + 1
    print(f"[distill] 收回 {len(concepts)} 條 concept（去重後）→ {concept_path}", file=sys.stderr)
    print(f"  kind 分布: {kinds}", file=sys.stderr)
    return 0


def show(task_path: Path, spec: str) -> int:
    """印出指定範圍的任務（``--show 0-12``），供蒸餾者讀取。

    存在的理由：整份任務檔 646 KB，一次讀進 context 會吃掉大半額度。
    分批才能平行處理。
    """
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    start, _, end = spec.partition("-")
    tasks = payload["tasks"][int(start):int(end or start) + 1]

    print(payload["instructions"])
    print(f"\n{'=' * 70}\n以下是 {len(tasks)} 組候選。\n{'=' * 70}")
    for task in tasks:
        print(f"\n{'=' * 70}\nID: {task['id']} | repo: {task['repo']}")
        print(f"重疊檔案: {', '.join(task['overlap_files'])}")
        for phase in ("before", "after"):
            block = task[phase]
            print(f"\n--- {phase.upper()} 使用者 ---\n{block['user']}")
            print(f"\n--- {phase.upper()} 助手 ---\n{block['assistant']}")
    return 0


def stats(episodes: list[dict[str, Any]]) -> int:
    candidates = find_candidates(episodes)
    repos: dict[str, int] = {}
    short = 0
    for c in candidates:
        repos[c["repo"] or "?"] = repos.get(c["repo"] or "?", 0) + 1
        short += bool(c["short_followup"])
    print(f"[distill] 語料 {len(episodes)} 輪 → 粗篩 {len(candidates)} 組候選", file=sys.stderr)
    print(f"  repo 分布: {repos}", file=sys.stderr)
    print(f"  其中後續輪輸入很短（<{SHORT_INPUT_CHARS} 字元）: {short}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1.5 concept 蒸餾")
    parser.add_argument("--stats", action="store_true", help="只看粗篩結果分布")
    parser.add_argument("--emit", action="store_true", help="產出蒸餾任務檔")
    parser.add_argument("--ingest", type=Path, help="收回蒸餾結果（JSON）")
    parser.add_argument("--show", type=str, help="印出指定範圍的任務，例如 0-12")
    parser.add_argument("--control", type=int, default=0,
                        help="改抽 N 組『無檔案重疊』的對照輪對，用來量粗篩訊號的召回率")
    parser.add_argument("--seed", type=int, default=20260807, help="對照組抽樣種子（可重現）")
    parser.add_argument("--episode-dir", type=Path, default=DEFAULT_EPISODE_DIR)
    parser.add_argument("--task-path", type=Path, default=DEFAULT_TASK_PATH)
    parser.add_argument("--concept-path", type=Path, default=DEFAULT_CONCEPT_PATH)
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if args.ingest:
        return ingest(args.ingest, args.concept_path, args.task_path)

    if args.show:
        return show(args.task_path, args.show)

    episodes = load_episodes(args.episode_dir)
    if args.emit:
        return emit(episodes, args.task_path, control=args.control, seed=args.seed)
    return stats(episodes)


if __name__ == "__main__":
    sys.exit(main())
