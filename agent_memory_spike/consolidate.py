#!/usr/bin/env python3
"""Phase 2.5：把蒸餾出來的 concept 池收斂。

處理兩個實測抓到的問題，它們**第一步完全相同**（都要先找出語意相近的 concept 對），
所以合成一支工具，只在 LLM 判斷時問不同的問題：

1. **語意重複**。字串去重幾乎沒用——780 條只去掉 5 條。
   相鄰窗口重疊會讓同一件事被多組候選各抽一次，措辭不同就躲過字串比對。
2. **記憶被後續語料推翻**。四批蒸餾者各自撞到「這條事實在後面幾輪就被改掉了」，
   而它們能自救純屬運氣——衝突剛好落在同一批。
   561 組拆成 24 批、各批獨立判斷，跨批的一律漏網。

第二個問題比第一個嚴重得多：重複只是浪費 context，
**過期的記憶是錯的，而且錯得理直氣壯**——它曾經是對的，所以寫得具體、有說服力，
檢索時也容易命中。

## 為什麼判定要交給 LLM

「這兩條是同一件事嗎」和「這兩條互相矛盾嗎」都需要語意判斷。
餘弦相似度只能挑出**值得看的配對**，挑不出關係是什麼——
高相似度既可能是重複，也可能是「同一個主題的兩個對立結論」，
那正是矛盾的典型長相。

## 為什麼不直接用時序決勝負

想過「新的一律壓過舊的」，不行：兩條關於同一檔案的記憶可以都成立
（一條講欄位語意、一條講呼叫慣例），時序在那種情況下毫無意義。
必須先確認關係是矛盾，時序才有資格當裁決依據。

## 用法

    python consolidate.py --pairs          # 算相似對，寫出候選配對
    python consolidate.py --show 0-19      # 取一批配對（給判定者）
    python consolidate.py --ingest <dir>   # 收回判定並套用

雙評審（見 ``panel``）：

    python consolidate.py --pairs --mode contradiction --floor 0.72
    python consolidate.py --show 0-39                      # 兩位評審各跑一次
    python consolidate.py --panel <dirA> <dirB>            # 合議 → 共識 + 爭議
    python consolidate.py --show 0-9 --pair-path <爭議檔>   # 仲裁
    python consolidate.py --panel <dirA> <dirB> <dirC>     # 三票多數決 → 最終判定
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from distill import DEFAULT_CONCEPT_PATH, WORK_DIR  # noqa: E402
from retrieve import VectorIndex, document_text  # noqa: E402

DEFAULT_PAIR_PATH = WORK_DIR / "consolidate_pairs.json"

# 低於這個餘弦相似度就不值得請 LLM 看。
# 訂在 0.80：實測 0.75 以下多半是「同一個 repo 的不相干兩條」，
# 而重複與矛盾都需要談論同一個對象才可能成立。
SIMILARITY_FLOOR = 0.80

# 每條最多配幾組。沒有上限的話，同一件事被抽出 8 次就會產生 28 組配對，
# 判定成本爆炸而資訊高度重複
MAX_PAIRS_PER_CONCEPT = 4


JUDGE_INSTRUCTIONS = """\
你在收斂一個 coding agent 的記憶池。輸入是兩條語意相近的記憶，判斷它們的關係。

## 三種關係

- `DUPLICATE`：**講的是同一件事**，只是措辭不同。保留其中一條就夠了。
- `CONTRADICTION`：**互相衝突，不可能同時為真**。典型長相是同一個對象的兩個對立結論
  （「X 欄位可以信」vs「X 欄位不可信」），通常是後來的改動推翻了先前的事實。
- `DISTINCT`：**都成立**，只是主題相近。這是最常見的答案。

## 判斷紀律

同一個檔案、同一個函式的兩條記憶**多半是 DISTINCT** ——
一條講欄位語意、一條講呼叫慣例，兩者都對。
相似度高只代表它們談論同一個對象，不代表關係特殊。

**不確定就給 DISTINCT。** 誤判成 DUPLICATE 會刪掉一條真實的記憶，
誤判成 CONTRADICTION 會刪掉一條仍然成立的事實——兩者都是不可逆的損失，
而多留一條重複的代價只是浪費一點 context。

## 輸出格式

只輸出 JSON，不要其他文字：

```json
{
  "verdicts": [
    {
      "pair_id": "配對的 id，原樣抄回",
      "relation": "DUPLICATE | CONTRADICTION | DISTINCT",
      "keep": "DUPLICATE 或 CONTRADICTION 時填要保留的那條的 concept id；DISTINCT 填 null",
      "why": "一句話說明依據"
    }
  ]
}
```

`keep` 的選法：
- `DUPLICATE`：留**寫得更具體、錨點更完整**的那條，不是更長的那條
- `CONTRADICTION`：留**來源輪次較晚**的那條（每組配對都附了時序），
  因為後來的改動推翻了先前的事實
