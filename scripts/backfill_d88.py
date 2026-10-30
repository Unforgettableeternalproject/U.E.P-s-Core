"""
D88 舊資料一次性重審工具。

D88 起證據審核新增 corroboration_class（identity／general／self_fact）：identity 與 general
判為 not_required，只有 self_fact 可能是 insufficient。Dream 的 EvidenceReviewer 只挑
not_checked，所以 D88 之前判成 insufficient 的舊概念不會被重審。本工具對這些概念再審一次，
並讓其共享投影依現有分享機制重判。

流程（每個 namespace）：
  1. 取得 dream 租約（有人持有即整批中止）
  2. 對目標概念寫 audit（意圖）-> EvidenceReviewer.review（經 Gate，Gate 自己 audit-before-mutate）
  3. 狀態離開 insufficient 且有投影者：清除已判定登記 -> SharingPhase 重判投影
  4. 寫處理登記簿（冪等依據）

用法（以 U.E.P-s-Core 的 Python 執行）：
  # dry-run（預設）：只讀、不呼叫 LLM、不寫資料目錄
  python scripts/backfill_d88.py --data-root <memory 資料目錄> --report <json 路徑>
  # apply：需 bot 停機並已備份
  python scripts/backfill_d88.py --data-root <資料目錄> --apply --i-have-backup <備份路徑>

報告與終端輸出的 namespace 一律以序號遮罩（ns#N），不輸出概念內容、名字或 uid。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from echo_memory.namespace import normalize_namespace_id  # noqa: E402
from echo_memory.schemas.concept import Concept  # noqa: E402
from echo_memory.schemas.sharing import is_scope_namespace_id  # noqa: E402
from echo_memory.storage.locking import FileLease  # noqa: E402

logger = logging.getLogger("echo_memory.backfill_d88")

LEDGER_NAME = "backfill_d88.json"
AUDIT_NAME = "backfill_d88_audit.jsonl"
STATUSES = ("not_required", "supported", "insufficient", "contradicted", "not_checked")


class BackfillError(RuntimeError):
    """工具前置條件不滿足或執行中必須中止。"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 唯讀分析（dry-run 與 apply 共用；只讀 JSON，不建 engine、不寫檔）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def list_user_namespaces(data_root: Path) -> list[str]:
    """資料目錄下的使用者 namespace id（排序）；不含共享 scope namespace。"""
    base = data_root / "namespaces"
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.iterdir(), key=lambda q: q.name):
        if not p.is_dir() or not (p / "semantic" / "concepts.json").exists():
            continue
        try:
            normalize_namespace_id(p.name)
        except ValueError:
            continue
        if is_scope_namespace_id(p.name):
            continue
        out.append(p.name)
    return out


def list_scope_namespaces(data_root: Path) -> list[str]:
    base = data_root / "namespaces"
    if not base.is_dir():
        return []
    return sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and is_scope_namespace_id(p.name) and (p / "semantic" / "concepts.json").exists()
    )


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_ledger(ns_dir: Path) -> dict[str, dict[str, Any]]:
    data = _read_json(ns_dir / "semantic" / LEDGER_NAME, {})
    return data if isinstance(data, dict) else {}


def _load_active_concepts(ns_dir: Path) -> list[Concept]:
    raw = _read_json(ns_dir / "semantic" / "concepts.json", [])
    out: list[Concept] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            c = Concept.from_dict(item)
        except Exception:  # noqa: BLE001 - 壞資料略過，不影響其他概念
            continue
        if c.status == "active" and not c.metadata.get("merged_into"):
            out.append(c)
    return out


def _projection_scopes(concept: Concept) -> list[str]:
    by_scope = (concept.sharing or {}).get("by_scope") if isinstance(concept.sharing, dict) else None
    if not isinstance(by_scope, dict):
        return []
    return [
        sid for sid, entry in by_scope.items()
        if isinstance(entry, dict) and entry.get("projection_id")
    ]


