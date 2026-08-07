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
import re
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


# 注入實驗的指示。與 probe 組共用同一批題目，差別只在多了一段記憶——
# 那批題目的無注入結果就是現成的對照組，所以這裡只需要跑實驗組。
INJECTION_INSTRUCTIONS = """\
以下是幾個開發上的問題。每題前面附了一段「可能相關的記憶」，那是過去在這些專案裡
累積下來的筆記；其中**有些與當前問題無關**，請自行判斷哪些用得上。

這些專案不在你手上，你也拿不到原始碼，所以請依記憶內容加上你既有的知識回答——
**不要去讀檔案、grep、搜尋或查任何資料**。

就照你平常回答同事的方式回答：你會怎麼做、其中有什麼要留意的地方。
不要條列說明你用了哪幾條記憶，直接把結論寫成正常的回答。
"""

INJECTION_JUDGE_INSTRUCTIONS = """\
你在評估一個 agent 有沒有**實際運用**注入給它的記憶。

你會看到：一條「目標記憶」、一個「題目」、agent 在**沒有**記憶時的舊回答、
以及它在**拿到一批記憶（含無關干擾項）之後**的新回答。

判斷新回答與目標記憶的關係，四選一：

- `APPLIED`：把目標記憶用在該用的地方，結論因此正確。**光是複述記憶內容不算**——
  要看得出它把那條知識轉成了對這一題的具體判斷或做法
- `RECITED`：有提到記憶的內容，但只是照抄或貼在旁邊，沒有真的影響回答的結論
- `IGNORED`：完全沒用上，新舊回答實質相同
- `MISAPPLIED`：用錯了——套用到不該套用的地方，或誤用了那些無關的干擾記憶

嚴格判定。`APPLIED` 與 `RECITED` 的界線是**結論有沒有因此改變**。

只輸出 JSON：

```json
{"verdict": "APPLIED", "evidence": "新回答裡最能支持判定的一小段引文", "note": "一句話說明"}
```
"""


# 物件邊界靠後面的 `}` + `,{` 或 `]` 錨定，這樣即使值裡有裸雙引號也切得開
_LENIENT_ITEM = re.compile(
    r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*(.*?)\s*\}\s*(?=,\s*\{|\s*\]|\s*$)', re.S
)
_LENIENT_FIELD = re.compile(r'"(\w+)"\s*:\s*"(.*?)"\s*(?=,\s*"\w+"\s*:|$)', re.S)


def load_agent_json(path: Path) -> list[dict[str, Any]]:
    """讀 agent 寫的 JSON，格式壞掉時退回寬鬆解析。

    實測必要：受測 agent 在回答裡寫了 ``role="dialog"``，裸雙引號沒跳脫，
    整份檔案 json.loads 直接失敗。提示裡要求跳脫只能降低機率，不能根除——
    讓一次 LLM 手誤丟掉整批結果不划算，而這些內容本來就只是純文字。
    """
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, list) else payload.get("results", [])
    except json.JSONDecodeError:
        pass

    items: list[dict[str, Any]] = []
    for item_id, body in _LENIENT_ITEM.findall(text):
        record: dict[str, Any] = {"id": item_id}
        for key, value in _LENIENT_FIELD.findall(body):
            record[key] = value.replace('\\"', '"').replace("\\n", "\n")
        items.append(record)
    print(f"[calibrate] {path.name} JSON 損壞，寬鬆解析救回 {len(items)} 筆", file=sys.stderr)
    return items


