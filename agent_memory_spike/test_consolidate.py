"""Phase 2.8 關係閉包的測試。

`--transitive` 是純資料處理，不叫 LLM，所以它是這條管線裡少數能被完整測到的部分。
測的都是實測抓到的形狀：等價類沒閉合、矛盾沒沿等價類傳播。

執行：``python -m pytest agent_memory_spike/test_consolidate.py -q``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from consolidate import load_concepts, transitive  # noqa: E402


def _setup(tmp_path, concepts, pairs, verdicts):
    concept_path = tmp_path / "concepts.json"
    concept_path.write_text(json.dumps(concepts), encoding="utf-8")

    pair_path = tmp_path / "pairs.json"
    pair_path.write_text(json.dumps({"instructions": "", "pairs": [
        {"pair_id": pid, "left": {"id": left}, "right": {"id": right}}
        for pid, left, right in pairs
    ]}), encoding="utf-8")

    result_dir = tmp_path / "out"
    result_dir.mkdir()
    (result_dir / "batch-00.json").write_text(json.dumps({"verdicts": [
        {"pair_id": pid, "relation": relation, "keep": keep}
        for pid, relation, keep in verdicts
    ]}), encoding="utf-8")
    return result_dir, concept_path, pair_path


def _ids(path):
    return [c["id"] for c in load_concepts(path)]


def test_contradiction_propagates_through_duplicates(tmp_path):
    """A≡B 且 B 被 C 推翻 ⇒ A 也過期了。

    實測形狀：`c-138`/`c-617` 被判與 `c-711` 重複，而 `c-711` 被 `c-238` 推翻。
    兩兩配對抓不到「A 也過期」，因為 A 與 C 從來沒有被放在一起看過。
    """
    result_dir, concept_path, pair_path = _setup(
        tmp_path,
        concepts=[{"id": x, "statement": x} for x in ("A", "B", "C")],
        pairs=[("p-0", "A", "B"), ("p-1", "B", "C")],
        # keep=A 讓 B 在第一組就出局，A 於是留下來——正是逃過矛盾的那條
        verdicts=[("p-0", "DUPLICATE", "A"), ("p-1", "CONTRADICTION", "C")],
    )
    transitive(result_dir, concept_path, pair_path, apply_changes=True)
    assert _ids(concept_path) == ["C"]


def test_equivalence_class_keeps_only_one(tmp_path):
    """A≡B、B≡C 就代表 A≡C，等價類裡只該留一條。

    兩兩判定各自選 keep，選出不同贏家時同一件事會留下兩條——
    DUPLICATE 是等價關係，但配對本身不會自己閉合。
    """
    result_dir, concept_path, pair_path = _setup(
        tmp_path,
        concepts=[{"id": x, "statement": x} for x in ("A", "B", "C")],
        pairs=[("p-0", "A", "B"), ("p-1", "B", "C")],
        verdicts=[("p-0", "DUPLICATE", "A"), ("p-1", "DUPLICATE", "C")],
    )
    transitive(result_dir, concept_path, pair_path, apply_changes=True)
    assert len(_ids(concept_path)) == 1


def test_dry_run_changes_nothing(tmp_path):
    """預設只報告。刪 concept 是不可逆的，要動手得明講。"""
    result_dir, concept_path, pair_path = _setup(
        tmp_path,
        concepts=[{"id": x, "statement": x} for x in ("A", "B", "C")],
        pairs=[("p-0", "A", "B"), ("p-1", "B", "C")],
        verdicts=[("p-0", "DUPLICATE", "A"), ("p-1", "CONTRADICTION", "C")],
    )
    transitive(result_dir, concept_path, pair_path, apply_changes=False)
    assert _ids(concept_path) == ["A", "B", "C"]


def test_contradiction_inside_one_class_is_left_alone(tmp_path):
    """同一個等價類內部互相矛盾 = 判定自相牴觸，自動刪任何一邊都可能是錯的。"""
    result_dir, concept_path, pair_path = _setup(
        tmp_path,
        concepts=[{"id": x, "statement": x} for x in ("A", "B")],
        pairs=[("p-0", "A", "B"), ("p-1", "A", "B")],
        verdicts=[("p-0", "DUPLICATE", "A"), ("p-1", "CONTRADICTION", "B")],
    )
    transitive(result_dir, concept_path, pair_path, apply_changes=True)
    # 等價類收斂仍會發生（A、B 確實被判過重複），但矛盾不會再多刪一條
    assert len(_ids(concept_path)) == 1