def lease_state(ns_dir: Path) -> dict[str, Any]:
    """唯讀檢查 dream 租約（直接讀 JSON，不取鎖、不寫檔）。"""
    state = _read_json(ns_dir / ".dream-lease.json", {})
    if not isinstance(state, dict):
        state = {}
    try:
        expires = float(state.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        expires = 0.0
    held = bool(state.get("holder_token")) and expires > time.time()
    return {"held": held, "remaining_seconds": max(0, int(expires - time.time())) if held else 0}


def analyze_namespace(data_root: Path, ns_id: str, scope_ids: list[str]) -> dict[str, Any]:
    """單一 namespace 的唯讀分析。回傳含內部 id 清單（_target_ids）的 dict；報告前須移除。"""
    ns_dir = data_root / "namespaces" / ns_id
    concepts = _load_active_concepts(ns_dir)
    ledger = load_ledger(ns_dir)

    status_counts = {s: 0 for s in STATUSES}
    raw_insufficient = 0
    already_classified = already_done = stale = structural = 0
    targets: list[Concept] = []
    for c in concepts:
        eff = c.effective_review_status()
        status_counts[eff] = status_counts.get(eff, 0) + 1
        review = c.evidence_review if isinstance(c.evidence_review, dict) else {}
        if review.get("status") == "insufficient":
            raw_insufficient += 1
            if eff != "insufficient":
                stale += 1  # 證據基礎已變動：已是 not_checked，Dream 會照常重審
                continue
        if eff != "insufficient":
            continue
        if c.id in ledger:
            already_done += 1
        elif review.get("corroboration_class"):
            already_classified += 1  # 已用 D88 規則審過，insufficient 即為 self_fact 結論
        else:
            targets.append(c)
            if review.get("reviewer") == "none":
                structural += 1  # 沒有可解析證據的結構性 insufficient，重審不叫 LLM
    targets.sort(key=lambda c: (-int(c.access_count), c.created_at.timestamp()))

    target_ids = {c.id for c in targets}
    owner_projection_scopes = {c.id: _projection_scopes(c) for c in targets}
    projections_affected = sum(len(v) for v in owner_projection_scopes.values())

    # 共享側交叉檢查：scope namespace 內由本 namespace 概念投影出來、仍有效的投影
    live_in_scope = 0
    projection_status: dict[str, int] = {}
    for sid in scope_ids:
        for p in _load_active_concepts(data_root / "namespaces" / sid):
            meta = p.metadata or {}
            if (
                meta.get("kind") == "projection"
                and meta.get("owner_uid") == ns_id
                and meta.get("source_concept_id") in target_ids
            ):
                live_in_scope += 1
                key = str(meta.get("review_status") or "unknown")
                projection_status[key] = projection_status.get(key, 0) + 1

    pending_share = sum(
        1 for e in ledger.values()
        if isinstance(e, dict) and e.get("projected") and not e.get("share_done")
    )
    return {
        "active_concepts": len(concepts),
        "status": status_counts,
        "raw_insufficient": raw_insufficient,
        "targets": len(targets),
        "skipped": {
            "already_classified": already_classified,
            "already_backfilled": already_done,
            "stale_digest": stale,
        },
        "structural_no_llm": structural,
        "projections_affected": projections_affected,
        "projections_live_in_scope": live_in_scope,
        "projection_status": projection_status,
        "pending_share_rejudge": pending_share,
        "lease": lease_state(ns_dir),
        "_target_ids": [c.id for c in targets],
    }


def _apply_limit(per_ns: dict[str, dict[str, Any]], order: list[str], limit: int | None) -> None:
    """依 namespace 順序分配全域名額；超出者從 _target_ids 與 targets 計數扣除。"""
    if limit is None:
        return
    remaining = max(0, limit)
    for ns_id in order:
        info = per_ns[ns_id]
        ids = info["_target_ids"]
        keep = ids[:remaining]
        remaining -= len(keep)
        dropped = len(ids) - len(keep)
        if dropped:
            info["limited_out"] = dropped
            info["_target_ids"] = keep
            info["targets"] = len(keep)


def _estimates(info: dict[str, Any], data_root: Path, ns_id: str) -> dict[str, int]:
    ns_dir = data_root / "namespaces" / ns_id
    keep = set(info["_target_ids"])
    review_calls = 0
    share_max = 0
    for c in _load_active_concepts(ns_dir):
        if c.id not in keep:
            continue
        review = c.evidence_review if isinstance(c.evidence_review, dict) else {}
        if review.get("reviewer") != "none":
            review_calls += 1
        share_max += len(_projection_scopes(c))
    return {"review": review_calls, "share_rejudge_max": share_max}


def plan(
    data_root: Path, namespaces: str, limit: int | None,
) -> tuple[list[str], dict[str, int], dict[str, dict[str, Any]]]:
    """回傳 (所有使用者 namespace 順序, ns_id -> 序號, 被選取 namespace 的分析)。"""
    all_ns = list_user_namespaces(data_root)
    index = {ns: i + 1 for i, ns in enumerate(all_ns)}
    if namespaces.strip().lower() == "all":
        selected = list(all_ns)
    else:
        wanted = [x.strip() for x in namespaces.split(",") if x.strip()]
        unknown = [x for x in wanted if x not in index]
        if unknown:
            raise BackfillError(f"--namespaces 含 {len(unknown)} 個不存在的 namespace")
        selected = [ns for ns in all_ns if ns in set(wanted)]
    scopes = list_scope_namespaces(data_root)
    per_ns = {ns: analyze_namespace(data_root, ns, scopes) for ns in selected}
    _apply_limit(per_ns, selected, limit)
    for ns in selected:
        per_ns[ns]["estimated_llm_calls"] = _estimates(per_ns[ns], data_root, ns)
    return selected, index, per_ns


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# apply
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _append_audit(ns_dir: Path, record: dict[str, Any]) -> None:
    """audit 先於資料修改：寫入失敗即拋例外，呼叫端不得繼續改該概念。"""
    path = ns_dir / "semantic" / AUDIT_NAME
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
        ok, reason = lease.try_acquire(owner="backfill_d88")
        if not ok:
            for got in held.values():
                got.release()
            raise BackfillError(f"dream 租約被占用，中止（未做任何修改）: {reason}")
        held[ns] = lease
    return held


def _check_embedding_dim(engine: Any, ns_dir: Path) -> None:
    """新寫入的向量維度必須與既有資料一致，否則中止（避免用錯 embedding 設定污染資料）。"""
    npy = ns_dir / "semantic" / "concept_embeddings.npy"
    if not npy.exists():
        return
    import numpy as np

    arr = np.load(npy, allow_pickle=False, mmap_mode="r")
    if arr.ndim != 2 or arr.shape[0] == 0:
        return
    probe = len(engine.episodic._generate_embedding("維度檢查"))
    if probe != int(arr.shape[1]):
        raise BackfillError(
            f"embedding 維度不符（目前設定 {probe}，既有資料 {arr.shape[1]}），請用與 bot 相同的 embedding 設定"
        )


def _process_namespace(
    data_root: Path, ns_id: str, target_ids: list[str], llm_fn: Callable[[str], str],
    engine_kwargs: dict[str, Any], lease: FileLease,
) -> dict[str, Any]:
    from echo_memory.engine import MemoryEngine

    ns_dir = data_root / "namespaces" / ns_id
    result: dict[str, Any] = {
        "outcomes": {"reviewed": 0, "held": 0, "stale": 0, "skipped": 0},
        "transitions": {}, "share": {"rejudged": 0, "final": 0, "not_ready": ""},
    }
    engine = MemoryEngine(storage_path=data_root, namespace_id=ns_id, llm_fn=llm_fn, **engine_kwargs)
    try:
        engine.ensure_initialized()
        _check_embedding_dim(engine, ns_dir)
        dream = engine._ensure_dream_engine()
        reviewer = dream._evidence_reviewer
        phase = dream._sharing_phase
        if reviewer is None:
            raise BackfillError("EvidenceReviewer 未建立（缺少 Gate 或 evidence store）")

        ledger = load_ledger(ns_dir)
        _append_audit(ns_dir, {"event": "run_start", "targets": len(target_ids)})

        # 階段 1：重審
        for cid in target_ids:
            if not lease.renew():
                raise BackfillError("dream 租約已遺失，中止")
            concept = engine.semantic.get(cid)
            if concept is None:
                continue
            before = concept.effective_review_status()
            if before != "insufficient":
                continue  # 執行期間已被別處處理
            projected = bool(_projection_scopes(concept))
            _append_audit(ns_dir, {"event": "intent", "concept_id": cid, "from": before})
            outcome = reviewer.review(cid)
            result["outcomes"][outcome.outcome] = result["outcomes"].get(outcome.outcome, 0) + 1
            _append_audit(ns_dir, {
                "event": "result", "concept_id": cid, "outcome": outcome.outcome,
                "reason": outcome.reason, "status": outcome.status, "patch_id": outcome.patch_id,
            })
            if outcome.outcome != "reviewed":
                continue
            key = f"insufficient->{outcome.status}"
            result["transitions"][key] = result["transitions"].get(key, 0) + 1
            ledger[cid] = {
                "at": datetime.now(timezone.utc).isoformat(), "from": "insufficient",
                "to": outcome.status, "patch_id": outcome.patch_id,
                "projected": projected,
                "share_done": (not projected) or outcome.status == "insufficient",
            }
            _atomic_write_json(ns_dir / "semantic" / LEDGER_NAME, ledger)

        # 階段 2：狀態已改變且有投影者，依現有分享機制重判（含前次未完成者）
        pending = [
            cid for cid, e in ledger.items()
            if isinstance(e, dict) and e.get("projected") and not e.get("share_done")
        ]
        if pending:
            if phase is None:
                result["share"]["not_ready"] = "no_sharing_phase"
            else:
                missing = phase.ready()
                if missing:
                    result["share"]["not_ready"] = missing
                else:
                    from echo_memory.dream.sharing_phase import SharingReport
                    from echo_memory.schemas.evidence_review import compute_basis_digest

                    rep = SharingReport()
                    for cid in pending:
                        if not lease.renew():
                            raise BackfillError("dream 租約已遺失，中止")
                        _append_audit(ns_dir, {"event": "share_rejudge", "concept_id": cid})
                        phase._forget_judged(cid)
                        try:
                            phase._process_concept(cid, rep)
                        except Exception as exc:  # noqa: BLE001 - 單筆失敗留待下次重判
                            logger.warning("分享重判失敗: %s", exc)
                            continue
                        result["share"]["rejudged"] += 1
                        concept = engine.semantic.get(cid)
                        rec = phase._load_judged().get(cid)
                        if concept is not None and rec and rec.get("digest") == compute_basis_digest(concept):
                            ledger[cid]["share_done"] = True
                            result["share"]["final"] += 1
                            _atomic_write_json(ns_dir / "semantic" / LEDGER_NAME, ledger)
                    result["share"]["projected"] = rep.projected
                    result["share"]["revoked"] = rep.revoked
    finally:
        engine.close()
    return result


def _scope_projection_status(data_root: Path, ns_id: str, source_ids: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for sid in list_scope_namespaces(data_root):
        for p in _load_active_concepts(data_root / "namespaces" / sid):
            meta = p.metadata or {}
            if (
                meta.get("kind") == "projection" and meta.get("owner_uid") == ns_id
                and meta.get("source_concept_id") in source_ids
            ):
                key = str(meta.get("review_status") or "unknown")
                out[key] = out.get(key, 0) + 1
    return out


def run(
    data_root: Path,
    *,
    namespaces: str = "all",
    apply: bool = False,
    backup: Path | None = None,
    limit: int | None = None,
    llm_fn: Callable[[str], str] | None = None,
    engine_kwargs: dict[str, Any] | None = None,
    lease_ttl: float = 3600.0,
) -> dict[str, Any]:
    """執行（或 dry-run）並回傳遮罩後的報告 dict。"""
    data_root = Path(data_root)
    if not (data_root / "namespaces").is_dir():
        raise BackfillError("資料目錄下找不到 namespaces/")
    if apply:
        if backup is None or not Path(backup).exists():
            raise BackfillError("--apply 必須搭配 --i-have-backup <存在的備份路徑>")
        if llm_fn is None:
            raise BackfillError("apply 需要可用的 LLM（llm_fn）")

    selected, index, per_ns = plan(data_root, namespaces, limit)
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": limit,
        "namespaces": [],
    }
    leases: dict[str, FileLease] = {}
    applied: dict[str, dict[str, Any]] = {}
    if apply:
        work = [ns for ns in selected if per_ns[ns]["_target_ids"] or per_ns[ns]["pending_share_rejudge"]]
        leases = _acquire_leases(data_root, work, lease_ttl)
        try:
            for ns in work:
                # 取得租約後再讀一次，避免計畫與實際落差
                scopes = list_scope_namespaces(data_root)
                fresh = analyze_namespace(data_root, ns, scopes)
                ids = [i for i in fresh["_target_ids"] if i in set(per_ns[ns]["_target_ids"])]
                applied[ns] = _process_namespace(
                    data_root, ns, ids, llm_fn, engine_kwargs or {}, leases[ns],
                )
        finally:
            for lease in leases.values():
                lease.release()

    for ns in selected:
        info = {k: v for k, v in per_ns[ns].items() if not k.startswith("_")}
        entry: dict[str, Any] = {"namespace": f"ns#{index[ns]}", **info}
        if apply and ns in applied:
            after = analyze_namespace(data_root, ns, list_scope_namespaces(data_root))
            ledger_ids = set(load_ledger(data_root / "namespaces" / ns))
            entry["apply"] = applied[ns]
            entry["status_after"] = after["status"]
            entry["projection_status_after"] = _scope_projection_status(data_root, ns, ledger_ids)
        report["namespaces"].append(entry)

    report["totals"] = _totals(report["namespaces"], apply)
    return report


def _totals(entries: list[dict[str, Any]], apply: bool) -> dict[str, Any]:
    tot: dict[str, Any] = {
        "namespaces": len(entries),
        "active_concepts": sum(e["active_concepts"] for e in entries),
        "targets": sum(e["targets"] for e in entries),
        "projections_affected": sum(e["projections_affected"] for e in entries),
        "projections_live_in_scope": sum(e["projections_live_in_scope"] for e in entries),
        "estimated_llm_calls": {
            "review": sum(e["estimated_llm_calls"]["review"] for e in entries),
            "share_rejudge_max": sum(e["estimated_llm_calls"]["share_rejudge_max"] for e in entries),
        },
        "status_before": {s: sum(e["status"].get(s, 0) for e in entries) for s in STATUSES},
    }
    if apply:
        tot["status_after"] = {
            s: sum(e.get("status_after", e["status"]).get(s, 0) for e in entries) for s in STATUSES
        }
        tot["hold"] = sum(e.get("apply", {}).get("outcomes", {}).get("held", 0) for e in entries)
    return tot


def format_report(report: dict[str, Any]) -> str:
    lines = [f"[D88 回填] 模式: {report['mode']}（limit={report['limit']}）"]
    lines.append("ns | active | insufficient(raw) | 目標 | 結構性(免LLM) | 投影(擁有端/共享端) | 預估LLM(審核/分享上限) | 租約")
    for e in report["namespaces"]:
        est = e["estimated_llm_calls"]
        lines.append(
            f"{e['namespace']} | {e['active_concepts']} | {e['raw_insufficient']} | {e['targets']} | "
            f"{e['structural_no_llm']} | {e['projections_affected']}/{e['projections_live_in_scope']} | "
            f"{est['review']}/{est['share_rejudge_max']} | {'持有中' if e['lease']['held'] else '空閒'}"
        )
    t = report["totals"]
    lines.append(
        f"合計: 目標 {t['targets']}, 投影 {t['projections_affected']}, "
        f"預估 LLM 審核 {t['estimated_llm_calls']['review']} + 分享重判上限 {t['estimated_llm_calls']['share_rejudge_max']}"
    )
    lines.append("審核狀態（前）: " + ", ".join(f"{k}={v}" for k, v in t["status_before"].items()))
    if report["mode"] == "apply":
        lines.append("審核狀態（後）: " + ", ".join(f"{k}={v}" for k, v in t["status_after"].items()))
        lines.append(f"hold（審核暫緩）: {t['hold']}")
    return "\n".join(lines)


def _build_llm_fn() -> Callable[[str], str]:
    """沿用專案既有的 LLM 設定：LLMRouter 的 EVAL 角色（同 Entry.py 對 engine 注入的 llm_fn）。"""
    from llm_integration.router import LLMRole, LLMRouter

    return LLMRouter().get_llm_fn(LLMRole.EVAL)


def _engine_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    from echo_memory.config import get_config

    cfg = get_config()
    return {
        "embedding_model": args.embedding_model or cfg.embedding_model,
        "embedding_backend": args.embedding_backend or cfg.embedding_backend,
        "ollama_base_url": args.ollama_url or cfg.ollama_base_url,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D88 舊資料一次性重審工具（預設 dry-run）")
    ap.add_argument("--data-root", required=True, type=Path, help="記憶資料目錄（含 namespaces/）")
    ap.add_argument("--namespaces", default="all", help="all 或逗號分隔的 namespace id")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只讀預覽（預設）")
    mode.add_argument("--apply", action="store_true", help="實際寫入（需 --i-have-backup）")
    ap.add_argument("--i-have-backup", type=Path, default=None, help="備份路徑（apply 必填且須存在）")
    ap.add_argument("--limit", type=int, default=None, help="本次最多重審幾個概念（跨 namespace 總數）")
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
        llm_fn = None
        kwargs: dict[str, Any] = {}
        if args.apply:
            if args.i_have_backup is None or not args.i_have_backup.exists():
                raise BackfillError("--apply 必須搭配 --i-have-backup <存在的備份路徑>")
            llm_fn = _build_llm_fn()
            kwargs = _engine_kwargs(args)
        report = run(
            args.data_root, namespaces=args.namespaces, apply=args.apply,
            backup=args.i_have_backup, limit=args.limit, llm_fn=llm_fn, engine_kwargs=kwargs,
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
