"""DL-2：``laya.shadow`` shadow proposer 殼（假的 laya 模組，不依賴真模型與網路、不需資料庫）。"""
from __future__ import annotations

import sys
import threading
import types

import pytest

from uep.config import UepSettings
from uep.echo import laya_wiring
from uep.echo.laya_wiring import (
    LayaProposer,
    build_state,
    create_laya_proposer,
    resolve_device,
)
from uep.turn.decision_config import decision_config_from_settings
from uep.turn.models import ReplyMode, SessionContext, TurnCandidate
from uep.turn.proposer import validate_proposal
from uep.turn.registry import PROPOSERS, create_proposer

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")


def _cand(text="小艾，今天會下雨嗎？", count: int | None = 1, hint=True) -> TurnCandidate:
    return TurnCandidate(
        candidate_id="cand1", utterance_refs=("e1",), speakers=("discord:12345",), ctx=CTX, interaction_state="IDLE",
        interaction_version=1, presence_active=True, text=text, participant_count=count, addressed_hint=hint,
    )


class _FakeAgent:
    def __init__(self, answers=None, revision="abc123def4567890"):
        self.revision = revision
        self.calls: list[tuple[dict, dict]] = []
        self.answers = answers or {
            "reply_mode": {"choice": "respond", "probabilities": {"respond": 0.9, "passive": 0.05, "pass": 0.03, "defer": 0.02}},
            "route": {"choice": "task_request", "probabilities": {"task_request": 0.7}},
            "addressed": {"noul": 0.8},
        }

    def predict(self, state, questions):
        self.calls.append((state, questions))
        return {"answers": self.answers}


def _install_fake(monkeypatch, agent=None, gate: threading.Event | None = None, fail: bool = False):
    fake = types.ModuleType("laya")
    fake.__version__ = "0.4.30"
    fake.loads: list[tuple] = []
    fake.agent = agent or _FakeAgent()

    def load(model_id, device=None, **kw):
        fake.loads.append((model_id, device, kw))
        if gate is not None:
            assert gate.wait(5)
        if fail:
            raise RuntimeError("boom")
        return fake.agent

    fake.load = load
    monkeypatch.setitem(sys.modules, "laya", fake)
    monkeypatch.setattr(laya_wiring, "resolve_device", lambda requested, cuda_available=None: requested)
    return fake


def test_build_state_excludes_ids_and_handles_headcount():
    state = build_state(_cand(count=3), "小艾")
    assert state == {
        "assistant_name": "小艾", "speaker": "user", "other_people_present": "2 other people present",
        "recent_context": "(none)", "message": "小艾，今天會下雨嗎？",
    }
    assert "12345" not in repr(state)
    assert build_state(_cand(count=None), "x")["other_people_present"] == "none"
    assert build_state(_cand(count=1), "x")["other_people_present"] == "none"


def test_proposal_assembly_and_validation(monkeypatch):
    fake = _install_fake(monkeypatch)
    p = LayaProposer(model_id="m/x", revision="rev1", device="cpu", assistant_name="小艾")
    assert p.wait_ready(5) and p.loaded
    proposal = p.propose(_cand())
    assert validate_proposal(proposal) is None
    assert proposal.reply_mode is ReplyMode.RESPOND and proposal.route == "task_request"
    assert proposal.addressed is True and proposal.confidence == pytest.approx(0.9)
    assert proposal.interaction_to is None
    assert proposal.proposer_id == "laya.shadow"
    assert proposal.proposer_version == "laya-0.4.30:abc123def4567890"
    assert fake.loads == [("m/x", "cpu", {"revision": "rev1"})]
    state, questions = fake.agent.calls[-1]  # calls[0] 是 warmup 假推論
    assert state["assistant_name"] == "小艾" and set(questions) == {"reply_mode", "route", "addressed"}
    assert set(questions["reply_mode"]["criteria"]) == {"respond", "passive", "pass", "defer"}
    assert questions["addressed"]["type"] == "noul"


def test_unknown_choice_raises_and_not_addressed(monkeypatch):
    agent = _FakeAgent({
        "reply_mode": {"choice": "pass", "probabilities": {"pass": 0.6}},
        "route": {"choice": "ignore", "probabilities": {}},
        "addressed": {"noul": 0.1},
    })
    _install_fake(monkeypatch, agent)
    p = LayaProposer(device="cpu")
    p.wait_ready(5)
    proposal = p.propose(_cand())
    assert proposal.reply_mode is ReplyMode.PASS and proposal.addressed is False
    agent.answers = {**agent.answers, "route": {"choice": "bogus"}}
    with pytest.raises(ValueError):
        p.propose(_cand())
    agent.answers = {**agent.answers, "route": {"choice": "ignore"}, "reply_mode": {"choice": "bogus"}}
    with pytest.raises(ValueError):
        p.propose(_cand())


