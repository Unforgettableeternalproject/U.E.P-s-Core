"""
測試 EpisodeGate 與 ReflectionExecutor 的非概念行動收口

涵蓋：
  - adjust_salience / archive_episode / add_note 經 Gate 留 audit（producer、行動 id、來源、前值）
  - audit 寫入失敗時不 mutate
  - rollback 正確（salience 還原、archive 還原含原 archived 狀態、note 撤回標記）
  - ActionQueue 重建（重啟）後 pre_state 不遺失、仍可回滾
"""

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from echo_memory.affect.action_queue import ActionStatus, ReflectionActionQueue
from echo_memory.affect.episode_gate import (
    EVIDENCE_KIND_REFLECTION_ACTION,
    EpisodeGate,
    EpisodeGateAuditError,
)
from echo_memory.affect.reflection import ReflectionOutput
from echo_memory.affect.reflection_executor import (
    ReflectionExecutor,
    parse_reflection_to_actions,
)


@dataclass
class FakeEpisode:
    id: str = "ep1"
    salience_score: float = 0.5
    metadata: dict = field(default_factory=dict)


class FakeEpisodic:
    """記憶體內 episodic 替身，語意對齊 EpisodicMemory。"""

    def __init__(self, *episodes):
        self.eps = {e.id: e for e in episodes}

    def get(self, eid):
        return self.eps.get(eid)

    def update_salience(self, eid, v):
        ep = self.eps.get(eid)
        if ep is None:
            return False
        ep.salience_score = min(1.0, max(0.0, v))
        return True

    def update_episode_metadata(self, eid, updates):
        ep = self.eps.get(eid)
        if ep is None:
            return False
        ep.metadata.update(updates)
        return True

    def archive_episode(self, eid, archive_salience=0.05):
        ep = self.eps.get(eid)
        if ep is None:
            return False
        ep.salience_score = archive_salience
        ep.metadata["archived"] = True
        ep.metadata["archived_by"] = "reflection"
        return True


class FakeNotes:
    def __init__(self):
        self.notes = []

    def add_note(self, note, raise_on_error=False):
        self.notes.append(note)


