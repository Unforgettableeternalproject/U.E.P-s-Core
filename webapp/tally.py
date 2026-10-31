"""彙整盲測評分：依 system + params 算平均分、最佳次數與評分人數。

用法：
    python webapp/tally.py --set <set_name> [--format md|csv] [--by-lang] [--out FILE]
    python webapp/tally.py --all            # 彙整 blind_sets 底下所有 set

評分檔讀 outputs/blind_sets/<set>/reviews/*.json，system / params 從 manifest 反查
（只在本機彙整時使用，不經過網站）。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from collections import defaultdict

# Windows 終端預設 cp950，中文輸出會亂碼
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SETS_DIR = os.path.join(PROJECT_ROOT, "outputs", "blind_sets")
DIMS = ("timbre", "naturalness", "emotion", "accent")
DIM_LABEL = {"timbre": "音色", "naturalness": "自然度", "emotion": "情緒", "accent": "腔調"}


def _load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def tally_set(sets_dir: str, set_name: str, by_lang: bool = False) -> tuple[list[dict], int]:
    """回傳 (rows, 評分人數)。"""
    manifest = _load_json(os.path.join(sets_dir, set_name, "manifest.json"))
    if manifest is None:
        raise SystemExit(f"找不到 manifest：{set_name}")

    # (item_id, cand_id) -> (system, params_json, lang)
    lookup: dict[tuple[str, str], tuple[str, str, str]] = {}
    for it in manifest.get("items", []):
        for c in it.get("candidates", []):
            params = json.dumps(c.get("params", {}), ensure_ascii=False, sort_keys=True)
            lookup[(str(it.get("item_id")), str(c.get("cand_id")))] = (
                str(c.get("system", "?")), params, str(it.get("lang", "")))

    rdir = os.path.join(sets_dir, set_name, "reviews")
    files = sorted(f for f in os.listdir(rdir) if f.endswith(".json")) if os.path.isdir(rdir) else []

    acc: dict[tuple, dict] = defaultdict(lambda: {
        "n": 0, "best": 0, "sums": dict.fromkeys(DIMS, 0), "reviewers": set()})
    reviewers = 0
    for fname in files:
        doc = _load_json(os.path.join(rdir, fname))
        if not doc:
            continue
        reviewers += 1
        for item_id, resp in (doc.get("responses") or {}).items():
            for cand_id, scores in (resp.get("ratings") or {}).items():
                meta = lookup.get((item_id, cand_id))
                if meta is None or not all(isinstance(scores.get(d), int) for d in DIMS):
                    continue
                system, params, lang = meta
                key = (system, params, lang if by_lang else "")
                a = acc[key]
                a["n"] += 1
                a["reviewers"].add(fname)
                for d in DIMS:
                    a["sums"][d] += scores[d]
                if resp.get("best") == cand_id:
                    a["best"] += 1

    rows = []
    for (system, params, lang), a in acc.items():
        row = {"set": set_name, "system": system, "params": params, "lang": lang,
               "ratings": a["n"], "reviewers": len(a["reviewers"]), "best": a["best"],
               "best_rate": a["best"] / a["n"] if a["n"] else 0.0}
        for d in DIMS:
            row[d] = a["sums"][d] / a["n"] if a["n"] else 0.0
        row["overall"] = sum(row[d] for d in DIMS) / len(DIMS)
        rows.append(row)
    rows.sort(key=lambda r: (r["lang"], -r["overall"], r["system"], r["params"]))
    return rows, reviewers


def _columns(by_lang: bool) -> list[tuple[str, str]]:
    cols = [("set", "set"), ("system", "system"), ("params", "params")]
    if by_lang:
        cols.append(("lang", "語言"))
    cols += [(d, DIM_LABEL[d]) for d in DIMS]
    cols += [("overall", "四項平均"), ("best", "最佳次數"), ("best_rate", "最佳率"),
             ("ratings", "評分數"), ("reviewers", "評分人數")]
    return cols


def _fmt(key: str, v) -> str:
    if key in DIMS or key == "overall":
        return f"{v:.2f}"
    if key == "best_rate":
        return f"{v * 100:.0f}%"
    return str(v)


def render_md(results: dict[str, tuple[list[dict], int]], by_lang: bool) -> str:
    cols = _columns(by_lang)
    cols = [c for c in cols if c[0] != "set"]
    out = []
    for set_name, (rows, reviewers) in results.items():
        out.append(f"## {set_name}（{reviewers} 位評分者）\n")
        if not rows:
            out.append("尚無評分資料。\n")
            continue
        out.append("| " + " | ".join(label for _, label in cols) + " |")
        out.append("|" + "|".join("---" for _ in cols) + "|")
        for r in rows:
            cells = [_fmt(k, r[k]).replace("|", "\\|") for k, _ in cols]
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
    return "\n".join(out)


def render_csv(results: dict[str, tuple[list[dict], int]], by_lang: bool) -> str:
    cols = _columns(by_lang)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([k for k, _ in cols])
    for rows, _ in results.values():
        for r in rows:
            w.writerow([f"{r[k]:.4f}" if isinstance(r[k], float) else r[k] for k, _ in cols])
    return buf.getvalue()


def main() -> None:
    p = argparse.ArgumentParser(description="彙整盲測評分")
    p.add_argument("--sets-dir", default=DEFAULT_SETS_DIR)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--set", dest="set_name")
    g.add_argument("--all", action="store_true")
    p.add_argument("--format", choices=("md", "csv"), default="md")
    p.add_argument("--by-lang", action="store_true", help="另依語言拆分")
    p.add_argument("--out", help="輸出檔（預設印到 stdout）")
    args = p.parse_args()

    if args.all:
        names = sorted(n for n in os.listdir(args.sets_dir)
                       if os.path.isfile(os.path.join(args.sets_dir, n, "manifest.json")))
    else:
        names = [args.set_name]
    results = {n: tally_set(args.sets_dir, n, args.by_lang) for n in names}
    text = (render_md if args.format == "md" else render_csv)(results, args.by_lang)
    if args.out:
        with open(args.out, "w", encoding="utf-8-sig" if args.format == "csv" else "utf-8", newline="") as fp:
            fp.write(text)
        print(f"已輸出：{args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
