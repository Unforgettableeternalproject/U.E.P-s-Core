"""TG-6／VC-S2-2：params_hash 的 canonical 規則與上限（不需資料庫的部分 + create_action 無副作用）。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from tests.uep.test_task_model import StubExecutor, add_action, make_manager, running
from uep.tasks.manager import (
    DEFAULT_PARAMS_LIMITS,
    PARAMS_NOT_CANONICAL,
    ParamsLimits,
    PolicyViolation,
    TaskManager,
    canonical_params_hash,
    params_hash,
)
from uep.tasks.models import ActionIntentRow, RunRow
from uep.tasks.states import RunStatus


def _nested(levels: int) -> dict:
    """頂層 dict 算第 1 層，共 levels 層。"""
    value: dict = {}
    for _ in range(levels - 1):
        value = {"k": value}
    return value


BAD_PARAMS = {
    "nan": {"threshold": float("nan")},
    "inf": {"a": float("inf")},
    "neg_inf": {"a": float("-inf")},
    "mixed_keys": {1: "x", "1": "y"},
    "int_key_only": {1: "x"},
    "nested_int_key": {"a": {2: "x"}},
    "lone_surrogate": {"t": "abc\ud800def"},
    "lone_surrogate_key": {"\udc00": "x"},
    "depth_9": _nested(9),
    "non_json_type": {"a": object()},
    "huge_int": {"a": 10 ** 5000},
}


@pytest.mark.parametrize("params", BAD_PARAMS.values(), ids=BAD_PARAMS.keys())
def test_non_canonical_params_raise_policy_violation(params):
    with pytest.raises(PolicyViolation) as exc:
        params_hash("t", 1, params)
    assert exc.value.code == PARAMS_NOT_CANONICAL
    with pytest.raises(PolicyViolation):
        TaskManager.params_hash("t", 1, params)


def test_circular_reference_is_rejected_not_recursion_error():
    loop: dict = {}
    loop["self"] = loop
    with pytest.raises(PolicyViolation) as exc:
        params_hash("t", 1, loop)
    assert exc.value.code == PARAMS_NOT_CANONICAL


def test_messages_do_not_leak_params_content():
    secrets = {
        "nan": {"secret_key_xyz": float("nan")},
        "mixed": {1: "secret_value_xyz", "1": "y"},
        "surrogate": {"secret_key_xyz": "secret_value_xyz\ud800"},
        "type": {"secret_key_xyz": object()},
        "long": {"secret_key_xyz": "secret_value_xyz" * 10_000},
    }
    for params in secrets.values():
        with pytest.raises(PolicyViolation) as exc:
            params_hash("t", 1, params)
        assert "secret" not in str(exc.value)
        assert "xyz" not in repr(exc.value.args)
        assert exc.value.__cause__ is None


def test_valid_params_hash_is_stable_and_order_independent():
    a = params_hash("t", 1, {"b": [1, 2.5, None, True], "a": {"x": "中文"}})
    b = params_hash("t", 1, {"a": {"x": "中文"}, "b": [1, 2.5, None, True]})
    assert a == b and len(a) == 64
    # D53：canonical 層不做數值正規化，1 與 1.0 本來就不同雜湊
    assert params_hash("t", 1, {"n": 1}) != params_hash("t", 1, {"n": 1.0})


# ---- 上限邊界 ----

def test_depth_boundary():
    params_hash("t", 1, _nested(DEFAULT_PARAMS_LIMITS.max_depth))  # 8 層通過
    with pytest.raises(PolicyViolation):
        params_hash("t", 1, _nested(DEFAULT_PARAMS_LIMITS.max_depth + 1))
    # list 與 dict 混合同樣計層
    deep_list: list = []
    for _ in range(8):
        deep_list = [deep_list]
    with pytest.raises(PolicyViolation):
        params_hash("t", 1, {"x": deep_list})  # 1 + 9 層


def test_string_boundary_counts_utf8_bytes():
    limit = DEFAULT_PARAMS_LIMITS.max_string_bytes
    params_hash("t", 1, {"t": "a" * limit})
    with pytest.raises(PolicyViolation):
        params_hash("t", 1, {"t": "a" * (limit + 1)})
    # 多位元組字元：字元數未超過但位元組超過
    params_hash("t", 1, {"t": "中" * (limit // 3)})
    with pytest.raises(PolicyViolation):
        params_hash("t", 1, {"t": "中" * (limit // 3 + 1)})


def test_total_length_boundary():
    limits = DEFAULT_PARAMS_LIMITS
    chunk = limits.max_string_bytes
    # 以 3 個滿長字串湊底，再用一個填充字串精準對到總長上限
    base = {f"k{i}": "a" * chunk for i in range(3)}
    fixed = len(json.dumps({**base, "p": ""}, separators=(",", ":"), sort_keys=True).encode())
    pad = limits.max_total_bytes - fixed
    assert 0 < pad <= chunk
    params_hash("t", 1, {**base, "p": "a" * pad})  # 恰好等於上限
    with pytest.raises(PolicyViolation) as exc:
        params_hash("t", 1, {**base, "p": "a" * (pad + 1)})  # 多 1 位元組
    assert exc.value.code == PARAMS_NOT_CANONICAL


def test_custom_limits_are_respected():
    small = ParamsLimits(max_depth=2, max_string_bytes=4, max_total_bytes=64)
    canonical_params_hash("t", 1, {"a": {"b": "abcd"}}, small)
    with pytest.raises(PolicyViolation):
        canonical_params_hash("t", 1, {"a": {"b": {"c": 1}}}, small)
    with pytest.raises(PolicyViolation):
        canonical_params_hash("t", 1, {"a": "abcde"}, small)
    with pytest.raises(PolicyViolation):
        canonical_params_hash("t", 1, {f"k{i}": "abcd" for i in range(10)}, small)


# ---- create_action：不合規不進交易 ----

def test_create_action_rejects_before_any_row_or_state_change(session_factory):
    mgr = make_manager(session_factory, StubExecutor())
    _task, run, step = running(mgr)
    with pytest.raises(PolicyViolation) as exc:
        add_action(mgr, run, step, {"threshold": float("nan")}, side_effect="external_effect")
    assert exc.value.code == PARAMS_NOT_CANONICAL
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(ActionIntentRow)) == 0
        assert RunStatus(s.get(RunRow, run.run_id).status) == RunStatus.RUNNING


def test_task_manager_limits_are_configurable(session_factory):
    mgr = make_manager(session_factory, StubExecutor(), params_limits=ParamsLimits(max_string_bytes=8))
    _task, run, step = running(mgr)
    add_action(mgr, run, step, {"t": "12345678"})
    with pytest.raises(PolicyViolation):
        add_action(mgr, run, step, {"t": "123456789"})