def emit(concept_path: Path, probe_path: Path, kinds: list[str] | None = None) -> int:
    """產出行為測試題目。

    ``kinds`` 用來只跑某幾類。612 條全跑的成本是一次蒸餾的量級以上，
    而 Phase 0 的結論指出價值集中在 ``user-stance`` 與 ``belief-correction``——
    ``project-fact`` 的 surprisal 多半較低。**「多半較低」目前是猜測，沒有數據**，
    所以未校準的那批不能當成「已知低價值」看待，只能當成「還沒測」。
    """
    concepts = load_concepts(concept_path)
    pending = [c for c in concepts if c.get("probe") and c.get("surprisal") is None]
    if kinds:
        pending = [c for c in pending if c.get("kind") in kinds]
    payload = {
        "probe_instructions": PROBE_INSTRUCTIONS,
        "judge_instructions": JUDGE_INSTRUCTIONS,
        # 記下這批限定了哪幾類，否則之後看到 probe_tasks.json 無從得知
        # 「沒出現在裡面」是因為已校準還是因為被篩掉
        "kinds": kinds or None,
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
        for item in load_agent_json(source):
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


DEFAULT_INJECTION_PATH = WORK_DIR / "injection_tasks.json"
PASS_THRESHOLD = 0.8
DISTRACTOR_COUNT = 4


def emit_injection(concept_path: Path, injection_path: Path, seed: int = 20260807) -> int:
    """產出注入實驗：每題一條目標記憶 + 數條干擾記憶。

    **為什麼要有干擾項**：只注入一條答案再問同一題，測到的是複述能力，不是利用能力。
    真實情境下召回會一次給好幾條，agent 必須自己判斷哪條相關——那才是要驗證的能力。

    干擾項同時充當安慰劑控制：如果行為改變只是因為「有東西被注入」而不是
    「注入了對的東西」，會表現成拿無關記憶亂套（判定 MISAPPLIED）。

    干擾項優先取不同 scope 的：同一個 repo 的記憶容易碰巧相關，
    那樣就分不清 agent 是挑對了還是全都拿來用。
    """
    import random

    concepts = load_concepts(concept_path)
    targets = [c for c in concepts if (c.get("surprisal") or 0) >= PASS_THRESHOLD]
    rng = random.Random(seed)

    tasks = []
    for target in targets:
        others = [c for c in concepts if c["id"] != target["id"]]
        different_scope = [c for c in others if c.get("scope") != target.get("scope")]
        pool = different_scope if len(different_scope) >= DISTRACTOR_COUNT else others
        distractors = rng.sample(pool, min(DISTRACTOR_COUNT, len(pool)))

        memories = [{"id": c["id"], "statement": c["statement"]} for c in [target, *distractors]]
        rng.shuffle(memories)  # 目標記憶不能總是排第一，否則位置本身就是提示
        tasks.append({
            "id": target["id"],
            "probe": target["probe"],
            "memories": memories,
            "target_statement": target["statement"],
            "baseline_verdict": (target.get("probe_result") or {}).get("verdict"),
        })

    payload = {
        "injection_instructions": INJECTION_INSTRUCTIONS,
        "judge_instructions": INJECTION_JUDGE_INSTRUCTIONS,
        "count": len(tasks),
        "tasks": tasks,
    }
    injection_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[calibrate] {len(tasks)} 題注入實驗（每題 {DISTRACTOR_COUNT} 條干擾）→ {injection_path}",
          file=sys.stderr)
    return 0


def show_injection(injection_path: Path, spec: str) -> int:
    """印出注入題目。目標記憶混在干擾項裡，**不標示哪條是目標**。"""
    payload = json.loads(injection_path.read_text(encoding="utf-8"))
    start, _, end = spec.partition("-")
    tasks = payload["tasks"][int(start):int(end or start) + 1]

    print(payload["injection_instructions"])
    print(f"\n{'=' * 70}\n以下 {len(tasks)} 題彼此無關，逐題獨立回答。\n{'=' * 70}")
    for task in tasks:
        print(f"\n### {task['id']}")
        print("\n<可能相關的記憶>")
        for i, memory in enumerate(task["memories"], 1):
            print(f"{i}. {memory['statement']}")
        print("</可能相關的記憶>")
        print(f"\n問題：{task['probe']}")
    return 0


def show_injection_judge(injection_path: Path, answer_path: Path, probe_path: Path, spec: str) -> int:
    """印出判卷材料：目標記憶、題目、無注入的舊回答、注入後的新回答。"""
    payload = json.loads(injection_path.read_text(encoding="utf-8"))

    def collect(path: Path) -> dict[str, str]:
        found: dict[str, str] = {}
        for source in sorted(path.glob("*.json")) if path.is_dir() else [path]:
            for item in load_agent_json(source):
                found[item["id"]] = item.get("answer") or ""
        return found

    baseline = collect(probe_path)
    injected = collect(answer_path)

    start, _, end = spec.partition("-")
    tasks = payload["tasks"][int(start):int(end or start) + 1]

    print(payload["judge_instructions"])
    for task in tasks:
        new_answer = injected.get(task["id"])
        if new_answer is None:
            continue
        print(f"\n{'=' * 70}\n### {task['id']}")
        print(f"\n[目標記憶]\n{task['target_statement']}")
        print(f"\n[題目]\n{task['probe']}")
        print(f"\n[無記憶時的舊回答]\n{baseline.get(task['id'], '(缺)')}")
        print(f"\n[拿到記憶後的新回答]\n{new_answer}")
    return 0


