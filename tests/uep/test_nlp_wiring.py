"""DL-5：``nlp.legacy`` shadow proposer（假後端測語言 gating／映射／warmup／缺檔；另有標 skipif 的真模型 smoke）。"""
from __future__ import annotations

import threading

import pytest

from uep.config import UepSettings
from uep.echo import nlp_wiring
from uep.echo.nlp_wiring import (
    BioSegment,
    NlpFeatures,
    NlpLegacyProposer,
    ascii_letter_ratio,
    create_nlp_proposer,
    is_english,
    map_features,
    missing_model_files,
    resolve_model_dir,
    segments_from_labels,
)
from uep.turn.decision_config import decision_config_from_settings
from uep.turn.models import ReplyMode, SessionContext, TurnCandidate
from uep.turn.proposer import validate_proposal
from uep.turn.registry import PROPOSERS, create_proposer

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")


def _cand(text="Hey U.E.P, open the file manager", count: int | None = 1, hint=False) -> TurnCandidate:
    return TurnCandidate(
        candidate_id="cand1", utterance_refs=("e1",), speakers=("discord:12345",), ctx=CTX, interaction_state="IDLE",
        interaction_version=1, presence_active=True, text=text, participant_count=count, addressed_hint=hint,
    )


def _feat(labels=(), intent="chat", conf=0.9) -> NlpFeatures:
    return NlpFeatures(tuple(BioSegment(lab, 0.8, 2) for lab in labels), intent, conf)


class _FakeBackend:
    def __init__(self, features: NlpFeatures | None = None) -> None:
        self.features = features or _feat(("CALL", "DIRECT_WORK"), "command")
        self.seen: list[str] = []

    def analyze(self, text: str) -> NlpFeatures:
        self.seen.append(text)
        return self.features


