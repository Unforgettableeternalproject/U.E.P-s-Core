"""train/decision 資料工具測試：轉換器對映與壞資料、合成器 schema（fake backend，不打 API）、切分與防洩漏。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "train" / "decision"
sys.path.insert(0, str(_TOOLS))

from laya_data import convert_nlp, kinds, split, synth


def _bio(label: str, n: int) -> list[str]:
    return [f"B-{label}"] + [f"I-{label}"] * (n - 1)


def _write(tmp_path: Path, rows: list) -> Path:
    p = tmp_path / "in.jsonl"
    p.write_text("".join((json.dumps(r) if isinstance(r, dict) else r) + "\n" for r in rows), encoding="utf-8")
    return p


# ---- 轉換器 ----

@pytest.mark.parametrize(
    ("label", "route"),
    [("CHAT", "direct_chat"), ("DIRECT_WORK", "task_request"), ("BACKGROUND_WORK", "agent_loop")],
)
def test_label_mapping(label: str, route: str) -> None:
    assert convert_nlp.route_for({label}) == (route, None)


def test_call_is_ignored_when_mixed_with_work() -> None:
    assert convert_nlp.route_for({"CALL", "DIRECT_WORK"}) == ("task_request", None)
    assert convert_nlp.route_for({"CALL"}) == (None, "call_only_no_route")


def test_unknown_mixed_and_unmapped_dropped() -> None:
    assert convert_nlp.route_for({"UNKNOWN"})[1] == "unknown_label"
    assert convert_nlp.route_for({"CHAT", "DIRECT_WORK"})[1] == "mixed_routes"
    assert convert_nlp.route_for({"WEIRD"})[1] == "unmapped_label"


def test_convert_counts_bad_data_and_never_modifies_source(tmp_path: Path) -> None:
    good = {"id": "a", "text": "open the window", "tokens": ["open", "the", "window"], "bio_labels": _bio("DIRECT_WORK", 3)}
    bad_len = {"id": "b", "text": "hello there", "tokens": ["hello", "there"], "bio_labels": ["B-CHAT"]}
    bad_tag = {"id": "c", "text": "hi", "tokens": ["hi"], "bio_labels": ["X-CHAT"]}
    unknown = {"id": "d", "text": "asdf", "tokens": ["asdf"], "bio_labels": ["B-UNKNOWN"]}
    str_seg = {"id": "e", "text": "nice day", "tokens": ["nice", "day"], "bio_labels": _bio("CHAT", 2), "segments": ["nice day"]}
    path = _write(tmp_path, [good, bad_len, bad_tag, unknown, str_seg, "{not json"])
    before = path.read_bytes()
    rows, stats = convert_nlp.convert([path])
    assert path.read_bytes() == before
    assert {r["expected"]["route"] for r in rows} == {"task_request", "direct_chat"}
    assert stats["read"] == 6 and stats["kept"] == 2
    assert stats["dropped_by_reason"] == {
        "bad_bio_tag": 1, "bad_json": 1, "bio_length_mismatch": 1, "unknown_label": 1,
    }


def test_convert_duplicate_handling(tmp_path: Path) -> None:
    a = {"id": "1", "text": "Open it", "tokens": ["Open", "it"], "bio_labels": _bio("DIRECT_WORK", 2)}
    same = {**a, "id": "2", "text": "open it"}
    conflict = {"id": "3", "text": "OPEN IT", "tokens": ["OPEN", "IT"], "bio_labels": _bio("CHAT", 2)}
    keep = {"id": "4", "text": "Nice song", "tokens": ["Nice", "song"], "bio_labels": _bio("CHAT", 2)}
    rows, stats = convert_nlp.convert([_write(tmp_path, [a, same, conflict, keep])])
    assert [r["state"]["message"] for r in rows] == ["Nice song"]
    assert stats["dropped_by_reason"]["duplicate_conflict"] == 2


def test_converted_row_matches_laya_train_schema(tmp_path: Path) -> None:
    good = {"id": "a", "text": "open the window", "tokens": ["open", "the", "window"], "bio_labels": _bio("DIRECT_WORK", 3)}
    rows, _ = convert_nlp.convert([_write(tmp_path, [good])])
    _assert_laya_row(rows[0])
    assert list(rows[0]["questions"]) == ["route"] and rows[0]["expected"] == {"route": "task_request"}


def _assert_laya_row(row: dict) -> None:
    """laya-train 的列格式：state／questions／expected，expected 的題目必須都在 questions 內且值合法。"""
    assert isinstance(row["state"], (str, dict)) and row["state"]
    assert set(row["expected"]) <= set(row["questions"])
    for qid, q in row["questions"].items():
        assert q["type"] in {"choice", "noul", "score"} and q["instructions"].strip()
        if q["type"] == "choice":
            assert row["expected"][qid] in q["criteria"]
        if q["type"] == "noul":
            assert isinstance(row["expected"][qid], bool)
    json.dumps(row, ensure_ascii=False)  # 可序列化


# ---- kinds ----

def test_make_row_rejects_bad_expected() -> None:
    with pytest.raises(ValueError, match="reply_mode"):
        kinds.make_row("turn.reply", {"message": "hi"}, {"reply_mode": "shout"}, sample_id="x", source="t")
    with pytest.raises(ValueError, match="target_location"):
        kinds.make_row(
            "presence.absent_intent", {"message": "hi"},
            {"absent.intent": "other", "absent.target_location": "discord"}, sample_id="x", source="t",
        )
    with pytest.raises(ValueError, match="尚未實作"):
        kinds.get_spec("memory.dream")


def test_state_has_no_actor_fields_for_absent_intent() -> None:
    state = kinds.build_state_absent({"message": "come over", "source_platform": "discord", "is_owner": True, "actor_ref": "discord:1"})
    assert set(state) == {"assistant_name", "source_platform", "message"}


def test_state_is_english_and_key_order_stable() -> None:
    state = kinds.build_state_turn_reply({
        "message": "ok", "others": 2, "context": [{"speaker": "U.E.P", "text": "Shall I book it?"}],
    })
    assert list(state) == ["assistant_name", "speaker", "other_people_present", "recent_context", "message"]
    assert state["other_people_present"] == "2 other(s) present"
    assert state["recent_context"] == "U.E.P: Shall I book it?"


def test_question_ids_are_unique_across_kinds() -> None:
    seen: dict[str, str] = {}
    for spec in kinds.KIND_SPECS.values():
        for qid in spec.questions:
            assert qid not in seen, f"{qid} 同時出現在 {seen[qid]} 與 {spec.kind}"
            seen[qid] = spec.kind


# ---- 合成器（fake backend）----

def _fake_reply(n: int, *, context: bool = False, prefix: str = "item") -> str:
    items = []
    for i in range(n):
        it: dict = {"message": f"{prefix} number {i} about something distinct {i * 7}"}
        if context:
            it["context"] = [{"speaker": "U.E.P", "text": f"Do you want that {i}?"}]
        items.append(it)
    return json.dumps(items)


def test_generate_schema_and_labels_from_cards() -> None:
    cards = (
        next(c for c in synth.TURN_REPLY_CARDS if c.card_id == "ack_after_solicit"),
        next(c for c in synth.TURN_REPLY_CARDS if c.card_id == "group_side_talk"),
    )
    backend = synth.FakeBackend([_fake_reply(4, context=True, prefix="alpha"), "```json\n" + _fake_reply(4, prefix="beta") + "\n```"])
    rows, stats = synth.generate("turn.reply", backend, max_samples=8, per_call=4, cards=cards)
    assert backend.calls == stats["api_calls"] == 2
    assert len(rows) == 8
    for r in rows:
        _assert_laya_row(r)
        assert r["source"] == "synthetic" and r["lang"] == "en"
        assert set(r["expected"]) == {"reply_mode", "addressed", "route"}
        assert r["meta"]["group_id"]
    ack = [r for r in rows if r["meta"]["card"] == "ack_after_solicit"]
    assert ack and all(r["expected"] == {"reply_mode": "respond", "addressed": True, "route": "direct_chat"} for r in ack)
    assert all(r["state"]["recent_context"].startswith("U.E.P:") for r in ack)
    side = [r for r in rows if r["meta"]["card"] == "group_side_talk"]
    assert all(r["expected"]["reply_mode"] == "pass" and r["state"]["other_people_present"] != "none" for r in side)
    assert stats["hard"] == len(ack)


def test_generate_rejects_bad_output_and_dedupes() -> None:
    card = next(c for c in synth.TURN_REPLY_CARDS if c.card_id == "private_chat")
    reply = json.dumps([
        {"message": "I really like the cold rain today in the city"},
        {"message": "i really like the cold rain today in the city!"},  # 正規化後重複
        {"message": "I really like the cold rain today in the city again"},  # Jaccard 近似重複 (9/10)
        {"message": "今天天氣很好"},  # CJK
        {"message": ""},
        {"message": "x" * 400},
        {"msg": "wrong key"},
        {"message": "Totally different sentence about cooking pasta"},
    ])
    rows, stats = synth.generate("turn.reply", synth.FakeBackend([reply]), max_samples=8, per_call=8, cards=(card,))
    assert [r["state"]["message"] for r in rows] == ["I really like the cold rain today in the city", "Totally different sentence about cooking pasta"]
    assert stats["rejected"] == {
        "bad_item": 1, "contains_cjk": 1, "duplicate": 2, "empty_message": 1, "too_long": 1,
    }


def test_generate_context_card_requires_context_and_unparseable_counted() -> None:
    card = next(c for c in synth.TURN_REPLY_CARDS if c.card_id == "ack_after_statement")
    rows, stats = synth.generate("turn.reply", synth.FakeBackend(['[{"message": "ok"}]', "not json at all"]), max_samples=2, per_call=1, cards=(card,))
    assert rows == []
    assert stats["rejected"] == {"bad_context": 1, "unparseable": 1}


def test_backend_errors_do_not_abort() -> None:
    class Boom:
        def complete(self, system: str, user: str) -> str:
            raise RuntimeError("boom")

    rows, stats = synth.generate("turn.reply", Boom(), max_samples=3, per_call=3)
    assert rows == [] and stats["rejected"] == {"backend_error:RuntimeError": 1}


def test_absent_cards_generate_valid_rows() -> None:
    backend = synth.FakeBackend([_fake_reply(2, prefix="gamma")])
    rows, _ = synth.generate("presence.absent_intent", backend, max_samples=2, per_call=2, cards=synth.ABSENT_CARDS[:1])
    assert rows and all(set(r["state"]) == {"assistant_name", "source_platform", "message"} for r in rows)
    for r in rows:
        _assert_laya_row(r)


def test_card_labels_are_valid_for_their_kind() -> None:
    for kind, cards in synth.CARDS.items():
        spec = kinds.get_spec(kind)
        for c in cards:
            spec.validate_expected(c.expected)
            assert c.kind == kind


def test_prompt_never_leaks_labels() -> None:
    for kind, cards in synth.CARDS.items():
        for c in cards:
            prompt = synth.build_prompt(c, "clean", "weather", 3)
            for forbidden in ("reply_mode", "task_request", "agent_loop", "presence_request"):
                assert forbidden not in prompt, (kind, c.card_id, forbidden)


def test_dry_run_does_not_need_key_and_cap_enforced(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("UEP_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    out = str(tmp_path / "o.jsonl")
    assert synth.main(["--out", out, "--max-samples", "50"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert synth.main(["--out", out, "--max-samples", "500"]) == 2
    with pytest.raises(RuntimeError):
        synth.OpenAIBackend()


# ---- 切分與防洩漏 ----

def test_split_is_group_stable_and_deterministic() -> None:
    rows = [{"sample_id": f"s{i}", "meta": {"group_id": f"g{i // 5}"}} for i in range(200)]
    t1, v1 = split.split_rows(rows, 0.2, seed=1)
    _, v2 = split.split_rows(rows, 0.2, seed=1)
    assert [r["sample_id"] for r in v1] == [r["sample_id"] for r in v2]
    assert {r["meta"]["group_id"] for r in t1}.isdisjoint({r["meta"]["group_id"] for r in v1})
    assert t1 and v1


def test_overlap_detection_exact_and_near() -> None:
    train = ["Please turn off the living room light", "What a lovely day today", "Play some jazz music for me now"]
    test = ["please, turn off the living-room light!", "Play some jazz music for me now please", "Completely unrelated sentence here"]
    hits = split.find_overlaps(train, test)
    kinds_hit = {(h[0], h[1]) for h in hits}
    assert ("Please turn off the living room light", "please, turn off the living-room light!") in kinds_hit
    assert ("Play some jazz music for me now", "Play some jazz music for me now please") in kinds_hit
    assert not any("lovely" in h[0] for h in hits)


def test_check_command_exit_code(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    test = tmp_path / "test.jsonl"
    train.write_text(json.dumps({"state": {"message": "Open the window"}, "sample_id": "a"}) + "\n", encoding="utf-8")
    test.write_text(json.dumps({"text": "open the window."}) + "\n", encoding="utf-8")
    assert split.main(["check", "--train", str(train), "--test", str(test)]) == 1
    test.write_text(json.dumps({"text": "Something else entirely"}) + "\n", encoding="utf-8")
    assert split.main(["check", "--train", str(train), "--test", str(test)]) == 0


def test_testset_draft_is_wellformed_and_pending() -> None:
    path = _TOOLS / "testset" / "zh_candidates_draft.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert 55 <= len(rows) <= 65
    assert len({r["id"] for r in rows}) == len(rows)
    assert all(r["status"] == "pending_review" for r in rows)
    for r in rows:
        kinds.get_spec(r["kind"]).validate_expected(r["intended"])
