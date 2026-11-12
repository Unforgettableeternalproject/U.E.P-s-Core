"""拆分器評測集（PR-C0）的列格式與驗證規則，依 Chain 介面契約 3.10。

列格式為契約的欄位，另加一個擴充欄位 ``segments[].source``（該段在原文中的逐字子字串，
尚未補完指代），用來在原文上定位切點（指標 1）。契約 3.10 的列格式沒有它，需裁決是否追加。
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

LANGS = ("zh", "en")
KINDS = ("independent", "dependent", "multi_item", "multi_step")
ROUTES = ("direct_chat", "agent_loop", "task_request", "ignore")
MIN_SEGMENTS, MAX_SEGMENTS = 2, 4
ID_RE = re.compile(r"^cs-[0-9a-z]+$")
CJK_RE = re.compile(r"[㐀-鿿]")
PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"{path}:{n} 不是合法 JSON：{e}") from e
    return rows


def is_punct(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch)[0] in "PSZ"


def contains_name(haystack: str, needle: str) -> bool:
    """名字／片語是否出現在文字中；拉丁字母以字界比對、不分大小寫，其餘為子字串。"""
    if not needle:
        return False
    if re.fullmatch(r"[A-Za-z0-9' .\-]+", needle):
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])", haystack, re.I) is not None
    return needle in haystack


def source_spans(text: str, segments: list[dict[str, Any]]) -> list[tuple[int, int]] | None:
    """依序在原文找各段 source；找不到或順序錯誤回傳 None。"""
    spans, pos = [], 0
    for seg in segments:
        src = seg.get("source")
        if not isinstance(src, str) or not src:
            return None
        i = text.find(src, pos)
        if i < 0:
            return None
        spans.append((i, i + len(src)))
        pos = i + len(src)
    return spans


def validate_row(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """回傳 (錯誤, 警告)。"""
    err: list[str] = []
    warn: list[str] = []

    def need(cond: bool, msg: str) -> bool:
        if not cond:
            err.append(msg)
        return cond

    if not need(isinstance(row.get("id"), str) and bool(ID_RE.match(row["id"])), "id 格式需為 cs-xxxx"):
        pass
    need(row.get("lang") in LANGS, f"lang 需為 {LANGS}")
    text = row.get("text")
    if not need(isinstance(text, str) and text.strip() != "", "text 需為非空字串"):
        return err, warn
    ctx = row.get("context")
    need(ctx is None or isinstance(ctx, str), "context 需為 null 或字串")
    need(row.get("notes") is None or isinstance(row.get("notes"), str), "notes 需為 null 或字串")
    kind = row.get("kind")
    need(kind in KINDS, f"kind 需為 {KINDS}")
    single = row.get("expect_single")
    need(isinstance(single, bool), "expect_single 需為布林")
    if kind in KINDS and isinstance(single, bool):
        need(single == (kind != "independent"), "expect_single 必須等於 (kind != independent)")
    segs = row.get("segments")
    if not need(isinstance(segs, list) and len(segs) > 0, "segments 需為非空 list"):
        return err, warn

    if single is True:
        need(len(segs) == 1, "expect_single=true 時 segments 必須恰為 1 段")
    elif kind == "independent":
        need(MIN_SEGMENTS <= len(segs) <= MAX_SEGMENTS, f"independent 需 {MIN_SEGMENTS}-{MAX_SEGMENTS} 段")

    for i, seg in enumerate(segs):
        p = f"segments[{i}]"
        if not need(isinstance(seg, dict), f"{p} 需為物件"):
            continue
        st, sen = seg.get("text"), seg.get("text_en")
        need(isinstance(st, str) and st.strip() != "", f"{p}.text 需為非空字串")
        need(isinstance(sen, str) and sen.strip() != "", f"{p}.text_en 需為非空字串")
        need(seg.get("route") in ROUTES, f"{p}.route 需為 {ROUTES}")
        need(isinstance(seg.get("addressed"), bool), f"{p}.addressed 需為布林")
        if isinstance(seg.get("addressed"), bool) and seg.get("route") in ROUTES:
            need(seg["addressed"] == (seg["route"] != "ignore"), f"{p} addressed=false 當且僅當 route=ignore")
        voc = seg.get("vocative")
        need(voc is None or (isinstance(voc, str) and voc != ""), f"{p}.vocative 需為 null 或非空字串")
        if isinstance(voc, str) and isinstance(st, str):
            need(contains_name(st, voc), f"{p}.text 必須保留 vocative「{voc}」")
        src = seg.get("source")
        need(isinstance(src, str) and src != "", f"{p}.source 需為非空字串")
        if isinstance(src, str):
            need(src in text, f"{p}.source 必須是原文的逐字子字串")
        if isinstance(st, str) and isinstance(sen, str):
            if row.get("lang") == "en":
                need(sen == st, f"{p}: lang=en 時 text_en 必須等於 text")
            elif row.get("lang") == "zh":
                need(CJK_RE.search(sen) is None, f"{p}.text_en 不應含中文字")
        prons = seg.get("pronouns")
        if need(isinstance(prons, list), f"{p}.pronouns 需為 list"):
            for j, pr in enumerate(prons):
                q = f"{p}.pronouns[{j}]"
                if not need(isinstance(pr, dict), f"{q} 需為物件"):
                    continue
                s, r = pr.get("surface"), pr.get("resolves_to")
                need(isinstance(s, str) and s != "", f"{q}.surface 需為非空字串")
                need(isinstance(r, str) and r != "", f"{q}.resolves_to 需為非空字串")
                acc = pr.get("accept", [])
                need(isinstance(acc, list) and all(isinstance(a, str) and a for a in acc), f"{q}.accept 需為字串 list")
                if isinstance(s, str) and isinstance(src, str):
                    need(contains_name(src, s), f"{q}.surface 必須出現在該段 source")
                if isinstance(r, str) and isinstance(st, str):
                    need(contains_name(st, r), f"{q}: 補完後 text 必須含 resolves_to")
                if isinstance(s, str) and isinstance(st, str) and isinstance(r, str) and s not in r:
                    need(not contains_name(st, s), f"{q}: 補完後 text 不應再含代名詞 surface")

    if single is True and len(segs) == 1 and isinstance(segs[0], dict):
        s0 = segs[0]
        need(s0.get("text") == text, "expect_single 時 segments[0].text 必須等於整句 text")
        need(s0.get("source") == text, "expect_single 時 segments[0].source 必須等於整句 text")
        need(s0.get("pronouns") == [], "expect_single 時 pronouns 必須為空")
        if kind in ("dependent", "multi_step"):
            need(s0.get("route") == "agent_loop", f"{kind} 的 route 必須為 agent_loop")
    if kind == "independent" and all(isinstance(s, dict) for s in segs):
        need(any(s.get("addressed") is True for s in segs), "independent 至少要有一段 addressed=true（chain 啟動條件 3）")
        spans = source_spans(text, segs)
        if need(spans is not None, "各段 source 必須依序、不重疊地出現在原文"):
            covered = sum(b - a for a, b in spans)
            rest = len(text) - covered
            if rest > max(10, len(text) // 3):
                warn.append("source 未涵蓋的原文過多，請確認沒有漏標段落")
    return err, warn


def validate_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    errors: dict[str, list[str]] = {}
    warns: dict[str, list[str]] = {}
    seen_ids: set[str] = set()
    seen_text: dict[str, str] = {}
    for idx, row in enumerate(rows):
        rid = str(row.get("id", f"#{idx}"))
        e, w = validate_row(row)
        if rid in seen_ids:
            e.append("id 重複")
        seen_ids.add(rid)
        t = PUNCT_RE.sub("", str(row.get("text", "")).lower())
        if t in seen_text:
            e.append(f"text 與 {seen_text[t]} 重複")
        seen_text[t] = rid
        if e:
            errors[rid] = e
        if w:
            warns[rid] = w
    return errors, warns