@pytest.fixture
def model_dir(tmp_path):
    """只放必要檔案的空殼目錄（不含真權重）。"""
    for rel in ("bio_tagger/config.json", "bio_tagger/model.safetensors",
                "command_chat_classifier/config.json", "command_chat_classifier/pytorch_model.bin"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    return tmp_path


def _install(monkeypatch, backend=None, gate: threading.Event | None = None, fail: bool = False):
    backend = backend or _FakeBackend()

    def fake_load(path):
        if gate is not None:
            assert gate.wait(5)
        if fail:
            raise RuntimeError("boom")
        return backend, "nlp-legacy-test"

    monkeypatch.setattr(nlp_wiring, "load_backend", fake_load)
    return backend


# ---- 語言 gating ----

def test_language_gating_by_ascii_letter_ratio():
    assert is_english("Hey U.E.P, what is the weather like today?")
    assert not is_english("今天天氣如何？")
    assert not is_english("uep 幫我設一個明天的提醒")  # 中英混雜：ASCII 字母不足門檻
    assert not is_english("12345 ...")  # 沒有字母
    assert ascii_letter_ratio("") == 0.0
    assert ascii_letter_ratio("ab中文") == pytest.approx(0.5)


def test_non_english_returns_d80_default_and_counts(model_dir, monkeypatch):
    backend = _install(monkeypatch)
    p = NlpLegacyProposer(model_dir=model_dir)
    assert p.wait_ready(5) and p.loaded
    proposal = p.propose(_cand("今天天氣如何？", count=3, hint=False))
    assert validate_proposal(proposal) is None
    assert proposal.reason_code == "nlp_lang_unsupported" and proposal.proposer_id == "nlp.legacy"
    assert proposal.reply_mode is ReplyMode.PASSIVE and proposal.confidence == 0.0  # 多人未點名 -> D80 passive
    assert p.propose(_cand("好的", count=1)).reply_mode is ReplyMode.RESPOND  # 單人 -> respond
    assert p.metrics["nlp_lang_unsupported"] == 2
    assert backend.seen == []  # 非英文不送模型


# ---- 映射 ----

@pytest.mark.parametrize(
    ("labels", "intent", "route", "addressed", "mode"),
    [
        (("CALL", "DIRECT_WORK"), "command", "task_request", True, ReplyMode.RESPOND),
        (("BACKGROUND_WORK",), "command", "agent_loop", False, ReplyMode.RESPOND),
        (("DIRECT_WORK", "BACKGROUND_WORK"), "command", "task_request", False, ReplyMode.RESPOND),  # 直接工作優先
        (("CHAT",), "chat", "direct_chat", False, ReplyMode.RESPOND),
        (("CALL", "CHAT"), "chat", "direct_chat", True, ReplyMode.RESPOND),
        ((), "command", "task_request", False, ReplyMode.RESPOND),  # BIO 無區段，分類器 command 補 route
        (("CHAT",), "command", "task_request", False, ReplyMode.RESPOND),  # CHAT 不擋分類器的 command
        (("UNKNOWN",), "non-sense", "direct_chat", False, ReplyMode.PASS),
        ((), "non-sense", "direct_chat", False, ReplyMode.PASS),
    ],
)
def test_mapping_table(labels, intent, route, addressed, mode):
    proposal = map_features(_cand(), _feat(labels, intent), version="v")
    assert validate_proposal(proposal) is None
    assert (proposal.route, proposal.addressed, proposal.reply_mode) == (route, addressed, mode)
    assert proposal.reason_code == "nlp_model" and proposal.proposer_id == "nlp.legacy"
    assert proposal.interaction_to is None


def test_reply_mode_follows_d80_when_not_nonsense():
    multi = _cand(count=3, hint=False)
    assert map_features(multi, _feat(("CHAT",), "chat"), version="v").reply_mode is ReplyMode.PASSIVE
    assert map_features(multi, _feat(("CALL",), "chat"), version="v").reply_mode is ReplyMode.RESPOND  # BIO 判 CALL
    assert map_features(_cand(count=3, hint=True), _feat(("CHAT",), "chat"), version="v").reply_mode is ReplyMode.RESPOND
    assert map_features(_cand(count=None), _feat(("CHAT",), "chat"), version="v").reply_mode is ReplyMode.RESPOND


def test_confidence_is_averaged_and_bounded():
    p = map_features(_cand(), NlpFeatures((BioSegment("CHAT", 0.6, 1),), "chat", 1.0), version="v")
    assert p.confidence == pytest.approx(0.8)
    assert map_features(_cand(), NlpFeatures((), "chat", 0.7), version="v").confidence == pytest.approx(0.7)


def test_segments_from_labels_groups_bio():
    segs = segments_from_labels(
        ["B-CALL", "O", "B-DIRECT_WORK", "I-DIRECT_WORK", "I-DIRECT_WORK", "I-CHAT", "B-CHAT"],
        [0.9, 0.5, 0.8, 0.6, 0.7, 0.4, 0.6],
    )
    assert [(s.label, s.words) for s in segs] == [
        ("CALL", 1), ("DIRECT_WORK", 3), ("CHAT", 1), ("CHAT", 1)]
    assert segs[1].confidence == pytest.approx(0.7)


def test_english_goes_through_backend(model_dir, monkeypatch):
    backend = _install(monkeypatch)
    p = NlpLegacyProposer(model_dir=model_dir)
    assert p.wait_ready(5)
    proposal = p.propose(_cand())
    assert backend.seen == ["Hey U.E.P, open the file manager"]
    assert proposal.route == "task_request" and proposal.addressed is True
    assert proposal.proposer_version == "nlp-legacy-test" and p.metrics["nlp_proposed"] == 1


# ---- warmup／載入失敗 ----

def test_warmup_returns_fallback_then_model(model_dir, monkeypatch):
    gate = threading.Event()
    _install(monkeypatch, gate=gate)
    p = NlpLegacyProposer(model_dir=model_dir)
    try:
        first = p.propose(_cand(count=3))
        assert first.reason_code == "nlp_warming_up" and first.proposer_id == "nlp.legacy"
        assert first.reply_mode is ReplyMode.PASSIVE and validate_proposal(first) is None
        assert p.metrics["nlp_warming_up"] == 1 and not p.loaded
    finally:
        gate.set()
    assert p.wait_ready(5)
    assert p.propose(_cand()).reason_code == "nlp_model"


def test_load_failure_keeps_fallback(model_dir, monkeypatch):
    _install(monkeypatch, fail=True)
    p = NlpLegacyProposer(model_dir=model_dir)
    assert p.wait_ready(5) and not p.loaded
    assert p.propose(_cand()).reason_code == "nlp_load_failed"
    assert p.metrics["nlp_load_failed"] == 1


# ---- 模型缺失／設定／註冊 ----

def test_missing_model_files_fail_fast(tmp_path):
    assert missing_model_files(tmp_path)
    with pytest.raises(ValueError, match="nlp.legacy"):
        NlpLegacyProposer(model_dir=tmp_path)
    with pytest.raises(ValueError, match="nlp.legacy"):
        create_proposer("nlp.legacy", settings=UepSettings(_env_file=None, UEP_NLP_MODEL_DIR=str(tmp_path)))


def test_registered_and_created_through_registry(model_dir, monkeypatch):
    _install(monkeypatch)
    assert "nlp.legacy" in PROPOSERS.ids()
    st = UepSettings(_env_file=None, UEP_NLP_MODEL_DIR=str(model_dir))
    proposer = create_proposer("nlp.legacy", config=None, settings=st)
    assert isinstance(proposer, NlpLegacyProposer)
    proposer.wait_ready(5)
    assert create_nlp_proposer(settings=st).wait_ready(5)


def test_model_dir_resolution(model_dir):
    assert resolve_model_dir(None) == nlp_wiring.DEFAULT_NLP_MODEL_DIR
    assert resolve_model_dir("  ") == nlp_wiring.DEFAULT_NLP_MODEL_DIR
    assert resolve_model_dir(str(model_dir)) == model_dir
    assert nlp_wiring.DEFAULT_NLP_MODEL_DIR.parts[-2:] == ("models", "nlp")


def test_shadow_list_accepts_nlp_id_and_default_is_off():
    st = UepSettings(_env_file=None, UEP_DECISION_SHADOW_PROPOSERS="respond.minimal,nlp.legacy")
    assert decision_config_from_settings(st).shadow_proposer_ids == ("respond.minimal", "nlp.legacy")
    assert "nlp.legacy" not in decision_config_from_settings(UepSettings(_env_file=None)).shadow_proposer_ids


# ---- 真模型 smoke（models/nlp 不存在則略過；可用 UEP_NLP_MODEL_DIR 指向）----

_REAL_DIR = resolve_model_dir(UepSettings(_env_file=None).nlp_model_dir)


@pytest.mark.skipif(bool(missing_model_files(_REAL_DIR)), reason="models/nlp 不存在（本機未追蹤資產）")
def test_real_model_smoke():
    p = NlpLegacyProposer(model_dir=_REAL_DIR)
    assert p.wait_ready(120) and p.loaded
    call = p.propose(_cand("Hey U.E.P, can you open the file manager?"))
    assert validate_proposal(call) is None and call.reason_code == "nlp_model"
    assert call.addressed is True and call.route == "task_request"
    junk = p.propose(_cand("asdfghjkl"))
    assert junk.reply_mode is ReplyMode.PASS
