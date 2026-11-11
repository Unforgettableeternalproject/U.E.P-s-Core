"""工具名稱的 provider 邊界轉譯（Agent 架構提案 §7.1：內部表示 provider 無關，轉譯在 Model Gateway）。

內部工具名稱可含 ``.``（例如 ``decision.consult``、``lore.recall``），但 provider（OpenAI function calling 等）的函式名只收
``[a-zA-Z0-9_-]``。送出時把 ``.`` 編成 ``__``，回來時只對**本次請求送出過的名稱**解碼；不在對照表內的名稱原樣放行，
由 Gateway 依 capability_set 以 ``capability_missing`` 拒絕（不會憑空解出一個可執行的內部名稱）。

fail-closed：內部名稱本身含 ``__``（解碼會歧義）、編碼後碰撞、或編碼後仍含 provider 不接受的字元，一律 ``ToolNameError``。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

_WIRE_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_SEP = "__"


class ToolNameError(ValueError):
    """名稱無法安全轉譯（歧義、碰撞或編碼後不合法）。"""


def encode_name(name: str) -> str:
    if _SEP in name:
        raise ToolNameError(f"工具名稱不得含 '__'（與編碼分隔符歧義）：{name!r}")
    wire = name.replace(".", _SEP)
    if not _WIRE_RE.fullmatch(wire):
        raise ToolNameError(f"工具名稱編碼後仍不合 provider 規則：{name!r}")
    return wire


def wire_map(names: Iterable[str]) -> dict[str, str]:
    """``{provider 名稱: 內部名稱}``；任何碰撞或不可轉譯都拋 ``ToolNameError``。"""
    mapping: dict[str, str] = {}
    for name in names:
        wire = encode_name(name)
        if wire in mapping and mapping[wire] != name:
            raise ToolNameError(f"工具名稱編碼後碰撞：{mapping[wire]!r} 與 {name!r}")
        mapping[wire] = name
    return mapping


def decode_name(wire: str, mapping: Mapping[str, str]) -> str:
    """對照表內的名稱解回內部名稱；表外原樣回傳（交給 Gateway 拒絕）。"""
    return mapping.get(wire, wire)
