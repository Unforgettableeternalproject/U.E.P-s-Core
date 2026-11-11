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


def test_turn_reply_definitions_are_shared_with_inference_schema() -> None:
    """訓練題目與 state 必須就是 uep/decision/laya_schema.py 的那一份（不另寫）。"""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "uep" / "decision" / "laya_schema.py"
    spec = importlib.util.spec_from_file_location("laya_schema_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert kinds.QUESTIONS_TURN_REPLY == module.QUESTIONS
    state = kinds.build_state_turn_reply({
        "message": "ok", "others": 2,
        "context": [{"speaker": "User", "text": "Book it?"}, {"speaker": "Assistant", "text": "Shall I book it?"}],
    })
    assert state == module.build_state_dict(
        assistant_name="Yuki", message="ok", participant_count=3,
        recent_context=module.format_recent_context("Book it?", "Shall I book it?"),
    )
    assert list(state) == ["assistant_name", "speaker", "other_people_present", "recent_context", "message"]
    assert state["assistant_name"] == "Yuki" and state["other_people_present"] == "2 other people present"
    assert state["recent_context"] == "User: Book it?\nAssistant: Shall I book it?"


def test_state_without_context_uses_shared_placeholders() -> None:
    state = kinds.build_state_turn_reply({"message": "hi"})
    assert state["recent_context"] == "(none)" and state["other_people_present"] == "none"


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
            it["context"] = [{"speaker": "Assistant", "text": f"Do you want that {i}?"}]
        items.append(it)
    return json.dumps(items)


def test_generate_schema_and_labels_from_cards() -> None:
    cards = (
        next(c for c in synth.TURN_REPLY_CARDS if c.card_id == "ack_after_solicit_chat"),
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
    ack = [r for r in rows if r["meta"]["card"] == "ack_after_solicit_chat"]
    assert ack and all(r["expected"] == {"reply_mode": "respond", "addressed": True, "route": "direct_chat"} for r in ack)
    assert all(r["state"]["recent_context"].startswith("Assistant:") for r in ack)
    side = [r for r in rows if r["meta"]["card"] == "group_side_talk"]
    assert all(r["expected"]["reply_mode"] == "pass" and r["meta"]["others"] in synth.GROUP and str(r["meta"]["others"]) in r["state"]["other_people_present"] for r in side)
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


def test_prompt_uses_placeholder_assistant_name() -> None:
    card = next(c for c in synth.TURN_REPLY_CARDS if c.card_id == "group_named_chat")
    prompt = synth.build_prompt(card, "mt_style", "weather", 3)
    assert "Yuki" in prompt and "U.E.P" not in prompt


# ---- 檔案式後端（匯出 prompt／匯入回應，不連網） ----

def _args(kind: str, out: Path, *extra: str) -> list[str]:
    return ["--kind", kind, "--max-samples", "12", "--per-call", "2", "--out", str(out), *extra]


def test_export_prompts_matches_plan_and_hides_labels(tmp_path: Path) -> None:
    exp = tmp_path / "prompts.jsonl"
    assert synth.main(_args("turn.reply", tmp_path / "unused.jsonl", "--export-prompts", str(exp))) == 0
    recs = split.read_rows(exp)
    plan = synth.plan_calls(synth.CARDS["turn.reply"], 12, 2, __import__("random").Random(0))
    assert [r["call_id"] for r in recs] == [f"turn.reply-{i:04d}" for i in range(len(plan))]
    assert [(r["card"], r["style"], r["n"]) for r in recs] == [(c.card_id, s, n) for c, s, _t, n in plan]
    for r in recs:
        assert r["system"] == synth.SYSTEM_PROMPT and "Yuki" in r["user"]
        assert "reply_mode" not in r["user"] and "route" not in r["user"]
    assert not (tmp_path / "unused.jsonl").exists()


def test_import_responses_matches_backend_path(tmp_path: Path) -> None:
    reply = json.dumps([{"message": "hello there"}, {"message": "what is the weather like today"}])
    exp = tmp_path / "p.jsonl"
    synth.main(_args("turn.reply", tmp_path / "x.jsonl", "--export-prompts", str(exp)))
    ids = [r["call_id"] for r in split.read_rows(exp)]
    resp = tmp_path / "r.jsonl"
    resp.write_text("".join(json.dumps({"call_id": i, "response": reply}) + "\n" for i in ids), encoding="utf-8")
    out = tmp_path / "out.jsonl"
    assert synth.main(_args("turn.reply", out, "--import-responses", str(resp))) == 0
    imported = split.read_rows(out)
    # 與 FakeBackend 路徑結果一致（相同回應、相同計畫）
    rows, stats = synth.generate("turn.reply", synth.FakeBackend([reply]), max_samples=12, per_call=2, seed=0)
    assert imported == rows
    assert stats["samples"] == len(imported) >= 1


def test_import_counts_missing_unparseable_and_unmatched(tmp_path: Path) -> None:
    good = json.dumps([{"message": "ok so that works"}])
    resp = tmp_path / "r.jsonl"
    resp.write_text(
        json.dumps({"call_id": "presence.absent_intent-0000", "response": good}) + "\n"
        + json.dumps({"call_id": "presence.absent_intent-0001", "response": "not json"}) + "\n"
        + json.dumps({"call_id": "presence.absent_intent-9999", "response": good}) + "\n",
        encoding="utf-8",
    )
    ids: list[str] = []
    plan_n = len(synth.plan_calls(synth.CARDS["presence.absent_intent"], 6, 2, __import__("random").Random(0)))
    planned = [synth.call_id_of("presence.absent_intent", i) for i in range(plan_n)]
    backend = synth.ReplayBackend(synth.load_responses(resp), planned)
    rows, stats = synth.generate("presence.absent_intent", backend, max_samples=6, per_call=2, seed=0, call_ids=ids)
    assert ids == planned and len(rows) == 1
    assert stats["rejected"]["unparseable"] == 1
    assert stats["rejected"]["backend_error:LookupError"] == plan_n - 2


def test_load_responses_rejects_duplicates_and_bad_rows(tmp_path: Path) -> None:
    dup = _write(tmp_path, [{"call_id": "a", "response": "x"}, {"call_id": "a", "response": "y"}])
    with pytest.raises(ValueError, match="重複"):
        synth.load_responses(dup)
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"call_id": "a"}) + "\n", encoding="utf-8")
    with pytest.raises(TypeError, match="call_id"):
        synth.load_responses(bad)


