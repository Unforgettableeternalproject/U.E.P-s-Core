"""決策中樞行為基線評測集（characterization golden）的共用邏輯：載入、離線重跑、golden 讀寫。

規格與定位見 ``tests/uep/fixtures/decision_eval/README.md``。測試與重產腳本共用此模組，避免兩邊重跑方式不一致。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from uep.decision.replay_turn import rerun_candidates
from uep.turn.proposer import RespondProposer
from uep.turn.rule_proposer import RuleProposer

EVAL_DIR = Path(__file__).resolve().parent / "fixtures" / "decision_eval"
CANDIDATES_PATH = EVAL_DIR / "candidates.jsonl"
GOLDEN_PATH = EVAL_DIR / "golden.jsonl"
META_PATH = EVAL_DIR / "golden_meta.json"

# baseline（primary）在前、被評測的規則 proposer 在後；順序決定 rerun 的 role
PROPOSER_IDS = ("respond.minimal", "rules.default")
REQUIRED_FIELDS = (
    "id", "scenario", "lang", "candidate_id", "utterance_refs", "speakers", "ctx", "interaction_state",
    "interaction_version", "presence_active", "segments", "participant_count", "addressed_hint", "text",
)
CTX_FIELDS = (
    "conversation_id", "segment_id", "audience_scope", "session_ref", "scope_ref", "guild_id", "platform",
    "profile_actor_id",
)


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def engine_versions() -> dict[str, str]:
    """目前程式內 proposer 的 engine version 字串（寫進 golden_meta，供比對時判斷是否為同一版本）。"""
    return {"rules.default": RuleProposer.version, "respond.minimal": RespondProposer.version}


def run_eval(candidates: list[dict[str, Any]]) -> dict[str, dict[str, tuple[str | None, str | None]]]:
    """依 scenario 分組重跑（每組一組全新的 proposer，組內依檔案順序回放，狀態只在組內延續）。

    回傳 ``{proposer_id: {record_id: (outcome, reason_code)}}``。
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        groups[item["scenario"]].append(item)
    result: dict[str, dict[str, tuple[str | None, str | None]]] = {pid: {} for pid in PROPOSER_IDS}
    for items in groups.values():
        rows = rerun_candidates(items, PROPOSER_IDS)
        by_candidate = {item["candidate_id"]: item["id"] for item in items}
        for row in rows:
            result[row["proposer_id"]][by_candidate[row["candidate_id"]]] = (row["outcome"], row["reason_code"])
    return result


def current_snapshot() -> list[dict[str, Any]]:
    """目前行為的 golden 列：每筆候選一列，含各 proposer 的 outcome／reason_code。"""
    candidates = read_jsonl_file(CANDIDATES_PATH)
    result = run_eval(candidates)
    return [
        {
            "id": item["id"],
            **{
                pid: {"outcome": result[pid][item["id"]][0], "reason_code": result[pid][item["id"]][1]}
                for pid in PROPOSER_IDS
            },
        }
        for item in candidates
    ]
