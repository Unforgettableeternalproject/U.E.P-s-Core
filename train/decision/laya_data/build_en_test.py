"""把 zh-TW 測試集照線上推論的同一套流程翻成英文，產出可直接評估的 ``testset/en_test.jsonl``。

流程對齊 ``uep.echo.laya_wiring.LayaProposer.propose``：
- 本句與前文都經 Boundary Translator（助理別名先換成佔位名 ``Yuki``，opus-mt-zh-en，greedy）；
- 前文只有「上一輪」一組（使用者句＋她的回覆），格式用 ``laya_schema.format_recent_context``，
  ``speaker`` 欄決定 ``User``／``Another user``；
- state 一律用 ``kinds.build_state_turn_reply``／``build_state_absent`` 組，鍵順序與訓練列相同。

本模組是評估端工具，會以檔案路徑載入 ``uep/echo/translate_wiring.py``（只需標準庫；torch／transformers
在真後端建立當下才 import）。翻完請結束行程以釋放 GPU。

用法（於 ``train/decision/`` 下）：
    python -m laya_data.build_en_test --out testset/en_test.jsonl --report out/en_test_report.json
    # 改用人工參考英譯（不載入 MT、不用 GPU）：
    python -m laya_data.build_en_test --reference testset/en_reference.jsonl --out testset/en_test_reference.jsonl
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from .kinds import DEFAULT_ASSISTANT_NAME, get_spec

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TESTSET = Path(__file__).resolve().parents[1] / "testset" / "zh_candidates_draft.jsonl"
ASSISTANT_PREFIXES = ("U.E.P", "UEP", "小U")  # 前文裡標示助理發言的前綴
CONTEXT_MAX_CHARS = 160  # 與 laya_schema.CONTEXT_MAX_CHARS 一致（翻譯前先截）


def load_translate_wiring() -> Any:
    path = REPO_ROOT / "uep" / "echo" / "translate_wiring.py"
    spec = importlib.util.spec_from_file_location("_uep_translate_wiring", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"找不到 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_uep_translate_wiring"] = module
    spec.loader.exec_module(module)
    return module


def parse_context_line(line: str) -> tuple[str, str]:
    """``"小明：U.E.P，今天會下雨嗎？"`` -> ``("user", 原句)``；以助理名開頭者為 ``assistant``。"""
    head, sep, rest = line.partition("：")
    if not sep:
        return "user", line.strip()
    role = "assistant" if head.strip() in ASSISTANT_PREFIXES else "user"
    return role, rest.strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="測試集 zh-TW -> 英文（線上同流程）")
    p.add_argument("--testset", type=Path, default=DEFAULT_TESTSET)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--report", type=Path)
    p.add_argument("--device", default="cuda")
    p.add_argument("--model-dir", type=Path)
    p.add_argument("--reference", type=Path, help="人工參考英譯 JSONL（id／text_en／context_en）；給了就不載入 MT")
    args = p.parse_args(argv)

    reference: dict[str, dict[str, Any]] | None = None
    translator: Any = None
    model_dir: Path | str = "(reference)"
    if args.reference:
        reference = {}
        for line in args.reference.read_text(encoding="utf-8").splitlines():
            if line.strip():
                ref = json.loads(line)
                reference[ref["id"]] = ref
    else:
        tw = load_translate_wiring()

        model_dir = tw.mt_model_dir(str(args.model_dir) if args.model_dir else None)
        # 別名 = 預設別名 + 決策組態的助理名稱（BotIdentityConfig：U.E.P、小U、UEP、uep），與 create_laya_proposer 一致
        aliases = (*tw.DEFAULT_ALIASES, "U.E.P", "小U", "UEP", "uep")
        translator = tw.BoundaryTranslator(
            backend_factory=lambda dev: tw.MarianBackend(model_dir, dev), device=args.device, aliases=aliases,
            cpu_fallback=False,
        )
        translator.start_warmup()
        translator.wait_ready()
        if not translator.available:
            print("MT 載入失敗", file=sys.stderr)
            return 2

    rows = [json.loads(line) for line in args.testset.read_text(encoding="utf-8").splitlines() if line.strip()]
    out_rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    statuses: dict[str, int] = {}

    def to_en(text: str, what: str, rid: str) -> str | None:
        res = translator.translate(text.strip()[:CONTEXT_MAX_CHARS] if what == "context" else text.strip(), timeout_ms=30000)
        statuses[f"{what}:{res.status}"] = statuses.get(f"{what}:{res.status}", 0) + 1
        if res.text_en is None:
            failures.append({"id": rid, "what": what, "reason": res.reason})
        return res.text_en

    for r in rows:
        rid = r["id"]
        context_en: list[dict[str, str]] = []
        if reference is not None:
            ref = reference.get(rid)
            if ref is None:
                failures.append({"id": rid, "what": "reference", "reason": "參考英譯缺少此 id"})
                continue
            msg_en = ref["text_en"].strip()
            context_en = [{"speaker": c["speaker"], "text": c["text"].strip()} for c in ref.get("context_en") or []]
        else:
            msg_en = to_en(r["zh"], "message", rid)
            for line in r.get("context_zh") or []:
                role, text = parse_context_line(line)
                en = to_en(text, "context", rid)
                if en is None:
                    continue
                context_en.append({"speaker": "Assistant" if role == "assistant" else "User", "text": en})
        if msg_en is None:
            continue
        same_speaker = r.get("speaker") != "other_user"
        if r["kind"] == "turn.reply":
            facts: dict[str, Any] = {
                "message": msg_en, "assistant_name": DEFAULT_ASSISTANT_NAME, "others": int(r["others"]),
                "context": [
                    {"speaker": c["speaker"] if c["speaker"] == "Assistant" or same_speaker else "Another user", "text": c["text"]}
                    for c in context_en
                ],
            }
            expected = {k: r["intended"][k] for k in ("reply_mode", "addressed", "route", "compound")}
        else:
            facts = {"message": msg_en, "assistant_name": DEFAULT_ASSISTANT_NAME, "source_platform": r["source_platform"]}
            expected = {k: r["intended"][k] for k in ("absent.intent", "absent.target_location", "absent.compound")}
        spec = get_spec(r["kind"])
        spec.validate_expected(expected)
        out_rows.append({
            "id": rid, "kind": r["kind"], "card": r["card"], "speaker": r.get("speaker"), "others": int(r["others"]),
            "state": spec.build_state(facts), "questions": {q: spec.questions[q] for q in expected}, "expected": expected,
            "text": msg_en, "zh": r["zh"], "context_zh": r.get("context_zh") or [],
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as f:
        for row in out_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "rows_in": len(rows), "rows_out": len(out_rows), "statuses": statuses, "failures": failures,
        "device": translator.device if translator else None, "placeholder": translator.placeholder if translator else None,
        "model_dir": str(model_dir),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
