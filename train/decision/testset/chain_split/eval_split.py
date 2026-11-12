"""拆分器評測腳本（Chain 介面契約 3.10 指標 1-6、9、10；7、8 只留介面）。

輸入：標註檔（--gold）與拆分器輸出（--pred，JSONL，一 id 一列）：
    {"id": "cs-s001", "segments": [{"seq": 1, "text": "...", "text_en": "..."}],
     "error": null, "first_segment_ms": 120, "total_ms": 400}
``error`` 為契約的閉集錯誤碼（split_timeout_first／split_timeout_total／split_error／split_invalid／
split_single_segment／split_over_limit）；有錯誤、缺列或輸出驗證失敗的 id 一律視為退回母句（單段、整句）。

沒有拆分器時可用內建的 --splitter：identity（整句一段）、oracle（回標註答案，應全滿分）、
naive_punct（照標點硬切）、fault（oracle 注入固定錯誤，用來確認各指標會動）。

指標 3（代名詞補完）在此是啟發式判定，不是契約的 LLM 輔助判定：補完後的段落必須含 resolves_to
（或 accept 之一），且不能再含代名詞本身。指標 10 在此是 proxy（用標註的 addressed 加稱呼保留與否推算），
真值需 Laya 對段落的重判。
用法：python eval_split.py --gold smoke.jsonl --splitter identity [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import unicodedata
from collections import defaultdict
from difflib import Match, SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from split_schema import contains_name, is_punct, load_jsonl, source_spans

K_LEN = 2          # 單段長度上限倍數（契約 3.4）
MAX_SEG = 4        # max_segments
TOL = 4            # 切點容許誤差（字元，已略過前導標點）
ALIGN_MIN = 0.15   # 段落對應的最低相似度

THRESHOLDS = {"cut_f1": 0.85, "single_recall": 0.95, "vocative": 0.95,
              "pron_task_request": 0.95, "pron_agent_loop": 0.95, "pron_direct_chat": 0.90}


# ---------- 內建拆分器 ----------

def split_identity(row: dict[str, Any], idx: int) -> dict[str, Any]:
    return {"id": row["id"], "segments": [{"seq": 1, "text": row["text"], "text_en": None}]}


def split_oracle(row: dict[str, Any], idx: int) -> dict[str, Any]:
    return {"id": row["id"], "segments": [{"seq": i + 1, "text": s["text"], "text_en": s["text_en"]}
                                          for i, s in enumerate(row["segments"])]}


def split_naive_punct(row: dict[str, Any], idx: int) -> dict[str, Any]:
    import re
    parts = [p.strip() for p in re.split(r"[，,。；;！!？?]+", row["text"]) if p.strip()]
    if len(parts) > MAX_SEG:
        parts = parts[:MAX_SEG - 1] + [" ".join(parts[MAX_SEG - 1:])]
    return {"id": row["id"], "segments": [{"seq": i + 1, "text": p, "text_en": None} for i, p in enumerate(parts)]}


def split_fault(row: dict[str, Any], idx: int) -> dict[str, Any]:
    """oracle 加固定錯誤：idx%4==0 拿掉稱呼；idx%5==0 代名詞補錯；idx%7==0 全併成一段；idx%11==0 輸出非法。"""
    segs = [dict(s) for s in row["segments"]]
    texts = [s["text"] for s in segs]
    if idx % 4 == 0:
        for i, s in enumerate(segs):
            if s.get("vocative"):
                texts[i] = texts[i].replace(s["vocative"], "").lstrip(" ,，、") or "好"
    if idx % 5 == 0:
        for i, s in enumerate(segs):
            for p in s["pronouns"]:
                texts[i] = texts[i].replace(p["resolves_to"], "那個東西" if row["lang"] == "zh" else "the thing")
    if idx % 7 == 0 and len(texts) > 1:
        texts = [" ".join(texts)]
    out = [{"seq": i + 1, "text": t, "text_en": None} for i, t in enumerate(texts)]
    if idx % 11 == 0:
        out[0]["text"] = out[0]["text"] + "\x07"
    return {"id": row["id"], "segments": out}


SPLITTERS: dict[str, Callable[[dict[str, Any], int], dict[str, Any]]] = {
    "identity": split_identity, "oracle": split_oracle, "naive_punct": split_naive_punct, "fault": split_fault,
}


# ---------- 輸出驗證（指標 9，對應契約 3.4 輸出視為不受信文字） ----------

def validate_pred(text: str, rec: dict[str, Any]) -> str | None:
    """回傳錯誤碼；合法為 None。"""
    if rec.get("error"):
        return str(rec["error"])
    segs = rec.get("segments")
    if not isinstance(segs, list) or not segs:
        return "split_invalid"
    if len(segs) > MAX_SEG:
        return "split_over_limit"
    for i, s in enumerate(segs, 1):
        t = s.get("text") if isinstance(s, dict) else None
        if not isinstance(t, str) or not (1 <= len(t) <= K_LEN * len(text)):
            return "split_invalid"
        if s.get("seq", i) != i:
            return "split_invalid"
        if any(unicodedata.category(c) == "Cc" for c in t):
            return "split_invalid"
    return None


# ---------- 對齊 ----------

def snap(text: str, i: int) -> int:
    while i < len(text) and is_punct(text[i]):
        i += 1
    return i


def gold_cuts(row: dict[str, Any]) -> list[int]:
    spans = source_spans(row["text"], row["segments"]) or []
    return [snap(row["text"], a) for a, _ in spans[1:]]


def pred_cuts(text: str, segs: list[dict[str, Any]]) -> list[int | None]:
    """估計各預測段落（第 2 段起）在原文的起點，即預測切點。

    補完指代會讓段落文字與原文不同，所以不能直接 find。做法：依序處理各段，只看原文中位於
    前一段已對應範圍之後的共同區塊（避免補進來的先行詞誤配到前面的段落），取其中最前面的區塊，
    起點 = max(前段終點, 區塊在原文位置 - 區塊在段落內位置)。對不到時該切點為 None（算誤報）。
    """
    cuts: list[int | None] = []
    prev_end = 0
    for i, seg in enumerate(segs):
        # 只在前一段已對應範圍之後的原文上比對，避免補進來的先行詞與前面的段落配成單調鏈的一部分
        sm = SequenceMatcher(None, text[prev_end:], seg["text"], autojunk=False)
        found = None
        for min_size in (3, 2):
            blocks = [Match(m.a + prev_end, m.b, m.size) for m in sm.get_matching_blocks() if m.size >= min_size]
            if blocks:
                found = blocks
                break
        if found is None:
            if i:
                cuts.append(None)
            continue
        if i:
            cuts.append(snap(text, max(prev_end, found[0].a - found[0].b)))
        # 段落終點：從第一個區塊往後串接，區塊之間相隔太遠（>15 字元）視為誤配到後面的段落
        end = found[0].a + found[0].size
        for blk in found[1:]:
            if blk.a - end > 15:
                break
            end = blk.a + blk.size
        prev_end = max(prev_end, end)
    return cuts


def match_cuts(g: list[int], p: list[int | None]) -> int:
    used: set[int] = set()
    tp = 0
    for gc in g:
        best, bd = None, TOL + 1
        for j, pc in enumerate(p):
            if pc is None or j in used:
                continue
            d = abs(pc - gc)
            if d < bd:
                best, bd = j, d
        if best is not None and bd <= TOL:
            used.add(best)
            tp += 1
    return tp


def best_pred(gold_text: str, pred_segs: list[dict[str, Any]]) -> dict[str, Any] | None:
    best, br = None, ALIGN_MIN
    for p in pred_segs:
        r = SequenceMatcher(None, gold_text, p["text"], autojunk=False).ratio()
        if r > br:
            best, br = p, r
    return best


def best_gold(pred_text: str, gold_segs: list[dict[str, Any]]) -> dict[str, Any]:
    return max(gold_segs, key=lambda g: SequenceMatcher(None, g["text"], pred_text, autojunk=False).ratio())


# ---------- 指標 3 判定介面 ----------

def heuristic_pronoun_judge(seg_text: str, pron: dict[str, Any]) -> bool:
    """啟發式判定：含 resolves_to（或 accept），且代名詞不再出現。LLM 判定可替換此函式。"""
    targets = [pron["resolves_to"], *pron.get("accept", [])]
    if not any(contains_name(seg_text, t) for t in targets):
        return False
    if pron["surface"] in pron["resolves_to"]:
        return True
    return not contains_name(seg_text, pron["surface"])


def ratio(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


# ---------- 主評測 ----------

def evaluate(gold: list[dict[str, Any]], preds: dict[str, dict[str, Any]],
             laya: dict[str, dict[str, Any]] | None = None, judge=heuristic_pronoun_judge,
             reviews: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    errors: dict[str, int] = defaultdict(int)
    missing = 0
    tp = fp = fn = 0
    ind_total = ind_count_ok = ind_single = 0
    es_total = es_one = 0
    pron_ok: dict[str, list[int]] = defaultdict(lambda: [0, 0])      # 全部（含退回單段）
    pron_disp: dict[str, list[int]] = defaultdict(lambda: [0, 0])    # 只算有拆出多段的 id
    voc_ok = voc_total = 0
    voc_disp = [0, 0]
    leaked = [0, 0]
    chain_items = all_false = 0
    r_ok = a_ok = j_ok = l_total = 0
    first_ms: list[float] = []
    total_ms: list[float] = []

    for row in gold:
        text = row["text"]
        rec = preds.get(row["id"])
        if rec is None:
            missing += 1
            code = "missing"
        else:
            code = validate_pred(text, rec)
        if code:
            errors[code] += 1
            segs = [{"seq": 1, "text": text}]
        else:
            segs = rec["segments"]  # type: ignore[index]
            if rec.get("first_segment_ms") is not None:
                first_ms.append(float(rec["first_segment_ms"]))
            if rec.get("total_ms") is not None:
                total_ms.append(float(rec["total_ms"]))
        multi = len(segs) >= 2

        g, p = gold_cuts(row), pred_cuts(text, segs)
        t = match_cuts(g, p)
        tp += t
        fp += len(p) - t
        fn += len(g) - t

        if row["expect_single"]:
            es_total += 1
            es_one += len(segs) == 1
        else:
            ind_total += 1
            ind_count_ok += len(segs) == len(row["segments"])
            ind_single += len(segs) == 1

        for gs in row["segments"]:
            bp = best_pred(gs["text"], segs)
            seg_text = bp["text"] if bp else ""
            for pn in gs["pronouns"]:
                ok = int(bool(bp) and judge(seg_text, pn))
                if reviews is not None:
                    reviews.append({"id": row["id"], "route": gs["route"], "dispatched": multi, "surface": pn["surface"],
                                    "resolves_to": pn["resolves_to"], "accept": pn.get("accept", []),
                                    "pred_segment": seg_text, "judge": bool(ok), "human_or_llm": None})
                for d in (pron_ok,) + ((pron_disp,) if multi else ()):
                    d[gs["route"]][0] += ok
                    d[gs["route"]][1] += 1
            if gs["vocative"]:
                kept = bool(bp) and contains_name(seg_text, gs["vocative"])
                voc_total += 1
                voc_ok += kept
                if multi:
                    voc_disp[0] += kept
                    voc_disp[1] += 1
                if not gs["addressed"]:
                    leaked[1] += 1
                    leaked[0] += (not kept)

        if multi:
            chain_items += 1
            proxy = []
            for ps in segs:
                gs = best_gold(ps["text"], row["segments"])
                kept = (not gs["vocative"]) or contains_name(ps["text"], gs["vocative"])
                proxy.append(gs["addressed"] and kept)
            all_false += not any(proxy)

        if laya is not None and row["id"] in laya and not code:
            lsegs = laya[row["id"]]["segments"]
            for ps, ls in zip(segs, lsegs):
                gs = best_gold(ps["text"], row["segments"])
                l_total += 1
                rr = ls.get("route") == gs["route"]
                aa = ls.get("addressed") == gs["addressed"]
                r_ok += rr
                a_ok += aa
                j_ok += rr and aa

    n = len(gold)
    prec, rec_ = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = round(2 * prec * rec_ / (prec + rec_), 4) if prec and rec_ else (0.0 if (tp + fp + fn) else None)
    risky_ok = sum(pron_disp[r][0] for r in ("task_request", "agent_loop"))
    risky_n = sum(pron_disp[r][1] for r in ("task_request", "agent_loop"))
    risky_ok_all = sum(pron_ok[r][0] for r in ("task_request", "agent_loop"))
    risky_n_all = sum(pron_ok[r][1] for r in ("task_request", "agent_loop"))
    m: dict[str, Any] = {
        "n_rows": n, "missing_pred": missing,
        "m1_cut_f1": {"f1": f1, "precision": prec, "recall": rec_, "tp": tp, "fp": fp, "fn": fn},
        "m2_segment_count_acc": {"value": ratio(ind_count_ok, ind_total), "n": ind_total},
        # 主要分母（裁決 11/13）：只算拆成多段、真的會派發的輸出；all 是含退回單段的分母，並列回報
        "m3_pronoun_by_route": {r: {"value": ratio(pron_disp[r][0], pron_disp[r][1]), "n": pron_disp[r][1],
                                    "all": ratio(v[0], v[1]), "n_all": v[1]}
                                for r, v in sorted(pron_ok.items())},
        "m3_pronoun_side_effect_routes": {"value": ratio(risky_ok, risky_n), "n": risky_n,
                                          "all": ratio(risky_ok_all, risky_n_all), "n_all": risky_n_all},
        "m4_single_recall": {"value": ratio(es_one, es_total), "n": es_total},
        "m5_miss_split_rate": {"value": ratio(ind_single, ind_total), "n": ind_total},
        "m6_vocative_retention": {"value": ratio(voc_ok, voc_total), "n": voc_total,
                                  "dispatched_only": ratio(voc_disp[0], voc_disp[1]),
                                  "lost_for_not_addressed_segments": {"lost": leaked[0], "n": leaked[1]}},
        "m9_invalid_rate": {"value": ratio(sum(errors.values()), n), "by_code": dict(errors)},
        "m10_no_addressed_segment_proxy": {"value": ratio(all_false, chain_items), "n_chain_items": chain_items,
                                           "note": "proxy：用標註 addressed 與稱呼保留推算，非 Laya 重判"},
    }
    if laya is None:
        m["m7_route_addressed_agreement"] = "N/A（需 --laya：Laya 對各段的 route／addressed 結果）"
    else:
        m["m7_route_addressed_agreement"] = {"route": ratio(r_ok, l_total), "addressed": ratio(a_ok, l_total),
                                             "joint": ratio(j_ok, l_total), "n": l_total}
    if first_ms:
        m["m8_latency_ms"] = {"first_p50": statistics.median(first_ms), "first_p95": pct(first_ms, 0.95),
                              "total_p95": pct(total_ms, 0.95) if total_ms else None, "n": len(first_ms)}
    else:
        m["m8_latency_ms"] = "N/A（預測列需含 first_segment_ms／total_ms，需實際拆分器）"
    return m


def verdicts(m: dict[str, Any]) -> list[str]:
    def chk(name: str, val: float | None, thr: float, n: int) -> str:
        if val is None:
            return f"{name}: 無樣本"
        return f"{name}: {val:.3f} (門檻 {thr}, n={n}) {'通過' if val >= thr else '未達'}"
    out = [chk("指標1 切點F1", m["m1_cut_f1"]["f1"], THRESHOLDS["cut_f1"], m["m1_cut_f1"]["tp"] + m["m1_cut_f1"]["fn"]),
           chk("指標4 依賴只回一段召回", m["m4_single_recall"]["value"], THRESHOLDS["single_recall"], m["m4_single_recall"]["n"]),
           chk("指標6 稱呼保留", m["m6_vocative_retention"]["value"], THRESHOLDS["vocative"], m["m6_vocative_retention"]["n"])]
    for r in ("task_request", "agent_loop", "direct_chat"):
        d = m["m3_pronoun_by_route"].get(r)
        out.append(chk(f"指標3 代名詞補完[{r}]", d["value"] if d else None, THRESHOLDS[f"pron_{r}"], d["n"] if d else 0))
    return out


def load_by_id(path: str | None) -> dict[str, dict[str, Any]] | None:
    return {r["id"]: r for r in load_jsonl(Path(path))} if path else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="拆分器評測")
    p.add_argument("--gold", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pred", help="拆分器輸出 JSONL")
    g.add_argument("--splitter", choices=sorted(SPLITTERS), help="內建拆分器")
    p.add_argument("--laya", help="指標 7：Laya 對段落的結果 JSONL（id, segments:[{route, addressed}]，與預測段落同序）")
    p.add_argument("--dump-pred", help="把內建拆分器輸出寫成 JSONL")
    p.add_argument("--out", help="寫出完整指標 JSON")
    p.add_argument("--review-out", help="指標 3 的人工／LLM 複核抽樣檔（JSONL，human_or_llm 欄位待填）")
    p.add_argument("--review-rate", type=float, default=0.2, help="複核抽樣比例（契約要求至少 20%%）")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    gold = load_jsonl(Path(args.gold))
    if args.splitter:
        recs = [SPLITTERS[args.splitter](r, i) for i, r in enumerate(gold)]
        if args.dump_pred:
            Path(args.dump_pred).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
        preds = {r["id"]: r for r in recs}
    else:
        preds = load_by_id(args.pred) or {}
    reviews: list[dict[str, Any]] = []
    m = evaluate(gold, preds, load_by_id(args.laya), reviews=reviews)
    if args.review_out:
        step = max(1, round(1 / args.review_rate))
        sample = [r for i, r in enumerate(reviews) if i % step == 0]
        Path(args.review_out).write_text("".join(json.dumps(r, ensure_ascii=False) + chr(10) for r in sample), encoding="utf-8")
        print(f"複核抽樣：{len(sample)}/{len(reviews)} -> {args.review_out}")
    print(json.dumps(m, ensure_ascii=False, indent=1))
    print("---- 門檻對照（暫定值；n 小於 20 不具統計意義） ----")
    for line in verdicts(m):
        print(line)
    if args.out:
        Path(args.out).write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
