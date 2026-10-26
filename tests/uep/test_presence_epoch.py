"""Host epoch 檔、共用契約型別與 migration 鏈（不需 DB）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from tests.uep.conftest import alembic_config
from uep.contracts import presence as contracts
from uep.presence.epoch import (
    HostEpochError,
    HostEpochs,
    HostEpochStore,
    default_epoch_path,
)


def contracts_epochs(fencing: int, core: int) -> HostEpochs:
    return HostEpochs(fencing, core)


def test_missing_file_reads_zero_and_bump_starts_at_one(tmp_path):
    store = HostEpochStore(tmp_path / "sub" / "host_epoch.json")
    assert store.read() == contracts_epochs(0, 0)
    assert store.bump() == contracts_epochs(1, 1)
    assert store.bump() == contracts_epochs(2, 2)
    assert json.loads(store.path.read_text(encoding="utf-8")) == {"fencing_epoch": 2, "core_epoch": 2}


def test_bump_can_target_a_single_epoch_and_raise_to_never_lowers(tmp_path):
    store = HostEpochStore(tmp_path / "e.json")
    store.bump()
    assert store.bump(core=False) == contracts_epochs(2, 1)
    assert store.raise_to(fencing_at_least=1, core_at_least=5) == contracts_epochs(2, 5)


def test_corrupt_file_raises_instead_of_resetting(tmp_path):
    path = tmp_path / "e.json"
    path.write_text('{"fencing_epoch": "x"}', encoding="utf-8")
    with pytest.raises(HostEpochError):
        HostEpochStore(path).bump()
    assert path.read_text(encoding="utf-8") == '{"fencing_epoch": "x"}'  # 不覆寫損毀檔


def test_unwritable_path_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(HostEpochError):
        HostEpochStore(blocker / "e.json").bump()


def test_default_path_comes_from_setting_or_user_data_dir(monkeypatch, tmp_path):
    from uep.config import get_settings

    monkeypatch.setenv("UEP_HOST_EPOCH_FILE", str(tmp_path / "custom.json"))
    get_settings.cache_clear()
    try:
        assert default_epoch_path() == tmp_path / "custom.json"
    finally:
        monkeypatch.delenv("UEP_HOST_EPOCH_FILE")
        get_settings.cache_clear()
    default = default_epoch_path()
    assert default.name == "host_epoch.json" and default.parent.name in ("UEP", "uep")


def test_gate_decision_codes_are_the_agreed_table():
    assert {d.value for d in contracts.GateDecision} == {
        "ADMIT", "REJECT_STALE", "REJECT_NO_PRESENCE", "REJECT_AUDIENCE", "REJECT_EXPIRED", "REJECT_REVOKED",
    }
    assert contracts.GateDecision.ADMIT.admitted and not contracts.GateDecision.REJECT_STALE.admitted
    assert {p.value for p in contracts.TransferPhase} == {
        "PREPARING", "QUIESCING", "CHECKPOINTED", "ACTIVATING", "DONE", "ABORTED",
    }  # D27
    assert {s.value for s in contracts.PresenceStatus} == {"NONE", "ACTIVE", "RECOVERING"}


def test_contracts_package_has_no_uep_dependencies():
    import ast

    root = Path(contracts.__file__).parent
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if module and module.startswith("uep.") and not module.startswith("uep.contracts"):
                raise AssertionError(f"{path.name} 依賴 {module}；共用契約須保持零依賴")


def test_migration_chain_head_is_journal_txid_after_state_stop():
    script = ScriptDirectory.from_config(alembic_config("postgresql://unused/unused_test"))
    assert script.get_revision("0004_presence").down_revision == "0003_context_output"
    assert script.get_revision("0005_state_stop").down_revision == "0004_presence"
    assert script.get_revision("0006_journal_txid").down_revision == "0005_state_stop"
    assert script.get_current_head() == "0006_journal_txid"
