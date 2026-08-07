#!/usr/bin/env python3
"""Phase 1.5：用行為測試校準每條 concept 的 surprisal。

## 為什麼不能用 LLM 自評

``experiment/surprisal-calibration.md`` 的實測結論：自評準確率約 60–70%，
而且**在最有價值的條目上系統性失準**。兩個評審把鑑別度最強的兩條
（無注入組會給出完全相反答案的那兩條）都標成「我會主動講」——
用自評篩選的話，最該留的記憶會被第一個剔除。

原因是模型無法內省自己的錯誤信念：它把「我對這個主題有看法」
誤認成「我的看法是對的」。

所以唯一可靠的方法是行為測試——拿 probe 去問一個沒看過答案的 agent，
看它會不會自己講出那一點。

## 三種判定（沿用 surprisal-calibration.md 的分類）

- ``VOLUNTEER``：主動講出來了 → surprisal 0，**零價值**，注入純浪費 context
- ``SILENT``：沒提到 → 有價值
- ``CONTRARY``：講了相反的 → **最高價值**，這是模型自信地相信錯誤的事

## 兩個角色必須分離

受測者是乾淨 subagent（沒看過 statement），判卷者是另一個角色（看得到
statement 與受測者的回答）。兩者合一就退化成自評。

## 乾淨的定義（比想像中脆弱）

受測 subagent 會自動吃到 CLAUDE.md 與記憶注入，手上還有檔案工具。
它只要去讀一下目標 repo 就能查到答案——那測到的是檢索能力，不是先驗知識。
所以 probe 的指示必須明確禁止任何查找，這一點在 ``PROBE_INSTRUCTIONS`` 裡。

## 用法

    python calibrate.py --emit                    # 產出行為測試題目
    python calibrate.py --ingest <verdicts.json>  # 收回判定結果
    python calibrate.py --report                  # 看校準後的分布
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from hook_stop import DEFAULT_EPISODE_DIR  # noqa: E402

WORK_DIR = DEFAULT_EPISODE_DIR.parent
DEFAULT_CONCEPT_PATH = WORK_DIR / "concepts.json"
DEFAULT_PROBE_PATH = WORK_DIR / "probe_tasks.json"

# 判定 → surprisal 分數。
# VOLUNTEER 是 0 而不是某個小數：模型本來就會講的東西，注入的價值不是「低」，
# 而是負的——它佔掉 context 卻不改變任何行為。gm-022 甚至更糟，
# 模型主動答出的版本比記憶本身更準確完整，注入反而讓 agent 變笨。
VERDICT_SCORES = {
    "VOLUNTEER": 0.0,
    "PARTIAL": 0.4,
    "SILENT": 0.8,
    "CONTRARY": 1.0,
}

# 刻意不說「這是一次知識測試」。講明了會讓模型進入應試狀態——變得異常謹慎、
# 把所有想得到的注意事項都列一遍，人為抬高 VOLUNTEER 率，測到的就不是它平常的行為。
# 要測的是「它在正常開發對話裡會不會自己提到這一點」，所以框架必須是正常的開發諮詢。
PROBE_INSTRUCTIONS = """\
以下是幾個開發上的問題。這些專案不在你手上，你也拿不到它們的原始碼，
所以請直接依你既有的知識與經驗回答——**不要去讀檔案、grep、搜尋或查任何資料**，
查了也找不到對的東西。

就照你平常回答同事的方式回答：你會怎麼做、其中有什麼要留意的地方。
不確定的部分講你的判斷即可，不要拒答，也不要反問。
"""

JUDGE_INSTRUCTIONS = """\
你在替一場知識測試判卷。

你會看到三樣東西：一條「陳述」、一個「題目」、以及一個受測 agent 的「回答」。
受測者沒有看過那條陳述。

判斷受測者的回答與那條陳述的關係，四選一：

- `VOLUNTEER`：回答裡主動講出了陳述的內容（用詞不必相同，意思到就算）
- `PARTIAL`：沾到邊但不完整，或講得含糊、沒有到可操作的程度
- `SILENT`：完全沒提到這件事
- `CONTRARY`：講了與陳述**相反**的話，或提出了會踩到這個坑的做法

判定要嚴格。「受測者提到了相關的主題」不等於 VOLUNTEER——
必須是它真的講出了那個具體的點。含糊帶過算 PARTIAL。

只輸出 JSON：

