"""Console 對話紀錄 transcript.jsonl：欄位、寫入、失敗只記警告、正式組裝不啟用（不需 DB，以最小 stub 驅動）。"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from uep.console import transcript as tr
from uep.console.commands import Console
from uep.stream.tracer import MARK_SPEAK_FIRST_CHUNK, LatencyTracer
from uep.tools.outcomes import TurnOutcomeCache
from uep.turn.models import IntakeResult, Proposal, ReplyMode

UEP_ROOT = Path(__file__).resolve().parents[3] / "uep"


def _ticket(turn_id: str = "t1", reply_mode: str = "respond"):
    return SimpleNamespace(turn_id=turn_id, reply_mode=reply_mode, degraded=None, user_text="你好")


def _opened(route: str = "direct_chat") -> IntakeResult:
    proposal = Proposal(
        reply_mode=ReplyMode.RESPOND, addressed=True, confidence=0.9, route=route, reason_code="called",
        proposer_id="laya.primary", proposer_version="3",
    )
    return IntakeResult(_ticket(), "opened", proposal=proposal)


def _console(tmp_path, tracer=None) -> Console:
    console = object.__new__(Console)  # 略過整個 Core 組裝；只需要 rt 的觀測點
    console.rt = SimpleNamespace(
        tracer=tracer or LatencyTracer(), session_dir=tmp_path / "e2e", session_factory=None, tool_tap=None,
    )
    return console


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    monkeypatch.delenv(tr.TRANSCRIPT_LOG_ENV, raising=False)
    monkeypatch.setattr(tr, "collect_tool_calls", lambda *_a, **_k: [{"tool": "web.search", "status": "succeeded"}])


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_每輪一行含輸入決策route回覆與耗時(tmp_path):
    tracer = LatencyTracer()
    tracer.start("t1")
    tracer.mark("t1", MARK_SPEAK_FIRST_CHUNK)
    tracer.finish("t1")
    console = _console(tmp_path, tracer)
    fact = SimpleNamespace(text="今天天氣", language="zh", confidence=0.8, duration_ms=1200)
    console._write_transcript(
        "t1", "turn.completed", _opened("agent_loop"), "mic", fact, events=["turn.opened"], reply="晴天",
    )
    (row,) = _lines(tmp_path / "e2e" / "transcript.jsonl")
    assert row["turn_id"] == "t1" and row["terminal"] == "turn.completed" and row["ts"]
    assert row["input"] == {
        "text": "今天天氣", "source": "mic", "stt": {"language": "zh", "confidence": 0.8, "duration_ms": 1200},
    }
    assert row["decision"]["opened"] is True and row["decision"]["reply_mode"] == "respond"
    assert row["decision"]["proposer"] == "laya.primary" and row["decision"]["proposal_reason_code"] == "called"
    assert row["decision"]["addressed"] is True and row["decision"]["reason_code"] == "opened"
    assert row["route"] == "agent_loop" and row["reply"] == "晴天"
    assert row["tools"] == [{"tool": "web.search", "status": "succeeded"}]
    assert row["timings"]["segments_ms"]["ttfa"] is not None


def test_say來源不帶stt且沒開turn也記錄(tmp_path):
    console = _console(tmp_path)
    console._write_transcript(None, None, IntakeResult(None, "laya_model"), "say", SimpleNamespace(text="嗯"))
    (row,) = _lines(tmp_path / "e2e" / "transcript.jsonl")
    assert row["turn_id"] is None and row["input"] == {"text": "嗯", "source": "say", "stt": None}
    assert row["decision"]["opened"] is False and row["decision"]["reason_code"] == "laya_model"
    assert row["tools"] == [] and row["reply"] is None and row["timings"] is None


def test_環境變數覆寫路徑與關閉(monkeypatch, tmp_path):
    target = tmp_path / "x" / "t.jsonl"
    monkeypatch.setenv(tr.TRANSCRIPT_LOG_ENV, str(target))
    _console(tmp_path)._write_transcript("t1", "turn.completed", _opened(), "say", None)
    assert target.exists() and not (tmp_path / "e2e" / "transcript.jsonl").exists()
    monkeypatch.setenv(tr.TRANSCRIPT_LOG_ENV, "OFF")
    other = tmp_path / "y"
    _console(other)._write_transcript("t1", "turn.completed", _opened(), "say", None)
    assert not (other / "e2e" / "transcript.jsonl").exists()


def test_寫檔失敗只記警告不拋例外(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")  # 父路徑是檔案 -> mkdir／open 必失敗
    monkeypatch.setenv(tr.TRANSCRIPT_LOG_ENV, str(blocker / "transcript.jsonl"))
    with caplog.at_level("WARNING"):
        _console(tmp_path)._write_transcript("t1", "turn.completed", _opened(), "say", None)
    assert "對話紀錄寫檔失敗" in caplog.text


def test_蒐集資料失敗也只記警告(monkeypatch, tmp_path, caplog):
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(tr, "collect_tool_calls", boom)
    with caplog.at_level("WARNING"):
        _console(tmp_path)._write_transcript("t1", "turn.completed", _opened(), "say", None)
    assert "對話紀錄寫檔失敗" in caplog.text and not (tmp_path / "e2e" / "transcript.jsonl").exists()


class _Executor:
    """像 ReadToolExecutor：結果文字放 cache，receipt 只帶 result_ref。"""

    lookup_authoritative = False

    def __init__(self) -> None:
        self.cache = TurnOutcomeCache()

    def execute(self, call):
        self.cache.put(call.action_id, {"success": True, "result": "x" * 2000})
        return SimpleNamespace(action_id=call.action_id)

    def lookup_receipt(self, action_id):
        return "looked"


def test_tap記錄參數結果與耗時且不改變executor行為():
    executor = _Executor()
    tasks = SimpleNamespace(_executors={"web": executor})
    tap = tr.ToolCallTap()
    assert tap.install(tasks) == 1 and tap.install(tasks) == 1  # 重複安裝不會包兩層
    wrapped = tasks._executors["web"]
    assert wrapped.lookup_receipt("a") == "looked" and wrapped.lookup_authoritative is False
    call = SimpleNamespace(action_id="act_1", tool="web.search", params={"query": "q" * 500})
    assert wrapped.execute(call).action_id == "act_1"
    rec = tap.records["act_1"]
    assert rec["tool"] == "web.search" and rec["elapsed_ms"] >= 0 and len(rec["result_text"]) == 2000
    assert len(rec["params"]["query"]) < 400  # 參數截斷


def test_tap在executor拋例外時照樣上拋並記錄():
    class Boom:
        def execute(self, call):
            raise ValueError("x")

    tap = tr.ToolCallTap()
    tasks = SimpleNamespace(_executors={"b": Boom()})
    tap.install(tasks)
    with pytest.raises(ValueError):
        tasks._executors["b"].execute(SimpleNamespace(action_id="act_2", tool="t", params={}))
    assert tap.records["act_2"]["raised"] == "ValueError"


def test_web_search的provider與嘗試結果進紀錄():
    ok = json.dumps({
        "provider": "wikipedia", "fallback_used": False, "attempts": [
            {"provider": "duckduckgo", "outcome": "blocked"}, {"provider": "wikipedia", "outcome": "ok"}],
        "results": [{"title": "A", "url": "https://a.test/", "snippet": "s", "provider": "wikipedia"}],
    })
    detail = tr._search_detail({"query": "貓"}, ok)
    assert detail["provider"] == "wikipedia" and detail["hit_count"] == 1 and detail["query"] == "貓"
    assert [a["outcome"] for a in detail["attempts"]] == ["blocked", "ok"]
    failed = tr._search_detail({"query": "q"}, "所有搜尋來源都失敗或沒有結果（duckduckgo:blocked、wikipedia:empty）。")
    assert failed["provider"] is None and failed["hit_count"] == 0
    assert failed["attempts"] == [
        {"provider": "duckduckgo", "outcome": "blocked"}, {"provider": "wikipedia", "outcome": "empty"}]


def test_正式組裝不啟用():
    """除 ``uep/console`` 自己外，沒有任何模組 import ``uep.console``：正式平台的組裝既不寫 transcript 也不包 executor。"""
    offenders = []
    for path in UEP_ROOT.rglob("*.py"):
        if path.relative_to(UEP_ROOT).parts[0] == "console":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            if any(n == "uep.console" or n.startswith("uep.console.") for n in names):
                offenders.append(str(path.relative_to(UEP_ROOT)))
    assert offenders == []
