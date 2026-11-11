"""composite 的受限路徑語法與註冊期靜態檢查（提案 §5.7.4、RA-14）。巢狀／並行／分類下限的 RA-14 案例在 ``test_tool_registry.py``。"""
from __future__ import annotations

import dataclasses

import pytest

from tests.uep.test_agent_loop_composite import READ_STEP, _entry, _notes
from uep.adapters.tool import CompositeSpec, CompositeStep
from uep.tools.composite import (
    CompositePathError,
    PathRef,
    iter_refs,
    parse_path,
    resolve_params,
)
from uep.tools.registry import ToolRegistry, ToolRegistryError

# ---- 路徑語法 ----

@pytest.mark.parametrize(("text", "expected"), [
    ("$.input.source", PathRef("input", None, ("source",))),
    ("$.input.items[2].name", PathRef("input", None, ("items", 2, "name"))),
    ("$.steps[0].output.text", PathRef("steps", 0, ("text",))),
    ("$.steps[12].output.rows[0]", PathRef("steps", 12, ("rows", 0))),
])
def test_parse_accepts_only_the_restricted_forms(text, expected):
    assert parse_path(text) == expected


@pytest.mark.parametrize("text", [
    "$", "$.", "$.input", "$.input.", "$.steps[0].output", "$.steps[x].output.a", "$.steps[0].input.a", "$.other.a",
    "$.input.a + 1", "$.input.a|upper", "$.input['a']", "$.input.a-b", "$.input..a", "$$.input.a", "$.steps[0].output.a.",
    "${input.a}", "$.input.a[ -1 ]",
])
def test_parse_rejects_expressions_and_malformed_paths(text):
    with pytest.raises(CompositePathError):
        parse_path(text)


def test_non_dollar_strings_are_literals_and_nested_structures_are_walked():
    params = {"a": "plain", "b": ["x", "$.input.k"], "c": {"d": "$.steps[0].output.text", "e": 3}}
    assert [t for t, _ in iter_refs(params)] == ["$.input.k", "$.steps[0].output.text"]
    out = resolve_params(params, inputs={"k": "K"}, output_of=lambda k: {"text": "T"})
    assert out == {"a": "plain", "b": ["x", "K"], "c": {"d": "T", "e": 3}}


@pytest.mark.parametrize("ref", ["$.input.missing", "$.input.k[0]", "$.input.k.deep", "$.steps[1].output.text", "$.steps[0].output.none"])
def test_unresolvable_refs_raise(ref):
    with pytest.raises(CompositePathError):
        resolve_params({"x": ref}, inputs={"k": "v", "n": None}, output_of=lambda k: {"text": "T", "none": None} if k == 0 else None)


def test_null_value_is_not_resolvable():
    with pytest.raises(CompositePathError):
        resolve_params("$.input.n", inputs={"n": None}, output_of=lambda k: None)


# ---- 註冊期（RA-14） ----

def _build(*steps, initial=None, **over):
    leaf_r = _entry("fs.read")
    leaf_w = _entry("fs.write", side="reversible_local", cap="memory.write")
    comp = _notes(list(steps), initial=initial)
    comp = dataclasses.replace(comp, **over)
    return ToolRegistry.build([leaf_r, leaf_w, comp])


def _rule(*steps, **kw):
    with pytest.raises(ToolRegistryError) as exc:
        _build(*steps, **kw)
    return exc.value.rule, str(exc.value)


def test_valid_linear_references_register():
    _build(READ_STEP, CompositeStep("fs.write", {"path": "$.input.source", "content": "$.steps[0].output.text"}),
           initial={"source": "/p"})


def test_ra14_rejects_bad_syntax_forward_and_self_references():
    assert _rule(CompositeStep("fs.read", {"path": "$.input.source + 1"}))[0] == "RA-14"
    assert _rule(CompositeStep("fs.read", {"path": "$.steps[0].output.text"}))[0] == "RA-14"  # 引用自己
    assert _rule(READ_STEP, CompositeStep("fs.write", {"path": "$.steps[2].output.text"}), READ_STEP)[0] == "RA-14"  # 向後引用


def test_ra14_rejects_fields_not_declared_by_input_or_output_schema():
    assert _rule(CompositeStep("fs.read", {"path": "$.input.nope"}))[0] == "RA-14"
    rule, message = _rule(READ_STEP, CompositeStep("fs.write", {"path": "$.steps[0].output.undeclared"}))
    assert rule == "RA-14" and "undeclared" in message


def test_ra14_requires_the_source_step_to_declare_an_output_schema():
    leaf = dataclasses.replace(_entry("fs.read"), output_schema=None, spec=dataclasses.replace(_entry("fs.read").spec, side_effect_class="internal_read"))
    other = dataclasses.replace(leaf, name="fs.peek")
    comp = _notes([CompositeStep("fs.peek", {"path": "$.input.source"}), CompositeStep("fs.peek", {"path": "$.steps[0].output.text"})])
    with pytest.raises(ToolRegistryError) as exc:
        ToolRegistry.build([leaf, other, comp])
    assert exc.value.rule in {"RA-14", "RA-16"}  # 讀取類條目本來就必須宣告 output_schema（RA-16）；兩條規則擇一擋下


def test_ra14_rejects_initial_params_outside_input_schema():
    assert _rule(READ_STEP, initial={"ghost": "x"})[0] == "RA-14"


def test_nested_composite_is_still_rejected():
    inner = _notes([READ_STEP], name="notes.inner")
    outer = _notes([CompositeStep("notes.inner", {"source": "$.input.source"})], name="notes.outer")
    with pytest.raises(ToolRegistryError) as exc:
        ToolRegistry.build([_entry("fs.read"), inner, outer])
    assert exc.value.rule == "RA-14" and "巢狀" in str(exc.value)


def test_composite_spec_defaults_have_no_initial_params():
    assert CompositeSpec(steps=(READ_STEP,)).initial_params == {}
