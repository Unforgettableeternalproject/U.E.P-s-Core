"""``advice_in_scope``（CN-2 迴圈側再驗）：舊判定（形狀＋modalities）加上 consult 標籤條件。離線、純函式。"""
from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any

import pytest

from uep.decision.loop_consult import (
    ADVICE_SECTION,
    CONSULT_TAG,
    SUB_KINDS,
    advice_in_scope,
)


def _legacy_recheck(sub_kind: str, advice: Mapping[str, Any] | None,
                    frozen_modalities: frozenset[str], now_modalities: frozenset[str]) -> str | None:
    """迴圈改呼叫 advice_in_scope 之前的判定（b5c821b loop.py `_recheck_escalation` 的 advice 段），逐字保留作對照。"""
    if advice is None:
        return None
    section = ADVICE_SECTION.get(sub_kind)
    if sub_kind == "placement" or section is None or set(advice) != {section}:
        return "scope_exceeded"
    if sub_kind == "output_modality":
        allowed = frozen_modalities & now_modalities
        if not set((advice.get("output") or {}).get("modalities") or ()) <= allowed:
            return "scope_exceeded"
    return None


_F = {"capability_tags": {CONSULT_TAG}}  # 只有 consult 標籤、沒有 modalities 鍵的範圍
_SUB_KINDS = (*SUB_KINDS, "", "unknown")
_ADVICES: tuple[Mapping[str, Any] | None, ...] = (
    None,
    {},
    {"reply": {"mode": "respond"}},
    {"loop": {"outcome": "finalize"}},
    {"followups": [{"kind": "completion_report"}]},
    {"placement": {"platform": "desktop"}},
    {"output": None},
    {"output": {}},
    {"output": {"modalities": []}},
    {"output": {"modalities": ["text"]}},
    {"output": {"modalities": ["text", "audio_stream"]}},
    {"output": {"modalities": ["animation"]}},
    {"output": {"modalities": ["text"]}, "reply": {"mode": "respond"}},
)
_SETS = (frozenset(), frozenset({"text"}), frozenset({"text", "audio_stream"}), frozenset({"animation", "typing"}))


_TAG_SETS = (frozenset({CONSULT_TAG}), frozenset({CONSULT_TAG, "memory.write"}))


@pytest.mark.parametrize("sub_kind", _SUB_KINDS)
def test_equivalent_to_legacy_recheck_when_consult_tag_in_intersection(sub_kind: str) -> None:
    # consult 標籤在交集內時，結果必須與舊判定逐輸入一致
    for advice, frozen, now in itertools.product(_ADVICES, _SETS, _SETS):
        expected = _legacy_recheck(sub_kind, advice, frozen, now)
        for tags in _TAG_SETS:
            got = advice_in_scope(
                sub_kind, advice, {"modalities": frozen, "capability_tags": tags}, {"modalities": now, "capability_tags": tags},
            )
            assert got == expected, (sub_kind, advice, frozen, now, tags)


@pytest.mark.parametrize("sub_kind", _SUB_KINDS)
def test_consult_tag_outside_intersection_or_missing_key_escalates(sub_kind: str) -> None:
    has, lacks = frozenset({CONSULT_TAG}), frozenset({"memory.write"})
    for advice, frozen, now in itertools.product(_ADVICES, _SETS, _SETS):
        mods = {"modalities": frozen}, {"modalities": now}
        cases = (
            ({**mods[0], "capability_tags": lacks}, {**mods[1], "capability_tags": has}),  # 凍結沒有
            ({**mods[0], "capability_tags": has}, {**mods[1], "capability_tags": lacks}),  # 當下投影沒有
            ({**mods[0], "capability_tags": lacks}, {**mods[1], "capability_tags": lacks}),
            (mods[0], {**mods[1], "capability_tags": has}),  # 凍結缺鍵 fail-closed
            ({**mods[0], "capability_tags": has}, mods[1]),  # 快照缺鍵 fail-closed
            (mods[0], mods[1]),
        )
        for fz, sn in cases:
            got = advice_in_scope(sub_kind, advice, fz, sn)
            assert got == (None if advice is None else "scope_exceeded"), (sub_kind, advice, fz, sn)


def test_none_advice_passes() -> None:
    assert advice_in_scope("output_modality", None, {}, {}) is None  # 沒有建議就沒有越權（連缺鍵也通過）


def test_unknown_always_escalates() -> None:
    assert advice_in_scope("bogus", {"reply": {}}, _F, _F) == "scope_exceeded"


_PF = {"capability_tags": {CONSULT_TAG}, "platforms": {"pc", "phone"}}
_PS = {"capability_tags": {CONSULT_TAG}, "platforms": {"pc"}, "current_platform": "pc", "executors": {"exec.a", "exec.b"}}
_PLACEMENT = {"target_platform": "pc", "executor_ref": "exec.a", "presence_intent": "stay"}


