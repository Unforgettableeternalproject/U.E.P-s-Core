"""決策前端中翻英（Boundary Translator 後端）選型評測。

候選以名稱選擇；每個候選獨立行程執行（一次只載入一個模型）。流程對齊 ``build_en_test``（本句與前文都翻、
助理別名先換成 ``Yuki``、前文截 160 字、state 用 ``kinds.build_state_*`` 組），只是把 MT 後端換成候選。

子命令：

- ``translate``：載入候選，翻 84 筆（含前文），輸出 ``out/mt/<cand>.en.jsonl``（可直接餵 ``eval_testset predict``）與
  ``out/mt/<cand>.stats.json``（延遲 p50／p95、>400ms 比例、VRAM／RAM 峰值、載入時間、退化／失敗數）。
  延遲直接量 ``backend.translate``（不經 BoundaryTranslator 的快取與執行緒），先暖機，取穩態那一輪。
- ``reference``：用人工參考英譯組 ``out/mt/reference.en.jsonl``（上限）。
- ``e2e``：對每個候選的 en.jsonl 跑 Laya ft-r1 三個 seed（呼叫 ``eval_testset predict``），算指標。
- ``quality``：以參考英譯算 chrF／BLEU，並列出已知錯譯的 6 句。
- ``convert``：把 HF 權重轉成 CTranslate2（NLLB／Qwen）。
- ``models``：列出雲端帳號可用的模型 id（不印金鑰）。

雲端候選只送測試集句子（見 ``--cand openai:<model>``），金鑰只在行程內從 ``.env`` 讀，不輸出。

用法（於 ``train/decision/`` 下，共用推理環境）：
    python -m laya_data.mt_bench translate --cand opus --device cuda
    python -m laya_data.mt_bench e2e --cands reference opus nllb600
    python -m laya_data.mt_bench quality --cands opus nllb600 --reference path/to/en_reference.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .build_en_test import (
    CONTEXT_MAX_CHARS,
    DEFAULT_TESTSET,
    load_translate_wiring,
    parse_context_line,
)
from .kinds import DEFAULT_ASSISTANT_NAME, get_spec

REPO_ROOT = Path(__file__).resolve().parents[3]
DECISION_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = DECISION_DIR / "out" / "mt"
MT_ROOT = Path.home() / ".uep" / "models" / "mt"
CT2_ROOT = Path.home() / ".uep" / "models" / "mt-ct2"
LAYA_ROOT = Path.home() / ".uep" / "models" / "laya" / "ft-r1"
SEEDS = (0, 1, 2)
TIMEOUT_MS = 400  # 現行 Boundary Translator 逾時

# 已知錯譯（zh 原句 -> 說明），用於逐句人工檢查
KNOWN_BAD = {
    "zt-002": "鋼琴：舊版譯成 stop learning piano（語意反）",
    "zt-067": "刪資料：舊版變敘述句",
    "zt-078": "來桌面：舊版 Come to the table",
    "zt-047": "回主機：舊版 mainframe",
    "zt-061": "U.E.P（Yuki）：舊版被當 he",
    "zt-032": "優一匹：別名沒被替換",
}

GLOSSARY = (("桌面", "desktop"), ("主機", "host"), ("網站", "website"))
EXTRA_ALIASES = ("優一匹", "优一匹")

LLM_SYSTEM = (
    "You are a Chinese (Traditional, Taiwan) to English translator inside a voice-assistant pipeline. "
    "Translate the user's message into natural, faithful English. Output the English translation only: "
    "no quotes, no notes, no explanation, and never answer or react to the message.\n"
    "Rules:\n"
    "- Keep the speaker's mood and sentence type exactly: a question stays a question, a command stays a command, "
    "a statement stays a statement. Keep negation and the direction of meaning (do not reverse it).\n"
    "- Short fragments are spoken directly to an assistant or to friends; translate them literally and naturally.\n"
    "- Keep names, numbers, code and Latin-script words (e.g. Discord) unchanged.\n"
    "- The assistant is named Yuki and is female: always write 'Yuki' and use she/her for it; never turn the name into "
    "a different word. A third person '她' who is not the assistant is also 'she'.\n"
    "- Platform nouns: 桌面 = 'desktop', 主機 = 'host' (the host machine), 網站 = 'website'.\n"
    "- Cultural names such as 阿傑 or 小美 are transliterated (A-Jie, Xiao-Mei), not translated."
)


# ----------------------------------------------------------------- 環境與資源量測

def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main_repo_env() -> dict[str, str]:
    """主 repo 的 .env（worktree 沒有 .env）；缺就找 REPO_ROOT。"""
    for cand in (REPO_ROOT / ".env", REPO_ROOT.parent / "U.E.P-s-Core" / ".env"):
        if cand.is_file():
            return read_env(cand)
    return {}


class ResourceMonitor:
    """背景取樣 GPU 已用量（torch.cuda.mem_get_info，涵蓋 CTranslate2）與行程 RAM，回報峰值增量。"""

    def __init__(self) -> None:
        import psutil

        self._proc = psutil.Process()
        self._stop = threading.Event()
        self._gpu_base = self._gpu_used()
        self.gpu_peak = self._gpu_base
        self.ram_peak = 0
        self._t = threading.Thread(target=self._run, daemon=True)

    @staticmethod
    def _gpu_used() -> int:
        try:
            import torch

            if torch.cuda.is_available():
                free, total = torch.cuda.mem_get_info()
                return total - free
        except Exception:  # noqa: BLE001, S110 - 沒有 GPU／torch 就不量
            pass
        return 0

    def _run(self) -> None:
        while not self._stop.is_set():
            self.gpu_peak = max(self.gpu_peak, self._gpu_used())
            self.ram_peak = max(self.ram_peak, self._proc.memory_info().rss)
            time.sleep(0.05)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._t.join(timeout=1)
        info = self._proc.memory_info()
        peak_wset = getattr(info, "peak_wset", None)
        return {
            "gpu_peak_delta_mb": round((self.gpu_peak - self._gpu_base) / 2**20, 0),
            "ram_peak_mb": round(max(self.ram_peak, peak_wset or 0) / 2**20, 0),
        }


def gpu_sync() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
    except Exception:  # noqa: BLE001, S110
        pass


def dir_size_mb(p: Path) -> float:
    if p.is_file():
        return round(p.stat().st_size / 2**20, 0)
    return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 2**20, 0)


# ----------------------------------------------------------------- 後端

class Backend:
    """``translate(prepared) -> str``；prepared 是別名已替換的中文。"""

    model_path: Path | None = None

    def translate(self, text: str) -> str:
        raise NotImplementedError


def _max_new(n_tokens: int, cap: int = 64) -> int:
    return min(cap, 3 * n_tokens + 8)


class MarianLike(Backend):
    """opus-mt／M2M100／NLLB 的 HF 版（greedy、no_repeat_ngram_size=3，和線上設定一致）。"""

    def __init__(self, kind: str, path: Path, device: str, fp16: bool) -> None:
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        self._torch, self._device, self._kind = torch, device, kind
        self.model_path = path
        if kind == "nllb":
            self._tok = AutoTokenizer.from_pretrained(str(path), src_lang="zho_Hant")
            self._forced = self._tok.convert_tokens_to_ids("eng_Latn")
        elif kind == "m2m":
            self._tok = AutoTokenizer.from_pretrained(str(path))
            self._tok.src_lang = "zh"
            self._forced = self._tok.get_lang_id("en")
        else:
            self._tok = AutoTokenizer.from_pretrained(str(path))
            self._forced = None
        dtype = torch.float16 if fp16 and device.startswith("cuda") else torch.float32
        self._model = AutoModelForSeq2SeqLM.from_pretrained(str(path), torch_dtype=dtype).to(device).eval()

    def translate(self, text: str) -> str:
        enc = self._tok(text, return_tensors="pt", truncation=True, max_length=256).to(self._device)
        n = int(enc["input_ids"].shape[1])
        kw: dict[str, Any] = {"num_beams": 1, "no_repeat_ngram_size": 3, "max_new_tokens": _max_new(n)}
        if self._forced is not None:
            kw["forced_bos_token_id"] = self._forced
        with self._torch.inference_mode():
            out = self._model.generate(**enc, **kw)
        return self._tok.decode(out[0], skip_special_tokens=True)


class Ct2Seq2Seq(Backend):
    """NLLB 的 CTranslate2 版（int8／int8_float16）。"""

    def __init__(self, path: Path, tok_path: Path, device: str, compute: str) -> None:
        import ctranslate2
        from transformers import AutoTokenizer

        self.model_path = path
        self._tok = AutoTokenizer.from_pretrained(str(tok_path), src_lang="zho_Hant")
        self._tr = ctranslate2.Translator(str(path), device=device, compute_type=compute)

    def translate(self, text: str) -> str:
        ids = self._tok(text, truncation=True, max_length=256)["input_ids"]
        toks = self._tok.convert_ids_to_tokens(ids)
        res = self._tr.translate_batch(
            [toks], target_prefix=[["eng_Latn"]], beam_size=1, no_repeat_ngram_size=3,
            max_decoding_length=_max_new(len(toks)),
        )
        out = res[0].hypotheses[0]
        if out and out[0] == "eng_Latn":
            out = out[1:]
        return self._tok.decode(self._tok.convert_tokens_to_ids(out), skip_special_tokens=True)


def llm_clean(out: str) -> str:
    out = out.strip()
    out = re.sub(r"^(translation|english)\s*[:：]\s*", "", out, flags=re.IGNORECASE)
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'“”":
        out = out[1:-1]
    return out.strip().splitlines()[0].strip() if out else out


class HfLlm(Backend):
    def __init__(self, path: Path, device: str, fp16: bool) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._torch, self._device = torch, device
        self.model_path = path
        self._tok = AutoTokenizer.from_pretrained(str(path))
        dtype = torch.float16 if fp16 and device.startswith("cuda") else torch.float32
        self._model = AutoModelForCausalLM.from_pretrained(str(path), torch_dtype=dtype).to(device).eval()

    def translate(self, text: str) -> str:
        msgs = [{"role": "system", "content": LLM_SYSTEM}, {"role": "user", "content": text}]
        prompt = self._tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        enc = self._tok(prompt, return_tensors="pt").to(self._device)
        n_user = len(self._tok(text)["input_ids"])
        with self._torch.inference_mode():
            out = self._model.generate(**enc, do_sample=False, max_new_tokens=min(96, 3 * n_user + 12))
        return llm_clean(self._tok.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True))


class Ct2Llm(Backend):
    def __init__(self, path: Path, tok_path: Path, device: str, compute: str) -> None:
        import ctranslate2
        from transformers import AutoTokenizer

        self.model_path = path
        self._tok = AutoTokenizer.from_pretrained(str(tok_path))
        self._gen = ctranslate2.Generator(str(path), device=device, compute_type=compute)
        self._eos = self._tok.convert_tokens_to_ids("<|im_end|>")

    def translate(self, text: str) -> str:
        msgs = [{"role": "system", "content": LLM_SYSTEM}, {"role": "user", "content": text}]
        prompt = self._tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        toks = self._tok.convert_ids_to_tokens(self._tok(prompt)["input_ids"])
        n_user = len(self._tok(text)["input_ids"])
        res = self._gen.generate_batch(
            [toks], max_length=min(96, 3 * n_user + 12), sampling_topk=1, include_prompt_in_result=False,
            end_token=["<|im_end|>", "<|endoftext|>"],
        )
        ids = res[0].sequences_ids[0]
        return llm_clean(self._tok.decode([i for i in ids if i != self._eos], skip_special_tokens=True))


class OpenAIBackend(Backend):
    """雲端：OpenAI Responses API（和專案 ``UEP_LLM_API=responses`` 一致）。只送測試集句子。"""

    def __init__(self, model: str, effort: str | None) -> None:
        from openai import OpenAI

        env = main_repo_env()
        key = env.get("UEP_LLM_API_KEY") or os.environ.get("UEP_LLM_API_KEY")
        if not key:
            raise RuntimeError("缺 UEP_LLM_API_KEY")
        kw: dict[str, Any] = {"api_key": key}
        for src, dst in (("UEP_LLM_PROJECT", "project"), ("UEP_LLM_ORGANIZATION", "organization"), ("UEP_LLM_BASE_URL", "base_url")):
            if env.get(src):
                kw[dst] = env[src]
        self._client = OpenAI(**kw)
        self._model, self._effort = model, effort

    def translate(self, text: str) -> str:
        kw: dict[str, Any] = {"model": self._model, "instructions": LLM_SYSTEM, "input": text, "max_output_tokens": 400}
        if self._effort:
            kw["reasoning"] = {"effort": self._effort}
        resp = self._client.responses.create(**kw)
        return llm_clean(resp.output_text or "")


# ----------------------------------------------------------------- 前處理（詞彙保護）

class Protect(Backend):
    """在 inner 前後做詞彙保護。mode=gloss：直接換成英文字；mode=ph：換成佔位名，翻完還原。"""

    PH = (("桌面", "Alpha", "desktop"), ("主機", "Gamma", "host"), ("網站", "Omega", "website"))

    def __init__(self, inner: Backend, mode: str) -> None:
        self._inner, self._mode = inner, mode
        self.model_path = inner.model_path
        self.stats = {"restored": 0, "lost": 0, "applied": 0}

    def translate(self, text: str) -> str:
        if self._mode == "gloss":
            for zh, en in GLOSSARY:
                text = text.replace(zh, f" {en} ")
            return self._inner.translate(text.strip())
        used = []
        for zh, ph, en in self.PH:
            if zh in text:
                text = text.replace(zh, f" {ph} ")
                used.append((ph, en))
        out = self._inner.translate(text.strip())
        for ph, en in used:
            self.stats["applied"] += 1
            if re.search(rf"\b{ph}\b", out, flags=re.IGNORECASE):
                out = re.sub(rf"\b{ph}\b", en, out, flags=re.IGNORECASE)
                self.stats["restored"] += 1
            else:
                self.stats["lost"] += 1
        return out


# ----------------------------------------------------------------- 候選註冊

def _opus(tw: Any, device: str) -> Backend:
    b = tw.MarianBackend(MT_ROOT / "opus-mt-zh-en", device)
    b.model_path = MT_ROOT / "opus-mt-zh-en"
    return b


def build_backend(cand: str, device: str) -> tuple[Backend, tuple[str, ...]]:
    """回傳 (後端, 額外別名)。額外別名只在候選名帶 ``+alias`` 或本就屬於前處理變體時加入。"""
    extra: tuple[str, ...] = ()
    name = cand.replace("+han", "")  # +han：只要有 Han 字就翻（見 cmd_translate）
    if name.endswith("+alias"):
        name, extra = name[: -len("+alias")], EXTRA_ALIASES
    cuda = device.startswith("cuda")
    comp_ct2 = "int8_float16" if cuda else "int8"

    if name == "opus":
        tw = load_translate_wiring()
        return _opus(tw, device), extra
    if name in ("opus-gloss", "opus-ph"):
        tw = load_translate_wiring()
        return Protect(_opus(tw, device), name.split("-")[1]), EXTRA_ALIASES
    if name == "m2m100":
        return MarianLike("m2m", MT_ROOT / "m2m100_418M", device, fp16=True), extra
    if name == "nllb600":
        return MarianLike("nllb", MT_ROOT / "nllb-200-distilled-600M", device, fp16=True), extra
    if name == "nllb13":
        return MarianLike("nllb", MT_ROOT / "nllb-200-distilled-1.3B", device, fp16=True), extra
    if name == "nllb600-ct2":
        return Ct2Seq2Seq(CT2_ROOT / "nllb-600M-int8", MT_ROOT / "nllb-200-distilled-600M", device, comp_ct2), extra
    if name == "nllb13-ct2":
        return Ct2Seq2Seq(CT2_ROOT / "nllb-1.3B-int8", MT_ROOT / "nllb-200-distilled-1.3B", device, comp_ct2), extra
    m = re.fullmatch(r"qwen(05|15|3|7)(-ct2)?", name)
    if m:
        size = {"05": "0.5B", "15": "1.5B", "3": "3B", "7": "7B"}[m.group(1)]
        src = MT_ROOT / f"Qwen2.5-{size}-Instruct"
        if m.group(2):
            return Ct2Llm(CT2_ROOT / f"qwen2.5-{size}-int8", src, device, comp_ct2), extra
        return HfLlm(src, device, fp16=True), extra
    if name.startswith("openai:"):
        parts = name.split(":")
        return OpenAIBackend(parts[1], parts[2] if len(parts) > 2 else None), extra
    raise SystemExit(f"未知候選 {cand}")


# ----------------------------------------------------------------- 翻譯流程

def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def percentile(vals: list[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def has_cjk(text: str) -> bool:
    return bool(re.search(r"[㐀-䶿一-鿿]", text))


def build_state_rows(rows: list[dict[str, Any]], msg_fn: Callable[[str, str, str], str | None],
                     ctx_fn: Callable[[str, str, str], str | None]) -> list[dict[str, Any]]:
    """和 ``build_en_test`` 同一套組 state 的邏輯；msg_fn／ctx_fn(text, what, rid) -> 英文（失敗回 None）。"""
    out_rows: list[dict[str, Any]] = []
    for r in rows:
        rid = r["id"]
        msg_en = msg_fn(r["zh"], "message", rid)
        context_en: list[dict[str, str]] = []
        for line in r.get("context_zh") or []:
            role, text = parse_context_line(line)
            en = ctx_fn(text, "context", rid)
            if en is None:
                continue
            context_en.append({"speaker": "Assistant" if role == "assistant" else "User", "text": en})
        if msg_en is None:
            msg_en = r["zh"]  # 翻譯失敗：照線上降級路徑原樣當英文送（分母仍是 84）
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
    return out_rows


def safe_tag(cand: str) -> str:
    """候選名轉檔名（Windows 檔名不能有 ':'；``openai:gpt-x:low`` -> ``openai_gpt-x_low``）。"""
    return re.sub(r"[^A-Za-z0-9._+-]", "_", cand)


def cmd_translate(args: argparse.Namespace) -> int:
    tw = load_translate_wiring()
    rows = read_rows(args.testset)
    mon = ResourceMonitor()
    mon.start()
    t0 = time.perf_counter()
    backend, extra = build_backend(args.cand, args.device)
    gpu_sync()
    load_s = time.perf_counter() - t0
    aliases = (*tw.DEFAULT_ALIASES, "U.E.P", "小U", "UEP", "uep", *extra)
    helper = tw.BoundaryTranslator(backend_factory=lambda d: backend, device=args.device, aliases=aliases, cpu_fallback=False)

    force_han = "+han" in args.cand  # 繞過 cjk_dominant 比例門檻：短中文夾英文名（例「到 discord 來」）也翻

    def prepare(text: str) -> str:
        return helper.replace_aliases(text.strip())

    def should_translate(prepared: str) -> bool:
        return tw.cjk_dominant(prepared) or (force_han and has_cjk(prepared))

    warm = tw.WARMUP_SENTENCES + ("今天要不要出門走走？", "把音樂關小聲一點。")
    t = time.perf_counter()
    for s in warm:
        backend.translate(s)
    gpu_sync()
    warm_s = time.perf_counter() - t

    # 要計時的句子：84 筆本句（別名替換後）
    msgs = [prepare(r["zh"]) for r in rows]
    if args.limit:
        msgs = msgs[: args.limit]
    lat_passes: list[list[float]] = []
    outputs: dict[str, str] = {}
    for p in range(args.passes):
        lat: list[float] = []
        for r, m in zip(rows, msgs):
            if not should_translate(m):
                lat.append(0.0)
                outputs.setdefault(r["id"], m)
                continue
            ts = time.perf_counter()
            try:
                out = backend.translate(m)
            except Exception as exc:  # noqa: BLE001
                out = ""
                print(f"[error] {r['id']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            gpu_sync()
            lat.append((time.perf_counter() - ts) * 1000.0)
            if p == 0:
                outputs[r["id"]] = out.strip()
        lat_passes.append(lat)
    steady = [x for x in lat_passes[-1] if x > 0]
    cold = [x for x in lat_passes[0] if x > 0]

    fail = {rid: o for rid, o in outputs.items() if not o or tw.is_degenerate(o)}
    cjk_left = {rid: o for rid, o in outputs.items() if has_cjk(o)}
    stats: dict[str, Any] = {
        "cand": args.cand, "device": args.device, "n_msgs": len(steady), "passes": args.passes,
        "load_s": round(load_s, 2), "warmup_s": round(warm_s, 2),
        "lat_ms_p50": round(statistics.median(steady), 1), "lat_ms_p95": round(percentile(steady, 0.95), 1),
        "lat_ms_mean": round(statistics.fmean(steady), 1), "lat_ms_max": round(max(steady), 1),
        "over_400ms": round(sum(1 for x in steady if x > TIMEOUT_MS) / len(steady), 3),
        "first_pass_p50": round(statistics.median(cold), 1), "first_pass_p95": round(percentile(cold, 0.95), 1),
        "failed_or_degenerate": sorted(fail), "contains_cjk_in_output": sorted(cjk_left),
        "model_size_mb": dir_size_mb(backend.model_path) if getattr(backend, "model_path", None) else None,
        "extra_aliases": list(extra),
    }
    if isinstance(backend, Protect):
        stats["protect"] = backend.stats
    if args.lat_only:
        stats.update(mon.stop())
        out_path = OUT_DIR / f"{safe_tag(args.cand)}.{args.device}.stats.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(stats, ensure_ascii=False))
        return 0

    # 完整輸出：本句沿用上面（已翻），前文另外翻
    ctx_cache: dict[str, str | None] = {}

    def ctx_fn(text: str, what: str, rid: str) -> str | None:
        prepared = prepare(text[:CONTEXT_MAX_CHARS])
        if not should_translate(prepared):
            return prepared
        if prepared not in ctx_cache:
            try:
                out = backend.translate(prepared).strip()
            except Exception:  # noqa: BLE001
                out = ""
            ctx_cache[prepared] = None if (not out or tw.is_degenerate(out)) else out
        return ctx_cache[prepared]

    def msg_fn(text: str, what: str, rid: str) -> str | None:
        o = outputs.get(rid, "")
        return None if (not o or tw.is_degenerate(o)) else o

    en_rows = build_state_rows(rows, msg_fn, ctx_fn)
    stats.update(mon.stop())
    out_dir = OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = safe_tag(args.cand) if args.device == "cuda" else f"{safe_tag(args.cand)}.{args.device}"
    with (out_dir / f"{tag}.en.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for row in en_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out_dir / f"{tag}.stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in stats.items() if k not in ("failed_or_degenerate", "contains_cjk_in_output")}, ensure_ascii=False))
    if fail or cjk_left:
        print(f"[warn] 失敗/退化 {sorted(fail)}；輸出含 CJK {sorted(cjk_left)}", file=sys.stderr)
    return 0


def cmd_reference(args: argparse.Namespace) -> int:
    ref = {json.loads(x)["id"]: json.loads(x) for x in args.reference.read_text(encoding="utf-8").splitlines() if x.strip()}
    rows = read_rows(args.testset)
    ctx_map = {r["id"]: ref[r["id"]].get("context_en") or [] for r in rows}

    def msg_fn(text: str, what: str, rid: str) -> str | None:
        return ref[rid]["text_en"].strip()

    # 前文用參考譯文（索引對應）
    seq: dict[str, int] = {}

    def ctx_fn(text: str, what: str, rid: str) -> str | None:
        i = seq.get(rid, 0)
        seq[rid] = i + 1
        return ctx_map[rid][i]["text"].strip()

    for r in rows:  # 核對：參考檔的前文數量與原文一致
        if len(ctx_map[r["id"]]) != len(r.get("context_zh") or []):
            raise SystemExit(f"{r['id']} 前文數量與參考檔不一致")
    en_rows = build_state_rows(rows, msg_fn, ctx_fn)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "reference.en.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for row in en_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"reference: {len(en_rows)} rows")
    return 0


# ----------------------------------------------------------------- 端到端（Laya ft-r1）

METRIC_KEYS = (
    ("reply_mode_acc", "reply_mode acc"), ("reply_mode_f1", "reply_mode F1"), ("route_acc", "route acc"),
    ("route_f1", "route F1"), ("addressed_acc", "addressed acc"), ("intent_acc", "absent.intent acc"),
    ("intent_f1", "absent.intent F1"), ("target_acc", "target_location acc"), ("target_f1", "target F1"),
    ("compound_acc", "compound acc"),
)


def cmd_e2e(args: argparse.Namespace) -> int:
    from . import eval_testset as ev

    summary: dict[str, Any] = {}
    for cand in args.cands:
        en_path = OUT_DIR / f"{cand}.en.jsonl"
        rows = ev.read_jsonl(en_path)
        runs = []
        for seed in SEEDS:
            pred_path = OUT_DIR / f"{cand}.laya_s{seed}.json"
            if args.force or not pred_path.is_file():
                cmd = [sys.executable, "-m", "laya_data.eval_testset", "predict", "--model", str(LAYA_ROOT / f"e8_seed{seed}"),
                       "--data", str(en_path), "--out", str(pred_path), "--device", args.device]
                r = subprocess.run(cmd, check=False, cwd=DECISION_DIR, capture_output=True, text=True, encoding="utf-8")
                if r.returncode != 0:
                    print(r.stderr[-2000:], file=sys.stderr)
                    return 2
            runs.append(json.loads(pred_path.read_text(encoding="utf-8")))
        ms = [ev.metrics_for(rows, run["preds"]) for run in runs]
        agg: dict[str, Any] = {}
        for key, _ in METRIC_KEYS:
            vals = [m.get(key) for m in ms if m.get(key) is not None]
            if vals:
                agg[key] = {"mean": round(statistics.fmean(vals), 4), "std": round(statistics.stdev(vals), 4) if len(vals) > 1 else 0.0,
                            "per_seed": [round(v, 4) for v in vals]}
        # 平均「全對列數」：reply_mode＋addressed／intent＋target 全對的列數（84 為分母）
        correct = [sum(1 for r in rows if ev.row_correct(r, run["preds"])) for run in runs]
        agg["rows_correct_mean"] = round(statistics.fmean(correct), 2)
        agg["rows_correct_per_seed"] = correct
        agg["n_rows"] = len(rows)
        summary[cand] = agg
    out = OUT_DIR / "e2e_summary.json"
    old = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    old.update(summary)
    out.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
    print("| cand | " + " | ".join(l for _, l in METRIC_KEYS) + " | rows ok |")
    for cand, agg in summary.items():
        cells = [f"{agg[k]['mean']:.3f}±{agg[k]['std']:.3f}" if k in agg else "n/a" for k, _ in METRIC_KEYS]
        print(f"| {cand} | " + " | ".join(cells) + f" | {agg['rows_correct_mean']} |")
    return 0


# ----------------------------------------------------------------- 翻譯品質

def cmd_quality(args: argparse.Namespace) -> int:
    sys.path.append(os.environ.get("MT_BENCH_PYLIB", ""))  # 隔離 venv／--target 目錄放在 sys.path 尾端，不蓋過共用環境
    import sacrebleu

    ref = {json.loads(x)["id"]: json.loads(x) for x in args.reference.read_text(encoding="utf-8").splitlines() if x.strip()}
    rows = read_rows(args.testset)
    ids = [r["id"] for r in rows]
    refs = [ref[i]["text_en"].strip() for i in ids]
    result: dict[str, Any] = {}
    known: dict[str, dict[str, str]] = {}
    for cand in args.cands:
        en = {r["id"]: r for r in read_rows(OUT_DIR / f"{cand}.en.jsonl")}
        hyps = [en[i]["text"] for i in ids]
        chrf = sacrebleu.corpus_chrf(hyps, [refs])
        bleu = sacrebleu.corpus_bleu(hyps, [refs])
        result[cand] = {"chrF": round(chrf.score, 1), "BLEU": round(bleu.score, 1)}
        for kid in KNOWN_BAD:
            known.setdefault(kid, {"zh": next(r["zh"] for r in rows if r["id"] == kid), "ref": ref[kid]["text_en"]})[cand] = en[kid]["text"]
    (OUT_DIR / "quality.json").write_text(json.dumps({"scores": result, "known": known}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    for kid, d in known.items():
        print(kid, KNOWN_BAD[kid])
        for k, v in d.items():
            print(f"   {k}: {v}")
    return 0


# ----------------------------------------------------------------- 轉換與雲端模型列表

def cmd_convert(args: argparse.Namespace) -> int:
    from ctranslate2.converters import TransformersConverter

    src = MT_ROOT / args.src
    dst = CT2_ROOT / args.dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    class _Conv(TransformersConverter):  # ct2 4.8 傳 dtype=，transformers 4.48 只認 torch_dtype=
        def load_model(self, model_class, model_name_or_path, **kwargs):
            if "dtype" in kwargs:
                kwargs["torch_dtype"] = kwargs.pop("dtype")
            return model_class.from_pretrained(model_name_or_path, **kwargs)

    wanted = ("tokenizer.json", "tokenizer_config.json", "sentencepiece.bpe.model", "vocab.json", "merges.txt", "special_tokens_map.json")
    conv = _Conv(str(src), copy_files=[f for f in wanted if (src / f).is_file()])
    conv.convert(str(dst), quantization=args.quant, force=True)
    print(f"converted {src} -> {dst} ({dir_size_mb(dst)} MB)")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    from openai import OpenAI

    env = main_repo_env()
    kw: dict[str, Any] = {"api_key": env["UEP_LLM_API_KEY"]}
    for src, dst in (("UEP_LLM_PROJECT", "project"), ("UEP_LLM_ORGANIZATION", "organization"), ("UEP_LLM_BASE_URL", "base_url")):
        if env.get(src):
            kw[dst] = env[src]
    ids = sorted(m.id for m in OpenAI(**kw).models.list())
    print("\n".join(ids))
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("translate")
    t.add_argument("--cand", required=True)
    t.add_argument("--device", default="cuda")
    t.add_argument("--testset", type=Path, default=DEFAULT_TESTSET)
    t.add_argument("--passes", type=int, default=2, help="計時輪數；取最後一輪為穩態（雲端建議 1）")
    t.add_argument("--limit", type=int, default=0, help="只量前 N 句（慢速 CPU 候選用）")
    t.add_argument("--lat-only", action="store_true", help="只量延遲與資源，不輸出 en.jsonl")
    r = sub.add_parser("reference")
    r.add_argument("--reference", type=Path, required=True)
    r.add_argument("--testset", type=Path, default=DEFAULT_TESTSET)
    e = sub.add_parser("e2e")
    e.add_argument("--cands", nargs="+", required=True)
    e.add_argument("--device", default="cuda")
    e.add_argument("--force", action="store_true")
    q = sub.add_parser("quality")
    q.add_argument("--cands", nargs="+", required=True)
    q.add_argument("--reference", type=Path, required=True)
    q.add_argument("--testset", type=Path, default=DEFAULT_TESTSET)
    c = sub.add_parser("convert")
    c.add_argument("--src", required=True, help="~/.uep/models/mt/ 下的目錄名")
    c.add_argument("--dst", required=True, help="~/.uep/models/mt-ct2/ 下的輸出目錄名")
    c.add_argument("--quant", default="int8_float16")
    sub.add_parser("models")
    args = p.parse_args(argv)
    return {"translate": cmd_translate, "reference": cmd_reference, "e2e": cmd_e2e, "quality": cmd_quality,
            "convert": cmd_convert, "models": cmd_models}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
