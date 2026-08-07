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
"""

from __future__ import annotations

import argparse
import json
import sys
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


def emit_pairs(concept_path: Path, pair_path: Path, floor: float, max_per: int) -> int:
    concepts = load_concepts(concept_path)
    pairs = build_pairs(concepts, floor, max_per)
    pair_path.parent.mkdir(parents=True, exist_ok=True)
    pair_path.write_text(json.dumps({
        "instructions": JUDGE_INSTRUCTIONS,
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

    if args.transitive:
        return transitive(args.transitive, args.concept_path, args.pair_path, args.apply)
    if args.ingest:
        return ingest(args.ingest, args.concept_path, args.pair_path)
    if args.show:
        return show(args.pair_path, args.show)
    if args.pairs:
        return emit_pairs(args.concept_path, args.pair_path, args.floor, args.max_per)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