"""


# 矛盾專用準則。**與 ``JUDGE_INSTRUCTIONS`` 的差別是刻意的，不是措辭變體。**
#
# Phase 2.8 量到評審間一致性 81%，而 6 組不一致**全部同方向**
# （第一輪 DISTINCT → 第二輪 DUPLICATE/CONTRADICTION，反方向 0 組）。
# 根因是上面那份準則寫「不確定就給 DISTINCT」——對重複來說那是對的
# （多留一條重複只浪費 context），但套到矛盾上就變成系統性低估：
# **漏掉一條過期記憶的代價遠大於多留一條重複**，它會被檢索命中並讓人相信錯的事。
#
# 所以這份準則做三件事：
# 1. 只問矛盾，不問重複——這條通道跑在 0.72 的低門檻上，
#    該區間的重複收益低（實測命中率 6.7%），而矛盾值得撈
# 2. 把問題換成可判定的形式：「兩條能否同時為真」，而不是「它們關係特殊嗎」
# 3. 不給任何一邊當安全答案，改為要求說出理由——雙評審才有分歧可比對
CONTRADICTION_INSTRUCTIONS = """\
你在檢查一個 coding agent 的記憶池有沒有**已經過期的記憶**。
輸入是兩條談論同一個對象的記憶，只回答一個問題：

> **這兩條能不能同時為真？**

## 兩種答案

- `CONTRADICTION`：**不能同時為真**。典型長相是同一個對象的兩個對立結論
  （「X 欄位可以信」vs「X 欄位不可信」、「用 A 做法」vs「A 做法已被改掉」），
  通常是後來的改動推翻了先前的事實。
- `DISTINCT`：**可以同時為真**。包含「都對，只是主題相近」
  （一條講欄位語意、一條講呼叫慣例）、以及「講的根本是同一件事」——
  這條通道不處理重複，措辭不同但意思一樣的一律給 DISTINCT。

## 判斷紀律

**這裡沒有安全答案。** 兩個方向都是實質的損失：

- 漏掉一組真矛盾 → 一條**錯的**記憶留在池子裡。它曾經是對的，所以寫得具體、
  有說服力、檢索時容易命中——**錯得理直氣壯**，比沒有記憶更糟。
- 誤判成矛盾 → 一條仍然成立的事實被刪掉，不可逆。

所以不要用「不確定就選某一邊」來省事。**逐條問自己：如果這兩句話同時貼在
同一份文件上，讀的人會不會被誤導？** 會 → CONTRADICTION，不會 → DISTINCT。

版本演進不算矛盾：「舊版用 A」與「現在用 B」如果兩條都明確標示了時期，
讀的人不會被誤導。**沒有標示時期、寫得像現行狀態的那種才算。**

## 輸出格式

只輸出 JSON，不要其他文字：

```json
{
  "verdicts": [
    {
      "pair_id": "配對的 id，原樣抄回",
      "relation": "CONTRADICTION | DISTINCT",
      "keep": "CONTRADICTION 時填要保留的那條的 concept id；DISTINCT 填 null",
      "why": "一句話說明依據——**每一組都要填**，包括 DISTINCT"
    }
  ]
}
```

`keep` 一律選**來源輪次較晚**的那條（每組配對都附了時序），
因為後來的改動推翻了先前的事實。
"""

# 仲裁準則。只在兩位評審分歧時用，所以它看得到雙方的判定與理由——
# 那是單獨判一次拿不到的資訊，也是仲裁唯一的優勢。
ARBITRATION_INSTRUCTIONS = """\
你在仲裁兩位評審對記憶池配對的分歧。每一組都附了雙方的判定與理由。

## 你的任務

判斷哪一方是對的，或者兩方都錯。問題與評審看到的相同：
**這兩條記憶能不能同時為真？**

- `CONTRADICTION`：不能同時為真，後來的改動推翻了先前的事實
- `DUPLICATE`：講的是同一件事，只是措辭不同
- `DISTINCT`：都成立，只是主題相近

## 仲裁紀律

**不要預設「兩票裡比較保守的那個」是對的。** 已知的偏誤方向正好相反：
單獨判定時 DISTINCT 是省事的答案，於是重複與過期被系統性低估
（實測 6 組不一致全部同方向）。分歧本身就是訊號——
有一位評審看出了什麼，你的工作是判斷那個東西是不是真的。

但也不要因此一律採信非 DISTINCT 的那票。**逐條讀理由**：
它指出的衝突點在陳述裡真的存在嗎？還是它把「主題相同」誤讀成「結論對立」？

## 輸出格式

只輸出 JSON：

