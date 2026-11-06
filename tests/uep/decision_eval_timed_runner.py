"""決策中樞行為基線評測集的「時間相關」情境（DL-2b）：事件腳本載入、假 clock 回放、golden 讀寫。

規格見 ``tests/uep/fixtures/decision_eval/README.md`` 的 timed 段落。只針對 ``rules.default``（RuleProposer）；
``respond.minimal`` 無時間狀態（無條件 respond），不納入。同 scenario 依序回放並共用一個 proposer 與假 clock，
不同 scenario 各用全新的。測試與重產腳本共用此模組。
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tests.uep.decision_eval_runner import EVAL_DIR, read_jsonl_file
from uep.decision.replay_turn import candidate_from_dict
from uep.turn.models import TurnOutcomeFacts
from uep.turn.rule_proposer import RuleProposer

TIMED_SCRIPTS_PATH = EVAL_DIR / "timed_scripts.jsonl"
TIMED_GOLDEN_PATH = EVAL_DIR / "timed_golden.jsonl"
META_PATH = EVAL_DIR / "golden_meta.json"

EVENT_TYPES = ("candidate", "advance", "tick", "turn_started", "turn_outcome")
BASE_FIELDS = ("scenario", "id", "lang", "note", "type")
OUTCOME_FIELDS = (
    "turn_id", "candidate_id", "speakers", "reply_mode", "reason_code", "status", "end_reason", "generation_ok",
    "barge_in", "started",
)
CLOCK_START = 1000.0  # 假 clock 的起點（每個 scenario 重設）


class FakeClock:
    """可手動推進的假 clock；RuleProposer 內部的耦合度、上下文窗口、engagement 共用同一個。"""

    def __init__(self, start: float = CLOCK_START) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def read_scripts() -> list[dict[str, Any]]:
    return read_jsonl_file(TIMED_SCRIPTS_PATH)


def read_golden(path: Path = TIMED_GOLDEN_PATH) -> list[dict[str, Any]]:
    return read_jsonl_file(path)


def _interjection_row(item: Any) -> dict[str, Any]:
    return {
        "reason_code": item.proposal.reason_code,
        "reply_mode": item.proposal.reply_mode.value,
        "speaker_ref": item.speaker_ref,
        "speakers": list(item.speakers),
        "utterance_refs": list(item.utterance_refs),
    }


def replay_scenario(events: Iterable[dict[str, Any]], *, skip_advance: bool = False) -> list[dict[str, Any]]:
    """以全新 proposer 與假 clock 依序回放一個 scenario，回傳 candidate／tick 事件的結果列。

    ``skip_advance=True`` 忽略所有 advance（clock 不動），用於對照「時間沒推進時結果是否不同」。
    """
    clock = FakeClock()
    proposer = RuleProposer(clock=clock)
    rows: list[dict[str, Any]] = []
    for event in events:
        kind = event["type"]
        if kind == "candidate":
            proposal = proposer.propose(candidate_from_dict(event))
            rows.append({
                "id": event["id"], "type": "candidate",
                "outcome": proposal.reply_mode.value, "reason_code": proposal.reason_code,
            })
        elif kind == "advance":
            if not skip_advance:
                clock.advance(float(event["seconds"]))
        elif kind == "tick":
            rows.append({
                "id": event["id"], "type": "tick",
                "interjections": [_interjection_row(item) for item in proposer.tick()],
            })
        elif kind == "turn_started":
            proposer.on_turn_started(event["turn_id"], event["candidate_id"], tuple(event["speakers"]))
        elif kind == "turn_outcome":
            facts = TurnOutcomeFacts(**{
                k: (tuple(event[k]) if k == "speakers" else event[k]) for k in OUTCOME_FIELDS if k in event
            })
            proposer.on_turn_outcome(facts)
        else:
            raise ValueError(f"未知事件類型 {kind!r}（{event.get('id')}）")
    return rows


def group_by_scenario(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        groups[event["scenario"]].append(event)
    return groups


def run_timed(events: list[dict[str, Any]], *, skip_advance: bool = False) -> dict[str, list[dict[str, Any]]]:
    """每個 scenario 各用全新 proposer 與假 clock 回放。回傳 ``{scenario: 結果列}``。"""
    return {
        name: replay_scenario(items, skip_advance=skip_advance) for name, items in group_by_scenario(events).items()
    }


def current_timed_snapshot() -> list[dict[str, Any]]:
    """目前行為的 timed golden 列（依腳本檔順序，scenario 依首次出現順序）。"""
    return [row for rows in run_timed(read_scripts()).values() for row in rows]
