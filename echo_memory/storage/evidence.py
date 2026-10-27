"""
EvidenceStore - append-only 證據存儲

每個 namespace 一份 evidence.jsonl（一行一個 EvidenceUnit）。
只提供追加與讀取；沒有更新或刪除 API。同 id 重複追加視為錯誤。

另提供：
- build_legacy_units: 舊介面呼叫／舊資料的 legacy_unknown 證據（只降不升）
- migrate_evidence: 把既有 episodes.json 補上 evidence_ids（先備份、idempotent）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.schemas.evidence import (
    EvidenceUnit,
    SourceRef,
    compute_content_hash,
)
from echo_memory.storage.atomic import replace_with_retry
from echo_memory.storage.locking import storage_lock, temporary_sibling

logger = logging.getLogger("echo_memory.storage.evidence")

EVIDENCE_FILENAME = "evidence.jsonl"


class EvidenceStore:
    """Append-only 證據存儲。"""

    def __init__(self, storage_path: str | Path, lock_path: str | Path | None = None):
        self.storage_path = Path(storage_path)
        self.lock_path = Path(lock_path) if lock_path is not None else self.storage_path
        self.file = self.storage_path / EVIDENCE_FILENAME

    # ── 寫入（僅追加）──────────────────────────────────────────────

    def append(self, units: Sequence[EvidenceUnit]) -> list[str]:
        """追加 units，回傳 id 清單。id 已存在（含同批重複）即拋 ValueError，不寫入任何一筆。"""
        if not units:
            return []
        with storage_lock(self.lock_path):
            existing = {u.id for u in self._read_all()}
            seen: set[str] = set()
            for u in units:
                if u.id in existing or u.id in seen:
                    raise ValueError(f"EvidenceUnit 不可變，id 已存在: {u.id}")
                seen.add(u.id)
            self.storage_path.mkdir(parents=True, exist_ok=True)
            payload = "".join(
                json.dumps(u.to_dict(), ensure_ascii=False) + "\n" for u in units
            )
            with open(self.file, "a", encoding="utf-8", newline="\n") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        return [u.id for u in units]

    # ── 讀取 ───────────────────────────────────────────────────────

    def _read_all(self) -> list[EvidenceUnit]:
        if not self.file.exists():
            return []
        units: list[EvidenceUnit] = []
        with open(self.file, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    units.append(EvidenceUnit.from_dict(json.loads(line)))
                except (ValueError, KeyError, TypeError) as exc:
                    # 尾端被截斷的半行等：略過並記錄，不讓整個 store 無法讀取
                    logger.warning("[EvidenceStore] 略過無法解析的第 %d 行: %s", lineno, exc)
        return units

    def get(self, ids: Iterable[str]) -> list[EvidenceUnit]:
        """依 id 取回（保持傳入順序，找不到的略過）。"""
        by_id = {u.id: u for u in self._read_all()}
        return [by_id[i] for i in ids if i in by_id]

    def get_by_episode(self, episode_id: str) -> list[EvidenceUnit]:
        return [u for u in self._read_all() if u.episode_id == episode_id]

    def list(
        self,
        *,
        source_type: str | None = None,
        speaker_id: str | None = None,
        channel: str | None = None,
        delivery_state: str | None = None,
        provenance_status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[EvidenceUnit]:
        out: list[EvidenceUnit] = []
        for u in self._read_all():
            if source_type is not None and u.source_type != source_type:
                continue
            if speaker_id is not None and u.speaker_id != speaker_id:
                continue
            if channel is not None and (u.source_ref is None or u.source_ref.channel != channel):
                continue
            if delivery_state is not None and u.delivery_state != delivery_state:
                continue
            if provenance_status is not None and u.provenance_status != provenance_status:
                continue
            if since is not None and u.occurred_at < since:
                continue
            if until is not None and u.occurred_at > until:
                continue
            out.append(u)
            if limit is not None and len(out) >= limit:
                break
        return out

    def count(self) -> int:
        return len(self._read_all())

    def units_for_episode(self, episode: Any) -> list[EvidenceUnit]:
        """取得 Episode 的證據；舊 Episode（尚未遷移）回傳即時合成的 legacy_unknown unit（不寫入）。"""
        if getattr(episode, "evidence_ids", None):
            return self.get(episode.evidence_ids)
        return build_legacy_units(
            episode_id=episode.id,
            user_input=episode.user_input,
            agent_response=episode.agent_response,
            occurred_at=episode.timestamp,
            metadata=episode.metadata,
            via="read_time",
        )


    def units_for_episodes(self, episodes: Iterable[Any]) -> dict[str, list[EvidenceUnit]]:
        """批次版 units_for_episode：只讀一次 evidence.jsonl。"""
        episodes = list(episodes)
        needs_store = any(getattr(ep, "evidence_ids", None) for ep in episodes)
        by_id = {u.id: u for u in self._read_all()} if needs_store else {}
        out: dict[str, list[EvidenceUnit]] = {}
        for ep in episodes:
            if getattr(ep, "evidence_ids", None):
                out[ep.id] = [by_id[i] for i in ep.evidence_ids if i in by_id]
            else:
                out[ep.id] = build_legacy_units(
                    episode_id=ep.id,
                    user_input=ep.user_input,
                    agent_response=ep.agent_response,
                    occurred_at=ep.timestamp,
                    metadata=ep.metadata,
                    via="read_time",
                )
        return out


# ─── 舊資料／舊介面：legacy_unknown ─────────────────────────────────────


def legacy_unit_id(episode_id: str, role: str) -> str:
    """確定性 id：同一 Episode 重跑遷移不會重複。"""
    return "ev_" + hashlib.sha1(f"{episode_id}:{role}".encode("utf-8")).hexdigest()[:12]


def build_legacy_units(
    *,
    episode_id: str,
    user_input: str,
    agent_response: str,
    occurred_at: datetime,
    metadata: dict[str, Any] | None = None,
    via: str = "legacy_signature",
    recorded_at: datetime | None = None,
) -> list[EvidenceUnit]:
    """
    由舊式 (user_input, agent_response) 產生 legacy_unknown 證據。

    只降不升：provenance_status 一律 legacy_unknown；agent 的 delivery_state 為 unknown。
    不從文字內容推測來源（含 "[force_remember]" 之類字首）。
    metadata 若已有結構化的 origin／speaker_id／truncated，僅往保守方向吸收。
    """
    metadata = metadata or {}
    recorded_at = recorded_at or datetime.now(timezone.utc)
    channel = metadata.get("origin")
    channel = str(channel) if channel else None
    speaker = metadata.get("speaker_id")
    speaker = str(speaker) if speaker else None
    ref = SourceRef(kind="legacy", channel=channel)

    units: list[EvidenceUnit] = []
    if user_input and user_input.strip():
        units.append(EvidenceUnit(
            id=legacy_unit_id(episode_id, "user"),
            episode_id=episode_id,
            source_type="user",
            content=user_input,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            speaker_id=speaker,
            source_ref=ref,
            delivery_state="delivered",
            provenance_status="legacy_unknown",
            content_hash=compute_content_hash(user_input),
            attrs={"legacy_field": "user_input", "via": via},
        ))
    if agent_response and agent_response.strip():
        truncated = metadata.get("truncated") is True
        units.append(EvidenceUnit(
            id=legacy_unit_id(episode_id, "agent"),
            episode_id=episode_id,
            source_type="agent",
            content=agent_response,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            speaker_id="agent",
            source_ref=ref,
            # 舊資料無從得知是否說出口；被打斷則至多為 partial（進度未知）
            delivery_state="partial" if truncated else "unknown",
            provenance_status="legacy_unknown",
            content_hash=compute_content_hash(agent_response),
            attrs={"legacy_field": "agent_response", "via": via},
        ))
    return units


# ─── 遷移 ───────────────────────────────────────────────────────────────


@dataclass
class MigrationReport:
    episodes_total: int = 0
    episodes_migrated: int = 0
    units_added: int = 0
    backup_path: Path | None = None


def migrate_evidence(namespace_dir: str | Path, *, dry_run: bool = False) -> MigrationReport:
    """
    為 namespace 目錄下 episodic/episodes.json 內尚無 evidence_ids 的 Episode 補上
    legacy_unknown 證據。

    - 變更前自動備份 episodes.json（episodes.json.pre-evidence-<UTC時間>.bak）
    - idempotent：已有 evidence_ids 的 Episode 不動，重跑零新增
    - 只降不升：所有產生的 unit 皆 legacy_unknown
    - dry_run=True：只計算報告（episodes_migrated／units_added），不備份、不寫任何檔案
    """
    namespace_dir = Path(namespace_dir)
    episodes_file = namespace_dir / "episodic" / "episodes.json"
    report = MigrationReport()
    if not episodes_file.exists():
        return report

    store = EvidenceStore(namespace_dir / "evidence", lock_path=namespace_dir)
    with storage_lock(namespace_dir):
        with open(episodes_file, encoding="utf-8") as f:
            episodes: list[dict[str, Any]] = json.load(f)
        report.episodes_total = len(episodes)

        existing_ids = {u.id for u in store._read_all()}
        new_units: list[EvidenceUnit] = []
        changed = False
        for ep in episodes:
            if ep.get("evidence_ids"):
                continue
            ts = ep.get("timestamp")
            occurred = datetime.fromisoformat(ts) if isinstance(ts, str) else datetime.now(timezone.utc)
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=timezone.utc)
            units = build_legacy_units(
                episode_id=ep["id"],
                user_input=ep.get("user_input", ""),
                agent_response=ep.get("agent_response", ""),
                occurred_at=occurred,
                metadata=ep.get("metadata") or {},
                via="migration",
            )
            new_units.extend(u for u in units if u.id not in existing_ids)
            ep["evidence_ids"] = [u.id for u in units]
            ep.setdefault("episode_kind", "turn")
            report.episodes_migrated += 1
            changed = True

        if not changed:
            return report
        if dry_run:
            report.units_added = len(new_units)
            return report

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = episodes_file.with_name(f"{episodes_file.name}.pre-evidence-{stamp}.bak")
        shutil.copy2(episodes_file, backup)
        report.backup_path = backup

        # 先追加證據、再發布 Episode 快照：中途失敗只留孤兒 evidence（無害）
        store.append(new_units)
        report.units_added = len(new_units)

        tmp = temporary_sibling(episodes_file)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(episodes, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp, episodes_file)
    logger.info(
        "[EvidenceStore] 遷移完成: %d/%d 個 Episode，新增 %d 個 legacy_unknown unit，備份 %s",
        report.episodes_migrated, report.episodes_total, report.units_added, backup,
    )
    return report
