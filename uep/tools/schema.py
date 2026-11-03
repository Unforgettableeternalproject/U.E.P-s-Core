"""工具參數 JSON Schema 子集（Agent 架構提案 §6.3、RA-7，切片 B）。

三件事，皆為純函式、無 I/O：
1. ``lint_subset``：註冊時檢查 schema 只用子集內的關鍵字（``$ref``／``oneOf``／``anyOf``／``allOf`` 等一律擋下）。
2. ``strict_schema``：RA-7 的 ``additionalProperties:false`` 強制。註冊表內的宣告可以不寫這個關鍵字（現行記憶工具的
   schema 位元組由舊路徑黃金測試鎖住，不能改），改在**有效 schema** 補上：render 與 Gateway 驗證都吃有效 schema，
   所以對新路徑而言所有物件節點都是封閉的。宣告成 ``true`` 或 schema 物件則在註冊時失敗。
3. ``validate_args``：依有效 schema 驗證參數，回傳違規欄位的 JSON Pointer 清單。**清單只含欄位路徑，不含任何值**
   （值可能是使用者原文，不得進回餵或 log）。
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

JSON_TYPES = frozenset({"object", "string", "integer", "number", "boolean", "array"})
X_UEP_TYPES = frozenset({"path", "url", "actor_ref", "scope_ref"})  # §6.3 標註型別；本片只驗詞彙，正規化在後續切片
# 子集內的關鍵字（含不影響驗證的註解類）。不在此集合的關鍵字一律視為不支援，因為驗證器不會執行它。
ALLOWED_KEYWORDS = frozenset({
    "type", "properties", "required", "enum", "minimum", "maximum", "minLength", "maxLength", "pattern",
    "items", "additionalProperties", "description", "title", "default", "x-uep-type",
})
UNSUPPORTED_COMPOSITION = frozenset({"$ref", "$defs", "definitions", "oneOf", "anyOf", "allOf", "not"})
MAX_SAFE_INTEGER = 2**53  # D53：超過的整數不可靠
MAX_FIELDS = 16  # 回餵的違規欄位數上限
MAX_POINTER_LEN = 120


def freeze(node: Any) -> Any:
    """遞迴唯讀化：映射 -> MappingProxyType，序列 -> tuple。"""
    if isinstance(node, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in node.items()})
    if isinstance(node, (list, tuple)):
        return tuple(freeze(v) for v in node)
    return node


def thaw(node: Any) -> Any:
    """``freeze`` 的反向：還原成可 JSON 序列化的 dict／list（新物件）。"""
    if isinstance(node, Mapping):
        return {k: thaw(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [thaw(v) for v in node]
    return node


# ---------------------------------------------------------------------------
# 註冊時 linter
# ---------------------------------------------------------------------------

def lint_subset(schema: Any) -> str | None:
    """schema 超出子集時回傳原因（不含值），否則 None。根節點必須是物件型。"""
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        return "schema 必須是 type=object 的映射"
    return _lint_node(schema, "")


def _lint_node(node: Any, where: str) -> str | None:
    if not isinstance(node, Mapping):
        return f"{where or '/'}: 節點必須是映射"
    for key in node:
        if key in UNSUPPORTED_COMPOSITION:
            return f"{where or '/'}: 不支援 {key}"
        if key not in ALLOWED_KEYWORDS:
            return f"{where or '/'}: 不支援的關鍵字 {key!r}"
    kind = node.get("type")
    if kind not in JSON_TYPES:
        return f"{where or '/'}: type 必須是 {sorted(JSON_TYPES)} 之一"
    if "additionalProperties" in node and node["additionalProperties"] is not False:
        return f"{where or '/'}: additionalProperties 只能是 false（RA-7）"
    if "x-uep-type" in node and (kind != "string" or node["x-uep-type"] not in X_UEP_TYPES):
        return f"{where or '/'}: x-uep-type 必須標在 string 且屬 {sorted(X_UEP_TYPES)}"
    if kind != "object" and ("properties" in node or "required" in node or "additionalProperties" in node):
        return f"{where or '/'}: 非 object 節點不得有 properties／required／additionalProperties"
    if kind != "array" and "items" in node:
        return f"{where or '/'}: 非 array 節點不得有 items"
    if "enum" in node:
        values = node["enum"]
        if not isinstance(values, (list, tuple)) or not values or any(
            not isinstance(v, (str, int, float, bool)) for v in values
        ):
            return f"{where or '/'}: enum 必須是非空的純量清單"
    for key in ("minimum", "maximum"):
        if key in node and (isinstance(node[key], bool) or not isinstance(node[key], (int, float))):
            return f"{where or '/'}: {key} 必須是數字"
    for key in ("minLength", "maxLength"):
        if key in node and (isinstance(node[key], bool) or not isinstance(node[key], int) or node[key] < 0):
            return f"{where or '/'}: {key} 必須是非負整數"
    if "pattern" in node:
        try:
            re.compile(node["pattern"])
        except (re.error, TypeError):
            return f"{where or '/'}: pattern 不是合法的正規表示式"
    if "properties" in node:
        props = node["properties"]
        if not isinstance(props, Mapping):
            return f"{where or '/'}: properties 必須是映射"
        for name, child in props.items():
            problem = _lint_node(child, f"{where}/{_escape(str(name))}")
            if problem:
                return problem
    if "items" in node:
        problem = _lint_node(node["items"], f"{where}/items")
        if problem:
            return problem
    return None


# ---------------------------------------------------------------------------
# 有效（嚴格）schema
# ---------------------------------------------------------------------------

def strict_schema(schema: Any) -> Any:
    """回傳深複本，每個 ``type=object`` 節點都補上 ``additionalProperties:false``（已寫 false 者不變）。"""
    node = thaw(schema)
    _close(node)
    return node


def _close(node: Any) -> None:
    if not isinstance(node, dict):
        return
    if node.get("type") == "object":
        node.setdefault("additionalProperties", False)
        for child in node.get("properties", {}).values():
            _close(child)
    if isinstance(node.get("items"), dict):
        _close(node["items"])


# ---------------------------------------------------------------------------
# 驗證
# ---------------------------------------------------------------------------

def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def validate_args(schema: Mapping[str, Any], args: Any) -> list[str]:
    """依（已嚴格化的）schema 驗證 ``args``。回傳違規欄位的 JSON Pointer（根為 ``/``），無違規回 ``[]``。

    輸出只由 schema 的欄位名與 args 的**鍵**組成，從不回傳值。缺必填、多餘欄位、型別／範圍／enum／pattern 不符都算。"""
    found: list[str] = []
    _check(schema, args, "", found)
    unique: list[str] = []
    for pointer in found:
        clipped = (pointer or "/")[:MAX_POINTER_LEN]
        if clipped not in unique:
            unique.append(clipped)
        if len(unique) >= MAX_FIELDS:
            break
    return unique


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "object":
        return isinstance(value, Mapping)
    if kind == "array":
        return isinstance(value, (list, tuple))
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":  # 整數與浮點不混用（D53）：1.0 不是整數
        return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= MAX_SAFE_INTEGER
    if kind == "number":
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and (isinstance(value, int) or math.isfinite(value))
        )
    return False


def _check(node: Mapping[str, Any], value: Any, ptr: str, out: list[str]) -> None:
    kind = node.get("type")
    if not isinstance(kind, str) or not _type_ok(kind, value):
        out.append(ptr)
        return
    if "enum" in node and not any(type(v) is type(value) and v == value for v in node["enum"]):
        out.append(ptr)
        return
    if kind == "object":
        props: Mapping[str, Any] = node.get("properties", {})
        for name in node.get("required", ()):
            if name not in value:
                out.append(f"{ptr}/{_escape(str(name))}")
        for key in value:
            child = props.get(key) if isinstance(key, str) else None
            if child is None:  # 嚴格化後 additionalProperties 恆為 false：多餘欄位一律違規
                out.append(f"{ptr}/{_escape(str(key))}")
            else:
                _check(child, value[key], f"{ptr}/{_escape(key)}", out)
    elif kind == "array":
        items = node.get("items")
        if isinstance(items, Mapping):
            for index, element in enumerate(value):
                _check(items, element, f"{ptr}/{index}", out)
    elif kind == "string":
        too_short_or_long = len(value) < node.get("minLength", 0) or len(value) > node.get("maxLength", len(value))
        if too_short_or_long or ("pattern" in node and re.search(node["pattern"], value) is None):
            out.append(ptr)
    elif kind in ("integer", "number"):
        if value < node.get("minimum", value) or value > node.get("maximum", value):
            out.append(ptr)
