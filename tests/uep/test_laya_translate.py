"""laya.shadow 接入 Boundary Translator：全英文 state、recent_context、translate_unavailable、warmup 含推論。"""
from __future__ import annotations

import sys
import types

import pytest

from uep.config import UepSettings
from uep.decision import laya_schema
from uep.echo import laya_wiring
from uep.echo.laya_wiring import LayaProposer, create_laya_proposer
from uep.echo.translate_wiring import BoundaryTranslator
from uep.turn.models import SessionContext, TurnCandidate, TurnOutcomeFacts

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")


def _cand(text="小U，明天會下雨嗎？", cid="cand1", count: int | None = 1) -> TurnCandidate:
    return TurnCandidate(
        candidate_id=cid, utterance_refs=("e1",), speakers=("discord:12345",), ctx=CTX, interaction_state="IDLE",
        interaction_version=1, presence_active=True, text=text, participant_count=count, addressed_hint=True,
    )


class _Agent:
    revision = "r1"

    def __init__(self):
        self.calls: list[tuple[dict, dict]] = []

    def predict(self, state, questions):
        self.calls.append((state, questions))
        return {"answers": {
            "reply_mode": {"choice": "respond", "probabilities": {"respond": 0.9}},
            "route": {"choice": "direct_chat"}, "addressed": {"noul": 0.8},
        }}


class _MT:
    """假 MT：以字典翻譯，查不到回 'en:<原文>'。"""

    def __init__(self, table=None, fail=False):
        self.table = table or {}
        self.fail = fail
        self.calls: list[str] = []

    def translate(self, text):
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("mt boom")
        return self.table.get(text, f"en {text}")


def _translator(mt: _MT) -> BoundaryTranslator:
    tr = BoundaryTranslator(backend_factory=lambda d: mt, device="cpu", warmup_sentences=())
    return tr


def _proposer(monkeypatch, mt: _MT | None = None, **kw):
    agent = _Agent()
    fake = types.ModuleType("laya")
    fake.__version__ = "0.4.30"
    fake.load = lambda model_id, device=None, **k: agent
    monkeypatch.setitem(sys.modules, "laya", fake)
    monkeypatch.setattr(laya_wiring, "resolve_device", lambda requested, cuda_available=None: requested)
    translator = _translator(mt) if mt is not None else None
    p = LayaProposer(device="cpu", assistant_name="U.E.P", translator=translator, **kw)
    assert p.wait_ready(5)
    return p, agent, translator


def _all_ascii(value) -> bool:
    if isinstance(value, dict):
        return all(_all_ascii(k) and _all_ascii(v) for k, v in value.items())
    return str(value).isascii()


def test_schema_is_pure_english_and_shared():
    assert _all_ascii(laya_schema.QUESTIONS)
    assert laya_wiring.QUESTIONS is laya_schema.QUESTIONS  # 推論端與訓練工具共用同一份
    state = laya_schema.build_state_dict(assistant_name="Yuki", message="hi", participant_count=3)
    assert _all_ascii({k: v for k, v in state.items() if k != "message"})
    assert state["other_people_present"] == "2 other people present" and state["recent_context"] == "(none)"
    assert laya_schema.format_recent_context(None, None) == "(none)"
    long = "a" * 500
    ctx = laya_schema.format_recent_context(long, long)
    assert all(len(line) <= laya_schema.CONTEXT_MAX_CHARS + len("Assistant: ") for line in ctx.splitlines())


def test_warmup_runs_a_fake_inference_before_ready(monkeypatch):
    p, agent, _ = _proposer(monkeypatch)
    assert p.loaded and len(agent.calls) == 1  # 載入後、ready 前已經推論過一次
    assert agent.calls[0][0]["message"] == laya_schema.WARMUP_MESSAGE
    assert agent.calls[0][1] is laya_schema.QUESTIONS


