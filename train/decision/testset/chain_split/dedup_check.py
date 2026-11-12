"""評測集與 Laya 訓練／測試資料的去重檢查（契約 3.10：不得使用訓練句）。

比對單位：整句 text，以及每個段落的 source 與補完後 text（指標 7 會把段落送回 Laya，段落洩漏同樣算）。
語料來源：``out/`` 之下所有 jsonl（含被 gitignore 的 mass/）、``testset/`` 的 jsonl（不含 chain_split 自己），
另可用 ``--git-ref`` 補讀某分支已追蹤、但目前工作樹沒有的 jsonl（例如 r4 分支的 testset）。
語料只取訊息類欄位（message／text／text_en／context／recent_context）並逐行串流，不整檔載入。
判定：正規化後完全相同為 exact；字元 3-gram Jaccard >= 門檻為 near。

用法：python dedup_check.py formal_a.jsonl --root ../.. --git-ref feature/decision-laya-data-r4 --extra smoke.jsonl
結束碼：有重疊為 1。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

from split_schema import load_jsonl

MSG_KEYS = {"message", "text", "text_en", "zh", "source", "context", "context_en", "context_zh", "recent_context"}
NORM_RE = re.compile(r"[\W_]+", re.UNICODE)
USER_RE = re.compile(r"^(user|assistant|another user)\s*:\s*", re.I)
MIN_LEN = 5
MAX_LEN = 300


def norm(s: str) -> str:
    return NORM_RE.sub("", s.lower())


def grams(s: str) -> set[str]:
    return {s[i:i + 3] for i in range(max(1, len(s) - 2))}


def strings_of(obj: Any, key: str | None = None) -> Iterator[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings_of(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings_of(v, key)
    elif isinstance(obj, str) and key in MSG_KEYS:
        for line in obj.splitlines():
            line = USER_RE.sub("", line.strip())
            if line:
                yield line


def lines_of_files(root: Path) -> Iterator[tuple[str, str]]:
    for sub in ("out", "testset"):
        for path in sorted((root / sub).rglob("*.jsonl")):
            if "chain_split" in path.parts:
                continue
            with open(path, encoding="utf-8") as f:
                for line in f:
                    yield str(path.relative_to(root)), line


def lines_of_git(ref: str, root: Path) -> Iterator[tuple[str, str]]:
    repo_rel = subprocess.run(["git", "rev-parse", "--show-prefix"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    names = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref, "--", f"{repo_rel}testset", f"{repo_rel}out"],
                           cwd=root, capture_output=True, text=True, check=True).stdout.split()
    for name in names:
        if not name.endswith(".jsonl") or "chain_split" in name:
            continue
        blob = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=root, capture_output=True, check=True).stdout
        for line in blob.decode("utf-8").splitlines():
            yield f"git:{ref}:{name}", line


def lines_of_extra(paths: list[str]) -> Iterator[tuple[str, str]]:
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                yield p, line


def build_corpus(root: Path, ref: str | None, extra: list[str] | None = None) -> tuple[dict[str, str], dict[str, set[str]], dict[str, list[str]]]:
    """回傳 (正規化字串 -> 原句, 3-gram 倒排索引 key 清單, 來源計數)。語料只留去重後的短訊息。"""
    corpus: dict[str, str] = {}
    sources: dict[str, int] = defaultdict(int)
    gens = [lines_of_files(root)] + ([lines_of_git(ref, root)] if ref else []) + ([lines_of_extra(extra)] if extra else [])
    for gen in gens:
        for src, line in gen:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for s in strings_of(obj):
                n = norm(s)
                if MIN_LEN <= len(n) <= MAX_LEN and n not in corpus:
                    corpus[n] = s
            sources[src] += 1
    index: dict[str, list[str]] = defaultdict(list)
    for n in corpus:
        for g in grams(n):
            index[g].append(n)
    return corpus, index, sources  # type: ignore[return-value]


def check(candidate: str, corpus: dict[str, str], index: dict[str, list[str]], threshold: float) -> tuple[str, float, str] | None:
    n = norm(candidate)
    if len(n) < MIN_LEN:
        return None
    if n in corpus:
        return "exact", 1.0, corpus[n]
    cg = grams(n)
    hits: dict[str, int] = defaultdict(int)
    for g in cg:
        for m in index.get(g, ()):
            hits[m] += 1
    best: tuple[float, str] | None = None
    for m, c in hits.items():
        mg_size = max(1, len(m) - 2)
        j = c / (len(cg) + mg_size - c)
        if j >= threshold and (best is None or j > best[0]):
            best = (j, m)
    return ("near", round(best[0], 2), corpus[best[1]]) if best else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="評測集去重檢查")
    p.add_argument("gold")
    p.add_argument("--root", default="../..", help="train/decision 目錄")
    p.add_argument("--git-ref", help="補讀該分支已追蹤的 jsonl")
    p.add_argument("--extra", nargs="*", default=[], help="額外納入語料的 jsonl（例如 smoke.jsonl）；比對對象本身不要放進來")
    p.add_argument("--threshold", type=float, default=0.6)
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    corpus, index, sources = build_corpus(Path(args.root).resolve(), args.git_ref, args.extra)
    print(f"語料：{len(corpus)} 句不重複訊息，來自 {len(sources)} 個檔案")
    bad = 0
    for row in load_jsonl(Path(args.gold)):
        cands = [("整句", row["text"])]
        for i, s in enumerate(row["segments"], 1):
            cands.append((f"段{i}.source", s["source"]))
            if s["text"] != s["source"]:
                cands.append((f"段{i}.text", s["text"]))
        for label, c in dict.fromkeys(cands):
            r = check(c, corpus, index, args.threshold)
            if r:
                bad += 1
                print(f"{row['id']} {label} [{r[0]} {r[1]}] 「{c}」 ~ 「{r[2]}」")
    print(f"重疊 {bad} 筆")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
