"""能力清單（state.available_capabilities）的資料處理：預設清單、成對樣本標籤、群組未點名缺能力時 pass、近似重複比對。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "train" / "decision"
sys.path.insert(0, str(_TOOLS))

from laya_data import capabilities as cap
from laya_data import leak_filter
from laya_data.kinds import get_spec, laya_schema, make_row


def _row(sid: str, message: str, *, others: int = 0, context: list | None = None, expected: dict | None = None, card: str = "private_task") -> dict[str, Any]:
    facts: dict[str, Any] = {"message": message, "others": others}
    if context:
        facts["context"] = context
    exp = expected or {"reply_mode": "respond", "addressed": True, "route": "task_request", "compound": False}
    return make_row("turn.reply", facts, exp, sample_id=sid, source="synthetic", meta={"card": card, "group_id": sid})


def test_default_state_has_default_capabilities_without_device_control() -> None:
    row = _row("a", "Turn off the porch light.")
    assert row["state"]["available_capabilities"] == laya_schema.format_capabilities()
    assert "device_control" not in row["state"]["available_capabilities"]
    assert list(row["state"])[-2:] == ["available_capabilities", "message"]


def test_device_request_gets_pair_with_and_without_device_control() -> None:
    sid = "synthetic-en-turn.reply-00014"
    rows, stats = cap.apply_capabilities([_row(sid, "Yuki, turn off the garden sprinkler.")])
    by_id = {r["sample_id"]: r for r in rows}
    with_dev, twin = by_id[sid], by_id[sid + "-nodev"]
    assert "device_control" in with_dev["state"]["available_capabilities"]
    assert with_dev["expected"]["route"] == "task_request"
    assert "device_control" not in twin["state"]["available_capabilities"]
    assert (twin["expected"]["reply_mode"], twin["expected"]["addressed"], twin["expected"]["route"]) == ("respond", True, "direct_chat")
    assert stats["device_pairs"] == 1 and twin["meta"]["pair_of"] == sid


def test_missing_capability_in_group_without_name_or_context_is_pass() -> None:
    private = _row("p", "Turn off the lights.", others=0)
    group = _row("g", "Turn off the lights.", others=2)
    named = _row("n", "Yuki, turn off the lights.", others=2)
    followup = _row("f", "Turn off the lights.", others=2, context=[{"speaker": "User", "text": "Hi"}, {"speaker": "Assistant", "text": "Hello"}])
    assert cap.no_capability_label(private, private["expected"])["reply_mode"] == "respond"
    assert cap.no_capability_label(named, named["expected"])["route"] == "direct_chat"
    assert cap.no_capability_label(followup, followup["expected"])["addressed"] is True
    out = cap.no_capability_label(group, group["expected"])
    assert (out["reply_mode"], out["addressed"], out["route"]) == ("pass", False, "ignore")


def test_assistant_offer_of_physical_action_adds_device_control() -> None:
    ctx = [{"speaker": "Assistant", "text": "Shall I turn off the living room lights now?"}]
    row = _row("synthetic-en-turn.reply-90001", "ok", context=ctx, card="ack_after_solicit_task")
    rows, stats = cap.apply_capabilities([row])
    assert "device_control" in rows[0]["state"]["available_capabilities"] and stats["assistant_device_context"] == 1
    # 音量、計時器這類系統內動作不算實體裝置
    ctx2 = [{"speaker": "Assistant", "text": "Shall I turn down the volume?"}]
    rows2, _ = cap.apply_capabilities([_row("synthetic-en-turn.reply-90002", "ok", context=ctx2, card="ack_after_solicit_task")])
    assert "device_control" not in rows2[0]["state"]["available_capabilities"]


def test_random_capability_lists_never_drop_needed_or_query_and_keep_labels() -> None:
    rows = [_row(f"unit-{i:05d}", f"Yuki, send Dana a text number {i}.") for i in range(300)]
    out, stats = cap.apply_capabilities(rows)
    assert stats["random_device"] > 0 and stats["random_drop"] > 0
    for r in out:
        if r["meta"].get("pair_of"):
            continue  # 成對樣本刻意拿掉該項能力
        caps = cap.caps_of(r)
        assert "query" in caps and "messaging" in caps  # 句子需要訊息能力、query 不抽掉
        assert r["expected"]["route"] == "task_request"  # 隨機清單不改標籤


def test_fuzzy_near_duplicate_catches_typos_and_ignores_different_sentences() -> None:
    a = _row("a", "Please remind me to water all of the tomato plants in the garden at six tonight.")
    b = _row("b", "Please remind me to water all of teh tomato plants in the garden at six tonigth.")
    c = _row("c", "Send the quarterly report to the whole finance team.")
    pairs = leak_filter.fuzzy_pairs([a, b, c])
    assert [(i, j) for i, j, _ in pairs] == [(0, 1)]


def test_schema_rejects_unknown_capability_and_keeps_fixed_order() -> None:
    with pytest.raises(ValueError):
        laya_schema.format_capabilities(["teleport"])
    assert laya_schema.format_capabilities(["timer_alarm", "query"]) == "query, timer_alarm"
    assert set(get_spec("turn.reply").questions) >= {"reply_mode", "route", "addressed"}