def test_warmup_inference_failure_means_not_loaded(monkeypatch):
    agent = _Agent()
    agent.predict = lambda s, q: (_ for _ in ()).throw(RuntimeError("cuda cold start failed"))
    fake = types.ModuleType("laya")
    fake.load = lambda model_id, device=None, **k: agent
    monkeypatch.setitem(sys.modules, "laya", fake)
    monkeypatch.setattr(laya_wiring, "resolve_device", lambda requested, cuda_available=None: requested)
    p = LayaProposer(device="cpu")
    assert p.wait_ready(5) and not p.loaded
    assert p.propose(_cand()).reason_code == "laya_load_failed"


def test_translator_on_sends_english_state_with_placeholder_name(monkeypatch):
    mt = _MT({"Yuki，明天會下雨嗎？": "Yuki, will it rain tomorrow?"})
    p, agent, tr = _proposer(monkeypatch, mt)
    assert tr.wait_ready(5)
    proposal = p.propose(_cand(count=2))
    assert proposal.reason_code == "laya_model"
    state = agent.calls[-1][0]
    assert state == {
        "assistant_name": "Yuki", "speaker": "user", "other_people_present": "1 other person present",
        "recent_context": "(none)", "message": "Yuki, will it rain tomorrow?",
    }
    assert _all_ascii(state) and "12345" not in repr(state)


def test_translator_off_sends_original_text(monkeypatch):
    p, agent, _ = _proposer(monkeypatch, None)
    p.propose(_cand())
    state = agent.calls[-1][0]
    assert state["message"] == "小U，明天會下雨嗎？" and state["assistant_name"] == "U.E.P"
    assert state["recent_context"] == "(none)"


def test_translate_unavailable_falls_back_by_default(monkeypatch):
    p, agent, tr = _proposer(monkeypatch, _MT(fail=True))
    tr.wait_ready(5)
    n = len(agent.calls)
    proposal = p.propose(_cand())
    assert proposal.reason_code == "translate_unavailable" and proposal.proposer_id == "laya.shadow"
    assert len(agent.calls) == n and p.metrics["translate_unavailable"] == 1  # 沒送模型


def test_translate_unavailable_can_send_original(monkeypatch):
    p, agent, tr = _proposer(monkeypatch, _MT(fail=True), on_translate_unavailable="original")
    tr.wait_ready(5)
    proposal = p.propose(_cand())
    assert proposal.reason_code == "laya_model"
    assert agent.calls[-1][0]["message"] == "小U，明天會下雨嗎？" and agent.calls[-1][0]["assistant_name"] == "U.E.P"


def test_translator_still_warming_is_unavailable(monkeypatch):
    # 翻譯器沒有啟動／尚未就緒：不能把中文送進只訓練英文的模型
    agent = _Agent()
    fake = types.ModuleType("laya")
    fake.load = lambda model_id, device=None, **k: agent
    monkeypatch.setitem(sys.modules, "laya", fake)
    monkeypatch.setattr(laya_wiring, "resolve_device", lambda requested, cuda_available=None: requested)
    tr = BoundaryTranslator(backend_factory=lambda d: (_ for _ in ()).throw(RuntimeError("x")), device="cpu")
    p = LayaProposer(device="cpu", translator=tr)
    assert p.wait_ready(5)
    assert p.propose(_cand()).reason_code == "translate_unavailable"


def _outcome(cid="cand1", reply="好的，明天可能會下雨。", status="completed") -> TurnOutcomeFacts:
    return TurnOutcomeFacts(
        turn_id="t1", candidate_id=cid, speakers=("discord:12345",), reply_mode="respond", reason_code="called",
        status=status, reply_text=reply,
    )


def test_recent_context_carries_previous_turn_in_english(monkeypatch):
    mt = _MT({
        "Yuki，明天會下雨嗎？": "Yuki, will it rain tomorrow?",
        "好的，明天可能會下雨。": "Okay, it may rain tomorrow.",
        "那後天呢": "And the day after?",
    })
    p, agent, tr = _proposer(monkeypatch, mt)
    tr.wait_ready(5)
    p.propose(_cand(text="小U，明天會下雨嗎？", cid="c1"))
    p.on_turn_outcome(_outcome("c1"))
    p.wait_context(5)
    p.propose(_cand(text="那後天呢", cid="c2"))
    state = agent.calls[-1][0]
    assert state["recent_context"] == "User: Yuki, will it rain tomorrow?\nAssistant: Okay, it may rain tomorrow."
    assert _all_ascii({k: v for k, v in state.items()})  # en 開頭的假譯文皆為 ASCII
    assert "12345" not in repr(state)


