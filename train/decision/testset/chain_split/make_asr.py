"""由 formal.jsonl 產生模擬語音辨識版 formal_asr.jsonl（固定 seed、確定性轉換）。

標準答案不變（kind、route、addressed、vocative、pronouns 的語意都同一份），只改文字：
- 中文：全形標點（，。？！、；：）依雜湊決定保留與否，大多數被拿掉，少數保留（模擬 ASR 的不規則斷句）；移除書名號與引號。
- 英文：全部轉小寫；句讀 , . ? ! ; 同樣依雜湊大多拿掉，少數保留；撇號與數字間的 . : 不動。
- 段落 source 以「原文字元位移 -> 新文字位移」對照表重算，所以切點與原文一致；expect_single 列的 text／source 同步。
- 補完後的段落 text 若與 source 相同就沿用新 source，否則另以同一規則轉換；resolves_to／accept／surface 同規則轉換。
- context 不轉換（前文對話不是這次的語音輸入）。
用法：python make_asr.py [--src formal.jsonl] [--out formal_asr.jsonl] [--seed 20261113]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from split_schema import load_jsonl, source_spans, validate_rows

ZH_PUNCT = set("，。？！、；：")
EN_PUNCT = set(",.?!;")
MARKS = set("《》「」『』")
KEEP_RATE = 0.15  # 保留標點的比例


def _keep(seed: int, key: str, i: int, ch: str) -> bool:
    h = hashlib.sha256(f"{seed}|{key}|{i}|{ch}".encode("utf-8")).digest()
    return h[0] / 255 < KEEP_RATE


def convert(s: str, lang: str, seed: int, key: str) -> tuple[str, list[int]]:
    """回傳 (新文字, 對照表)；對照表長度 len(s)+1，map[i] 是原文第 i 個字元在新文字中的起點。"""
    out: list[str] = []
    mp: list[int] = []
    for i, ch in enumerate(s):
        mp.append(len(out))
        nxt = s[i + 1] if i + 1 < len(s) else " "
        if ch in MARKS:
            continue
        if lang == "zh":
            if ch in ZH_PUNCT and not _keep(seed, key, i, ch):
                continue
            out.append(ch)
        else:
            if ch in EN_PUNCT:
                inside_number = ch == "." and nxt.isdigit()
                if not inside_number and not _keep(seed, key, i, ch):
                    continue
            out.append(ch.lower())
    mp.append(len(out))
    return "".join(out), mp


def conv_plain(s: str, lang: str, seed: int, key: str) -> str:
    return convert(s, lang, seed, key)[0].strip()


def convert_row(row: dict[str, Any], seed: int) -> dict[str, Any]:
    lang, rid = row["lang"], row["id"]
    full, mp = convert(row["text"], lang, seed, rid)
    new_text = full.strip()
    lead = len(full) - len(full.lstrip())
    spans = source_spans(row["text"], row["segments"]) or []
    out = json.loads(json.dumps(row, ensure_ascii=False))
    out["text"] = new_text
    expect_starts: list[int] = []
    for k, (seg, (a, b)) in enumerate(zip(out["segments"], spans)):
        raw = full[mp[a]:mp[b]]
        src = raw.strip()
        expect_starts.append(mp[a] + len(raw) - len(raw.lstrip()) - lead)
        orig = row["segments"][k]
        if row["expect_single"]:
            seg["source"] = seg["text"] = new_text
        else:
            seg["source"] = src
            seg["text"] = src if orig["text"] == orig["source"] else conv_plain(orig["text"], lang, seed, f"{rid}.{k}")
        seg["text_en"] = seg["text"] if lang == "en" else orig["text_en"]
        for p, op in zip(seg["pronouns"], orig["pronouns"]):
            p["surface"] = conv_plain(op["surface"], lang, seed, "p")
            p["resolves_to"] = conv_plain(op["resolves_to"], lang, seed, "p")
            p["accept"] = [conv_plain(x, lang, seed, "p") for x in op.get("accept", [])]
    out["_expect_starts"] = expect_starts
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="formal.jsonl")
    ap.add_argument("--out", default="formal_asr.jsonl")
    ap.add_argument("--seed", type=int, default=20261113)
    a = ap.parse_args()
    rows = load_jsonl(Path(a.src))
    res = []
    bad = 0
    for r in rows:
        n = convert_row(r, a.seed)
        if not r["expect_single"]:
            # 新文字上重新找 source，必須與對照表算出的位置一致（避免重複字串造成錯位）
            got = source_spans(n["text"], n["segments"])
            if got is None or [g[0] for g in got] != n["_expect_starts"]:
                bad += 1
                print("source 位移與對照表不一致", r["id"])
        n.pop("_expect_starts", None)
        res.append(n)
    errors, warns = validate_rows(res)
    for rid, m in errors.items():
        print("錯誤", rid, m)
    Path(a.out).write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in res), encoding="utf-8", newline="\n")
    print(f"{a.out}: {len(res)} 列，驗證錯誤 {len(errors)}，source 位移不一致 {bad}")
    return 1 if errors or bad else 0


if __name__ == "__main__":
    sys.exit(main())