def test_warmup_returns_fallback_and_counts(monkeypatch):
    gate = threading.Event()
    _install_fake(monkeypatch, gate=gate)
    p = LayaProposer(device="cpu")
    try:
        first = p.propose(_cand(count=3, hint=False))
        assert first.reason_code == "laya_warming_up" and first.proposer_id == "laya.shadow"
        assert first.reply_mode is ReplyMode.PASSIVE and first.confidence == 0.0  # 多人未被點名 -> D80 passive
        assert validate_proposal(first) is None
        assert p.propose(_cand()).reply_mode is ReplyMode.RESPOND
        assert p.metrics["laya_warming_up"] == 2 and not p.loaded
    finally:
        gate.set()
    assert p.wait_ready(5)
    assert p.propose(_cand()).reason_code == "laya_model"


def test_load_failure_keeps_fallback(monkeypatch):
    _install_fake(monkeypatch, fail=True)
    p = LayaProposer(device="cpu")
    assert p.wait_ready(5) and not p.loaded
    assert p.propose(_cand()).reason_code == "laya_load_failed"
    assert p.metrics["laya_load_failed"] == 1


def test_missing_package_fails_fast(monkeypatch):
    monkeypatch.delitem(sys.modules, "laya", raising=False)
    monkeypatch.setattr(laya_wiring.importlib.util, "find_spec", lambda name: None)
    assert "laya.shadow" in PROPOSERS.ids()  # 仍註冊
    with pytest.raises(ValueError, match="laya"):
        create_proposer("laya.shadow")
    with pytest.raises(ValueError, match="laya"):
        LayaProposer()


def test_registered_and_created_through_registry(monkeypatch):
    _install_fake(monkeypatch)
    proposer = create_proposer("laya.shadow", config=None)
    assert isinstance(proposer, LayaProposer)
    proposer.wait_ready(5)


def _make_model_dir(root, revision: str | None = "abc123"):
    model_dir = root / "laya-multilingual"
    model_dir.mkdir(parents=True)
    if revision:
        (model_dir / "REVISION").write_text(revision, encoding="utf-8")
    return model_dir


def test_settings_default_resolves_local_dir(monkeypatch, tmp_path):
    fake = _install_fake(monkeypatch, agent=_FakeAgent(revision=None))  # 本地載入的 agent 沒有 hub revision
    model_dir = _make_model_dir(tmp_path)
    proposer = create_laya_proposer(settings=UepSettings(_env_file=None, UEP_LAYA_MODELS_DIR=str(tmp_path)))
    proposer.wait_ready(5)
    assert fake.loads[-1] == (str(model_dir), "cuda", {})
    assert proposer.version.endswith(":abc123")


def test_default_models_dir_is_under_user_home(monkeypatch):
    monkeypatch.setattr(laya_wiring.Path, "home", classmethod(lambda cls: cls("/home/u")))
    assert laya_wiring.default_laya_model_dir() == laya_wiring.Path("/home/u/.uep/models/laya/laya-multilingual")
    assert laya_wiring.default_laya_model_dir("/x/y") == laya_wiring.Path("/x/y/laya-multilingual")


def test_default_local_dir_ignores_hub_revision(monkeypatch, tmp_path):
    fake = _install_fake(monkeypatch, agent=_FakeAgent(revision=None))
    _make_model_dir(tmp_path, revision=None)
    st = UepSettings(_env_file=None, UEP_LAYA_MODELS_DIR=str(tmp_path), UEP_LAYA_REVISION="r9", UEP_LAYA_DEVICE="cpu")
    proposer = create_laya_proposer(settings=st)
    proposer.wait_ready(5)
    assert fake.loads[-1][2] == {}
    assert proposer.version.endswith(":unpinned")


def test_missing_default_local_dir_fails_fast(monkeypatch, tmp_path):
    fake = _install_fake(monkeypatch)
    missing = tmp_path / "nope"
    with pytest.raises(ValueError) as exc:
        create_laya_proposer(settings=UepSettings(_env_file=None, UEP_LAYA_MODELS_DIR=str(missing)))
    assert str(missing / "laya-multilingual") in str(exc.value)
    assert fake.loads == []


def test_explicit_model_used_as_is(monkeypatch, tmp_path):
    fake = _install_fake(monkeypatch)
    # 明確設定時不檢查預設本地目錄（即使不存在），HF id 與其他路徑都原樣傳入
    st = UepSettings(_env_file=None, UEP_LAYA_MODELS_DIR=str(tmp_path / "nope"), UEP_LAYA_MODEL="a/b",
                     UEP_LAYA_REVISION="r9", UEP_LAYA_DEVICE="cpu")
    custom = create_laya_proposer(settings=st)
    custom.wait_ready(5)
    assert fake.loads[-1] == ("a/b", "cpu", {"revision": "r9"})
    other = create_laya_proposer(settings=UepSettings(_env_file=None, UEP_LAYA_MODEL=str(tmp_path / "other")))
    other.wait_ready(5)
    assert fake.loads[-1][0] == str(tmp_path / "other")


def test_device_falls_back_to_cpu_without_cuda():
    assert resolve_device("cuda", cuda_available=False) == "cpu"
    assert resolve_device("cuda", cuda_available=True) == "cuda"
    assert resolve_device("cpu", cuda_available=True) == "cpu"


def test_shadow_list_accepts_laya_id():
    st = UepSettings(_env_file=None, UEP_DECISION_SHADOW_PROPOSERS="respond.minimal,laya.shadow")
    assert decision_config_from_settings(st).shadow_proposer_ids == ("respond.minimal", "laya.shadow")