def _placement(**over: Any) -> dict[str, Any]:
    return {"placement": {**_PLACEMENT, **over}}


def test_placement_same_platform_executor_passes() -> None:
    assert advice_in_scope("placement", _placement(), _PF, _PS) is None
    assert advice_in_scope("placement", _placement(presence_intent="none"), _PF, _PS) is None
    placement = {k: v for k, v in _PLACEMENT.items() if k != "presence_intent"}  # 缺省視為 none
    assert advice_in_scope("placement", {"placement": placement}, _PF, _PS) is None
    assert advice_in_scope("placement", _placement(audience_scope_max=None), _PF, {**_PS, "executors": ["exec.a"]}) is None


@pytest.mark.parametrize("over", [
    {"target_platform": "phone"},  # 跨平台（不等於 current_platform）
    {"target_platform": "tv"},
    {"target_platform": None}, {"target_platform": 1},
    {"executor_ref": "exec.zzz"}, {"executor_ref": ""}, {"executor_ref": None}, {"executor_ref": "Bad Ref"},
    {"presence_intent": "request_move"}, {"presence_intent": "x"},
    {"audience_scope_max": "public"},
])
def test_placement_out_of_scope_values_escalate(over: dict[str, Any]) -> None:
    assert advice_in_scope("placement", _placement(**over), _PF, _PS) == "scope_exceeded"


def test_placement_missing_or_bad_snapshot_keys_escalate() -> None:
    for key in ("current_platform", "executors", "platforms", "capability_tags"):
        snap = {k: v for k, v in _PS.items() if k != key}
        assert advice_in_scope("placement", _placement(), _PF, snap) == "scope_exceeded", key
    assert advice_in_scope("placement", _placement(), {"capability_tags": {CONSULT_TAG}}, _PS) == "scope_exceeded"  # 凍結缺 platforms
    assert advice_in_scope("placement", _placement(), _PF, {**_PS, "current_platform": 1}) == "scope_exceeded"
    assert advice_in_scope("placement", _placement(), _PF, {**_PS, "executors": "exec.a"}) == "scope_exceeded"
    assert advice_in_scope("placement", _placement(), _PF, {**_PS, "executors": [1]}) == "scope_exceeded"
    # current_platform 與 target 相同但已不在交集內（凍結或快照任一方收回）
    assert advice_in_scope("placement", _placement(), {**_PF, "platforms": {"phone"}}, _PS) == "scope_exceeded"
    assert advice_in_scope("placement", _placement(), _PF, {**_PS, "platforms": set()}) == "scope_exceeded"
    # consult 標籤不在交集內仍先擋
    assert advice_in_scope("placement", _placement(), {**_PF, "capability_tags": set()}, _PS) == "scope_exceeded"


def test_placement_section_shape_and_non_mapping() -> None:
    assert advice_in_scope("placement", {}, _PF, _PS) == "scope_exceeded"
    assert advice_in_scope("placement", {"placement": None}, _PF, _PS) == "scope_exceeded"
    assert advice_in_scope("placement", {"placement": {}}, _PF, _PS) == "scope_exceeded"
    assert advice_in_scope("placement", {**_placement(), "reply": {"mode": "ask"}}, _PF, _PS) == "scope_exceeded"
    assert advice_in_scope("placement", {"placement": {"platform": "desktop"}}, _PF, _PS) == "scope_exceeded"


def test_section_must_match_exactly() -> None:
    assert advice_in_scope("speak", {"output": {"modalities": ["text"]}}, _F, _F) == "scope_exceeded"
    assert advice_in_scope("speak", {"reply": {"mode": "respond"}, "loop": {}}, _F, _F) == "scope_exceeded"
    assert advice_in_scope("ask_user", {"reply": {"mode": "ask"}}, _F, _F) is None


def test_output_modality_narrowed_by_intersection() -> None:
    frozen = {"modalities": {"text", "audio_stream"}, "capability_tags": {CONSULT_TAG}}
    snap = {"modalities": {"text"}, "capability_tags": {CONSULT_TAG}}
    assert advice_in_scope("output_modality", {"output": {"modalities": ["text"]}}, frozen, snap) is None
    # 凍結內有、但當下快照已收回 -> 超出
    assert advice_in_scope("output_modality", {"output": {"modalities": ["audio_stream"]}}, frozen, snap) == "scope_exceeded"


def test_missing_modalities_key_is_empty_set() -> None:
    # 缺鍵視為空集合（與迴圈現況一致）：非空 modalities 一律超出，空 modalities 通過
    assert advice_in_scope("output_modality", {"output": {"modalities": ["text"]}}, _F, {**_F, "modalities": {"text"}}) == "scope_exceeded"
    assert advice_in_scope("output_modality", {"output": {"modalities": []}}, _F, _F) is None