def test_export_and_import_are_mutually_exclusive(tmp_path: Path) -> None:
    assert synth.main(_args("turn.reply", tmp_path / "o.jsonl", "--export-prompts", "a", "--import-responses", "b")) == 2


# ---- 情境卡規則、去重、盲標流程 ----

def test_plan_rotates_styles_per_card_and_others_cover_zero_to_three_plus() -> None:
    import random

    cards = synth.CARDS["turn.reply"]
    plan = synth.plan_calls(cards, len(cards) * 4 * 4, 4, random.Random(0))
    by_card: dict[str, list[str]] = {}
    for c, s, _t, _n in plan:
        by_card.setdefault(c.card_id, []).append(s)
    assert all(set(v) == set(synth.STYLES) for v in by_card.values())
    seen = {synth.others_for(c, i, len(cards)) for i, (c, *_r) in enumerate(plan)}
    assert {0, 1, 2} <= seen and max(seen) >= 3


def test_topics_and_card_counts() -> None:
    assert len(set(synth.TOPICS)) >= 40
    assert synth.PER_CALL == 4
    ids = {c.card_id for c in synth.TURN_REPLY_CARDS}
    assert {"ack_after_solicit_chat", "ack_after_solicit_task", "ack_after_solicit_agent", "group_promise_update", "defer_busy_hands"} <= ids
    modes = [c.expected["reply_mode"] for c in synth.TURN_REPLY_CARDS]
    assert modes.count("passive") >= 3 and modes.count("defer") >= 4


def test_route_follows_side_effect_cards() -> None:
    by = {c.card_id: c.expected["route"] for c in synth.TURN_REPLY_CARDS}
    assert by["private_lookup"] == "direct_chat" and by["private_task"] == "task_request"
    assert by["ack_after_solicit_task"] == "task_request" and by["ack_after_solicit_agent"] == "agent_loop"


def test_guidance_reaches_prompt_and_absent_rules() -> None:
    tent = next(c for c in synth.TURN_REPLY_CARDS if c.card_id == "group_tentative_name")
    assert "is that you" in synth.build_prompt(tent, "clean", "pets", 2, 2)
    q = next(c for c in synth.ABSENT_CARDS if c.card_id == "absent_question_ability")
    prompt = synth.build_prompt(q, "clean", "pets", 1)
    assert "unmistakably" in prompt and "'she' only to mean the assistant" in prompt
    go = next(c for c in synth.ABSENT_CARDS if c.card_id == "absent_request_discord")
    assert "never 'come'" in synth.build_prompt(go, "clean", "pets", 1)


def test_dedupe_is_per_card_and_short_text_exact_only() -> None:
    d = synth.Deduper()
    assert d.add("ok sure") and not d.add("OK, sure!")
    assert d.add("sure go on")  # 短句不套 Jaccard
    long_a = "please set a timer for ten minutes for the pasta tonight"
    assert d.add(long_a) and not d.add(long_a + " thanks")
    card_a, card_b = synth.TURN_REPLY_CARDS[0], synth.TURN_REPLY_CARDS[1]
    reply = json.dumps([{"message": "hello there friend"}])
    rows, stats = synth.generate("turn.reply", synth.FakeBackend([reply]), max_samples=2, per_call=1, cards=(card_a, card_b))
    assert len(rows) == 2 and "duplicate" not in stats["rejected"]


