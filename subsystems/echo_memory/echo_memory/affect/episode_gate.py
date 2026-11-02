"""
EpisodeGate — episode／soul note 類寫入的 audit-before-mutate 入口

Reflection 的概念行動（create/tag/merge）走 ConceptInhibitor；其餘三種非概念行動
（adjust_salience / archive_episode / add_note）在此統一收口：

  1. 先寫一筆 status=pending 的 audit 記錄（含前值、來源、理由）
  2. audit 寫入失敗 -> 拋出 EpisodeGateAuditError，**不** mutate
  3. mutate 成功／失敗後再追加 committed／failed 記錄（同 record_id，取最後一筆為準）
  4. 回滾（restore_*／retract_note）同樣先 audit 再還原

設計約束：
  - Salience 是權重不是門檻：本模組只記錄與還原數值，不依 salience 決定是否寫入
  - 不使用任何關鍵字比對；note 撤回以 note_id 精確對應
  - SoulNoteManager 不支援刪除，撤回 = 追加一筆 category=reflection_retract 的撤回標記

audit 記錄（JSONL，一行一筆）：
  record_id, phase(mutate|rollback), operation, producer, evidence_kind, status,
  action_id, reasoning, source{reflection_cycle_id, episode_ids}, target_id,
  before{...}, after{...}, error, timestamp
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("echo_memory.affect.episode_gate")

# 本模組自有的 evidence_kind（不動 concept_patch／concept_inhibitor）
EVIDENCE_KIND_REFLECTION_ACTION = "reflection_action"
PRODUCER_REFLECTION = "reflection"

# 歸檔後的 salience（與 EpisodicMemory.archive_episode 預設一致，僅用於 audit 的 after 欄位）
_ARCHIVE_SALIENCE = 0.05


class EpisodeGateAuditError(RuntimeError):
    """audit 寫入失敗；呼叫端必須中止 mutate。"""


class EpisodeGate:
    """episode／soul note 寫入的 Gate：audit-before-mutate + 可回滾。"""

    def __init__(self, audit_path: str | Path | None = None):
        """
        Args:
            audit_path: audit JSONL 路徑。None 時只保留記憶體內記錄（無持久化）。
        """
        self._audit_path = Path(audit_path) if audit_path else None
        self._lock = threading.Lock()
        self.records: list[dict[str, Any]] = []

    # ──────────────────────────────────────────────
    # audit
    # ──────────────────────────────────────────────

    def _append(self, record: dict[str, Any]) -> None:
        """追加一筆 audit 記錄；有設定路徑時寫檔失敗會拋 EpisodeGateAuditError。"""
        record = {**record, "timestamp": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            if self._audit_path is not None:
                try:
                    self._audit_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self._audit_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception as e:
                    raise EpisodeGateAuditError(f"episode audit 寫入失敗: {e}") from e
            self.records.append(record)

    @staticmethod
    def _source(context: dict[str, Any] | None) -> dict[str, Any]:
        ctx = context or {}
        prov = ctx.get("provenance") or {}
        return {
            "reflection_cycle_id": prov.get("reflection_cycle_id"),
            "episode_ids": list(prov.get("source_episode_ids") or []),
        }

    def _begin(
        self,
        phase: str,
        operation: str,
        target_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """寫 pending 記錄；失敗即拋出（呼叫端據此不 mutate）。"""
        ctx = context or {}
        record = {
            "record_id": f"epg_{uuid.uuid4().hex[:16]}",
            "phase": phase,
            "operation": operation,
            "producer": PRODUCER_REFLECTION,
            "evidence_kind": EVIDENCE_KIND_REFLECTION_ACTION,
            "status": "pending",
            "action_id": ctx.get("action_id"),
            "reasoning": str(ctx.get("reasoning", "")),
            "source": self._source(ctx),
            "target_id": target_id,
            "before": before,
            "after": after,
            "error": "",
        }
        self._append(record)
        return record

    def _finish(self, record: dict[str, Any], ok: bool, error: str = "") -> None:
        """追加 committed／failed 終態記錄；寫失敗只記 log（mutate 已發生，不能再中止）。"""
        final = {**record, "status": "committed" if ok else "failed", "error": error}
        try:
            self._append(final)
        except EpisodeGateAuditError as e:
            logger.warning("[EpisodeGate] 終態 audit 寫入失敗（mutate 已完成）: %s", e)

    def _run(
        self,
        record: dict[str, Any],
        mutate: Any,
    ) -> Any:
        """執行 mutate 並寫終態記錄。mutate 回傳 False／拋例外視為失敗。"""
        try:
            ok = mutate()
        except Exception as e:
            self._finish(record, False, str(e))
            raise
        if ok is False:
            self._finish(record, False, "mutate 回傳失敗")
            return False
        self._finish(record, True)
        return True

    # ──────────────────────────────────────────────
    # 前向行動
    # ──────────────────────────────────────────────

    def adjust_salience(
        self, episodic: Any, target_id: str, new_salience: float,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """調整 episode salience（audit 先行）。回傳 {old_salience, new_salience}。"""
        ep = episodic.get(target_id)
        if ep is None:
            raise ValueError(f"Episode 不存在: {target_id}")
        old = ep.salience_score
        record = self._begin(
            "mutate", "adjust_salience", target_id,
            {"salience_score": old}, {"salience_score": new_salience}, context,
        )
        ok = self._run(record, lambda: episodic.update_salience(target_id, new_salience))
        if not ok:
            raise RuntimeError(f"update_salience 失敗: {target_id}")
        return {"old_salience": old, "new_salience": new_salience, "audit_record_id": record["record_id"]}

    def archive_episode(
        self, episodic: Any, target_id: str, context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """歸檔 episode（audit 先行，前值含 salience／archived／archived_by）。"""
        ep = episodic.get(target_id)
        if ep is None:
            raise ValueError(f"Episode 不存在: {target_id}")
        meta = getattr(ep, "metadata", None) or {}
        before = {
            "salience_score": ep.salience_score,
            "archived": bool(meta.get("archived", False)),
            "archived_by": meta.get("archived_by"),
        }
        record = self._begin(
            "mutate", "archive_episode", target_id, before,
            {"salience_score": _ARCHIVE_SALIENCE, "archived": True, "archived_by": "reflection"},
            context,
        )
        ok = self._run(record, lambda: episodic.archive_episode(target_id))
        if not ok:
            raise RuntimeError(f"archive_episode 失敗: {target_id}")
        return {
            "old_salience": ep.salience_score,
            "new_salience": _ARCHIVE_SALIENCE,
            "archived": True,
            "audit_record_id": record["record_id"],
        }

    def add_note(
        self, soul_notes: Any, content: str, note_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """寫入 soul note（audit 先行）。note 帶 metadata.note_id 供撤回精確對應。"""
        from echo_memory.affect.soul_note import SoulNote

        note_id = note_id or f"note_{uuid.uuid4().hex[:12]}"
        record = self._begin(
            "mutate", "add_note", note_id,
            {}, {"note_id": note_id, "content_length": len(content)}, context,
        )

        def _write() -> bool:
            soul_notes.add_note(SoulNote(
                category="reflection_action",
                content=content,
                metadata={
                    "source": "reflection_executor",
                    "note_id": note_id,
                    "audit_record_id": record["record_id"],
                },
                tags=["reflection", "auto"],
            ), raise_on_error=True)
            return True

        self._run(record, _write)
        return {"written": True, "content_length": len(content),
                "note_id": note_id, "audit_record_id": record["record_id"]}

    # ──────────────────────────────────────────────
    # 回滾
    # ──────────────────────────────────────────────

    def restore_salience(
        self, episodic: Any, target_id: str, old_salience: float,
        context: dict[str, Any] | None = None,
    ) -> None:
        """還原 salience（audit 先行）。失敗拋出，讓 ActionQueue 不誤報 ROLLED_BACK。"""
        ep = episodic.get(target_id)
        current = ep.salience_score if ep is not None else None
        record = self._begin(
            "rollback", "adjust_salience", target_id,
            {"salience_score": current}, {"salience_score": old_salience}, context,
        )
        ok = self._run(record, lambda: episodic.update_salience(target_id, old_salience))
        if not ok:
            raise RuntimeError(f"回滾 adjust_salience 失敗: {target_id}")

    def restore_archive(
        self, episodic: Any, target_id: str, pre_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> None:
        """還原歸檔：salience 與 archived／archived_by 回到前值。"""
        old_salience = pre_state.get("salience_score")
        restored = {
            "archived": bool(pre_state.get("archived", False)),
            "archived_by": pre_state.get("archived_by"),
        }
        ep = episodic.get(target_id)
        meta = (getattr(ep, "metadata", None) or {}) if ep is not None else {}
        current = {
            "salience_score": ep.salience_score if ep is not None else None,
            "archived": bool(meta.get("archived", False)),
            "archived_by": meta.get("archived_by"),
        }
        record = self._begin(
            "rollback", "archive_episode", target_id,
            current, {"salience_score": old_salience, **restored}, context,
        )

        def _restore() -> bool:
            if old_salience is not None and not episodic.update_salience(target_id, old_salience):
                return False
            return bool(episodic.update_episode_metadata(target_id, restored))

        if not self._run(record, _restore):
            raise RuntimeError(f"回滾 archive_episode 失敗: {target_id}")

    def retract_note(
        self, soul_notes: Any, note_id: str, context: dict[str, Any] | None = None,
    ) -> None:
        """撤回 note：SoulNoteManager 無刪除能力，追加撤回標記並留 audit。"""
        from echo_memory.affect.soul_note import SoulNote

        record = self._begin(
            "rollback", "add_note", note_id,
            {"note_id": note_id}, {"retracted": True}, context,
        )

        def _write() -> bool:
            soul_notes.add_note(SoulNote(
                category="reflection_retract",
                content=f"撤回反思筆記 {note_id}",
                metadata={
                    "source": "reflection_executor",
                    "retracts": note_id,
                    "audit_record_id": record["record_id"],
                },
                tags=["reflection", "retract"],
            ), raise_on_error=True)
            return True

        self._run(record, _write)