def _read(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture
def env(tmp_path):
    episodic = FakeEpisodic(
        FakeEpisode("ep1", 0.5),
        FakeEpisode("ep2", 0.7, {"archived": True, "archived_by": "dream"}),
    )
    notes = FakeNotes()
    engine = SimpleNamespace(episodic=episodic, storage_path=tmp_path)
    executor = ReflectionExecutor(engine=engine, soul_notes=notes)
    return SimpleNamespace(
        episodic=episodic, notes=notes, executor=executor,
        audit=tmp_path / "episode_gate_audit.jsonl",
        queue_audit=tmp_path / "actions.jsonl",
    )


def _enqueue(env, queue, op, target, params, cycle="cyc1", sources=("epA", "epB")):
    """走 parse -> capture -> enqueue -> bind -> persist 的實際流程。"""
    ref = ReflectionOutput(memory_ops=[{"op": op, "target": target, "params": params, "reason": "理由X"}])
    d = parse_reflection_to_actions(ref, cycle_id=cycle, source_episode_ids=list(sources))[0]
    pre = env.executor.capture_pre_state(d["action_type"], d["target_id"], d["params"])
    act = queue.enqueue(d["action_type"], d["target_id"], d["params"], d["reasoning"])
    act.pre_state = pre
    env.executor.bind_action(act)
    queue.persist_action(act)
    return act


def _run(env, queue, act):
    if act.permission.value == "confirm":
        assert queue.confirm(act.id)
    assert queue.execute(act, env.executor.build_executor_fn())


class TestGateRecords:
    def test_adjust_salience_audit(self, env):
        q = ReflectionActionQueue(audit_path=env.queue_audit)
        act = _enqueue(env, q, "adjust_salience", "ep1", {"new_salience": 0.9})
        _run(env, q, act)
        assert env.episodic.get("ep1").salience_score == 0.9
        recs = _read(env.audit)
        assert [r["status"] for r in recs] == ["pending", "committed"]
        r = recs[-1]
        assert r["producer"] == "reflection"
        assert r["evidence_kind"] == EVIDENCE_KIND_REFLECTION_ACTION
        assert r["operation"] == "adjust_salience" and r["phase"] == "mutate"
        assert r["action_id"] == act.id
        assert r["reasoning"] == "理由X"
        assert r["source"] == {"reflection_cycle_id": "cyc1", "episode_ids": ["epA", "epB"]}
        assert r["before"] == {"salience_score": 0.5}
        assert r["after"] == {"salience_score": 0.9}

    def test_archive_audit(self, env):
        q = ReflectionActionQueue(audit_path=env.queue_audit)
        act = _enqueue(env, q, "archive_episode", "ep1", {})
        _run(env, q, act)
        assert env.episodic.get("ep1").metadata["archived"] is True
        r = _read(env.audit)[-1]
        assert r["operation"] == "archive_episode" and r["status"] == "committed"
        assert r["before"]["salience_score"] == 0.5 and r["before"]["archived"] is False
        assert r["after"]["archived"] is True
        assert r["action_id"] == act.id

    def test_add_note_audit(self, env):
        q = ReflectionActionQueue(audit_path=env.queue_audit)
        act = _enqueue(env, q, "add_note", "", {"content": "筆記"})
        _run(env, q, act)
        assert len(env.notes.notes) == 1
        note = env.notes.notes[0]
        assert note.metadata["note_id"] == act.pre_state["note_id"]
        r = _read(env.audit)[-1]
        assert r["operation"] == "add_note" and r["status"] == "committed"
        assert r["target_id"] == act.pre_state["note_id"]
        assert r["action_id"] == act.id
        assert r["source"]["reflection_cycle_id"] == "cyc1"

    def test_low_salience_not_filtered(self, env):
        """Salience 是權重不是門檻：極低值也照常寫入。"""
        fn = env.executor.build_executor_fn()
        fn("adjust_salience", "ep1", {"new_salience": 0.0})
        assert env.episodic.get("ep1").salience_score == 0.0


class TestAuditFailure:
    @pytest.fixture
    def broken(self, env, tmp_path):
        # audit 路徑的父層是檔案 -> mkdir／open 必失敗
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        gate = EpisodeGate(blocker / "audit.jsonl")
        env.executor = ReflectionExecutor(
            engine=env.executor._engine, soul_notes=env.notes, episode_gate=gate,
        )
        return env

    def test_adjust_not_mutated(self, broken):
        fn = broken.executor.build_executor_fn()
        with pytest.raises(EpisodeGateAuditError):
            fn("adjust_salience", "ep1", {"new_salience": 0.9})
        assert broken.episodic.get("ep1").salience_score == 0.5

    def test_archive_not_mutated(self, broken):
        fn = broken.executor.build_executor_fn()
        with pytest.raises(EpisodeGateAuditError):
            fn("archive_episode", "ep1", {})
        assert broken.episodic.get("ep1").metadata == {}
        assert broken.episodic.get("ep1").salience_score == 0.5

    def test_note_not_written(self, broken):
        fn = broken.executor.build_executor_fn()
        with pytest.raises(EpisodeGateAuditError):
            fn("add_note", "", {"content": "x"})
        assert broken.notes.notes == []

    def test_queue_marks_failed(self, broken):
        q = ReflectionActionQueue()
        act = q.enqueue("adjust_salience", "ep1", {"new_salience": 0.9})
        assert not q.execute(act, broken.executor.build_executor_fn())
        assert act.status == ActionStatus.FAILED
        assert broken.episodic.get("ep1").salience_score == 0.5

    def test_rollback_audit_failure_not_restored(self, env, tmp_path):
        q = ReflectionActionQueue()
        act = _enqueue(env, q, "adjust_salience", "ep1", {"new_salience": 0.9})
        _run(env, q, act)
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        env.executor = ReflectionExecutor(
            engine=env.executor._engine, soul_notes=env.notes,
            episode_gate=EpisodeGate(blocker / "a.jsonl"),
        )
        assert not q.rollback(act.id, env.executor.build_rollback_fn())
        assert env.episodic.get("ep1").salience_score == 0.9
        assert act.status == ActionStatus.EXECUTED


class FailingNotes:
    """add_note 在 raise_on_error=True 時拋錯（模擬 SoulNoteManager 寫檔失敗）。"""

    def add_note(self, note, raise_on_error=False):
        if raise_on_error:
            raise OSError("disk full")


class TestNoteWriteFailure:
    def test_gate_marks_failed(self, env):
        env.executor = ReflectionExecutor(
            engine=env.executor._engine, soul_notes=FailingNotes(),
        )
        with pytest.raises(OSError):
            env.executor.build_executor_fn()("add_note", "", {"content": "x"})
        recs = _read(env.audit)
        assert [r["status"] for r in recs] == ["pending", "failed"]
        assert "disk full" in recs[-1]["error"]

    def test_soul_note_manager_raise_on_error(self, tmp_path, monkeypatch):
        from echo_memory.affect.soul_note import SoulNote, SoulNoteManager

        mgr = SoulNoteManager(tmp_path)

        def boom(*a, **k):
            raise OSError("nope")

        monkeypatch.setattr("builtins.open", boom)
        mgr.add_note(SoulNote(content="a"))  # 預設仍吞例外
        with pytest.raises(OSError):
            mgr.add_note(SoulNote(content="a"), raise_on_error=True)


class TestRollback:
    def test_salience_restored(self, env):
        q = ReflectionActionQueue(audit_path=env.queue_audit)
        act = _enqueue(env, q, "adjust_salience", "ep1", {"new_salience": 0.9})
        _run(env, q, act)
        assert q.rollback(act.id, env.executor.build_rollback_fn())
        assert env.episodic.get("ep1").salience_score == 0.5
        assert act.status == ActionStatus.ROLLED_BACK
        r = _read(env.audit)[-1]
        assert r["phase"] == "rollback" and r["status"] == "committed"
        assert r["action_id"] == act.id
        assert r["after"] == {"salience_score": 0.5}

    def test_archive_restored(self, env):
        q = ReflectionActionQueue(audit_path=env.queue_audit)
        act = _enqueue(env, q, "archive_episode", "ep1", {})
        _run(env, q, act)
        assert q.rollback(act.id, env.executor.build_rollback_fn())
        ep = env.episodic.get("ep1")
        assert ep.salience_score == 0.5
        assert ep.metadata["archived"] is False and ep.metadata["archived_by"] is None

    def test_archive_restores_prior_archived_state(self, env):
        """原本就被別人歸檔的 episode，回滾後保持原歸檔狀態與 archived_by。"""
        q = ReflectionActionQueue(audit_path=env.queue_audit)
        act = _enqueue(env, q, "archive_episode", "ep2", {})
        _run(env, q, act)
        assert q.rollback(act.id, env.executor.build_rollback_fn())
        ep = env.episodic.get("ep2")
        assert ep.salience_score == 0.7
        assert ep.metadata == {"archived": True, "archived_by": "dream"}

    def test_note_retracted(self, env):
        q = ReflectionActionQueue(audit_path=env.queue_audit)
        act = _enqueue(env, q, "add_note", "", {"content": "筆記"})
        _run(env, q, act)
        assert q.rollback(act.id, env.executor.build_rollback_fn())
        assert len(env.notes.notes) == 2
        retract = env.notes.notes[-1]
        assert retract.category == "reflection_retract"
        assert retract.metadata["retracts"] == act.pre_state["note_id"]
        r = _read(env.audit)[-1]
        assert r["phase"] == "rollback" and r["operation"] == "add_note"

    def test_rollback_failure_keeps_executed(self, env):
        q = ReflectionActionQueue()
        act = _enqueue(env, q, "adjust_salience", "ep1", {"new_salience": 0.9})
        _run(env, q, act)
        del env.episodic.eps["ep1"]
        assert not q.rollback(act.id, env.executor.build_rollback_fn())
        assert act.status == ActionStatus.EXECUTED


class TestRebuildAfterRestart:
    """入隊後重啟：pre_state／來源從 audit 重建，仍可執行並回滾。"""

    def test_pending_pre_state_survives_and_rollbacks(self, env):
        q1 = ReflectionActionQueue(audit_path=env.queue_audit)
        act = _enqueue(env, q1, "archive_episode", "ep1", {})
        original_pre = dict(act.pre_state)
        assert original_pre["salience_score"] == 0.5

        q2 = ReflectionActionQueue(audit_path=env.queue_audit)
        restored = q2.get_pending()[0]
        assert restored.id == act.id
        assert restored.pre_state == original_pre
        assert restored.pre_state["provenance"]["reflection_cycle_id"] == "cyc1"
        assert restored.params["_action_id"] == act.id

        assert q2.confirm(restored.id)
        assert q2.execute(restored, env.executor.build_executor_fn())
        assert _read(env.audit)[-1]["source"]["episode_ids"] == ["epA", "epB"]
        assert env.episodic.get("ep1").metadata["archived"] is True

        assert q2.rollback(restored.id, env.executor.build_rollback_fn())
        assert env.episodic.get("ep1").salience_score == 0.5
        assert env.episodic.get("ep1").metadata["archived"] is False

    def test_add_note_note_id_survives_restart(self, env):
        q1 = ReflectionActionQueue(audit_path=env.queue_audit)
        # add_note 為 AUTO：入隊後尚未執行即重啟
        act = _enqueue(env, q1, "add_note", "", {"content": "筆記"})
        q2 = ReflectionActionQueue(audit_path=env.queue_audit)
        restored = q2.get_pending()[0]
        assert restored.pre_state["note_id"] == act.pre_state["note_id"]
        assert q2.execute(restored, env.executor.build_executor_fn())
        assert env.notes.notes[0].metadata["note_id"] == act.pre_state["note_id"]
        assert q2.rollback(restored.id, env.executor.build_rollback_fn())
        assert env.notes.notes[-1].metadata["retracts"] == act.pre_state["note_id"]
