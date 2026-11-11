"""Laya 微調模型 vs ``rules.default`` 的評估（英文測試集／val）。

三個子命令各自獨立行程，避免同時載入多個模型：

- ``predict``：載入一個 Laya checkpoint，對資料集每列預測（和 ``laya_wiring`` 相同：整個 kind 的題目一次送，
  noul 以機率 >= 0.5 為是），記錄延遲與 VRAM 峰值；
- ``rules``：在 CPU 上用 ``RuleProposer``（turn.reply）與 ``RulesAbsentIntentProposer``（absent_intent）
  對**中文原句**預測。有前文的列先重播前文（見 ``replay_context``）；
- ``report``：讀預測檔，算指標、跨 seed 平均±標準差、錯誤分析。

評估端工具，需要 import ``uep``（只有 ``rules`` 用得到；``laya`` 只有 ``predict`` 用得到）。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]

REPLY_MODES = ("respond", "passive", "pass", "defer")
ROUTES = ("direct_chat", "agent_loop", "task_request", "ignore")
INTENTS = ("presence_request", "message", "task", "other")
LOCATIONS = ("desktop", "discord", "website", "host", "none")
ASSISTANT_PREFIXES = ("U.E.P", "UEP", "小U")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for r in rows:
        r.setdefault("id", r.get("sample_id"))
    return rows


# ---------------------------------------------------------------- predict (Laya)

def cmd_predict(args: argparse.Namespace) -> int:
    import laya
    import torch

    rows = read_jsonl(args.data)
    t0 = time.perf_counter()
    agent = laya.load(args.model, device=args.device)
    load_s = time.perf_counter() - t0
    warm = rows[: args.warmup]
    for r in warm:  # 冷啟動 CUDA kernel，不計入延遲
        agent.predict(r["state"], r["questions"])
    if args.device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()
    preds: dict[str, Any] = {}
    lat: list[float] = []
    for r in rows:
        t = time.perf_counter()
        res = agent.predict(r["state"], r["questions"])
        if args.device.startswith("cuda"):
            torch.cuda.synchronize()
        lat.append((time.perf_counter() - t) * 1000.0)
        out: dict[str, Any] = {}
        for qid, ans in res["answers"].items():
            if "choice" in ans and ans.get("choice") is not None:
                out[qid] = {"pred": ans["choice"], "conf": float((ans.get("probabilities") or {}).get(ans["choice"], 0.0))}
            else:
                p = float(ans["noul"])
                out[qid] = {"pred": p >= 0.5, "conf": p if p >= 0.5 else 1.0 - p, "p_yes": p}
        preds[r.get("id") or r["sample_id"]] = out
    lat_sorted = sorted(lat)
    meta = {
        "model": str(args.model), "device": args.device, "load_s": round(load_s, 2), "n": len(rows),
        "latency_ms_p50": round(statistics.median(lat), 2),
        "latency_ms_p95": round(lat_sorted[min(len(lat_sorted) - 1, int(0.95 * len(lat_sorted)))], 2),
        "latency_ms_mean": round(statistics.fmean(lat), 2),
        "vram_peak_alloc_mb": round(torch.cuda.max_memory_allocated() / 2**20, 1) if args.device.startswith("cuda") else None,
        "vram_peak_reserved_mb": round(torch.cuda.max_memory_reserved() / 2**20, 1) if args.device.startswith("cuda") else None,
        "laya_version": getattr(laya, "__version__", "?"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"meta": meta, "preds": preds}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- rules.default

class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _split_context(lines: list[str]) -> tuple[str | None, str | None]:
    """前文行 -> (使用者原句, 她的回覆原句)；以助理名開頭者為回覆。"""
    user = reply = None
    for line in lines:
        head, sep, rest = line.partition("：")
        if sep and head.strip() in ASSISTANT_PREFIXES:
            reply = rest.strip()
        else:
            user = rest.strip() if sep else line.strip()
    return user, reply


def others_from_state(state: dict[str, Any]) -> int:
    """``other_people_present`` 字面值 -> 在場他人數（``none`` -> 0；``3 other people present`` -> 3）。"""
    text = str(state.get("other_people_present", "none"))
    return 0 if text == "none" else int(text.split()[0])


def rules_turn_reply(row: dict[str, Any]) -> dict[str, Any]:
    from uep.turn.models import SessionContext, TurnCandidate, TurnOutcomeFacts
    from uep.turn.rule_proposer import RuleProposer

    clock = _Clock()
    proposer = RuleProposer(clock=clock)
    ctx = SessionContext(conversation_id="c", segment_id="s", audience_scope="guild", session_ref="sess")
    count = int(row["others"]) + 1 if "others" in row else others_from_state(row["state"]) + 1
    prev_user, prev_reply = _split_context(row.get("context_zh") or [])
    ctx_speaker = "discord:u1"
    cur_speaker = "discord:u2" if row.get("speaker") == "other_user" else ctx_speaker
    seq = 0

    def cand(text: str, speaker: str) -> TurnCandidate:
        nonlocal seq
        seq += 1
        return TurnCandidate(
            candidate_id=f"cand{seq}", utterance_refs=(f"u{seq}",), speakers=(speaker,), ctx=ctx, interaction_state="IDLE",
            interaction_version=1, presence_active=True, text=text, participant_count=count,
        )

    # 重播前文：使用者上一句 -> 她回覆（強制視為已回覆，即使規則版自己會 pass）；只有她的一句時直接合成「她開口」
    if prev_user is not None:
        c = cand(prev_user, ctx_speaker)
        proposer.propose(c)
        clock.t += 2.0
        if prev_reply is not None:
            proposer.on_turn_started(f"t{seq}", c.candidate_id, (ctx_speaker,))
            proposer.on_turn_outcome(TurnOutcomeFacts(
                turn_id=f"t{seq}", candidate_id=c.candidate_id, speakers=(ctx_speaker,), reply_mode="respond", reason_code="replay",
                status="completed", generation_ok=True, reply_text=prev_reply,
            ))
    elif prev_reply is not None:
        c = cand("(assistant-initiated)", ctx_speaker)
        proposer.on_turn_started(f"t{seq}", c.candidate_id, (ctx_speaker,))
        proposer.on_turn_outcome(TurnOutcomeFacts(
            turn_id=f"t{seq}", candidate_id=c.candidate_id, speakers=(ctx_speaker,), reply_mode="respond", reason_code="replay",
            status="completed", generation_ok=True, reply_text=prev_reply,
        ))
    clock.t += 3.0
    prop = proposer.propose(cand(row["zh"], cur_speaker))
    return {
        "reply_mode": {"pred": str(prop.reply_mode.value), "conf": float(prop.confidence)},
        "addressed": {"pred": bool(prop.addressed), "conf": float(prop.confidence)},
        "route": {"pred": prop.route, "conf": float(prop.confidence)},
        "_reason": prop.reason_code,
    }


def rules_absent(row: dict[str, Any]) -> dict[str, Any]:
    from uep.decision.presence_absent_intent import AbsentIntentInput, RulesAbsentIntentProposer

    prop = RulesAbsentIntentProposer().propose(AbsentIntentInput(text=row["zh"], source_platform=row["state"]["source_platform"]))
    return {
        "absent.intent": {"pred": prop.intent, "conf": float(prop.confidence)},
        "absent.target_location": {"pred": prop.target_location or "none", "conf": float(prop.confidence)},
    }


def cmd_rules(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(REPO_ROOT))
    preds: dict[str, Any] = {}
    for r in read_jsonl(args.data):
        preds[r["id"]] = rules_turn_reply(r) if r["kind"] == "turn.reply" else rules_absent(r)
    args.out.write_text(json.dumps({"meta": {"proposer": "rules.default / rules.absent_intent", "input": "zh-TW original"}, "preds": preds},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"rules: {len(preds)} rows")
    return 0


# ---------------------------------------------------------------- metrics

def macro_f1(gold: list[Any], pred: list[Any]) -> float:
    labels = sorted(set(gold) | set(pred), key=str)
    f1s = []
    for lab in labels:
        tp = sum(1 for g, p in zip(gold, pred) if g == lab and p == lab)
        fp = sum(1 for g, p in zip(gold, pred) if g != lab and p == lab)
        fn = sum(1 for g, p in zip(gold, pred) if g == lab and p != lab)
        f1s.append(0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn))
    return sum(f1s) / len(f1s) if f1s else float("nan")


def acc(gold: list[Any], pred: list[Any]) -> float:
    return sum(1 for g, p in zip(gold, pred) if g == p) / len(gold) if gold else float("nan")


def recall_by_class(gold: list[Any], pred: list[Any], labels: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    out = {}
    for lab in labels:
        idx = [i for i, g in enumerate(gold) if g == lab]
        out[lab] = {"n": len(idx), "recall": (sum(1 for i in idx if pred[i] == lab) / len(idx)) if idx else None}
    return out


def metrics_for(rows: list[dict[str, Any]], preds: dict[str, Any], *, rules: bool = False) -> dict[str, Any]:
    """rules=True 時不支援的題（route 的語意、compound、passive／defer、task／message）照其實際輸出計。"""
    out: dict[str, Any] = {}
    tr = [r for r in rows if r["kind"] == "turn.reply"]
    ab = [r for r in rows if r["kind"] == "presence.absent_intent"]

    def get(r: dict[str, Any], q: str) -> Any:
        a = preds[r["id"]].get(q)
        return None if a is None else a["pred"]

    if tr:
        g = {q: [r["expected"][q] for r in tr] for q in ("reply_mode", "addressed", "route", "compound")}
        p = {q: [get(r, q) for r in tr] for q in ("reply_mode", "addressed", "route", "compound")}
        out["n_turn"] = len(tr)
        out["reply_mode_acc"] = acc(g["reply_mode"], p["reply_mode"])
        out["reply_mode_f1"] = macro_f1(g["reply_mode"], p["reply_mode"])
        out["reply_mode_recall"] = recall_by_class(g["reply_mode"], p["reply_mode"], REPLY_MODES)
        # 二元：respond vs 其他（規則版只會出 respond／pass，這是兩邊都能表達的比較）
        gb = [x == "respond" for x in g["reply_mode"]]
        pb = [x == "respond" for x in p["reply_mode"]]
        out["respond_binary_acc"] = acc(gb, pb)
        out["respond_binary_f1"] = macro_f1(gb, pb)
        sub = [i for i, x in enumerate(g["reply_mode"]) if x in ("respond", "pass")]
        out["reply_mode_acc_respond_pass_only"] = acc([g["reply_mode"][i] for i in sub], [p["reply_mode"][i] for i in sub])
        out["n_respond_pass_only"] = len(sub)
        out["addressed_acc"] = acc(g["addressed"], p["addressed"])
        out["addressed_f1"] = macro_f1(g["addressed"], p["addressed"])
        out["route_acc"] = acc(g["route"], p["route"])
        out["route_f1"] = macro_f1(g["route"], p["route"])
        out["route_recall"] = recall_by_class(g["route"], p["route"], ROUTES)
        ad = [i for i, x in enumerate(g["addressed"]) if x]
        out["route_acc_addressed_only"] = acc([g["route"][i] for i in ad], [p["route"][i] for i in ad])
        out["route_f1_addressed_only"] = macro_f1([g["route"][i] for i in ad], [p["route"][i] for i in ad])
        out["n_addressed"] = len(ad)
        out["joint_mode_route_acc"] = sum(1 for i in range(len(tr)) if g["reply_mode"][i] == p["reply_mode"][i] and g["route"][i] == p["route"][i]) / len(tr)
        if p["compound"][0] is not None:
            out["compound_acc"] = acc(g["compound"], p["compound"])
            out["compound_n_pos"] = sum(1 for x in g["compound"] if x)
            out["compound_pred_pos"] = sum(1 for x in p["compound"] if x)
        else:
            out["compound_acc"] = None
    if ab:
        g = {q: [r["expected"][q] for r in ab] for q in ("absent.intent", "absent.target_location", "absent.compound")}
        p = {q: [get(r, q) for r in ab] for q in g}
        out["n_absent"] = len(ab)
        out["intent_acc"] = acc(g["absent.intent"], p["absent.intent"])
        out["intent_f1"] = macro_f1(g["absent.intent"], p["absent.intent"])
        out["intent_recall"] = recall_by_class(g["absent.intent"], p["absent.intent"], INTENTS)
        out["target_acc"] = acc(g["absent.target_location"], p["absent.target_location"])
        out["target_f1"] = macro_f1(g["absent.target_location"], p["absent.target_location"])
        pr = [i for i, x in enumerate(g["absent.intent"]) if x == "presence_request"]
        out["target_acc_request_only"] = acc([g["absent.target_location"][i] for i in pr], [p["absent.target_location"][i] for i in pr])
        out["n_request"] = len(pr)
        if p["absent.compound"][0] is not None:
            out["absent_compound_acc"] = acc(g["absent.compound"], p["absent.compound"])
            out["absent_compound_n_pos"] = sum(1 for x in g["absent.compound"] if x)
            out["absent_compound_pred_pos"] = sum(1 for x in p["absent.compound"] if x)
        else:
            out["absent_compound_acc"] = None
    return out


def row_correct(r: dict[str, Any], preds: dict[str, Any]) -> bool:
    """規則版能表達的題全對才算對（turn.reply: reply_mode＋addressed；absent: intent＋target）。"""
    qs = ("reply_mode", "addressed") if r["kind"] == "turn.reply" else ("absent.intent", "absent.target_location")
    return all(preds[r["id"]][q]["pred"] == r["expected"][q] for q in qs)


def fmt(x: Any) -> str:
    return "n/a" if x is None else f"{x:.3f}"


SCALARS = [
    ("reply_mode_acc", "reply_mode acc (4-way)"), ("reply_mode_f1", "reply_mode macro-F1"),
    ("respond_binary_acc", "respond vs rest acc"), ("reply_mode_acc_respond_pass_only", "reply_mode acc (gold respond/pass only)"),
    ("addressed_acc", "addressed acc"), ("addressed_f1", "addressed macro-F1"),
    ("route_acc", "route acc (all rows)"), ("route_f1", "route macro-F1 (all rows)"),
    ("route_acc_addressed_only", "route acc (addressed=True rows)"), ("route_f1_addressed_only", "route macro-F1 (addressed=True rows)"),
    ("joint_mode_route_acc", "reply_mode x route joint acc"), ("compound_acc", "compound acc (turn.reply)"),
    ("intent_acc", "absent.intent acc"), ("intent_f1", "absent.intent macro-F1"),
    ("target_acc", "absent.target_location acc (all rows)"), ("target_f1", "absent.target_location macro-F1"),
    ("target_acc_request_only", "absent.target_location acc (presence_request rows)"), ("absent_compound_acc", "absent.compound acc"),
]


def agg(values: list[float | None]) -> str:
    vs = [v for v in values if v is not None]
    if not vs:
        return "n/a"
    if len(vs) == 1:
        return f"{vs[0]:.3f}"
    return f"{statistics.fmean(vs):.3f} ± {statistics.stdev(vs):.3f}"


def cmd_report(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.data)
    if args.exclude:
        ex = set(args.exclude.split(","))
        rows = [r for r in rows if r["id"] not in ex]
    laya_runs = [json.loads(p.read_text(encoding="utf-8")) for p in args.laya]
    rules_run = json.loads(args.rules.read_text(encoding="utf-8")) if args.rules else None
    lines: list[str] = []
    laya_m = [metrics_for(rows, run["preds"]) for run in laya_runs]
    rules_m = metrics_for(rows, rules_run["preds"], rules=True) if rules_run else None
    lines.append(f"rows: {len(rows)} (turn.reply {sum(1 for r in rows if r['kind'] == 'turn.reply')}, absent {sum(1 for r in rows if r['kind'] != 'turn.reply')}); seeds: {len(laya_runs)}")
    lines.append("")
    lines.append("| metric | Laya (mean ± std over seeds) | " + " | ".join(f"seed{i}" for i in range(len(laya_runs))) + " | rules.default |")
    lines.append("|---|---|" + "---|" * len(laya_runs) + "---|")
    for key, label in SCALARS:
        vals = [m.get(key) for m in laya_m]
        if all(v is None for v in vals) and (rules_m is None or rules_m.get(key) is None):
            continue
        lines.append(f"| {label} | {agg(vals)} | " + " | ".join(fmt(v) for v in vals) + f" | {fmt(rules_m.get(key)) if rules_m else 'n/a'} |")
    if rules_run:
        # 規則版沒有 route 輸出（恆為預設 direct_chat）；寬鬆版本：pass 一律視為 ignore，其餘 direct_chat
        derived = {rid: {**p, "route": {"pred": "ignore" if p["reply_mode"]["pred"] == "pass" else "direct_chat", "conf": 0.0}} if "reply_mode" in p else p
                   for rid, p in rules_run["preds"].items()}
        dm = metrics_for(rows, derived, rules=True)
        lines.append("")
        lines.append("rules.default 寬鬆版 route（reply_mode=pass 視為 ignore，其餘 direct_chat；規則版本身不輸出 route）: "
                     f"route acc {fmt(dm.get('route_acc'))}, macro-F1 {fmt(dm.get('route_f1'))}, addressed-only acc {fmt(dm.get('route_acc_addressed_only'))}, "
                     f"joint mode x route {fmt(dm.get('joint_mode_route_acc'))}")
    lines.append("")
    lines.append("per-class recall (support n)")
    for grp, labels in (("reply_mode_recall", REPLY_MODES), ("route_recall", ROUTES), ("intent_recall", INTENTS)):
        for lab in labels:
            sup = laya_m[0].get(grp, {}).get(lab, {}).get("n")
            vals = [m.get(grp, {}).get(lab, {}).get("recall") for m in laya_m]
            rv = rules_m.get(grp, {}).get(lab, {}).get("recall") if rules_m else None
            lines.append(f"- {grp}.{lab} (n={sup}): Laya {agg(vals)} | rules {fmt(rv)}")
    lines.append("")
    for i, run in enumerate(laya_runs):
        m = run["meta"]
        lines.append(f"seed{i}: p50 {m['latency_ms_p50']} ms, p95 {m['latency_ms_p95']} ms, VRAM peak alloc {m['vram_peak_alloc_mb']} MB (reserved {m['vram_peak_reserved_mb']} MB), load {m['load_s']} s")
    # 錯誤分析
    if rules_run:
        lines.append("")
        by_id = {r["id"]: r for r in rows}
        l_wrong_r_right, r_wrong_l_right = [], []
        for r in rows:
            wrong_seeds = sum(1 for run in laya_runs if not row_correct(r, run["preds"]))
            r_ok = row_correct(r, rules_run["preds"])
            if wrong_seeds and r_ok:
                l_wrong_r_right.append((wrong_seeds, r["id"]))
            if wrong_seeds < len(laya_runs) and not r_ok:
                r_wrong_l_right.append((len(laya_runs) - wrong_seeds, r["id"]))

        def show(title: str, items: list[tuple[int, str]]) -> None:
            lines.append(f"{title} (total {len(items)}; 列出前 10，依 seed 一致度排序)")
            for n, rid in sorted(items, key=lambda x: (-x[0], x[1]))[:10]:
                r = by_id[rid]
                qs = ("reply_mode", "addressed", "route") if r["kind"] == "turn.reply" else ("absent.intent", "absent.target_location")
                gold = {q: r["expected"][q] for q in qs}
                lp = [{q: run["preds"][rid][q]["pred"] for q in qs} for run in laya_runs]
                # 顯示第一個「與題目標籤不同」（錯誤清單）或第一個全對（反向清單）的 seed
                want_wrong = title.startswith("Laya 錯")
                pick = next((i for i, run in enumerate(laya_runs) if row_correct(r, run["preds"]) != want_wrong), 0)
                lp = [lp[pick]]
                rp = {q: rules_run["preds"][rid][q]["pred"] for q in qs}
                ctx = " / ".join(r.get("context_zh") or [])
                lines.append(f"- {rid} [{n}/{len(laya_runs)} seeds] zh: {r['zh']}" + (f" (context: {ctx})" if ctx else ""))
                lines.append(f"    en: {r['text']}")
                lines.append(f"    gold {json.dumps(gold, ensure_ascii=False)} | laya {json.dumps(lp[0], ensure_ascii=False)}" + f" (seed{pick})" + f" | rules {json.dumps(rp, ensure_ascii=False)}")

        show("Laya 錯、規則版對", l_wrong_r_right)
        lines.append("")
        show("規則版錯、Laya 對", r_wrong_l_right)
    text = "\n".join(lines)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    if args.json_out:
        args.json_out.write_text(json.dumps({"laya": laya_m, "rules": rules_m}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("predict")
    a.add_argument("--model", required=True)
    a.add_argument("--data", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--device", default="cuda")
    a.add_argument("--warmup", type=int, default=5)
    b = sub.add_parser("rules")
    b.add_argument("--data", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("report")
    c.add_argument("--data", type=Path, required=True)
    c.add_argument("--laya", type=Path, nargs="+", required=True)
    c.add_argument("--rules", type=Path)
    c.add_argument("--exclude", help="逗號分隔的測試列 id（例如與訓練集本句相同的裸附和）")
    c.add_argument("--out", type=Path)
    c.add_argument("--json-out", type=Path)
    args = p.parse_args(argv)
    return {"predict": cmd_predict, "rules": cmd_rules, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