def ingest_injection(verdict_path: Path, concept_path: Path) -> int:
    """收回注入實驗的判定，寫進 concept 的 usability 欄位。"""
    sources = sorted(verdict_path.glob("injection-*.json")) if verdict_path.is_dir() else [verdict_path]
    by_id: dict[str, dict[str, Any]] = {}
    for source in sources:
        for verdict in load_agent_json(source):
            by_id[verdict["id"]] = verdict

    concepts = load_concepts(concept_path)
    counts: dict[str, int] = {}
    for concept in concepts:
        verdict = by_id.get(concept["id"])
        if verdict is None:
            continue
        concept["usability"] = {
            "verdict": verdict.get("verdict"),
            "evidence": verdict.get("evidence"),
            "note": verdict.get("note"),
        }
        counts[verdict.get("verdict") or "?"] = counts.get(verdict.get("verdict") or "?", 0) + 1

    concept_path.write_text(json.dumps(concepts, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(counts.values())
    applied = counts.get("APPLIED", 0)
    print(f"[calibrate] 注入實驗 {total} 題：{counts}", file=sys.stderr)
    if total:
        print(f"  實際被運用（APPLIED）: {applied}/{total} = {applied / total * 100:.0f}%", file=sys.stderr)
    return 0


def ingest(verdict_path: Path, concept_path: Path) -> int:
    """收回判定，填 surprisal。

    判定結果連同證據一起存：Dream Engine 之後要重跑校準時（模型升級後
    原本不知道的事可能就知道了），需要看得出上一次是憑什麼判的。
    """
    # 判卷同樣是分批平行跑的，結果散在多個檔案裡。
    # glob 限定 verdicts-*：判卷結果與 concepts.json / probe_tasks.json 放在同一層，
    # 用 *.json 會把它們一起吃進來。目前靠欄位不符擋得住，但那是碰運氣不是設計。
    sources = sorted(verdict_path.glob("verdicts-*.json")) if verdict_path.is_dir() else [verdict_path]
    by_id: dict[str, dict[str, Any]] = {}
    for source in sources:
        for verdict in load_agent_json(source):
            by_id[verdict["id"]] = verdict

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
    parser.add_argument("--kinds", type=str,
                        help="只跑指定的 kind，逗號分隔（例如 user-stance,belief-correction）")
    parser.add_argument("--ingest", type=Path, help="收回判定結果")
    parser.add_argument("--report", action="store_true", help="看校準後的分布")
    parser.add_argument("--show-probes", type=str, help="印出指定範圍的題目（不含答案），例如 0-3")
    parser.add_argument("--show-judge", type=str, help="印出指定範圍的判卷材料，例如 0-3")
    parser.add_argument("--answer-path", type=Path, default=WORK_DIR / "probe_out")
    parser.add_argument("--emit-injection", action="store_true", help="產出注入實驗（可利用性驗證）")
    parser.add_argument("--show-injection", type=str, help="印出注入題目，例如 0-3")
    parser.add_argument("--show-injection-judge", type=str, help="印出注入判卷材料，例如 0-3")
    parser.add_argument("--ingest-injection", type=Path, help="收回注入實驗判定")
    parser.add_argument("--injection-path", type=Path, default=DEFAULT_INJECTION_PATH)
    parser.add_argument("--injection-answer-path", type=Path, default=WORK_DIR / "injection_out")
    parser.add_argument("--concept-path", type=Path, default=DEFAULT_CONCEPT_PATH)
    parser.add_argument("--probe-path", type=Path, default=DEFAULT_PROBE_PATH)
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if args.ingest_injection:
        return ingest_injection(args.ingest_injection, args.concept_path)
    if args.emit_injection:
        return emit_injection(args.concept_path, args.injection_path)
    if args.show_injection:
        return show_injection(args.injection_path, args.show_injection)
    if args.show_injection_judge:
        return show_injection_judge(args.injection_path, args.injection_answer_path,
                                    args.answer_path, args.show_injection_judge)
    if args.ingest:
        return ingest(args.ingest, args.concept_path)
    if args.emit:
        kinds = [k.strip() for k in args.kinds.split(",") if k.strip()] if args.kinds else None
        return emit(args.concept_path, args.probe_path, kinds)
    if args.show_probes:
        return show_probes(args.probe_path, args.show_probes)
    if args.show_judge:
        return show_judge(args.probe_path, args.answer_path, args.show_judge)
    return report(args.concept_path)


if __name__ == "__main__":
    sys.exit(main())
