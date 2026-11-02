"""彙整 A/B 盲測：依 system + params 算勝場、出場、勝率、受測人數，並產生兩兩對戰矩陣。

用法：
    python webapp/tally.py --set <set_name> [--format md|csv] [--by-lang] [--out FILE]
    python webapp/tally.py --all            # 彙整 blind_sets 底下所有 set

作答檔讀 outputs/blind_sets/<set>/reviews/*.json 的 `choices`（舊的多維評分檔會略過），
system / params 從 manifest 反查（只在本機彙整時使用，不經過網站）。
csv 搭配 --out 時，對戰矩陣另存為 <FILE 去掉副檔名>_matrix.csv；不加 --out 則兩段依序印出。
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


def _load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _label(system: str, params: str) -> str:
    return system if params in ("", "{}") else f"{system} {params}"


def tally_set(sets_dir: str, set_name: str, by_lang: bool = False) -> dict:
    """回傳 {"rows": [...], "reviewers": 人數, "matrices": {lang: {...}}}。

    同一個 system+params 在同一題的兩側（無法分勝負）會略過。
    """
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

    acc: dict[tuple, dict] = defaultdict(lambda: {"wins": 0, "games": 0, "reviewers": set()})
    # lang_key -> (key_a, key_b) -> [a 勝場, b 勝場]；key_a < key_b
    duels: dict[str, dict[tuple, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    reviewers: set[str] = set()
    for fname in files:
        doc = _load_json(os.path.join(rdir, fname))
        choices = doc.get("choices") if doc else None
        if not isinstance(choices, dict) or not choices:
            continue
        counted = False
        for pair_id, resp in choices.items():
            if not isinstance(resp, dict):
                continue
            try:
                item_id, ca, cb = str(resp.get("pair", pair_id)).rsplit(":", 2)
            except ValueError:
                continue
            chosen = resp.get("chosen")
            ma, mb = lookup.get((item_id, ca)), lookup.get((item_id, cb))
            if ma is None or mb is None or chosen not in (ca, cb):
                continue
            ka, kb = (ma[0], ma[1]), (mb[0], mb[1])
            if ka == kb:
                continue
            lang = ma[2] if by_lang else ""
            winner, loser = (ka, kb) if chosen == ca else (kb, ka)
            for k, won in ((winner, 1), (loser, 0)):
                a = acc[(k[0], k[1], lang)]
                a["games"] += 1
                a["wins"] += won
                a["reviewers"].add(fname)
            lo, hi = sorted((ka, kb))
            duels[lang][(lo, hi)][0 if winner == lo else 1] += 1
            counted = True
        if counted:
            reviewers.add(fname)

    rows = []
    for (system, params, lang), a in acc.items():
        rows.append({"set": set_name, "system": system, "params": params, "lang": lang,
                     "wins": a["wins"], "games": a["games"],
                     "win_rate": a["wins"] / a["games"] if a["games"] else 0.0,
                     "reviewers": len(a["reviewers"])})
    rows.sort(key=lambda r: (r["lang"], -r["win_rate"], -r["games"], r["system"], r["params"]))

    matrices = {}
    for lang, table in duels.items():
        keys = sorted({k for pair in table for k in pair},
                      key=lambda k: next((i for i, r in enumerate(rows)
                                          if r["lang"] == lang and (r["system"], r["params"]) == k), 0))
        cells: dict[tuple[int, int], tuple[int, int]] = {}
        for (lo, hi), (w_lo, w_hi) in table.items():
            i, j = keys.index(lo), keys.index(hi)
            cells[(i, j)] = (w_lo, w_lo + w_hi)
            cells[(j, i)] = (w_hi, w_lo + w_hi)
        matrices[lang] = {"labels": [_label(*k) for k in keys], "cells": cells}
    return {"rows": rows, "reviewers": len(reviewers), "matrices": matrices}


def _columns(by_lang: bool) -> list[tuple[str, str]]:
    cols = [("system", "system"), ("params", "params")]
    if by_lang:
        cols.append(("lang", "語言"))
    cols += [("wins", "勝場"), ("games", "出場"), ("win_rate", "勝率"), ("reviewers", "受測人數")]
    return cols


def _fmt(key: str, v) -> str:
    return f"{v * 100:.0f}%" if key == "win_rate" else str(v)


def render_md(results: dict[str, dict], by_lang: bool) -> str:
    cols = _columns(by_lang)
    out = []
    for set_name, res in results.items():
        out.append(f"## {set_name}（{res['reviewers']} 位受測者）\n")
        if not res["rows"]:
            out.append("尚無作答資料。\n")
            continue
        out.append("| " + " | ".join(label for _, label in cols) + " |")
        out.append("|" + "|".join("---" for _ in cols) + "|")
        for r in res["rows"]:
            out.append("| " + " | ".join(_fmt(k, r[k]).replace("|", "\\|") for k, _ in cols) + " |")
        out.append("")
        for lang, m in sorted(res["matrices"].items()):
            n = len(m["labels"])
            title = "兩兩對戰矩陣（列勝行：勝場/對戰數）"
            out.append(f"### {title}" + (f" · {lang}" if lang else "") + "\n")
            for i, label in enumerate(m["labels"], 1):
                out.append(f"- #{i} {label}")
            out.append("")
            out.append("| | " + " | ".join(f"#{j}" for j in range(1, n + 1)) + " |")
            out.append("|---|" + "|".join("---" for _ in range(n)) + "|")
            for i in range(n):
                cells = []
                for j in range(n):
                    c = m["cells"].get((i, j))
                    cells.append("—" if i == j else (f"{c[0]}/{c[1]}" if c else ""))
                out.append(f"| #{i + 1} | " + " | ".join(cells) + " |")
            out.append("")
    return "\n".join(out)


def render_csv(results: dict[str, dict], by_lang: bool) -> str:
    cols = _columns(by_lang)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["set"] + [k for k, _ in cols])
    for set_name, res in results.items():
        for r in res["rows"]:
            w.writerow([set_name] + [f"{r[k]:.4f}" if isinstance(r[k], float) else r[k]
                                     for k, _ in cols])
    return buf.getvalue()


def render_matrix_csv(results: dict[str, dict]) -> str:
    """長格式：每個有對戰的 (row, col) 一列。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["set", "lang", "row", "col", "row_wins", "games", "row_win_rate"])
    for set_name, res in results.items():
        for lang, m in sorted(res["matrices"].items()):
            for (i, j), (wins, games) in sorted(m["cells"].items()):
                w.writerow([set_name, lang, m["labels"][i], m["labels"][j], wins, games,
                            f"{wins / games:.4f}"])
    return buf.getvalue()


def main() -> None:
    p = argparse.ArgumentParser(description="彙整 A/B 盲測")
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
    if args.format == "md":
        text, matrix_text = render_md(results, args.by_lang), None
    else:
        text, matrix_text = render_csv(results, args.by_lang), render_matrix_csv(results)
    if args.out:
        enc = "utf-8-sig" if args.format == "csv" else "utf-8"
        with open(args.out, "w", encoding=enc, newline="") as fp:
            fp.write(text)
        print(f"已輸出：{args.out}")
        if matrix_text is not None:
            mpath = os.path.splitext(args.out)[0] + "_matrix.csv"
            with open(mpath, "w", encoding=enc, newline="") as fp:
                fp.write(matrix_text)
            print(f"已輸出：{mpath}")
    else:
        print(text)
        if matrix_text is not None:
            print(matrix_text)


if __name__ == "__main__":
    main()
