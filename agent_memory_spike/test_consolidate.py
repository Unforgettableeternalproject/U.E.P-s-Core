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

from consolidate import _judged_key_set, load_concepts, panel, transitive  # noqa: E402


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


def _panel_setup(tmp_path, pairs, *judges):
    """建一份配對檔與 N 位評審的判定目錄。"""
    pair_path = tmp_path / "pairs.json"
    pair_path.write_text(json.dumps({"instructions": "", "pairs": [
        {"pair_id": pid, "similarity": 0.75, "scope": "s",
         "left": {"id": left, "statement": left, "anchors": [], "source_turns": []},
         "right": {"id": right, "statement": right, "anchors": [], "source_turns": []}}
        for pid, left, right in pairs
    ]}), encoding="utf-8")

    dirs = []
    for index, verdicts in enumerate(judges):
        judge_dir = tmp_path / f"judge{index}"
        judge_dir.mkdir()
        (judge_dir / "batch-00.json").write_text(json.dumps({"verdicts": [
            {"pair_id": pid, "relation": relation, "keep": keep, "why": "因為"}
            for pid, relation, keep in verdicts
        ]}), encoding="utf-8")
        dirs.append(judge_dir)
    return pair_path, dirs


def _panel_run(tmp_path, pairs, *judges):
    pair_path, dirs = _panel_setup(tmp_path, pairs, *judges)
    out_dir = tmp_path / "panel"
    panel(dirs, pair_path, out_dir)
    return (json.loads((out_dir / "consensus.json").read_text(encoding="utf-8"))["verdicts"],
            json.loads((out_dir / "disputed_pairs.json").read_text(encoding="utf-8"))["pairs"])


def test_panel_agreement_becomes_consensus(tmp_path):
    """兩票一致就結案，不必再花仲裁成本。"""
    consensus, disputed = _panel_run(
        tmp_path,
        [("p-0", "A", "B")],
        [("p-0", "CONTRADICTION", "B")],
        [("p-0", "CONTRADICTION", "B")],
    )
    assert disputed == []
    assert consensus[0]["relation"] == "CONTRADICTION"
    assert consensus[0]["keep"] == "B"


def test_panel_disagreement_goes_to_arbitration(tmp_path):
    """一票 DISTINCT、一票 CONTRADICTION —— 實測的分歧就是這個方向。

    這種組不能自動採信任何一邊：採信 DISTINCT 會漏掉過期記憶，
    採信 CONTRADICTION 會刪掉仍然成立的事實，兩者都是實質損失。
    """
    consensus, disputed = _panel_run(
        tmp_path,
        [("p-0", "A", "B")],
        [("p-0", "DISTINCT", None)],
        [("p-0", "CONTRADICTION", "B")],
    )
    assert consensus == []
    assert [p["pair_id"] for p in disputed] == ["p-0"]
    # 仲裁者要看得到雙方的票，否則它只是再擲一次骰子
    assert {v["relation"] for v in disputed[0]["votes"]} == {"DISTINCT", "CONTRADICTION"}


def test_panel_same_relation_different_keep_is_disputed(tmp_path):
    """兩票都判矛盾但選了不同的存活者 —— 刪哪一條是不可逆的，不能亂猜。"""
    consensus, disputed = _panel_run(
        tmp_path,
        [("p-0", "A", "B")],
        [("p-0", "CONTRADICTION", "A")],
        [("p-0", "CONTRADICTION", "B")],
    )
    assert consensus == []
    assert len(disputed) == 1


def test_panel_three_way_split_defaults_to_not_deleting(tmp_path):
    """三票全異 = 沒有任何兩個人看到同一件事，此時不刪才是安全的。"""
    consensus, disputed = _panel_run(
        tmp_path,
        [("p-0", "A", "B")],
        [("p-0", "DISTINCT", None)],
        [("p-0", "DUPLICATE", "A")],
        [("p-0", "CONTRADICTION", "B")],
    )
    assert disputed == []
    assert consensus[0]["relation"] == "DISTINCT"
    assert consensus[0]["keep"] is None


def test_panel_majority_of_three_wins(tmp_path):
    """三票 2:1 走多數決。"""
    consensus, _ = _panel_run(
        tmp_path,
        [("p-0", "A", "B")],
        [("p-0", "DISTINCT", None)],
        [("p-0", "CONTRADICTION", "B")],
        [("p-0", "CONTRADICTION", "B")],
    )
    assert consensus[0]["relation"] == "CONTRADICTION"


def test_panel_skips_pairs_a_judge_missed(tmp_path):
    """有評審沒判到的組不算共識也不算爭議——當成已判會靜默漏掉一整組。"""
    consensus, disputed = _panel_run(
        tmp_path,
        [("p-0", "A", "B"), ("p-1", "C", "D")],
        [("p-0", "DISTINCT", None), ("p-1", "CONTRADICTION", "D")],
        [("p-0", "DISTINCT", None)],
    )
    assert [v["pair_id"] for v in consensus] == ["p-0"]
    assert disputed == []


def test_skip_judged_only_counts_pairs_that_were_actually_decided(tmp_path):
    """配對檔裡有 2 組、判定目錄只判了 1 組 —— 只有那 1 組算判過。

    實測形狀：`consolidate_pairs_band.json` 有數百組配對，
    而 `consolidate_out_band/` 只判了 30 組。拿整份配對檔當「判過」，
    會把從未判過的組靜默略過。
    """
    pair_path, dirs = _panel_setup(
        tmp_path,
        [("p-0", "A", "B"), ("p-1", "C", "D")],
        [("p-0", "DISTINCT", None)],
    )
    judged = _judged_key_set([Path(f"{pair_path}::{dirs[0]}")])
    assert judged == {frozenset(("A", "B"))}


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