def test_recent_context_is_truncated_and_skips_non_completed(monkeypatch):
    mt = _MT()
    p, agent, tr = _proposer(monkeypatch, mt)
    tr.wait_ready(5)
    long_reply = "好" * 400
    p.propose(_cand(text="你好" * 200, cid="c1"))
    p.on_turn_outcome(_outcome("c1", reply=long_reply))
    p.wait_context(5)
    assert len(mt.calls[0]) == 400  # propose 的本句不截斷
    assert sorted(len(c) for c in mt.calls[-2:]) == [160, 160]  # 脈絡兩段送翻譯前各截到 160 字元
    p.propose(_cand(text="再問一次", cid="c2"))
    ctx = agent.calls[-1][0]["recent_context"]
    assert all(len(line) <= 160 + len("Assistant: ") for line in ctx.splitlines())
    # 未完成的回合不更新脈絡
    before = ctx
    p.on_turn_outcome(_outcome("c2", status="cancelled"))
    p.on_turn_outcome(_outcome("c2", reply="  "))
    p.wait_context(5)
    p.propose(_cand(text="第三次", cid="c3"))
    assert agent.calls[-1][0]["recent_context"] == before


def test_context_not_used_when_translator_off(monkeypatch):
    p, agent, _ = _proposer(monkeypatch, None)
    p.propose(_cand(cid="c1"))
    p.on_turn_outcome(_outcome("c1"))
    p.propose(_cand(cid="c2"))
    assert agent.calls[-1][0]["recent_context"] == "(none)"


def test_factory_builds_translator_only_when_enabled(monkeypatch, tmp_path):
    agent = _Agent()
    fake = types.ModuleType("laya")
    fake.load = lambda model_id, device=None, **k: agent
    monkeypatch.setitem(sys.modules, "laya", fake)
    monkeypatch.setattr(laya_wiring, "resolve_device", lambda requested, cuda_available=None: requested)
    monkeypatch.setattr(laya_wiring, "create_boundary_translator", lambda st, extra_aliases=(): _translator(_MT()))
    (tmp_path / "laya-multilingual").mkdir()
    base = {"_env_file": None, "UEP_LAYA_MODELS_DIR": str(tmp_path), "UEP_LAYA_DEVICE": "cpu"}
    off = create_laya_proposer(settings=UepSettings(**base))
    assert off._translator is None
    on = create_laya_proposer(settings=UepSettings(**base, UEP_DECISION_TRANSLATE="1", UEP_TRANSLATE_ON_UNAVAILABLE="original"))
    assert on._translator is not None and on._send_original_on_unavailable is True
    on2 = create_laya_proposer(settings=UepSettings(**base, UEP_DECISION_TRANSLATE="1"))
    assert on2._send_original_on_unavailable is False
    for proposer in (off, on, on2):
        proposer.wait_ready(5)


@pytest.mark.parametrize("count,expected", [(None, "none"), (1, "none"), (2, "1 other person present"), (4, "3 other people present")])
def test_headcount_literals(count, expected):
    assert laya_schema.others_present(count) == expected


def test_translator_warmup_starts_only_after_laya_load(monkeypatch):
    # transformers 的延遲 import 不是執行緒安全：翻譯器必須等 laya.load 返回才開始載入
    order: list[str] = []
    agent = _Agent()
    fake = types.ModuleType("laya")
    fake.load = lambda model_id, device=None, **k: (order.append("laya.load"), agent)[1]
    monkeypatch.setitem(sys.modules, "laya", fake)
    monkeypatch.setattr(laya_wiring, "resolve_device", lambda requested, cuda_available=None: requested)
    tr = BoundaryTranslator(backend_factory=lambda d: (order.append("mt.load"), _MT())[1], device="cpu", warmup_sentences=())
    p = LayaProposer(device="cpu", translator=tr)
    assert p.wait_ready(5) and tr.wait_ready(5)
    assert order == ["laya.load", "mt.load"]
