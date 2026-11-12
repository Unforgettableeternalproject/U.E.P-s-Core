"""決策點註冊表（提案 §3.1）：decision_kind -> 規格＋engine。葉節點。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class DecisionPointSpec:
    """註冊一種 decision_kind。P0 只收 hub 實際用到的欄位，其餘（schema、plan_sections…）隨 P1 之後補。"""

    kind: str  # 如 "turn.reply"
    version: int = 1
    mode: Literal["sync", "async", "publish"] = "sync"
    owner: str = ""
    outcomes: tuple[str, ...] = ()
    budget_ms: int | None = None  # sync 預算；async 為 None；<= 0 代表不設預算
    on_timeout_reason: str = ""  # 逾時預設的 reason_code（D80）
    on_busy_reason: str = ""  # engine 忙碌（上一個呼叫未回）時的預設 reason_code
    on_error_reason: str = ""  # engine 例外時的預設 reason_code
    on_invalid_reason: str = ""  # 輸出驗證失敗時的預設 reason_code
    allow_native: bool = False  # 是否接受 DecisionRequest.native（同行程原生物件；P0 只有 turn.reply）
    shadow: bool = False  # 是否支援 shadow 並行
    plan_sections: tuple[str, ...] = ()  # 允許額外帶的 plan 區段白名單；目前管 "policy"（僅 policy.snapshot／channel.mode 宣告）、"capability" 與 "chain"（僅 turn.reply 宣告）


class DecisionPointRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, DecisionPointSpec] = {}

    def register(self, spec: DecisionPointSpec, *, replace: bool = False) -> None:
        if spec.kind in self._specs and not replace:
            raise ValueError(f"decision_kind 已註冊：{spec.kind}")
        self._specs[spec.kind] = spec

    def get(self, kind: str) -> DecisionPointSpec:
        try:
            return self._specs[kind]
        except KeyError:
            raise KeyError(f"未註冊的 decision_kind：{kind}") from None

    def kinds(self) -> list[str]:
        return sorted(self._specs)
