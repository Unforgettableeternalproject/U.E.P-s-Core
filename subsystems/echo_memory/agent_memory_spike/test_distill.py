"""蒸餾收料端的測試。

重點是 `scope` 的三態：蒸餾指示要求「跨專案通用則填 null」，所以
**填了 null 是明確表態，缺這個鍵才是沒說**。原本收料端用 `or` 把兩者
合併到 repo 那一邊，780 條原始輸出裡 47 條通用知識被靜默改標成單一 repo，
池子裡 global 的數量因此是精確的零。沒有這組測試，那個 bug 會回來。

執行：``python -m pytest agent_memory_spike/test_distill.py -q``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from distill import ingest, resolve_scope  # noqa: E402

TASK = {"repo": "Eternity"}


# --- resolve_scope：三態 ---------------------------------------------------

def test_explicit_null_stays_global():
    """填了 null = 蒸餾者說「這條跨專案通用」，必須保持 None。"""
    assert resolve_scope({"scope": None}, TASK) is None


def test_missing_key_falls_back_to_repo():
    """沒有這個鍵 = 沒說，退回觀察到它的 repo。

    這是與上一項相反的結果，兩者不可合併——這正是原本那個 `or` 的錯。
    """
    assert resolve_scope({}, TASK) == "Eternity"


def test_explicit_string_wins():
    assert resolve_scope({"scope": "AI-Website"}, TASK) == "AI-Website"


def test_blank_string_is_not_a_statement():
    """空字串不是表態，是填壞了——當成沒說，不可當成 global。"""
    assert resolve_scope({"scope": "   "}, TASK) == "Eternity"


def test_global_literals_normalize_to_none():
    """字串形式的 null 也是表態；正規化成 None，否則只有一條注入路徑認得。"""
    for literal in ("null", "None", "global", "*", "GLOBAL"):
        assert resolve_scope({"scope": literal}, TASK) is None, literal


def test_scope_is_trimmed():
    assert resolve_scope({"scope": " Eternity "}, TASK) == "Eternity"


# --- 端到端：收料寫進 concepts.json ----------------------------------------

def _run_ingest(tmp_path, concepts):
    task = {
        "id": "t-1",
        "repo": "Eternity",
        "source_turns": [["s1", 0]],
        "overlap_files": ["src/a.ts"],
    }
    (tmp_path / "tasks.json").write_text(
        json.dumps({"tasks": [task]}), encoding="utf-8")
    (tmp_path / "out.json").write_text(
        json.dumps([{"id": "t-1", "concepts": concepts}]), encoding="utf-8")

    concept_path = tmp_path / "concepts.json"
    ingest(tmp_path / "out.json", concept_path, tmp_path / "tasks.json",
           tmp_path / "watermark.json")
    return json.loads(concept_path.read_text(encoding="utf-8"))


def test_ingest_preserves_global_and_repo_side_by_side(tmp_path):
    """同一批裡兩種表態要各自保留，不能被收料端抹平成同一個 scope。"""
    result = _run_ingest(tmp_path, [
        {"statement": "通用的那條", "kind": "belief-correction", "scope": None},
        {"statement": "專案特有的那條", "kind": "project-fact"},
    ])
    by_statement = {c["statement"]: c["scope"] for c in result}
    assert by_statement["通用的那條"] is None
    assert by_statement["專案特有的那條"] == "Eternity"
