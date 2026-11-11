"""composite 的受限路徑語法與參數解析（Agent 架構提案 §5.7.4）：純函式，不做 I/O。

步驟參數的值若是以 ``$`` 開頭的字串，就是一個**路徑引用**（不是任意表達式），只有兩種形狀：

- ``$.input.<name>(.<name>|[<n>])*``：composite 自己的輸入（含 ``initial_params`` 預填與 schema ``default``）。
- ``$.steps[<k>].output.<name>(.<name>|[<n>])*``：前序步驟 k 的輸出欄位。步驟 k 必須在引用它的步驟之前（線性），
  且第一層欄位必須是該步驟工具 ``output_schema.properties`` 宣告過的欄位（受 output_schema 約束）。

不以 ``$`` 開頭的值都是字面值（dict／list 會遞迴處理）。路徑引用不支援運算、條件、函式；
解析失敗一律拋 ``CompositePathError``，由呼叫端轉成 ``rejected(invalid_tool_args)``。
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

COMPOSITE_STEP_SKIPPED = "composite_step_skipped"  # 前序步驟未成功，這一步沒有執行（逐步狀態列的 reason_code）

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_TAIL = rf"((?:\.{_NAME}|\[\d{{1,4}}\])+)"
_INPUT = re.compile(rf"\$\.input{_TAIL}")
_STEPS = re.compile(rf"\$\.steps\[(\d{{1,4}})\]\.output{_TAIL}")
_SEGMENT = re.compile(rf"\.({_NAME})|\[(\d+)\]")


class CompositePathError(ValueError):
    """路徑語法不合、或解析不到值。訊息只含路徑文字與原因，不含值。"""


@dataclass(frozen=True)
class PathRef:
    root: str  # "input" 或 "steps"
    step: int | None  # root == "steps" 時的步驟序號
    segments: tuple[str | int, ...]  # 欄位名（str）與陣列索引（int）


def is_path(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("$")


def parse_path(text: str) -> PathRef:
    """解析路徑引用；不符合受限語法拋 ``CompositePathError``。"""
    match = _INPUT.fullmatch(text)
    if match is not None:
        return PathRef("input", None, _segments(match.group(1)))
    match = _STEPS.fullmatch(text)
    if match is not None:
        return PathRef("steps", int(match.group(1)), _segments(match.group(2)))
    raise CompositePathError(f"不合法的路徑引用：{text[:80]!r}")


def _segments(tail: str) -> tuple[str | int, ...]:
    return tuple(name if name else int(index) for name, index in _SEGMENT.findall(tail))


def iter_refs(params: Any):
    """走訪參數中所有路徑引用，產生 ``(原始字串, PathRef)``；語法不合者拋 ``CompositePathError``。"""
    if is_path(params):
        yield params, parse_path(params)
    elif isinstance(params, Mapping):
        for value in params.values():
            yield from iter_refs(value)
    elif isinstance(params, (list, tuple)):
        for value in params:
            yield from iter_refs(value)


def validate_static(
    name: str, steps: list[tuple[str, Mapping[str, Any]]], input_schema: Mapping[str, Any],
    initial_params: Mapping[str, Any], outputs_of: Callable[[str], Mapping[str, Any] | None],
) -> None:
    """註冊期的靜態檢查（RA-14）：語法、線性（只能引用前序步驟）、輸入／輸出欄位存在、``initial_params`` 鍵合法。

    ``steps`` 是 ``(工具名, params)``；``outputs_of(tool)`` 回該工具的 ``output_schema``（沒有則 None）。
    只檢查第一層欄位名；更深的路徑在執行期逐段解析。失敗拋 ``CompositePathError``。"""
    input_props = set((input_schema or {}).get("properties", {}))
    extra = sorted(set(initial_params) - input_props)
    if extra:
        raise CompositePathError(f"{name}: initial_params 含 input_schema 沒有的欄位：{extra}")
    for index, (tool, params) in enumerate(steps):
        for text, ref in iter_refs(params):
            if ref.root == "input":
                if ref.segments[0] not in input_props:
                    raise CompositePathError(f"{name}: 步驟 {index} 引用了未宣告的輸入欄位：{text}")
                continue
            assert ref.step is not None
            if ref.step >= index:
                raise CompositePathError(f"{name}: 步驟 {index} 只能引用前序步驟（線性）：{text}")
            source_tool = steps[ref.step][0]
            schema = outputs_of(source_tool)
            declared = set((schema or {}).get("properties", {}))
            if not declared or ref.segments[0] not in declared:
                raise CompositePathError(f"{name}: 步驟 {index} 引用了 {source_tool} 的 output_schema 未宣告的欄位：{text}")


def resolve_value(ref: PathRef, root: Any) -> Any:
    """沿 ``segments`` 取值；任一層不存在、型別不符、或 None 都拋 ``CompositePathError``。"""
    current = root
    for seg in ref.segments:
        if isinstance(seg, int):
            if not isinstance(current, (list, tuple)) or seg >= len(current):
                raise CompositePathError("路徑索引超出範圍")
            current = current[seg]
        else:
            if not isinstance(current, Mapping) or seg not in current:
                raise CompositePathError(f"路徑欄位不存在：{seg}")
            current = current[seg]
    if current is None:
        raise CompositePathError("路徑的值是 null")
    return current


def resolve_params(
    params: Any, *, inputs: Mapping[str, Any], output_of: Callable[[int], Mapping[str, Any] | None],
) -> Any:
    """把步驟參數裡的路徑引用代入實際值（回新物件，不改原宣告）。

    ``inputs``：composite 的輸入（驗證並預填後）；``output_of(k)``：前序步驟 k 的輸出，取不到回 None（⇒ 解析失敗）。"""
    if is_path(params):
        ref = parse_path(params)
        if ref.root == "input":
            return resolve_value(ref, inputs)
        assert ref.step is not None
        output = output_of(ref.step)
        if output is None:
            raise CompositePathError(f"步驟 {ref.step} 沒有可引用的輸出")
        return resolve_value(ref, output)
    if isinstance(params, Mapping):
        return {key: resolve_params(value, inputs=inputs, output_of=output_of) for key, value in params.items()}
    if isinstance(params, (list, tuple)):
        return [resolve_params(value, inputs=inputs, output_of=output_of) for value in params]
    return params
