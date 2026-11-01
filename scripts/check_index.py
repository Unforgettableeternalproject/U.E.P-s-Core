"""檢查 docs/index 的 Index／Tracking 一致性（只用標準庫、唯讀）。

規則：
1. TRACKING.md 必須各有一列：V01–V36、D1–D21、A1–A7，且 ID 不重複。
2. TRACKING.md 內提到的 T-xx 必須在任務卡表（第一欄為 T-xx 的列）中存在。
3. INDEX.md、TRACKING.md 內的反引號路徑與相對連結必須存在；
   該行含 `planned` 者略過，`hidden_docs/` 與 http 連結略過，程式碼區塊略過。
4. （警告）若頂層 `uep/` 存在，其子套件須在 INDEX.md 出現 `uep/<名稱>`。

用法：
    python scripts/check_index.py [--root DIR] [--json] [--strict]
錯誤回傳 1；--strict 時警告也回傳 1。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REQUIRED = {
    "V": [f"V{n:02d}" for n in range(1, 37)],
    "D": [f"D{n}" for n in range(1, 22)],
    "A": [f"A{n}" for n in range(1, 8)],
}
ROW_ID = re.compile(r"^\|\s*(V\d{2}|D\d{1,2}|A\d{1,2}|T-\d{2})\s*\|")
TASK_REF = re.compile(r"\bT-\d{2}\b")
BACKTICK = re.compile(r"`([^`\n]+)`")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
PATH_EXT = (".py", ".md", ".yaml", ".yml", ".json", ".toml", ".lock", ".txt")
SKIP_PREFIX = ("hidden_docs/",)
CHECKED_FILES = ("INDEX.md", "TRACKING.md")


def strip_fences(text: str) -> list[tuple[int, str]]:
    """回傳 (行號, 行內容)，略過 ``` 程式碼區塊。"""
    lines, in_fence = [], False
    for no, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append((no, line))
    return lines


def looks_like_path(s: str) -> bool:
    if any(c in s for c in " *<>|{}$") or s.startswith(("http", "-")):
        return False
    return "/" in s or s.endswith(PATH_EXT)


def clean_path(s: str) -> str:
    s = re.sub(r":\d+(–\d+)?$", "", s)  # 去掉 :line
    return s


def check_ids(tracking_lines: list[tuple[int, str]]) -> tuple[list[str], set[str]]:
    errors, seen, cards = [], {}, set()
    for no, line in tracking_lines:
        m = ROW_ID.match(line)
        if not m:
            continue
        rid = m.group(1)
        if rid in seen:
            errors.append(f"TRACKING.md:{no} ID 重複：{rid}（先前於第 {seen[rid]} 行）")
        seen[rid] = no
        if rid.startswith("T-"):
            cards.add(rid)
    for kind, ids in REQUIRED.items():
        for rid in ids:
            if rid not in seen:
                errors.append(f"TRACKING.md 缺少 {kind} 類列：{rid}")
    return errors, cards


def check_task_refs(tracking_lines: list[tuple[int, str]], cards: set[str]) -> list[str]:
    errors = []
    for no, line in tracking_lines:
        for ref in TASK_REF.findall(line):
            if ref not in cards:
                errors.append(f"TRACKING.md:{no} 引用不存在的任務卡 {ref}")
    return errors


def check_paths(root: Path, doc: Path, lines: list[tuple[int, str]]) -> list[str]:
    errors = []
    for no, line in lines:
        if "planned" in line:
            continue
        targets = []  # (原始字串, 絕對路徑)
        for raw in BACKTICK.findall(line):
            if looks_like_path(raw):
                p = clean_path(raw)
                if not p.startswith(SKIP_PREFIX):
                    targets.append((raw, root / p))
        for raw in MD_LINK.findall(line):
            if raw.startswith(("http", "#", "mailto:")):
                continue
            p = raw.split("#", 1)[0]
            if p and not p.startswith(SKIP_PREFIX):
                targets.append((raw, (doc.parent / p).resolve()))
        for raw, path in targets:
            if not path.exists():
                errors.append(f"{doc.name}:{no} 路徑不存在：{raw}")
    return errors


def check_uep_coverage(root: Path, index_text: str) -> list[str]:
    uep = root / "uep"
    if not uep.is_dir():
        return []
    warnings = []
    for child in sorted(uep.iterdir()):
        if child.is_dir() and not child.name.startswith(("_", ".")):
            if f"uep/{child.name}" not in index_text:
                warnings.append(f"uep/{child.name}/ 未列入 INDEX.md")
    return warnings


def run(root: Path) -> dict:
    index_dir = root / "docs" / "index"
    errors, warnings = [], []
    texts = {}
    for name in CHECKED_FILES:
        f = index_dir / name
        if not f.is_file():
            errors.append(f"缺少檔案：docs/index/{name}")
        else:
            texts[name] = f.read_text(encoding="utf-8")
    if errors:
        return {"errors": errors, "warnings": warnings}

    tracking_lines = strip_fences(texts["TRACKING.md"])
    id_errors, cards = check_ids(tracking_lines)
    errors += id_errors
    errors += check_task_refs(tracking_lines, cards)
    for name in CHECKED_FILES:
        lines = tracking_lines if name == "TRACKING.md" else strip_fences(texts[name])
        errors += check_paths(root, index_dir / name, lines)
    warnings += check_uep_coverage(root, texts["INDEX.md"])
    return {"errors": errors, "warnings": warnings}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="檢查 Index／Tracking 一致性")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="警告也視為失敗")
    args = ap.parse_args(argv)

    result = run(args.root.resolve())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for e in result["errors"]:
            print(f"錯誤：{e}")
        for w in result["warnings"]:
            print(f"警告：{w}")
        if not result["errors"] and not result["warnings"]:
            print("Index／Tracking 檢查通過")
    failed = bool(result["errors"]) or (args.strict and bool(result["warnings"]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