def _blind_rows(tmp_path: Path) -> Path:
    cards = {c.card_id: c for c in synth.TURN_REPLY_CARDS}
    rows = []
    for i, (cid, msg) in enumerate([("private_task", "set a timer for ten minutes"), ("group_tentative_name", "that milk smells yucky"), ("private_filler", "hmm")]):
        c = cards[cid]
        rows.append(kinds.make_row(
            "turn.reply", {"message": msg, "others": 0}, dict(c.expected), sample_id=f"s{i}", source="synthetic",
            meta={"card": cid, "style": "clean", "hard": c.hard, **({"acceptable_modes": list(c.acceptable_modes)} if c.acceptable_modes else {})},
        ))
    path = tmp_path / "data.jsonl"
    synth.write_jsonl(path, rows)
    return path


def test_export_label_tasks_hides_labels(tmp_path: Path) -> None:
    data = _blind_rows(tmp_path)
    out = tmp_path / "tasks.jsonl"
    assert synth.main(["--export-label-tasks", str(data), "--out", str(out)]) == 0
    tasks = split.read_rows(out)
    assert [t["sample_id"] for t in tasks] == ["s0", "s1", "s2"]
    blob = json.dumps(tasks)
    for leak in ("private_task", "group_tentative_name", "hard", "expected", "card", "meta"):
        assert leak not in blob
    assert set(tasks[0]) == {"sample_id", "kind", "state", "questions"}
    assert set(tasks[0]["questions"]) == {"reply_mode", "route", "addressed"}


def test_import_labels_accept_dispute_and_acceptable_modes(tmp_path: Path) -> None:
    data = _blind_rows(tmp_path)
    labels = tmp_path / "labels.jsonl"
    good = {"reply_mode": "respond", "route": "task_request", "addressed": True}
    # s1：tentative_name 的 acceptable_modes 含 respond，落在範圍內算一致（addressed／route 不再比對）
    alt = {"reply_mode": "respond", "route": "direct_chat", "addressed": True}
    # s2：filler 被標成 respond，不一致
    bad = {"reply_mode": "respond", "route": "direct_chat", "addressed": True}
    labels.write_text("".join(json.dumps({"sample_id": s, "answers": a}) + "\n" for s, a in (("s0", good), ("s1", alt), ("s2", bad))), encoding="utf-8")
    out = tmp_path / "res"
    assert synth.main(["--import-labels", str(labels), "--data", str(data), "--out-dir", str(out)]) == 0
    accepted = split.read_rows(out / "accepted.jsonl")
    disputed = split.read_rows(out / "disputed.jsonl")
    assert [r["sample_id"] for r in accepted] == ["s0", "s1"]
    assert accepted[0]["meta"]["blind_check"] == "agree"
    assert [d["sample_id"] for d in disputed] == ["s2"]
    d = disputed[0]
    assert d["card"] == "private_filler" and d["card_answers"]["reply_mode"] == "pass" and d["blind_answers"]["reply_mode"] == "respond"
    stats = json.loads((out / "stats.json").read_text(encoding="utf-8"))
    assert stats["total"] == 3 and stats["accepted"] == 2 and stats["unlabeled"] == 0
    assert stats["per_card"]["private_filler"]["rate"] == 0.0 and stats["per_question"]["reply_mode"]["n"] == 3


def test_import_labels_missing_invalid_and_duplicate(tmp_path: Path) -> None:
    data = _blind_rows(tmp_path)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps({"sample_id": "s0", "answers": {"reply_mode": "shout", "route": "task_request"}}) + "\n", encoding="utf-8")
    out = tmp_path / "res"
    assert synth.main(["--import-labels", str(labels), "--data", str(data), "--out-dir", str(out)]) == 0
    stats = json.loads((out / "stats.json").read_text(encoding="utf-8"))
    assert stats["accepted"] == 0 and stats["unlabeled"] == 2
    d0 = split.read_rows(out / "disputed.jsonl")[0]
    assert d0["invalid"] == ["reply_mode"] and d0["missing"] == ["addressed"]
    labels.write_text(json.dumps({"sample_id": "s0", "answers": {}}) + "\n" + json.dumps({"sample_id": "s0", "answers": {}}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重複"):
        synth.main(["--import-labels", str(labels), "--data", str(data), "--out-dir", str(out)])


def test_import_responses_ignores_other_kind_ids(tmp_path: Path) -> None:
    reply = json.dumps([{"message": "hello there friend"}])
    resp = tmp_path / "r.jsonl"
    resp.write_text(json.dumps({"call_id": "presence.absent_intent-0000", "response": reply}) + "\n" + json.dumps({"call_id": "turn.reply-0000", "response": reply}) + "\n", encoding="utf-8")
    out = tmp_path / "o.jsonl"
    assert synth.main(["--kind", "turn.reply", "--max-samples", "1", "--per-call", "1", "--import-responses", str(resp), "--out", str(out)]) == 0
    assert len(split.read_rows(out)) == 1
