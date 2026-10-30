"""
D87 既有共享投影 about_members 回填工具。

D87 起分享審核在有名冊時會輸出 about_members，寫成投影的 subject_uids。D87 之前產生的投影
沒有 subject_uids 欄位（舊格式），要等來源概念的 digest 變動才會重投影，所以需要回填。
本工具對每個「有名冊的 guild scope」內、active 且沒有 subject_uids 欄位的投影，取其擁有者
namespace 的來源概念，套用名冊後做分享重判（重新判定：sensitivity、摘要都可能改變，
結果可能是撤回或改寫投影，報告會列出前後變化）。

流程（每個擁有者 namespace）：
  1. 取得 dream 租約（有人持有即整批中止）
  2. 擁有者 engine 注入名冊；對每個目標概念先寫 audit（意圖），再清除已判定登記並呼叫
     SharingPhase._process_concept（經 Gate、共享層，不繞過任何閘門）
  3. 寫處理登記簿 semantic/backfill_d87.json（冪等依據）

用法（以 U.E.P-s-Core 的 Python 執行）：
  # dry-run（預設）：只讀、不呼叫 LLM、不寫資料目錄
  python scripts/backfill_d87.py --data-root <資料目錄> --roster <名冊.json> --report <json 路徑>
  # apply：需 bot 停機並已備份
  python scripts/backfill_d87.py --data-root <資料目錄> --roster <名冊.json> --apply --i-have-backup <備份路徑>
  # 全部重判：改過分享摘要 prompt 後，對所有 active 投影的來源概念重寫摘要（另用 backfill_d87_rejudge.json 登記）
  python scripts/backfill_d87.py ... --rejudge-all [--apply --i-have-backup <備份路徑>]

名冊 JSON 格式（與 MemoryEngine.set_member_roster_fn 回傳值相同，外層以 guild id 分組）：
  {"<guild_id>": [{"uid": "<字串>", "display_name": "<字串>", "aliases": ["<字串>", ...]}]}
名冊裡沒有的 guild 一律跳過。報告與終端輸出的 namespace、scope 以序號遮罩（ns#N、scope#N），
名冊只報人數，不輸出內容、名字或 uid。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from echo_memory.affect.evidence_reviewer import ROSTER_MAX_MEMBERS  # noqa: E402
from echo_memory.schemas.sharing import (  # noqa: E402
    LEVEL_SCOPE_SHARED,
    is_valid_guild_id,
    namespace_to_scope,
    parse_guild_scope,
    scope_to_namespace_id,
)
from echo_memory.storage.locking import FileLease  # noqa: E402
from scripts import backfill_d88 as d88  # noqa: E402

logger = logging.getLogger("echo_memory.backfill_d87")

BackfillError = d88.BackfillError
LEDGER_NAME = "backfill_d87.json"
AUDIT_NAME = "backfill_d87_audit.jsonl"
# --rejudge-all 專用登記簿／audit：與原回填登記分開，互不影響冪等
REJUDGE_LEDGER_NAME = "backfill_d87_rejudge.json"
REJUDGE_AUDIT_NAME = "backfill_d87_rejudge_audit.jsonl"
_DONE_OUTCOMES = ("revoked", "rewritten", "reissued_same")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 名冊
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def load_roster(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    """讀並嚴格驗證名冊檔；不存在或格式錯誤即拋 BackfillError（錯誤訊息不含檔案內容）。"""
    if path is None:
        raise BackfillError("必須提供 --roster <名冊.json>")
    p = Path(path)
    if not p.is_file():
        raise BackfillError("名冊檔不存在")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackfillError("名冊檔無法解析為 JSON") from exc
    if not isinstance(data, dict):
        raise BackfillError("名冊格式錯誤：最外層必須是 {guild_id: [成員...]}")
    out: dict[str, list[dict[str, Any]]] = {}
    for gid, members in data.items():
        if not isinstance(gid, str) or not is_valid_guild_id(gid):
            raise BackfillError("名冊格式錯誤：guild id 必須是 1～48 位純數字字串")
        if not isinstance(members, list):
            raise BackfillError("名冊格式錯誤：每個 guild 的值必須是成員 list")
        seen: set[str] = set()
        clean: list[dict[str, Any]] = []
        for m in members:
            if not isinstance(m, dict):
                raise BackfillError("名冊格式錯誤：成員必須是物件")
            uid = m.get("uid")
            if not isinstance(uid, str) or not uid.strip():
                raise BackfillError("名冊格式錯誤：成員 uid 必須是非空字串")
            uid = uid.strip()
            if uid in seen:
                raise BackfillError("名冊格式錯誤：同一 guild 內 uid 重複")
            seen.add(uid)
            name = m.get("display_name", "")
            if not isinstance(name, str):
                raise BackfillError("名冊格式錯誤：display_name 必須是字串")
            aliases = m.get("aliases", [])
            if aliases is None:
                aliases = []
            if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                raise BackfillError("名冊格式錯誤：aliases 必須是字串 list")
            if not any(s.strip() for s in [name, *aliases]):
                raise BackfillError("名冊格式錯誤：成員的 display_name 與 aliases 至少要有一個非空")
            clean.append({"uid": uid, "display_name": name, "aliases": list(aliases)})
        out[gid] = clean
    return out


def _roster_fn(roster: dict[str, list[dict[str, Any]]]) -> Callable[[str], Any]:
    def fn(guild_id: str) -> list[dict[str, Any]] | None:
        members = roster.get(str(guild_id))
        return [dict(m) for m in members] if members else None

    return fn


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 唯讀分析（dry-run 與 apply 共用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def load_ledger(ns_dir: Path, rejudge_all: bool = False) -> dict[str, dict[str, Any]]:
    name = REJUDGE_LEDGER_NAME if rejudge_all else LEDGER_NAME
    data = d88._read_json(ns_dir / "semantic" / name, {})
    return data if isinstance(data, dict) else {}


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _proj_snapshot(p: Any) -> dict[str, Any]:
    """舊投影（scope namespace 的概念）的前狀態；只留雜湊與列舉值，不留摘要原文。"""
    meta = p.metadata or {}
    return {
        "pid": str(meta.get("projection_id") or ""),
        "summary_sha": _sha(p.description),
        "sensitivity": str(meta.get("sensitivity") or "none"),
        "review_status": str(meta.get("review_status") or "unknown"),
    }


def _scope_old_projections(
    data_root: Path, scope_ns: str, rejudge_all: bool = False,
) -> dict[str, dict[str, Any]]:
    """scope namespace 內 active 的投影：projection_id -> 前狀態＋來源。

    預設只取沒有 subject_uids 欄位的舊格式投影；rejudge_all 取全部 active 投影。
    """
    out: dict[str, dict[str, Any]] = {}
    for p in d88._load_active_concepts(data_root / "namespaces" / scope_ns):
        meta = p.metadata or {}
        if meta.get("kind") != "projection" or (not rejudge_all and "subject_uids" in meta):
            continue
        snap = _proj_snapshot(p)
        if not snap["pid"]:
            continue
        snap["owner_uid"] = str(meta.get("owner_uid") or "")
        snap["source_concept_id"] = str(meta.get("source_concept_id") or "")
        out[snap["pid"]] = snap
    return out


def analyze_namespace(
    data_root: Path, ns_id: str, roster: dict[str, list[dict[str, Any]]],
    scope_old: dict[str, dict[str, dict[str, Any]]], rejudge_all: bool = False,
) -> dict[str, Any]:
    """單一擁有者 namespace 的唯讀分析。回傳含內部目標清單（_targets）的 dict；報告前須移除。

    scope_old：scope_id（guild:N）-> {projection_id: 舊投影前狀態}。
    """
    ns_dir = data_root / "namespaces" / ns_id
    concepts = d88._load_active_concepts(ns_dir)
    ledger = load_ledger(ns_dir, rejudge_all)
    targets: list[tuple[Any, list[dict[str, Any]]]] = []
    skipped = {"no_roster": 0, "pinned_or_not_shared": 0, "already_backfilled": 0}
    old_total = 0
    for c in concepts:
        entries = []
        for scope_id, entry in c.sharing_state().by_scope.items():
            pid = entry.projection_id
            old = scope_old.get(scope_id, {}).get(pid) if pid else None
            if old is None or old["owner_uid"] != ns_id or old["source_concept_id"] != c.id:
                continue
            old_total += 1
            gid = parse_guild_scope(scope_id)
            if not gid or not roster.get(gid):
                skipped["no_roster"] += 1
            elif entry.pinned or entry.level != LEVEL_SCOPE_SHARED:
                skipped["pinned_or_not_shared"] += 1
            elif (ledger.get(c.id) or {}).get("scopes", {}).get(scope_id) in _DONE_OUTCOMES:
                skipped["already_backfilled"] += 1
            else:
                entries.append({"scope_id": scope_id, "before": old})
        if entries:
            targets.append((c, entries))
    targets.sort(key=lambda t: (-int(t[0].access_count), t[0].created_at.timestamp()))
    return {
        "active_concepts": len(concepts),
        "old_format_projections": old_total,
        "targets": len(targets),
        "target_projections": sum(len(e) for _, e in targets),
        "skipped": skipped,
        "lease": d88.lease_state(ns_dir),
        "estimated_llm_calls": {"share_rejudge_est": sum(len({x["scope_id"] for x in e}) for _, e in targets)},
        "_targets": [{"cid": c.id, "scopes": e} for c, e in targets],
    }


def _apply_limit(per_ns: dict[str, dict[str, Any]], order: list[str], limit: int | None) -> None:
    """依 namespace 順序分配全域名額（單位：來源概念）。"""
    if limit is None:
        return
    remaining = max(0, limit)
    for ns_id in order:
        info = per_ns[ns_id]
        keep = info["_targets"][:remaining]
        remaining -= len(keep)
        dropped = len(info["_targets"]) - len(keep)
        if dropped:
            info["limited_out"] = dropped
            info["_targets"] = keep
            info["targets"] = len(keep)
            info["target_projections"] = sum(len(t["scopes"]) for t in keep)
            info["estimated_llm_calls"] = {
                "share_rejudge_est": sum(len({x["scope_id"] for x in t["scopes"]}) for t in keep),
            }


def plan(
    data_root: Path, roster: dict[str, list[dict[str, Any]]], namespaces: str, limit: int | None,
    rejudge_all: bool = False,
) -> tuple[list[str], dict[str, int], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """回傳 (被選 namespace, ns_id -> 序號, 各 namespace 分析, 各 scope 概況)。"""
    all_ns = d88.list_user_namespaces(data_root)
    index = {ns: i + 1 for i, ns in enumerate(all_ns)}
    if namespaces.strip().lower() == "all":
        selected = list(all_ns)
    else:
        wanted = [x.strip() for x in namespaces.split(",") if x.strip()]
        unknown = [x for x in wanted if x not in index]
        if unknown:
            raise BackfillError(f"--namespaces 含 {len(unknown)} 個不存在的 namespace")
        selected = [ns for ns in all_ns if ns in set(wanted)]

    scope_old: dict[str, dict[str, dict[str, Any]]] = {}
    scopes_report: list[dict[str, Any]] = []
    for i, scope_ns in enumerate(d88.list_scope_namespaces(data_root), start=1):
        scope_id = namespace_to_scope(scope_ns)
        if scope_id is None:
            continue
        gid = parse_guild_scope(scope_id) or ""
        old = _scope_old_projections(data_root, scope_ns, rejudge_all)
        scope_old[scope_id] = old
        scopes_report.append({
            "scope": f"scope#{i}",
            "roster_members": len(roster.get(gid, [])),
            "has_roster": bool(roster.get(gid)),
            "old_format_projections": len(old),
        })

    per_ns = {ns: analyze_namespace(data_root, ns, roster, scope_old, rejudge_all) for ns in selected}
    _apply_limit(per_ns, selected, limit)
    return selected, index, per_ns, scopes_report


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# apply
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _append_audit(ns_dir: Path, record: dict[str, Any], rejudge_all: bool = False) -> None:
    """audit 先於資料修改：寫入失敗即拋例外，呼叫端不得繼續改該概念。"""
    import os

    path = ns_dir / "semantic" / (REJUDGE_AUDIT_NAME if rejudge_all else AUDIT_NAME)
    record = {"at": datetime.now(timezone.utc).isoformat(), **record}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        raise BackfillError(f"audit 寫入失敗: {exc}") from exc


def _acquire_leases(data_root: Path, ns_ids: list[str], ttl: float) -> dict[str, FileLease]:
    """全部 namespace 的 dream 租約一次取得；任一取不到即全數釋放並中止。"""
    held: dict[str, FileLease] = {}
    for ns in ns_ids:
        lease = FileLease(data_root / "namespaces" / ns, "dream", ttl_seconds=ttl)
        ok, reason = lease.try_acquire(owner="backfill_d87")
        if not ok:
            for got in held.values():
                got.release()
            raise BackfillError(f"dream 租約被占用，中止（未做任何修改）: {reason}")
        held[ns] = lease
    return held


def _bump(d: dict[str, int], key: str, n: int = 1) -> None:
    d[key] = d.get(key, 0) + n


def _process_namespace(
    data_root: Path, ns_id: str, targets: list[dict[str, Any]], llm_fn: Callable[[str], str],
    roster: dict[str, list[dict[str, Any]]], engine_kwargs: dict[str, Any], lease: FileLease,
    rejudge_all: bool = False,
) -> dict[str, Any]:
    from echo_memory.engine import MemoryEngine

    ns_dir = data_root / "namespaces" / ns_id
    result: dict[str, Any] = {
        "outcomes": {}, "subject_distribution": {}, "subjects_total": 0,
        "projections_with_subjects": 0, "projection_status_before": {}, "projection_status_after": {},
        "not_ready": "",
    }
    engine = MemoryEngine(storage_path=data_root, namespace_id=ns_id, llm_fn=llm_fn, **engine_kwargs)
    try:
        engine.ensure_initialized()
        d88._check_embedding_dim(engine, ns_dir)
        for scope_id in sorted({x["scope_id"] for t in targets for x in t["scopes"]}):
            d88._check_embedding_dim(engine, data_root / "namespaces" / scope_to_namespace_id(scope_id))
        engine.set_member_roster_fn(_roster_fn(roster))
        dream = engine._ensure_dream_engine()
        phase = dream._sharing_phase
        if phase is None:
            result["not_ready"] = "no_sharing_phase"
            return result
        missing = phase.ready()
        if missing:
            result["not_ready"] = missing
            return result

        from echo_memory.dream.sharing_phase import SharingReport

        layer = engine.share_layer
        ledger = load_ledger(ns_dir, rejudge_all)
        _append_audit(ns_dir, {"event": "run_start", "targets": len(targets)}, rejudge_all)
        for t in targets:
            if not lease.renew():
                raise BackfillError("dream 租約已遺失，中止")
            cid = t["cid"]
            for x in t["scopes"]:
                _bump(result["projection_status_before"], x["before"]["review_status"])
            _append_audit(ns_dir, {
                "event": "intent", "concept_id": cid,
                "projections": [{"scope": x["scope_id"], "projection_id": x["before"]["pid"]} for x in t["scopes"]],
            }, rejudge_all)
            rep = SharingReport()
            phase._forget_judged(cid)
            try:
                phase._process_concept(cid, rep)
            except Exception as exc:  # noqa: BLE001 - 單筆失敗留待下次重跑
                logger.warning("分享重判失敗: %s", exc)
                _bump(result["outcomes"], "error")
                _append_audit(ns_dir, {"event": "result", "concept_id": cid, "error": type(exc).__name__}, rejudge_all)
                continue
            concept = engine.semantic.get(cid)
            state = concept.sharing_state() if concept is not None else None
            per_scope: dict[str, str] = {}
            for x in t["scopes"]:
                sid, before = x["scope_id"], x["before"]
                entry = state.get(sid) if state is not None else None
                new_pid = entry.projection_id if entry is not None else ""
                if not new_pid:
                    outcome, subjects = "revoked", 0
                elif new_pid == before["pid"]:
                    outcome, subjects = "unchanged_pending", 0  # 判定被擱置或失敗：維持舊投影，下次重跑
                else:
                    view = layer.get_projection(sid, new_pid)
                    if view is None:
                        outcome, subjects = "unchanged_pending", 0
                    else:
                        subjects = len(view.subject_uids)
                        same = (
                            _sha(view.summary) == before["summary_sha"]
                            and view.sensitivity == before["sensitivity"]
                            and view.review_status == before["review_status"]
                        )
                        outcome = "reissued_same" if same else "rewritten"
                        _bump(result["projection_status_after"], view.review_status)
                        _bump(result["subject_distribution"], str(subjects))
                        result["subjects_total"] += subjects
                        if subjects:
                            result["projections_with_subjects"] += 1
                per_scope[sid] = outcome
                _bump(result["outcomes"], outcome)
            _append_audit(ns_dir, {
                "event": "result", "concept_id": cid, "outcomes": per_scope,
                "judged": rep.judged, "projected": rep.projected, "held": rep.held,
            }, rejudge_all)
            if any(v in _DONE_OUTCOMES for v in per_scope.values()):
                prev = ledger.get(cid) if isinstance(ledger.get(cid), dict) else {}
                scopes = dict(prev.get("scopes") or {})
                scopes.update({k: v for k, v in per_scope.items() if v in _DONE_OUTCOMES})
                ledger[cid] = {"at": datetime.now(timezone.utc).isoformat(), "scopes": scopes}
                d88._atomic_write_json(ns_dir / "semantic" / (REJUDGE_LEDGER_NAME if rejudge_all else LEDGER_NAME), ledger)
    finally:
        engine.close()
    return result


def run(
    data_root: Path,
    *,
    roster: dict[str, list[dict[str, Any]]] | None = None,
    namespaces: str = "all",
    apply: bool = False,
    backup: Path | None = None,
    limit: int | None = None,
    llm_fn: Callable[[str], str] | None = None,
    engine_kwargs: dict[str, Any] | None = None,
    lease_ttl: float = 3600.0,
    rejudge_all: bool = False,
) -> dict[str, Any]:
    """執行（或 dry-run）並回傳遮罩後的報告 dict。roster 為 load_roster 的結果。"""
    data_root = Path(data_root)
    if roster is None:
        raise BackfillError("必須提供名冊（--roster）")
    if not (data_root / "namespaces").is_dir():
        raise BackfillError("資料目錄下找不到 namespaces/")
    if apply:
        if backup is None or not Path(backup).exists():
            raise BackfillError("--apply 必須搭配 --i-have-backup <存在的備份路徑>")
        if llm_fn is None:
            raise BackfillError("apply 需要可用的 LLM（llm_fn）")

    selected, index, per_ns, scopes_report = plan(data_root, roster, namespaces, limit, rejudge_all)
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "rejudge_all": rejudge_all,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": limit,
        "roster": {
            "guilds": len(roster),
            "members": sum(len(v) for v in roster.values()),
            "max_members_per_prompt": ROSTER_MAX_MEMBERS,
        },
        "scopes": scopes_report,
        "namespaces": [],
    }
    applied: dict[str, dict[str, Any]] = {}
    if apply:
        work = [ns for ns in selected if per_ns[ns]["_targets"]]
        leases = _acquire_leases(data_root, work, lease_ttl)
        try:
            for ns in work:
                # 取得租約後再讀一次，避免計畫與實際落差
                scope_old = {
                    sid: _scope_old_projections(data_root, scope_to_namespace_id(sid), rejudge_all)
                    for sid in (namespace_to_scope(s) for s in d88.list_scope_namespaces(data_root)) if sid
                }
                fresh = analyze_namespace(data_root, ns, roster, scope_old, rejudge_all)
                planned = {t["cid"] for t in per_ns[ns]["_targets"]}
                targets = [t for t in fresh["_targets"] if t["cid"] in planned]
                applied[ns] = _process_namespace(
                    data_root, ns, targets, llm_fn, roster, engine_kwargs or {}, leases[ns],  # type: ignore[arg-type]
                    rejudge_all,
                )
        finally:
            for lease in leases.values():
                lease.release()

    for ns in selected:
        info = {k: v for k, v in per_ns[ns].items() if not k.startswith("_")}
        entry: dict[str, Any] = {"namespace": f"ns#{index[ns]}", **info}
        if apply and ns in applied:
            entry["apply"] = applied[ns]
        report["namespaces"].append(entry)

    report["totals"] = _totals(report["namespaces"], apply)
    return report


def _totals(entries: list[dict[str, Any]], apply: bool) -> dict[str, Any]:
    skipped: dict[str, int] = {}
    for e in entries:
        for k, v in e["skipped"].items():
            skipped[k] = skipped.get(k, 0) + v
    tot: dict[str, Any] = {
        "namespaces": len(entries),
        "active_concepts": sum(e["active_concepts"] for e in entries),
        "old_format_projections": sum(e["old_format_projections"] for e in entries),
        "targets": sum(e["targets"] for e in entries),
        "target_projections": sum(e["target_projections"] for e in entries),
        "skipped": skipped,
        "estimated_llm_calls": {
            "share_rejudge_est": sum(e["estimated_llm_calls"]["share_rejudge_est"] for e in entries),
        },
    }
    if apply:
        outcomes: dict[str, int] = {}
        dist: dict[str, int] = {}
        before: dict[str, int] = {}
        after: dict[str, int] = {}
        with_subjects = subjects_total = 0
        for e in entries:
            a = e.get("apply")
            if not a:
                continue
            for src, dst in (
                (a["outcomes"], outcomes), (a["subject_distribution"], dist),
                (a["projection_status_before"], before), (a["projection_status_after"], after),
            ):
                for k, v in src.items():
                    dst[k] = dst.get(k, 0) + v
            with_subjects += a["projections_with_subjects"]
            subjects_total += a["subjects_total"]
        tot.update({
            "outcomes": outcomes,
            "projections_with_subjects": with_subjects,
            "subjects_total": subjects_total,
            "subject_distribution": dist,
            "projection_status_before": before,
            "projection_status_after": after,
        })
    return tot


def format_report(report: dict[str, Any]) -> str:
    r = report["roster"]
    lines = [
        f"[D87 回填] 模式: {report['mode']}{'（rejudge-all）' if report.get('rejudge_all') else ''}（limit={report['limit']}）",
        f"名冊: {r['guilds']} 個 guild、共 {r['members']} 人（每次 prompt 最多取 {r['max_members_per_prompt']} 位）",
        "scope | 名冊人數 | 舊格式投影",
    ]
    for s in report["scopes"]:
        lines.append(f"{s['scope']} | {s['roster_members'] if s['has_roster'] else '無名冊（跳過）'} | {s['old_format_projections']}")
    lines.append("ns | active | 舊格式投影 | 目標概念 | 目標投影 | 預估LLM(分享重判) | 租約")
    for e in report["namespaces"]:
        lines.append(
            f"{e['namespace']} | {e['active_concepts']} | {e['old_format_projections']} | {e['targets']} | "
            f"{e['target_projections']} | {e['estimated_llm_calls']['share_rejudge_est']} | "
            f"{'持有中' if e['lease']['held'] else '空閒'}"
        )
    t = report["totals"]
    lines.append(
        f"合計: 目標概念 {t['targets']}、目標投影 {t['target_projections']}、"
        f"預估 LLM 分享重判 {t['estimated_llm_calls']['share_rejudge_est']}（下限估計，實際依各概念的候選場域數）"
    )
    lines.append("跳過: " + ", ".join(f"{k}={v}" for k, v in t["skipped"].items()))
    if report["mode"] == "apply":
        lines.append("處理結果: " + (", ".join(f"{k}={v}" for k, v in sorted(t["outcomes"].items())) or "無"))
        lines.append(
            f"被標上 subject 的投影 {t['projections_with_subjects']}、subject 總數 {t['subjects_total']}、"
            "分布（每則投影 subject 數 -> 投影數）: "
            + (", ".join(f"{k}:{v}" for k, v in sorted(t["subject_distribution"].items())) or "無")
        )
        lines.append("投影審核狀態（前）: " + (", ".join(f"{k}={v}" for k, v in sorted(t["projection_status_before"].items())) or "無"))
        lines.append("投影審核狀態（後）: " + (", ".join(f"{k}={v}" for k, v in sorted(t["projection_status_after"].items())) or "無"))
        not_ready = {e["apply"]["not_ready"] for e in report["namespaces"] if e.get("apply", {}).get("not_ready")}
        if not_ready:
            lines.append("分享前置條件不齊備: " + ", ".join(sorted(not_ready)))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D87 既有投影 about_members 回填工具（預設 dry-run）")
    ap.add_argument("--data-root", required=True, type=Path, help="記憶資料目錄（含 namespaces/）")
    ap.add_argument("--roster", type=Path, default=None, help="名冊 JSON：{guild_id: [{uid, display_name, aliases}]}（必填）")
    ap.add_argument("--namespaces", default="all", help="all 或逗號分隔的 namespace id")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只讀預覽（預設）")
    mode.add_argument("--apply", action="store_true", help="實際寫入（需 --i-have-backup）")
    ap.add_argument("--i-have-backup", type=Path, default=None, help="備份路徑（apply 必填且須存在）")
    ap.add_argument(
        "--rejudge-all", action="store_true",
        help="目標放寬為所有 active 投影的來源概念（含已有 subject_uids），用於改 prompt 後重寫摘要；"
             "冪等登記改用 backfill_d87_rejudge.json",
    )
    ap.add_argument("--limit", type=int, default=None, help="本次最多重判幾個來源概念（跨 namespace 總數）")
    ap.add_argument("--report", type=Path, default=None, help="報告 JSON 輸出路徑（遮罩後）")
    ap.add_argument("--embedding-model", default=None, help="apply 用；預設取 config/env，須與 bot 一致")
    ap.add_argument("--embedding-backend", default=None, help="apply 用；sentence-transformers 或 ollama")
    ap.add_argument("--ollama-url", default=None, help="apply 用；Ollama 位址")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    try:
        if args.report is not None:
            root = args.data_root.resolve()
            rp = args.report.resolve()
            if rp == root or root in rp.parents:
                raise BackfillError("--report 不得位於資料目錄內")
        roster = load_roster(args.roster)
        llm_fn = None
        kwargs: dict[str, Any] = {}
        if args.apply:
            if args.i_have_backup is None or not args.i_have_backup.exists():
                raise BackfillError("--apply 必須搭配 --i-have-backup <存在的備份路徑>")
            llm_fn = d88._build_llm_fn()
            kwargs = d88._engine_kwargs(args)
        report = run(
            args.data_root, roster=roster, namespaces=args.namespaces, apply=args.apply,
            backup=args.i_have_backup, limit=args.limit, llm_fn=llm_fn, engine_kwargs=kwargs,
            rejudge_all=args.rejudge_all,
        )
    except BackfillError as exc:
        print(f"中止: {exc}", file=sys.stderr)
        return 2

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