```json
{"verdict": "SILENT", "evidence": "回答裡最能支持這個判定的一小段引文", "note": "一句話說明"}
```
"""


def load_concepts(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def emit(concept_path: Path, probe_path: Path) -> int:
    concepts = load_concepts(concept_path)
    pending = [c for c in concepts if c.get("probe") and c.get("surprisal") is None]
    payload = {
        "probe_instructions": PROBE_INSTRUCTIONS,
        "judge_instructions": JUDGE_INSTRUCTIONS,
        "count": len(pending),
        "probes": [
            {"id": c["id"], "probe": c["probe"], "statement": c["statement"], "scope": c.get("scope")}
            for c in pending
        ],
    }
    probe_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[calibrate] {len(pending)} 條待校準 → {probe_path}", file=sys.stderr)
    return 0


def show_probes(probe_path: Path, spec: str) -> int:
    """只印出題目，**絕不印 statement**。

    受測者看到 statement 就等於看到答案，整場測試立刻失去意義——
    退化成自評，而自評的準確率只有 60-70% 且在最有價值的條目上系統性失準。
    分批也是刻意的：一個受測者一次答太多題會開始猜測出題意圖。
    """
    payload = json.loads(probe_path.read_text(encoding="utf-8"))
    start, _, end = spec.partition("-")
    probes = payload["probes"][int(start):int(end or start) + 1]

    print(payload["probe_instructions"])
    print(f"\n{'=' * 70}\n以下 {len(probes)} 題彼此無關，逐題獨立回答。\n{'=' * 70}")
    for probe in probes:
        print(f"\n### {probe['id']}\n{probe['probe']}")
    return 0


def show_judge(probe_path: Path, answer_path: Path, spec: str) -> int:
    """印出判卷所需的三件事：陳述、題目、受測者的回答。"""
    payload = json.loads(probe_path.read_text(encoding="utf-8"))
    answers: dict[str, str] = {}
    for source in sorted(answer_path.glob("*.json")) if answer_path.is_dir() else [answer_path]:
        for item in json.loads(source.read_text(encoding="utf-8")):
            answers[item["id"]] = item.get("answer") or ""

    start, _, end = spec.partition("-")
    probes = payload["probes"][int(start):int(end or start) + 1]

    print(payload["judge_instructions"])
    for probe in probes:
        answer = answers.get(probe["id"])
        if answer is None:
            continue
        print(f"\n{'=' * 70}\n### {probe['id']}")
        print(f"\n[陳述]\n{probe['statement']}")
        print(f"\n[題目]\n{probe['probe']}")
        print(f"\n[受測者的回答]\n{answer}")
    return 0


def ingest(verdict_path: Path, concept_path: Path) -> int:
    """收回判定，填 surprisal。

    判定結果連同證據一起存：Dream Engine 之後要重跑校準時（模型升級後
    原本不知道的事可能就知道了），需要看得出上一次是憑什麼判的。
    """
    verdicts = json.loads(verdict_path.read_text(encoding="utf-8"))
    by_id = {v["id"]: v for v in (verdicts if isinstance(verdicts, list) else verdicts.get("verdicts", []))}

    concepts = load_concepts(concept_path)
    updated = 0
    for concept in concepts:
        verdict = by_id.get(concept["id"])
        if verdict is None:
            continue
        label = verdict.get("verdict")
        if label not in VERDICT_SCORES:
            continue
        concept["surprisal"] = VERDICT_SCORES[label]
        concept["probe_result"] = {
            "verdict": label,
            "evidence": verdict.get("evidence"),
            "note": verdict.get("note"),
        }
        updated += 1

    concept_path.write_text(json.dumps(concepts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[calibrate] 更新 {updated} 條 → {concept_path}", file=sys.stderr)
    return report(concept_path)


def report(concept_path: Path) -> int:
    concepts = load_concepts(concept_path)
    verdicts: dict[str, int] = {}
    for concept in concepts:
        result = concept.get("probe_result") or {}
        verdicts[result.get("verdict") or "(未校準)"] = verdicts.get(result.get("verdict") or "(未校準)", 0) + 1

    calibrated = [c for c in concepts if c.get("surprisal") is not None]
    keep = [c for c in calibrated if (c.get("surprisal") or 0) >= 0.8]

    out = sys.stderr
    print(f"[calibrate] {len(concepts)} 條 concept，已校準 {len(calibrated)}", file=out)
    print(f"  判定分布: {verdicts}", file=out)
    print(f"  通過（surprisal >= 0.8）: {len(keep)}", file=out)
    if calibrated:
        wasted = sum(1 for c in calibrated if c["surprisal"] == 0.0)
        print(f"  模型本來就會、注入純浪費: {wasted}"
              f"（{wasted / len(calibrated) * 100:.0f}%）", file=out)
    for concept in sorted(keep, key=lambda c: -(c.get("surprisal") or 0))[:10]:
        print(f"\n  [{concept['probe_result']['verdict']}] {concept['statement'][:90]}", file=out)
        print(f"      scope={concept.get('scope')} kind={concept.get('kind')}", file=out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1.5 surprisal 行為測試校準")
    parser.add_argument("--emit", action="store_true", help="產出行為測試題目")
    parser.add_argument("--ingest", type=Path, help="收回判定結果")
    parser.add_argument("--report", action="store_true", help="看校準後的分布")
    parser.add_argument("--show-probes", type=str, help="印出指定範圍的題目（不含答案），例如 0-3")
    parser.add_argument("--show-judge", type=str, help="印出指定範圍的判卷材料，例如 0-3")
    parser.add_argument("--answer-path", type=Path, default=WORK_DIR / "probe_out")
    parser.add_argument("--concept-path", type=Path, default=DEFAULT_CONCEPT_PATH)
    parser.add_argument("--probe-path", type=Path, default=DEFAULT_PROBE_PATH)
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if args.ingest:
        return ingest(args.ingest, args.concept_path)
    if args.emit:
        return emit(args.concept_path, args.probe_path)
    if args.show_probes:
        return show_probes(args.probe_path, args.show_probes)
    if args.show_judge:
        return show_judge(args.probe_path, args.answer_path, args.show_judge)
    return report(args.concept_path)


if __name__ == "__main__":
    sys.exit(main())
