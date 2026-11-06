"""``advice_in_scope``（CN-2 迴圈側再驗）：與迴圈原本 ``_recheck_escalation`` 的判定逐輸入等價。離線、純函式。"""
from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any

import pytest

from uep.decision.loop_consult import ADVICE_SECTION, SUB_KINDS, advice_in_scope


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


@pytest.mark.parametrize("sub_kind", _SUB_KINDS)
def test_equivalent_to_legacy_recheck(sub_kind: str) -> None:
    for advice, frozen, now in itertools.product(_ADVICES, _SETS, _SETS):
        expected = _legacy_recheck(sub_kind, advice, frozen, now)
        got = advice_in_scope(sub_kind, advice, {"modalities": frozen}, {"modalities": now})
        assert got == expected, (sub_kind, advice, frozen, now)


def test_none_advice_passes() -> None:
    assert advice_in_scope("output_modality", None, {}, {}) is None


def test_placement_and_unknown_always_escalate() -> None:
    assert advice_in_scope("placement", {"placement": {}}, {"modalities": {"text"}}, {"modalities": {"text"}}) == "scope_exceeded"
    assert advice_in_scope("bogus", {"reply": {}}, {}, {}) == "scope_exceeded"


def test_section_must_match_exactly() -> None:
    assert advice_in_scope("speak", {"output": {"modalities": ["text"]}}, {}, {}) == "scope_exceeded"
    assert advice_in_scope("speak", {"reply": {"mode": "respond"}, "loop": {}}, {}, {}) == "scope_exceeded"
    assert advice_in_scope("ask_user", {"reply": {"mode": "ask"}}, {}, {}) is None


def test_output_modality_narrowed_by_intersection() -> None:
    frozen, snap = {"modalities": {"text", "audio_stream"}}, {"modalities": {"text"}}
    assert advice_in_scope("output_modality", {"output": {"modalities": ["text"]}}, frozen, snap) is None
    # 凍結內有、但當下快照已收回 -> 超出
    assert advice_in_scope("output_modality", {"output": {"modalities": ["audio_stream"]}}, frozen, snap) == "scope_exceeded"


def test_missing_modalities_key_is_empty_set() -> None:
    # 缺鍵視為空集合（與迴圈現況一致）：非空 modalities 一律超出，空 modalities 通過
    assert advice_in_scope("output_modality", {"output": {"modalities": ["text"]}}, {}, {"modalities": {"text"}}) == "scope_exceeded"
    assert advice_in_scope("output_modality", {"output": {"modalities": []}}, {}, {}) is None