```json
{
  "verdicts": [
    {
      "pair_id": "原樣抄回",
      "relation": "CONTRADICTION | DUPLICATE | DISTINCT",
      "keep": "非 DISTINCT 時填要保留的 concept id；DISTINCT 填 null",
      "why": "一句話說明你採信哪一方、依據是什麼"
    }
  ]
}
"""

INSTRUCTION_MODES = {
    "relation": JUDGE_INSTRUCTIONS,
    "contradiction": CONTRADICTION_INSTRUCTIONS,
}


def load_concepts(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_turn(concept: dict[str, Any]) -> str:
    """這條記憶的來源輪次裡最晚的那個，當時序依據。

    用 prompt_id 而不是時間戳：episode 沒存時間戳，
    但 source_turns 的順序反映了語料裡的先後。
    """
    turns = concept.get("source_turns") or []
    return str(turns[-1][0]) if turns else ""


def build_pairs(concepts: list[dict[str, Any]], floor: float,
                max_per: int) -> list[dict[str, Any]]:
    """找出值得送去判定的相似對。

    **只在同一個 scope 內配對**：跨 repo 的兩條記憶不可能是重複或矛盾，
    而全量兩兩比對是 O(n²)，775 條就有 30 萬對。
    """
    by_scope: dict[str, list[int]] = {}
    for index, concept in enumerate(concepts):
        by_scope.setdefault(str(concept.get("scope")), []).append(index)

    texts = [document_text(c, with_cue=False) for c in concepts]
    print(f"[consolidate] 取 {len(texts)} 條的向量……", file=sys.stderr)
    vectors = VectorIndex(texts).matrix  # 已正規化，點積即餘弦

    pairs: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for scope, indices in sorted(by_scope.items()):
        if len(indices) < 2:
            continue
        scored: list[tuple[float, int, int]] = []
        for position, i in enumerate(indices):
            for j in indices[position + 1:]:
                similarity = sum(a * b for a, b in zip(vectors[i], vectors[j]))
                if similarity >= floor:
                    scored.append((similarity, i, j))
        scored.sort(key=lambda row: -row[0])

        used: dict[int, int] = {}
        for similarity, i, j in scored:
            if used.get(i, 0) >= max_per or used.get(j, 0) >= max_per:
                continue
            if (i, j) in seen:
                continue
            seen.add((i, j))
            used[i] = used.get(i, 0) + 1
            used[j] = used.get(j, 0) + 1
            left, right = concepts[i], concepts[j]
            pairs.append({
                "pair_id": f"p-{len(pairs):04d}",
                "scope": scope,
                "similarity": round(similarity, 4),
                "left": {
                    "id": left.get("id"),
                    "statement": left.get("statement"),
                    "anchors": left.get("anchors"),
                    "source_turns": left.get("source_turns"),
                },
                "right": {
                    "id": right.get("id"),
                    "statement": right.get("statement"),
                    "anchors": right.get("anchors"),
                    "source_turns": right.get("source_turns"),
                },
            })
        print(f"  {scope}: {len(indices)} 條 → {len(scored)} 對超過門檻", file=sys.stderr)
    return pairs


def emit_pairs(concept_path: Path, pair_path: Path, floor: float, max_per: int,
               mode: str = "relation", skip_paths: list[Path] | None = None) -> int:
    concepts = load_concepts(concept_path)
    pairs = build_pairs(concepts, floor, max_per)

    if skip_paths:
        # 已經判過的組不必再花一次判定成本。比對用 concept id 的無序對，
        # 不用 pair_id——pair_id 按位置編號，重算配對後會整批位移
        judged = _judged_key_set(skip_paths)
        before = len(pairs)
        pairs = [p for p in pairs
                 if frozenset((p["left"]["id"], p["right"]["id"])) not in judged]
        # 重編號：pair_id 必須與檔案裡的順序一致，否則 --show 取出來的
        # 那一批與判定者抄回的 id 對不上
        for position, pair in enumerate(pairs):
            pair["pair_id"] = f"p-{position:04d}"
        print(f"[consolidate] 略過 {before - len(pairs)} 組已判定過的配對", file=sys.stderr)

    pair_path.parent.mkdir(parents=True, exist_ok=True)
    pair_path.write_text(json.dumps({
        "instructions": INSTRUCTION_MODES[mode],
        "mode": mode,
        "pair_count": len(pairs),
        "pairs": pairs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[consolidate] {len(concepts)} 條 → {len(pairs)} 組待判定配對 → {pair_path}",
          file=sys.stderr)
    return 0


def show(pair_path: Path, spec: str) -> int:
    payload = json.loads(pair_path.read_text(encoding="utf-8"))
    start, _, end = spec.partition("-")
    pairs = payload["pairs"][int(start):int(end or start) + 1]

    print(payload["instructions"])
    print(f"\n{'=' * 70}\n以下是 {len(pairs)} 組配對。\n{'=' * 70}")
    for pair in pairs:
        print(f"\n{'=' * 70}\npair_id: {pair['pair_id']} | scope: {pair['scope']} "
              f"| 相似度: {pair['similarity']}")
        for side in ("left", "right"):
            block = pair[side]
            print(f"\n--- {side.upper()} (id={block['id']}) ---")
            print(f"陳述: {block['statement']}")
            print(f"錨點: {', '.join(block['anchors'] or []) or '（無）'}")
            print(f"來源輪次: {block['source_turns']}")
        # 仲裁用的配對帶著雙方的票。沒有這段，仲裁者看到的東西與評審完全一樣，
        # 那就只是再擲一次骰子而不是仲裁
        for vote in pair.get("votes") or []:
            print(f"\n--- 評審 {vote['judge']} 判 {vote['relation']}"
                  f"{'（保留 ' + vote['keep'] + '）' if vote.get('keep') else ''} ---")
            print(f"理由: {vote.get('why') or '（未填）'}")
    return 0


def ingest(result_path: Path, concept_path: Path, pair_path: Path) -> int:
    """收回判定並套用。

    **只刪不改**：被判定為重複或過期的那條直接從池子移除，
    保留的那條完全不動。想過把兩條合併成一條更完整的陳述，不做——
    那等於讓判定者重寫記憶內容，而它看到的只有兩行字，
    沒有原始語料，改出來的東西無從驗證。
    """
    sources = sorted(result_path.glob("*.json")) if result_path.is_dir() else [result_path]
    verdicts: list[dict[str, Any]] = []
    for source in sources:
        payload = json.loads(source.read_text(encoding="utf-8"))
        verdicts.extend(payload if isinstance(payload, list) else payload.get("verdicts", []))

    pairs = {p["pair_id"]: p for p in json.loads(pair_path.read_text(encoding="utf-8"))["pairs"]}
    concepts = load_concepts(concept_path)
    by_id = {c.get("id"): c for c in concepts}

    dropped: dict[str, str] = {}
    counts = {"DUPLICATE": 0, "CONTRADICTION": 0, "DISTINCT": 0}
    unmatched = 0

    for verdict in verdicts:
        pair = pairs.get(verdict.get("pair_id"))
        if pair is None:
            unmatched += 1
            continue
        relation = verdict.get("relation")
        counts[relation] = counts.get(relation, 0) + 1
        if relation not in ("DUPLICATE", "CONTRADICTION"):
            continue

        keep = verdict.get("keep")
        ids = {pair["left"]["id"], pair["right"]["id"]}
        if keep not in ids:
            # 判定者填了不存在的 id，寧可整組跳過也不要亂刪
            unmatched += 1
            continue
        loser = (ids - {keep}).pop()
        # 已經因為別組配對被刪掉的，不重複記
        dropped.setdefault(loser, relation)

    survivors = [c for c in concepts if c.get("id") not in dropped]
    concept_path.write_text(json.dumps(survivors, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[consolidate] 判定 {len(verdicts)} 組: {counts}", file=sys.stderr)
    print(f"  移除 {len(dropped)} 條（重複 "
          f"{sum(1 for r in dropped.values() if r == 'DUPLICATE')}、"
          f"過期 {sum(1 for r in dropped.values() if r == 'CONTRADICTION')}）", file=sys.stderr)
    print(f"  {len(concepts)} → {len(survivors)} 條 → {concept_path}", file=sys.stderr)
    if unmatched:
        print(f"  ⚠ {unmatched} 組判定無法套用（pair_id 對不上或 keep 不是配對中的 id）",
              file=sys.stderr)
    if by_id and not survivors:
        print("  ⚠ 池子被清空了，這幾乎不可能是對的——請檢查判定結果", file=sys.stderr)
    return 0


class _Union:
    """等價類。DUPLICATE 是等價關係，但配對是兩兩產生的，關係本身不會自己閉合。"""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a

    def classes(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for item in self.parent:
            groups.setdefault(self.find(item), []).append(item)
        return groups


def _load_verdicts(result_path: Path, pair_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sources = sorted(result_path.glob("*.json")) if result_path.is_dir() else [result_path]
    verdicts: list[dict[str, Any]] = []
    for source in sources:
        payload = json.loads(source.read_text(encoding="utf-8"))
        verdicts.extend(payload if isinstance(payload, list) else payload.get("verdicts", []))
    pairs = {p["pair_id"]: p for p in json.loads(pair_path.read_text(encoding="utf-8"))["pairs"]}
    return verdicts, pairs


def run_judges(pair_path: Path, out_root: Path, judge_count: int, batch_size: int,
               concurrency: int) -> int:
    """把整份配對檔交給 N 位獨立評審，各自分批判定。

    **每位評審是一次獨立的 headless 呼叫，彼此看不到對方的判定。**
    這是雙評審唯一有意義的前提——共用上下文的兩次判定不是兩票，
    是同一票講了兩次。

    分批是必要的：一次塞 284 組進去，判定品質會隨長度衰減，
    而且中途失敗就整批重來。批次大小訂在 40 是沿用管線既有的成本封頂。
    """
    from pipeline import AUTO_PREAMBLE, TOOL_PYTHON, adjudicate_to_file  # noqa: PLC0415

    payload = json.loads(pair_path.read_text(encoding="utf-8"))
    total = len(payload["pairs"])
    if total == 0:
        print("[judges] 沒有待判定的配對", file=sys.stderr)
        return 0

    jobs: list[tuple[int, int, int, int]] = []  # (評審, 批次, 起, 迄)
    for judge in range(judge_count):
        for batch, start in enumerate(range(0, total, batch_size)):
            jobs.append((judge, batch, start, min(start + batch_size, total) - 1))

    print(f"[judges] {total} 組 × {judge_count} 位評審 = {len(jobs)} 次裁決"
          f"（併發 {concurrency}）", file=sys.stderr)

    def run(job: tuple[int, int, int, int]) -> tuple[tuple[int, int], bool, str]:
        judge, batch, start, end = job
        out_path = out_root / f"judge{judge}" / f"batch-{batch:02d}.json"
        if out_path.exists():
            # 續跑：整批重來很貴，而且已經落地的判定沒有理由丟掉
            return (judge, batch), True, f"已存在，略過 {out_path.name}"
        prompt = (
            AUTO_PREAMBLE + "記憶池的配對判定。在這個 repo 底下執行：\n\n"
            f'"{TOOL_PYTHON}" agent_memory_spike/consolidate.py --show {start}-{end} '
            f'--pair-path "{pair_path}"\n\n'
            "輸出開頭是判定準則，照著做。除了那個指令之外不需要讀取其他檔案。\n"
            "**不要寫任何檔案**——把結果直接以一個 ```json 區塊回覆給我即可。"
        )
        ok, detail = adjudicate_to_file(prompt, out_path)
        return (judge, batch), ok, detail

    results: list[tuple[tuple[int, int], bool, str]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for result in pool.map(run, jobs):
            (judge, batch), ok, detail = result
            print(f"  {'OK  ' if ok else 'FAIL'} judge{judge}/batch-{batch:02d}: {detail}",
                  file=sys.stderr)
            results.append(result)

    failed = [r for r in results if not r[1]]
    print(f"[judges] {len(results) - len(failed)}/{len(results)} 成功", file=sys.stderr)
    if failed:
        # 失敗的批次沒有落地檔案，重跑同一個指令只會補上缺的那幾批
        print("  ⚠ 失敗的批次沒有寫檔，重跑同一個指令即可補齊", file=sys.stderr)
    return 1 if failed else 0


def _judged_key_set(specs: list[Path]) -> set[frozenset[str]]:
    """歷史上**真的被判定過**的配對，以 concept id 的無序對表示。

    每個 spec 的形式是 ``<配對檔>::<判定目錄>``，兩邊都要給。
    **不能只看配對檔**：實測 `consolidate_pairs_band.json` 裡有數百組配對，
    而 `consolidate_out_band/` 只判了其中 30 組——拿整份配對檔當「判過」，
    會把從未判過的組一起略過，而且是靜默的（少判的組不會有任何訊號）。
    """
    judged: set[frozenset[str]] = set()
    for spec in specs:
        text = str(spec)
        pair_part, sep, result_part = text.partition("::")
        if not sep:
            raise SystemExit(
                f"--skip-judged 要寫成 <配對檔>::<判定目錄>，收到 {text!r}"
            )
        pair_path, result_path = Path(pair_part), Path(result_part)
        if not pair_path.exists() or not result_path.exists():
            print(f"[consolidate] ⚠ {text} 有一邊不存在，略過", file=sys.stderr)
            continue
        pairs = {p["pair_id"]: p
                 for p in json.loads(pair_path.read_text(encoding="utf-8")).get("pairs", [])}
        decided = _collect_by_pair(result_path)
        hits = 0
        for pair_id in decided:
            pair = pairs.get(pair_id)
            if pair is None:
                continue
            judged.add(frozenset((pair["left"]["id"], pair["right"]["id"])))
            hits += 1
        print(f"[consolidate] {pair_path.name}: {len(pairs)} 組配對、"
              f"{len(decided)} 筆判定 → 認列 {hits} 組", file=sys.stderr)
    return judged


def _collect_by_pair(result_path: Path) -> dict[str, dict[str, Any]]:
    """一位評審的全部判定，按 pair_id 索引。

    同一個 pair_id 在一位評審的輸出裡出現兩次時**保留先出現的那筆**。
    重複多半是分批時邊界重疊，而 `dict[k] = v` 讓後寫者贏是隱形的資料遺失
    ——這個專案已經因為同一個形狀踩過兩次（bridge 的條文索引、空欄位分析腳本）。
    """
    sources = sorted(result_path.glob("*.json")) if result_path.is_dir() else [result_path]
    by_pair: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for source in sources:
        payload = json.loads(source.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("verdicts", [])
        for row in rows:
            pair_id = row.get("pair_id")
            if pair_id is None:
                continue
            if pair_id in by_pair:
                duplicates += 1
                continue
            by_pair[pair_id] = row
    if duplicates:
        print(f"  ⚠ {result_path.name}: {duplicates} 筆重複 pair_id，保留先出現的",
              file=sys.stderr)
    return by_pair


def _relation_of(row: dict[str, Any] | None) -> str:
    return (row or {}).get("relation") or "MISSING"


def panel(result_paths: list[Path], pair_path: Path, out_dir: Path) -> int:
    """把多位評審的判定合議成一份最終判定。

    ## 為什麼要雙評審

    Phase 2.8 重判 32 組量到評審間一致性只有 **81%**，而 6 組不一致
    **全部同方向**（DISTINCT → DUPLICATE/CONTRADICTION，反方向 0 組）。
    單評審不是隨機噪音，是**系統性低估**：判定準則寫「不確定就給 DISTINCT」，
    於是 DISTINCT 成了省事的答案，重複與過期一起被漏掉。

    多跑幾輪關係閉包救不了這個——閉包只能傳播已經判出來的關係，
    判漏的那組它看不見。要提高收斂品質，投資點在這裡。

    ## 合議規則

    - **兩票（含以上）一致**：直接採用。都判 DISTINCT 也算共識，配對就此結案
    - **一致但 keep 不同**：算爭議。刪哪一條是不可逆的，兩位評審選了不同的存活者
      時，沒有理由相信其中任何一個
    - **不一致**：算爭議，送仲裁
    - **三票以上**：多數決（同 relation 且同 keep 才算同一票）。
      **三票全異就給 DISTINCT**——那代表沒有任何兩個人看到同一件事，
      此時不刪是唯一不會造成不可逆損失的選擇

    輸出兩份檔案：共識的判定（可直接 ``--ingest``）與爭議組
    （格式與配對檔相同，可直接 ``--show`` 給仲裁者，且附上雙方的票）。
    """
    payload = json.loads(pair_path.read_text(encoding="utf-8"))
    pairs = {p["pair_id"]: p for p in payload["pairs"]}
    panels = [(path.name, _collect_by_pair(path)) for path in result_paths]

    consensus: list[dict[str, Any]] = []
    disputed: list[dict[str, Any]] = []
    counts = {"共識": 0, "爭議": 0, "未判": 0}
    # 分歧方向：(較保守的一方, 較積極的一方) → 次數。用來複驗 Phase 2.8 的
    # 「不一致全部同方向」是不是仍然成立
    directions: dict[tuple[str, str], int] = {}

    for pair_id, pair in pairs.items():
        votes = []
        for judge, by_pair in panels:
            row = by_pair.get(pair_id)
            if row is not None:
                votes.append({"judge": judge, "relation": _relation_of(row),
                              "keep": row.get("keep"), "why": row.get("why")})
        if len(votes) < len(panels):
            counts["未判"] += 1
            continue

        # 同 relation 且同 keep 才算同一票——DISTINCT 的 keep 一律視為 None，
        # 免得判定者填了 null 與空字串被算成兩種不同的票
        def _key(vote: dict[str, Any]) -> tuple[str, str | None]:
            relation = vote["relation"]
            return (relation, None if relation == "DISTINCT" else vote.get("keep"))

        tally: dict[tuple[str, str | None], int] = {}
        for vote in votes:
            tally[_key(vote)] = tally.get(_key(vote), 0) + 1
        best, best_count = max(tally.items(), key=lambda item: item[1])

        if best_count > len(votes) / 2:
            counts["共識"] += 1
            relation, keep = best
            consensus.append({
                "pair_id": pair_id, "relation": relation, "keep": keep,
                "why": next(v.get("why") for v in votes if _key(v) == best),
                "votes": [v["relation"] for v in votes],
            })
            continue

        # 三票全異：沒有任何兩個人看到同一件事，不刪是唯一不會造成不可逆損失的選擇
        if len(votes) >= 3 and best_count == 1:
            counts["共識"] += 1
            consensus.append({
                "pair_id": pair_id, "relation": "DISTINCT", "keep": None,
                "why": "三票全異，保守處理：不刪",
                "votes": [v["relation"] for v in votes],
            })
            continue

        counts["爭議"] += 1
        relations = sorted({v["relation"] for v in votes})
        if len(relations) == 2:
            conservative = "DISTINCT" if "DISTINCT" in relations else relations[0]
            other = next(r for r in relations if r != conservative)
            directions[(conservative, other)] = directions.get((conservative, other), 0) + 1
        disputed.append({**pair, "votes": votes})

    out_dir.mkdir(parents=True, exist_ok=True)
    consensus_path = out_dir / "consensus.json"
    consensus_path.write_text(json.dumps({"verdicts": consensus}, ensure_ascii=False,
                                         indent=2), encoding="utf-8")
    # 爭議組**保留原本的 pair_id**，不重新編號：仲裁結果要對回同一份配對檔，
    # 而 --ingest 是按 pair_id 查的
    disputed_path = out_dir / "disputed_pairs.json"
    disputed_path.write_text(json.dumps({
        "instructions": ARBITRATION_INSTRUCTIONS,
        "mode": "arbitration",
        "pair_count": len(disputed),
        "pairs": disputed,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    out = sys.stderr
    print(f"\n[panel] {len(panels)} 位評審 × {len(pairs)} 組配對", file=out)
    print(f"  共識 {counts['共識']}、爭議 {counts['爭議']}、未判 {counts['未判']}", file=out)
    decided = counts["共識"] + counts["爭議"]
    if decided:
        print(f"  評審間一致率 {counts['共識'] / decided:.1%}", file=out)
    actionable = [v for v in consensus if v["relation"] != "DISTINCT"]
    print(f"  共識中要處理的（非 DISTINCT）: {len(actionable)}", file=out)
    if directions:
        print("  分歧方向（保守 → 積極）:", file=out)
        for (conservative, other), n in sorted(directions.items(), key=lambda i: -i[1]):
            print(f"    {conservative} vs {other}: {n} 組", file=out)
    print(f"\n  共識 → {consensus_path}", file=out)
    print(f"  爭議 → {disputed_path}", file=out)
    if disputed:
        print(f"  仲裁: --show 0-{len(disputed) - 1} --pair-path {disputed_path}", file=out)
    return 0


def transitive(result_path: Path, concept_path: Path, pair_path: Path,
               apply_changes: bool) -> int:
    """把既有判定的關係閉包補上。

    **單輪兩兩配對處理不完矛盾。** 實測 `c-138`/`c-617` 被判與 `c-711` 重複，
    而 `c-711` 又被 `c-238` 推翻——那兩條講的是同一件事，所以同樣過期了，
    卻因為沒有被直接配對到而留在池子裡。過期的記憶是**錯得理直氣壯**的那種：
    它曾經是對的，寫得具體、有說服力，檢索時也容易命中。

    這裡不叫 LLM，純粹補上兩個閉包：

    1. **DUPLICATE 是等價關係**。A≡B、B≡C 就代表 A≡C，等價類裡只該留一條。
       兩兩判定各自選 keep，選出兩個不同的贏家時，同一件事仍會留下兩條。
    2. **矛盾沿等價類傳播**。B 被 C 推翻，而 A≡B，那 A 也被推翻了。

    做不到的部分要講清楚：**這解不了「該配對卻沒配到」**——
    A 與 C 語意相近但相似度沒過門檻，閉包無從得知。那要重算配對再判一輪，
    見 ``--pairs`` 的迭代用法。
    """
    verdicts, pairs = _load_verdicts(result_path, pair_path)
    concepts = load_concepts(concept_path)
    alive = {c.get("id") for c in concepts}

    union = _Union()
    keep_votes: dict[str, int] = {}
    contradictions: list[tuple[str, str]] = []  # (輸家, 贏家)

    for verdict in verdicts:
        pair = pairs.get(verdict.get("pair_id"))
        if pair is None:
            continue
        left, right = pair["left"]["id"], pair["right"]["id"]
        relation = verdict.get("relation")
        keep = verdict.get("keep")
        if relation == "DUPLICATE":
            union.union(left, right)
            if keep in (left, right):
                keep_votes[keep] = keep_votes.get(keep, 0) + 1
        elif relation == "CONTRADICTION" and keep in (left, right):
            loser = right if keep == left else left
            contradictions.append((loser, keep))

    # 矛盾沿等價類傳播。贏家所在的類要排除：同一類內部互相矛盾代表判定不一致，
    # 那種情況自動刪任何一邊都可能是錯的，只報告
    stale: dict[str, str] = {}
    inconsistent: list[tuple[str, str]] = []
    for loser, winner in contradictions:
        loser_class, winner_class = union.find(loser), union.find(winner)
        if loser_class == winner_class:
            inconsistent.append((loser, winner))
            continue
        for member in union.classes().get(loser_class, [loser]):
            stale.setdefault(member, winner)

    # 等價類裡多於一條存活的，收斂到被 keep 最多次的那條；
    # 平手時取 id 字典序最小，讓結果可重現
    redundant: dict[str, str] = {}
    for members in union.classes().values():
        survivors = sorted(m for m in members if m in alive and m not in stale)
        if len(survivors) < 2:
            continue
        winner = max(survivors, key=lambda m: (keep_votes.get(m, 0), [-ord(ch) for ch in m]))
        for member in survivors:
            if member != winner:
                redundant[member] = winner

    stale_alive = {k: v for k, v in stale.items() if k in alive}
    out = sys.stderr
    print(f"[transitive] 池子 {len(concepts)} 條、判定 {len(verdicts)} 組", file=out)
    print(f"  等價類 {len(union.classes())} 個、矛盾 {len(contradictions)} 組", file=out)
    print(f"  沿等價類傳播出的過期條目（仍在池子裡）: {len(stale_alive)}", file=out)
    for item, winner in sorted(stale_alive.items()):
        concept = next((c for c in concepts if c.get("id") == item), {})
        print(f"    {item} ← 被 {winner} 推翻 | {(concept.get('statement') or '')[:70]}", file=out)
    print(f"  等價類內重複存活: {len(redundant)}", file=out)
    for item, winner in sorted(redundant.items()):
        concept = next((c for c in concepts if c.get("id") == item), {})
        print(f"    {item} ≡ {winner} | {(concept.get('statement') or '')[:70]}", file=out)
    if inconsistent:
        print(f"  ⚠ {len(inconsistent)} 組矛盾發生在同一個等價類內部——"
              f"判定自相牴觸，不自動處理: {inconsistent}", file=out)

    drop = set(stale_alive) | set(redundant)
    if not drop:
        print("\n[transitive] 沒有需要處理的條目", file=out)
        return 0
    if not apply_changes:
        print(f"\n[transitive] 共 {len(drop)} 條可移除。加 --apply 才會寫入。", file=out)
        return 0

    survivors = [c for c in concepts if c.get("id") not in drop]
    concept_path.write_text(json.dumps(survivors, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[transitive] {len(concepts)} → {len(survivors)} 條 → {concept_path}", file=out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2.5 concept 池收斂")
    parser.add_argument("--pairs", action="store_true", help="算相似對並寫出待判定配對")
    parser.add_argument("--show", type=str, help="印出指定範圍的配對，例如 0-19")
    parser.add_argument("--ingest", type=Path, help="收回判定結果並套用")
    parser.add_argument("--transitive", type=Path,
                        help="對既有判定補上關係閉包（等價類 + 矛盾傳播），預設只報告")
    parser.add_argument("--apply", action="store_true", help="搭配 --transitive 才真的寫入")
    parser.add_argument("--run-judges", type=int, metavar="N",
                        help="用 headless claude -p 跑 N 位獨立評審")
    parser.add_argument("--judge-out", type=Path, help="搭配 --run-judges，評審輸出根目錄")
    parser.add_argument("--batch-size", type=int, default=40, help="每位評審每批判幾組")
    parser.add_argument("--concurrency", type=int, default=4, help="同時跑幾次裁決")
    parser.add_argument("--panel", type=Path, nargs="+",
                        help="合議多位評審的判定目錄 → 共識 + 爭議組")
    parser.add_argument("--panel-out", type=Path,
                        help="搭配 --panel，輸出目錄（預設 <pair-path 同層>/panel）")
    parser.add_argument("--mode", choices=sorted(INSTRUCTION_MODES), default="relation",
                        help="配對要問什麼：relation（重複+矛盾）或 contradiction（只問矛盾）")
    parser.add_argument("--skip-judged", type=Path, nargs="*", metavar="配對檔::判定目錄",
                        help="搭配 --pairs，略過已經判定過的組（兩邊都要給，只給配對檔會連沒判的一起略過）")
    parser.add_argument("--floor", type=float, default=SIMILARITY_FLOOR)
    parser.add_argument("--max-per", type=int, default=MAX_PAIRS_PER_CONCEPT)
    parser.add_argument("--concept-path", type=Path, default=DEFAULT_CONCEPT_PATH)
    parser.add_argument("--pair-path", type=Path, default=DEFAULT_PAIR_PATH)
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if args.run_judges:
        out_root = args.judge_out or args.pair_path.parent / "judges"
        return run_judges(args.pair_path, out_root, args.run_judges,
                          args.batch_size, args.concurrency)
    if args.panel:
        out_dir = args.panel_out or args.pair_path.parent / "panel"
        return panel(args.panel, args.pair_path, out_dir)
    if args.transitive:
        return transitive(args.transitive, args.concept_path, args.pair_path, args.apply)
    if args.ingest:
        return ingest(args.ingest, args.concept_path, args.pair_path)
    if args.show:
        return show(args.pair_path, args.show)
    if args.pairs:
        return emit_pairs(args.concept_path, args.pair_path, args.floor, args.max_per,
                          mode=args.mode, skip_paths=args.skip_judged)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
